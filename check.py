"""Check a disposable copy; never opens the user's data/workmap.sqlite3."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class PersonalStarterCheck(unittest.TestCase):
    def test_isolated_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='workmap-personal-check-') as temp:
            target = Path(temp) / 'kit'
            target.mkdir()
            for name in ('src', 'tools'):
                shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(ROOT / 'workmap.py', target / 'workmap.py')
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONPATH=str(target / 'src') + os.pathsep + str(target))
            def run(*args, success=True):
                r = subprocess.run([sys.executable, '-X', 'utf8', 'workmap.py', *args], cwd=target,
                                   env=env, text=True, encoding='utf-8', capture_output=True)
                self.assertEqual(r.returncode == 0, success, r.stdout + r.stderr)
                return r.stdout
            common = ['--name', '合成試験', '--intent', '判断理由を保つ', '--origin', '隔離確認', '--owner-instruction', 'この合成Workを登録する']
            first = json.loads(run('init', *common))
            self.assertEqual(first['readback']['display_id'], 'W0')
            self.assertIn('W0', run('show'))
            run('init', *common, success=False)
            added = json.loads(run('add', '--parent', 'W0', *common))
            self.assertEqual(added['readback']['display_id'], 'W1')
            self.assertEqual(json.loads(run('thread', '--work', 'W1'))['thread']['thread_entries'], [])
            replay = json.loads(run('resume-add', '--request', added['request_path']))
            self.assertEqual(replay['result'], added['result'])
            run('add', '--parent', 'W404', *common, success=False)
            reason = json.loads(run('add', '--parent', 'W1', *common, '--reason', '独立して残す必要がある検証理由'))
            self.assertEqual(len(reason['readback']['thread']['thread']['thread_entries']), 1)
            detail = json.loads(run('show', '--work', 'W1'))['context']
            self.assertEqual(detail['pins'], [])
            self.assertEqual(detail['permissions'], [])
            backup_dir = target / 'backups'; backup_dir.mkdir()
            backup = backup_dir / 'backup.json'
            result = json.loads(run('backup', '--output', str(backup)))
            restored = json.loads(run('recover-check', '--backup', str(backup), '--sha256', result['sha256'], '--target', str(target / 'recovery/candidate.sqlite3')))
            self.assertEqual(restored['management_recovery'], 'verified')
            self.assertEqual(restored['role'], 'recovery_candidate')
            run('recover-check', '--backup', str(backup), '--sha256', result['sha256'], '--target', str(target / 'data/workmap.sqlite3'), success=False)
            self.assertIn('W1.1', run('show'))


if __name__ == '__main__':
    unittest.main()
