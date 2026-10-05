"""Versioned, transactional updater. User data and the runtime are never payload files."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import py_compile
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile

MAX_PACKAGE = 8 * 1024 * 1024
ALLOWED_FILES = {'app.py', 'overlay.py', 'version.json', 'requirements.txt'}


def version_tuple(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}',value):
        raise ValueError('버전 형식은 숫자.숫자.숫자여야 합니다.')
    return tuple(map(int,value.split('.')))


def atomic_json(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temp,path)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return default


def fetch_https(url, maximum):
    parsed=urllib.parse.urlparse(url)
    if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError('업데이트 주소는 인증정보 없는 HTTPS 주소여야 합니다.')
    request=urllib.request.Request(url,headers={'User-Agent':'GameChatTranslator-Updater/1','Cache-Control':'no-cache'})
    with urllib.request.urlopen(request,timeout=12) as response:
        if urllib.parse.urlparse(response.geturl()).scheme!='https':
            raise ValueError('보안 연결이 아닌 다운로드 이동은 허용하지 않습니다.')
        data=response.read(maximum+1)
        if len(data)>maximum:
            raise ValueError('업데이트 파일 크기 제한을 초과했습니다.')
        return data


class UpdateManager:
    def __init__(self, root, current_version):
        self.root=Path(root).resolve()
        self.data=self.root/'data'
        self.versions=self.root/'versions'
        self.current=current_version
        version_tuple(current_version)
        self.data.mkdir(parents=True,exist_ok=True)
        self.versions.mkdir(parents=True,exist_ok=True)

    def channel_url(self):
        explicit = self.data/'update-channel.json'
        if explicit.exists():
            return read_json(explicit,{}).get('manifest_url','')
        return read_json(self.root/'update-default.json',{}).get('manifest_url','')

    def configure_channel(self,url):
        if url:
            parsed=urllib.parse.urlparse(url)
            if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError('HTTPS 업데이트 주소를 입력하세요.')
        atomic_json(self.data/'update-channel.json',{'manifest_url':url})

    def check_and_stage(self):
        url=self.channel_url()
        if not url:
            return {'status':'unconfigured','message':'배포 주소 미연결 · 업데이트 파일 적용 버튼도 사용할 수 있습니다.'}
        manifest=json.loads(fetch_https(url,64*1024).decode('utf-8'))
        version=manifest.get('version')
        if manifest.get('schema')!=1:
            raise ValueError('지원하지 않는 업데이트 규격입니다.')
        if version_tuple(version)<=version_tuple(self.current):
            return {'status':'current','message':'최신 버전입니다.'}
        pending=read_json(self.data/'pending-update.json',{})
        if pending.get('version')==version and self.valid_version(version):
            return {'status':'ready','version':version,'message':version+' 업데이트 준비됨 · 재시작하면 적용됩니다.'}
        digest=manifest.get('sha256','')
        if not re.fullmatch(r'[a-f0-9]{64}',digest):
            raise ValueError('업데이트 검증값이 올바르지 않습니다.')
        payload=fetch_https(manifest.get('package_url',''),MAX_PACKAGE)
        if hashlib.sha256(payload).hexdigest()!=digest:
            raise ValueError('업데이트 파일 검증 실패. 현재 버전을 유지합니다.')
        result=self.stage_bytes(payload,expected_version=version)
        result['notes']=str(manifest.get('notes',''))[:2000]
        return result

    def stage_file(self,path):
        path=Path(path)
        if path.stat().st_size>MAX_PACKAGE:
            raise ValueError('업데이트 파일이 너무 큽니다.')
        return self.stage_bytes(path.read_bytes())

    def stage_bytes(self,payload,expected_version=None):
        if len(payload)>MAX_PACKAGE:
            raise ValueError('업데이트 파일이 너무 큽니다.')
        import io
        stage=Path(tempfile.mkdtemp(prefix='stage-',dir=self.versions))
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                infos=archive.infolist()
                names=[item.filename for item in infos]
                if len(set(names))!=len(names) or set(names)!=ALLOWED_FILES:
                    raise ValueError('전용 업데이트 파일이 아닙니다. 필요한 프로그램 파일만 포함해야 합니다.')
                if sum(item.file_size for item in infos)>MAX_PACKAGE:
                    raise ValueError('압축 해제 크기 제한을 초과했습니다.')
                for item in infos:
                    name=PurePosixPath(item.filename)
                    if name.is_absolute() or '..' in name.parts or len(name.parts)!=1 or '\\' in item.filename:
                        raise ValueError('잘못된 업데이트 경로입니다.')
                    if (item.external_attr>>16)&0o170000==0o120000:
                        raise ValueError('업데이트에 링크 파일을 넣을 수 없습니다.')
                    (stage/item.filename).write_bytes(archive.read(item))
            metadata=read_json(stage/'version.json',{})
            version=metadata.get('version')
            if version_tuple(version)<=version_tuple(self.current):
                raise ValueError('현재보다 새로운 버전의 업데이트만 적용할 수 있습니다.')
            if expected_version is not None and version!=expected_version:
                raise ValueError('업데이트 버전과 배포 안내가 일치하지 않습니다.')
            # This updater deliberately reuses the existing runtime/dependencies.
            existing=self.versions/self.current/'requirements.txt'
            if existing.exists() and existing.read_bytes()!=(stage/'requirements.txt').read_bytes():
                raise ValueError('설치 패키지가 변경된 버전입니다. 전용 설치 업데이트가 필요합니다.')
            for file in stage.glob('*.py'):
                py_compile.compile(str(file),doraise=True)
            destination=self.versions/version
            if destination.exists():
                raise ValueError('같은 버전이 이미 설치되어 있습니다. 배포 버전 번호를 올려야 합니다.')
            os.replace(stage,destination)
            atomic_json(self.data/'pending-update.json',{'version':version})
            return {'status':'ready','version':version,'message':version+' 업데이트 준비됨 · 재시작하면 적용됩니다.'}
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def valid_version(self,version):
        try:
            version_tuple(version)
            folder=self.versions/version
            return folder.is_dir() and all((folder/name).is_file() for name in ALLOWED_FILES)
        except (TypeError,ValueError):
            return False

    def activate_pending(self):
        active=read_json(self.data/'active-version.json',{'version':self.current})
        pending=read_json(self.data/'pending-update.json',{})
        candidate=pending.get('version')
        if candidate and self.valid_version(candidate) and version_tuple(candidate)>version_tuple(active['version']):
            atomic_json(self.data/'active-version.json',{'version':candidate,'previous':active['version'],'unconfirmed':True})
            (self.data/'pending-update.json').unlink(missing_ok=True)
            return candidate
        return active['version']

    def confirm(self,version):
        active=read_json(self.data/'active-version.json',{})
        if active.get('version')==version:
            active['unconfirmed']=False
            atomic_json(self.data/'active-version.json',active)

    def rollback(self):
        active=read_json(self.data/'active-version.json',{'version':self.current})
        previous=active.get('previous')
        if not previous or not self.valid_version(previous):
            return None
        atomic_json(self.data/'active-version.json',{'version':previous,'unconfirmed':False})
        return previous
