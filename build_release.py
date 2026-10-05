"""Maintainer utility: package ONLY versioned app files; never bundle user data."""
import argparse,hashlib,json,py_compile,zipfile
from pathlib import Path
from updater import ALLOWED_FILES,version_tuple

def build(source,destination,version,base_url=None):
    version_tuple(version)
    source=Path(source);destination=Path(destination)
    destination.mkdir(parents=True,exist_ok=True)
    for name in ['app.py','overlay.py']:
        py_compile.compile(str(source/name),doraise=True)
    package=destination/(version+'.zip')
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(ALLOWED_FILES):
            if name=='version.json':archive.writestr(name,json.dumps({'version':version}))
            else:archive.write(source/name,name)
    manifest={'schema':1,'version':version,'sha256':hashlib.sha256(package.read_bytes()).hexdigest(),
              'package_url':(base_url.rstrip('/')+'/'+package.name) if base_url else '',
              'notes':'게임 채팅 번역 업데이트'}
    (destination/'latest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return package

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True);parser.add_argument('--output',required=True);parser.add_argument('--version',required=True);parser.add_argument('--base-url')
    a=parser.parse_args();print(build(a.source,a.output,a.version,a.base_url))
