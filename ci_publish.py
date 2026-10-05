import json,zipfile
from pathlib import Path
from build_release import build
from updater import ALLOWED_FILES

source=Path('src');version=json.loads((source/'version.json').read_text())['version']
existing=Path('updates')/(version+'.zip')
if existing.exists():
    with zipfile.ZipFile(existing) as archive:
        unchanged=all(archive.read(name)==(source/name).read_bytes() for name in ALLOWED_FILES if name!='version.json')
    if not unchanged:
        raise SystemExit('Application changed without a new version. Increase src/version.json.')
    print('This version is already published unchanged.')
else:
    build(source,'updates',version,'https://raw.githubusercontent.com/shkim1-droid/game-chat-translator/main/updates')
