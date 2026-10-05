import io,json,tempfile,unittest,zipfile
from pathlib import Path
from updater import UpdateManager,ALLOWED_FILES,atomic_json,read_json

class UpdateTests(unittest.TestCase):
    def make_package(self, version='8.0.1', extra=False):
        result=io.BytesIO()
        with zipfile.ZipFile(result,'w') as archive:
            for name in ALLOWED_FILES:
                archive.writestr(name,json.dumps({'version':version}) if name=='version.json' else ('fixed' if name=='requirements.txt' else 'x=1'))
            if extra:archive.writestr('../data/settings.json','{}')
        return result.getvalue()

    def test_valid_update_and_rollback_preserve_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base=root/'versions/8.0.0';base.mkdir(parents=True)
            for name in ALLOWED_FILES:(base/name).write_text('fixed' if name=='requirements.txt' else '{}')
            atomic_json(root/'data/settings.json',{'history':['old']})
            manager=UpdateManager(root,'8.0.0')
            self.assertEqual(manager.stage_bytes(self.make_package())['status'],'ready')
            self.assertEqual(manager.activate_pending(),'8.0.1')
            self.assertEqual(manager.rollback(),'8.0.0')
            self.assertEqual(read_json(root/'data/settings.json',{})['history'],['old'])

    def test_reject_extra_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=UpdateManager(temp,'8.0.0')
            with self.assertRaises(ValueError):manager.stage_bytes(self.make_package(extra=True))
            self.assertFalse((Path(temp)/'data/pending-update.json').exists())

if __name__=='__main__':unittest.main()
