"""Stable entry point. Each application version runs in its own folder."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from updater import UpdateManager, read_json, atomic_json

ROOT=Path(__file__).resolve().parent
BASE_VERSION='8.0.0'


def main():
    data=ROOT/'data'
    data.mkdir(exist_ok=True)
    state=read_json(data/'active-version.json',{'version':BASE_VERSION})
    if not (data/'active-version.json').exists():
        atomic_json(data/'active-version.json',state)
    manager=UpdateManager(ROOT,state['version'])
    while True:
        version=manager.activate_pending()
        if not manager.valid_version(version):
            raise RuntimeError('Installed application version is missing: '+version)
        folder=ROOT/'versions'/version
        fd,health=tempfile.mkstemp(prefix='gct-ready-',dir=data)
        os.close(fd)
        Path(health).unlink()
        env=os.environ.copy()
        env['GCT_INSTALL_ROOT']=str(ROOT)
        env['GCT_DATA_DIR']=str(data)
        env['GCT_HEALTH_FILE']=health
        env['PYTHONPATH']=str(ROOT)+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        process=subprocess.Popen([sys.executable,str(folder/'app.py')],cwd=folder,env=env)
        deadline=time.monotonic()+30
        while process.poll() is None and not Path(health).exists() and time.monotonic()<deadline:
            time.sleep(.1)
        healthy=Path(health).exists()
        Path(health).unlink(missing_ok=True)
        if healthy:
            manager.confirm(version)
        else:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            previous=manager.rollback()
            if previous:
                print('Update did not start. Restoring version '+previous)
                manager.current=previous
                continue
            raise RuntimeError('Application did not start. Please send runtime-log.txt.')
        code=process.wait()
        if code==75:
            manager.current=version
            continue
        return code


if __name__=='__main__':
    sys.exit(main())
