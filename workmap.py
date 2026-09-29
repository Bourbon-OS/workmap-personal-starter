"""Personal starter: thin CLI over the existing Work Map Core (Python 3.11+)."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from pma.workmap_contract import validate_snapshot
from pma.workmap_sqlite_store import SQLiteStore
from tools.add_workmap_work import add_work
from tools.read_workmap_store import read_result
from tools.backup_workmap_store import export_one, recover_isolated

DB = ROOT / 'data' / 'workmap.sqlite3'


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='個人用Work Map導入入口。保存先はこのフォルダのdata/workmap.sqlite3。')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'add'):
        p = sub.add_parser(name)
        p.add_argument('--name', required=True)
        p.add_argument('--intent', required=True)
        p.add_argument('--origin', required=True)
        p.add_argument('--owner-instruction', required=True, help='本人が指示した登録内容。AIは本人の指示を捏造しない。')
        p.add_argument('--actor', default='actor:local-owner')
        if name == 'add':
            p.add_argument('--parent', required=True)
            p.add_argument('--reason', help='Intent/Originだけで残せない特別な出生理由がある場合のみ')
    show = sub.add_parser('show'); show.add_argument('--work')
    thread = sub.add_parser('thread'); thread.add_argument('--work')
    history = sub.add_parser('history'); history.add_argument('--work')
    resume = sub.add_parser('resume-add'); resume.add_argument('--request', required=True, type=Path)
    backup = sub.add_parser('backup'); backup.add_argument('--output', required=True, type=Path)
    recover = sub.add_parser('recover-check')
    recover.add_argument('--backup', type=Path, required=True)
    recover.add_argument('--sha256', required=True)
    recover.add_argument('--target', type=Path, required=True, help='新しい隔離確認用DB。現用DBは指定しない。')
    args = parser.parse_args()
    try:
        if args.command in ('init', 'add'):
            if args.command == 'init':
                if DB.exists():
                    raise ValueError('初期化済みです。応答が不明ならdata/transition-requestsの保存要求をresume-addで確認してください。')
                if not all(getattr(args, k).strip() for k in ('name', 'intent', 'origin', 'owner_instruction', 'actor')):
                    raise ValueError('Name / Intent / Origin / Owner指示 / actorは空にできません。')
                DB.parent.mkdir(parents=True, exist_ok=True)
                state = {'format_version': '0.4.1', 'project_id': 'project:' + str(uuid4()),
                    'revision': uuid4().hex, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                    'works': {}, 'aliases': {}, 'display_ids': {}, 'display_id_history': [],
                    'map': {k: [] for k in ('members', 'relations', 'pins', 'permissions', 'conditions', 'notes')},
                    'retirements': {}, 'thread_entries': [], 'receipts': {},
                    'metadata': {'completeness': 'full', 'store_role': 'canonical_current', 'basis': 'new_empty_personal_store'}}
                SQLiteStore(DB).initialize(validate_snapshot(state))
            (ROOT / 'docs/work-management/evidence').mkdir(parents=True, exist_ok=True)
            result = add_work(DB, backend='sqlite', actor=args.actor,
                parent=getattr(args, 'parent', None), name=args.name, intent=args.intent, origin=args.origin,
                owner_instruction=args.owner_instruction, reviewed_owner_add=True,
                reason=getattr(args, 'reason', None), root=args.command == 'init',
                display_id='W0' if args.command == 'init' else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('readback', {}).get('status') == 'confirmed' else 2
        if args.command in ('show', 'thread', 'history'):
            s = SQLiteStore(DB).read().snapshot
            result = read_result(DB, args.work, tree=args.command == 'show' and not args.work,
                thread=args.command == 'thread', history=args.command == 'history',
                backend='sqlite', expected_project_id=s['project_id'], evidence_repo=ROOT)
            print(result['tree'] if 'tree' in result else json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('status') == 'confirmed' else 2
        if args.command == 'resume-add':
            result = add_work(DB, backend='sqlite', resume=args.request.resolve(), reviewed_owner_add=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('readback', {}).get('status') == 'confirmed' else 2
        if args.command == 'backup':
            result = export_one(DB, args.output.resolve(), backend='sqlite', evidence_repo=ROOT)
        else:
            if args.target.resolve() == DB:
                raise ValueError('復旧確認先には現用DBを指定できません。')
            args.target.resolve().parent.mkdir(parents=True, exist_ok=True)
            result = recover_isolated(args.backup.resolve(), args.target.resolve(), args.sha256, backend='sqlite')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('management_recovery', 'verified') == 'verified' else 2
    except Exception as exc:
        print(json.dumps({'status': 'unknown_or_rejected', 'reason': str(exc),
            'recovery': '失敗を未実行と決めつけずshowと保存済み要求を確認。登録結果不明時はresume-addで同じ要求を照会する。'}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
