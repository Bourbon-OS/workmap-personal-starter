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


def copy_pin_guide(target):
    # This is the only bundled document required by the Pin binding. Never
    # copy a used kit's docs tree: it can contain private Owner evidence.
    relative = Path('docs/work-management/PIN_ROUTINE_ENTRY_2026-10-02.md')
    dest = target / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / relative, dest)


class PersonalStarterCheck(unittest.TestCase):
    def test_isolated_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='wm-') as temp:
            target = Path(temp) / 'kit'
            target.mkdir()
            for name in ('src', 'tools'):
                shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(ROOT / 'workmap.py', target / 'workmap.py')
            shutil.copyfile(ROOT / 'WorkMap.pyw', target / 'WorkMap.pyw')
            copy_pin_guide(target)
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1')
            env.pop('PYTHONPATH', None)
            def run(*args, success=True):
                r = subprocess.run([sys.executable, '-I', '-X', 'utf8', 'workmap.py', *args], cwd=target,
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
            # New day-to-day entries must work from this extracted copy alone.
            run('reason', '--work', 'W1', '--text', '未許可では記録しない', '--owner-instruction', 'この理由を残す', success=False)
            run('permit', '--work', 'W0', '--map-add', '--owner-instruction', 'このMapの全Workの下への新規Work登録をOwnerに許可する')
            run('permit', '--work', 'W1', '--map-add', '--owner-instruction', '子をMap全体の許可元と誤認しない', success=False)
            day = json.loads(run('add', '--parent', 'W0', *common))
            self.assertEqual(day['readback']['display_id'], 'W2')
            run('pin', '--work', 'W2', '--reason', 'まだ未許可', '--owner-instruction', 'W2の引受', success=False)
            run('permit', '--work', 'W2', '--edit', '--record', '--close', '--pin',
                '--owner-instruction', 'W2の訂正、理由と結果の記録、本人Pinの引受・変更・解除、通常終了をOwnerに許可する')
            recorded_reason = json.loads(run('reason', '--work', 'W2', '--text', '親目的に必要な通常確認に絞り、追加機能は比較後に判断する',
                '--owner-instruction', 'この判断理由を記録する'))
            self.assertEqual(json.loads(run('resume', '--request', recorded_reason['request_path']))['readback']['status'], 'confirmed')
            proof = target / 'docs/work-management/evidence/proof.txt'
            proof.write_text('合成確認の目的を達成した。通常の保存・再読と理由回収を確認。実業務の完了証明ではない。', encoding='utf-8')
            recorded_result = json.loads(run('result', '--work', 'W2', '--text', '合成の確認を完了', '--source', 'repo:docs/work-management/evidence/proof.txt'))
            self.assertEqual(json.loads(run('resume', '--request', recorded_result['request_path']))['readback']['status'], 'confirmed')
            why = json.loads(run('thread', '--work', 'W2'))['thread']['thread_entries']
            self.assertEqual(len(why), 1, why)
            self.assertIn('追加機能', why[0]['why'])
            first_pin = json.loads(run('pin', '--work', 'W2', '--reason', 'この仕事を引き受ける', '--owner-instruction', 'W2を本人の主作業として引き受ける'))
            self.assertEqual(json.loads(run('resume', '--request', first_pin['request_path']))['readback']['status'], 'confirmed')
            moved_pin = json.loads(run('pin', '--work', 'W1', '--reason', '通常の別Work切替', '--owner-instruction', '本人の主作業をW2からW1へ切り替える'))
            self.assertEqual(json.loads(run('resume', '--request', moved_pin['request_path']))['readback']['status'], 'confirmed')
            run('pin', '--work', 'W2', '--reason', '元の仕事へ戻る', '--owner-instruction', '本人の主作業をW1からW2へ戻す')
            run('permit', '--work', 'W0', '--close', '--owner-instruction', '通常契約を満たす場合だけW0の終了を許可する')
            run('close', '--work', 'W0', '--outcome', 'completed', '--reason', '子が残るので拒否される',
                '--owner-instruction', 'W0を閉じる', '--evidence', 'repo:docs/work-management/evidence/proof.txt', success=False)
            closed_result = json.loads(run('close', '--work', 'W2', '--outcome', 'completed', '--reason', '合成のIntentに必要な確認を終えた',
                '--owner-instruction', 'W2をcompletedで閉じ本人Pinを解除する', '--release-own-pin',
                '--evidence', 'repo:docs/work-management/evidence/proof.txt'))
            self.assertEqual(json.loads(run('resume', '--request', closed_result['request_path']))['readback']['status'], 'confirmed')
            self.assertNotIn('W2 ', run('show'))
            closed_key = day.get('work_key') or day['readback'].get('work_key')
            self.assertTrue(closed_key, day)
            closed = json.loads(run('thread', '--work', closed_key))['thread']['thread_entries']
            closure = next(e for e in closed if e.get('outcome') == 'completed')
            self.assertTrue(all(closure['identity'].get(k) for k in ('name', 'intent', 'origin')))
            run('pin', '--work', 'W1', '--reason', '終了後に保持中の仕事へ戻る', '--owner-instruction', '本人のW1 held Pinをactiveへ戻す')
            backup_dir = target / 'backups'; backup_dir.mkdir()
            backup = backup_dir / 'backup.json'
            result = json.loads(run('backup', '--output', str(backup)))
            candidate = target / 'recovery/candidate.sqlite3'
            restored = json.loads(run('recover-check', '--backup', str(backup), '--sha256', result['sha256'], '--target', str(candidate)))
            self.assertEqual(restored['management_recovery'], 'verified')
            self.assertEqual(restored['role'], 'recovery_candidate')
            run('recover-check', '--backup', str(backup), '--sha256', result['sha256'], '--target', str(target / 'data/workmap.sqlite3'), success=False)
            self.assertIn('W1.1', run('show'))
            view = json.loads(run('recovery-show', '--candidate', str(candidate), '--work', closed_key, '--thread'))
            self.assertEqual(view['role'], 'recovery_candidate')
            self.assertTrue(view['read_only'])
            self.assertTrue(any(e.get('outcome') == 'completed' for e in view['thread']['thread_entries']))
            restore_args = ['--backup',str(backup),'--candidate',str(candidate),'--sha256',result['sha256'],
                            '--candidate-token',view['candidate_token'],'--project-id',view['project_id'],
                            '--owner-instruction','候補の目的・現在値・理由を確認し、元を保全して同じMapへ復旧する']
            run('restore',*restore_args,success=False)
            original = target / 'data/original-preserved.sqlite3'
            original_bytes = (target / 'data/workmap.sqlite3').read_bytes()
            (target / 'data/workmap.sqlite3').rename(original)
            recovered = json.loads(run('restore',*restore_args))
            self.assertNotEqual(recovered['readback']['revision'],result['revision'])
            self.assertEqual(json.loads(run('resume','--request',recovered['request_path']))['readback']['status'],'confirmed')
            self.assertIn('W1.1',run('show'))
            self.assertTrue(any(e.get('outcome') == 'completed' for e in json.loads(run('thread','--work',closed_key))['thread']['thread_entries']))
            run('permit','--work','W1','--record','--owner-instruction','復旧後の確認結果と判断理由をW1へ残すことを許可する')
            run('reason','--work','W1','--text','確認したbackup時点から、残る不明を照合して再開する',
                '--owner-instruction','この復旧後の判断理由を記録する')
            self.assertTrue(any('残る不明' in e['why'] for e in json.loads(run('thread','--work','W1'))['thread']['thread_entries']))
            self.assertEqual(original.read_bytes(),original_bytes)

    def test_actual_gui_launcher_without_development_context(self):
        with tempfile.TemporaryDirectory(prefix='wmg-') as temp:
            target = Path(temp) / 'kit'; target.mkdir()
            for name in ('src', 'tools'):
                shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('workmap.py', 'WorkMap.pyw'):
                shutil.copyfile(ROOT / name, target / name)
            copy_pin_guide(target)
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1')
            env.pop('PYTHONPATH', None)
            for args in (
                ['init', '--name', 'GUI隔離確認', '--intent', '空の環境で表示できる', '--origin', 'GUI起動試験', '--owner-instruction', 'このMapを作る'],
                ['permit', '--work', 'W0', '--map-add', '--owner-instruction', 'このMapへのOwnerのWork追加を許可する'],
                ['permit', '--work', 'W0', '--edit', '--owner-instruction', 'W0のOwner編集を許可する'],
            ):
                result = subprocess.run([sys.executable, '-I', '-X', 'utf8', 'workmap.py', *args], cwd=target, env=env, capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            probe = target / 'gui_probe.py'
            probe.write_text('''import json, runpy, sys, time
from pathlib import Path
from unittest.mock import patch
base=Path(__file__).resolve().parent
relocated=len(sys.argv)>1
missing=len(sys.argv)>1 and sys.argv[1]=='missing'
sys.path[:0]=[str(base/'src'),str(base)]
import tkinter as tk
import pma.workmap_viewer as viewer
original_viewer=viewer.WorkMapViewer
original_loop=tk.Tk.mainloop
instances=[]; errors=[]; checked=[]
class CapturedViewer(original_viewer):
    def __init__(self,*args,**kw):
        super().__init__(*args,**kw); instances.append(self)
viewer.WorkMapViewer=CapturedViewer
def loop(root,*args,**kw):
    root.withdraw(); deadline=time.monotonic()+10
    def check():
        app=instances[0]
        if not missing and app.data is None and time.monotonic()<deadline:
            root.after(20,check); return
        try:
            if missing:
                assert app.data is None and not app.sources, 'missing own DB opened an old session Map'
                assert not app.session_writable, 'missing DB overwrote the saved connections'
                checked.append(True)
                return
            assert app.data is not None, 'Map did not load'
            assert len(app.sources)==1,[(str(s.path),s.project_id) for s in app.sources]
            assert app.sources[0].path==(base/'data/workmap.sqlite3').resolve()
            assert app.sources[0].evidence_root==base
            assert app.operator_id=='actor:local-owner'
            assert app.task_index_path is None
            assert app.tree.get_children(), 'Map tree is empty'
            assert app.app_icon.width()>0
            assert app.add_button.instate(['!disabled']), 'Map registration form is unavailable'
            if relocated:
                from pma.workmap_map_permission import map_add_permissions
                key=app.data['snapshot']['aliases']['W0']
                assert not map_add_permissions(app.data['snapshot'],key,'actor:local-owner',store_path=base/'data/workmap.sqlite3'), 'old location grant was inherited'
            app.auto_button.invoke(); assert app.auto_enabled.get()
            app.auto_button.invoke(); assert not app.auto_enabled.get()
            from pma.workmap_editor_ui import EditDialog
            evidence=base/'docs/work-management/evidence/owner_gui_trial.txt'
            evidence.write_text('OwnerはGUIで今回の名前を訂正する。目的は変更しない。',encoding='utf-8')
            key=app.data['snapshot']['aliases']['W0']
            dialog=EditDialog(app,key)
            dialog.new_text.delete('1.0','end')
            dialog.new_text.insert('1.0','複製先のGUI確認' if relocated else 'GUIで訂正した名前')
            dialog.reason_input.set('名前を分かりやすく訂正する')
            with patch('pma.workmap_editor_ui.filedialog.askopenfilenames',return_value=[str(evidence)]), patch('pma.workmap_editor_ui.messagebox.askyesno',return_value=True):
                dialog.choose_button.invoke(); dialog.preview_button.invoke()
                assert dialog.prepared is not None,dialog.message.get()
                dialog.save_button.invoke()
                assert dialog.last_result['status']=='confirmed',dialog.last_result
            dialog.destroy()
            checked.append(True)
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            app.close_window()
    root.after(20,check); return original_loop(root,*args,**kw)
tk.Tk.mainloop=loop
try:
    runpy.run_path(str(base/'WorkMap.pyw'),run_name='__main__')
except SystemExit as exc:
    assert exc.code==0, exc.code
assert checked and not errors, errors
assert (base/'data/viewer-session.json').is_file()
print(json.dumps({'gui':'started','own_map_only':True,'auto_on_off':True,'no_codex_dependency':True}))
''', encoding='utf-8')
            result = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(probe)], cwd=target, env=env,
                                    text=True, encoding='utf-8', capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['gui'], 'started')
            # Copy only this synthetic used kit, never the user's ROOT/data.
            original_bytes = (target / 'data/workmap.sqlite3').read_bytes()
            relocated = Path(temp) / 'moved'
            shutil.copytree(target, relocated, ignore=shutil.ignore_patterns('__pycache__'))
            result = subprocess.run([sys.executable,'-I','-X','utf8',str(relocated/'gui_probe.py'),'relocated'],
                                    cwd=relocated,env=env,text=True,encoding='utf-8',capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual((target/'data/workmap.sqlite3').read_bytes(),original_bytes)
            missing = Path(temp) / 'missing-own-db'
            shutil.copytree(target, missing, ignore=shutil.ignore_patterns('__pycache__'))
            (missing/'data/workmap.sqlite3').rename(missing/'data/preserved.sqlite3')
            session_before = (missing/'data/viewer-session.json').read_bytes()
            result = subprocess.run([sys.executable,'-I','-X','utf8',str(missing/'gui_probe.py'),'missing'],
                                    cwd=missing,env=env,text=True,encoding='utf-8',capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual((missing/'data/viewer-session.json').read_bytes(),session_before)
            self.assertEqual((target/'data/workmap.sqlite3').read_bytes(),original_bytes)


if __name__ == '__main__':
    unittest.main()
