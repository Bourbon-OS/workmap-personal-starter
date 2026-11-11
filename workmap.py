"""Personal local trial: thin CLI over the existing Work Map Core (Python 3.11+)."""
import argparse
import hashlib
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
from tools.backup_workmap_store import (export_one, recover_isolated, inspect_recovery,
                                       prepare_restore, restore_verified)

DB = ROOT / 'data' / 'workmap.sqlite3'
OWNER = 'actor:local-owner'
NEW_COMPRESSION = 'a'
NEW_RETENTION = 'current_and_previous'


def initial_pin_review(state, request, args):
    from pma.workmap_pin_move import initial_pin_scope
    from pma.workmap_contract import _collection_status, rule_requires_disposition
    keys, records, required = initial_pin_scope(state, request)
    if any(rule_requires_disposition(r, ('add_pin', 'append_thread')) for r in records['action_rules']) and not args.action_rule_basis:
        raise ValueError('初回の引受に関係する行動ルールを確認し、--action-rule-basisへ判断根拠を指定してください。')
    target = request['changes'][0]['target']
    if any(d['kind'] == 'dependency' and target in (d['consumer'], d['provider'])
           for d in state['map']['relations']) and not args.dependency_basis:
        raise ValueError('初回の引受に関係する依存を確認し、--dependency-basisへ判断根拠を指定してください。')
    unknown = (any(_collection_status(state, n, k) != 'confirmed' for k in keys
                   for n in ('pins', 'permissions', 'action_rules'))
               or any(_collection_status(state, 'pins', k) != 'confirmed' for k in state['map']['members']))
    if unknown and not args.scope_basis:
        raise ValueError('本人のPin全体・関連する許可や行動ルールが未確認です。根拠を確認した--scope-basisが必要です。')
    refs = sorted(set(request['source_refs']) | required)
    request['source_refs'] = refs
    request['thread_entries'][0]['source_refs'] = refs
    request['changes'][0]['value']['refs'] = refs
    request['thread_entries'][0]['assumptions'] = json.dumps({
        'action_rule_basis': args.action_rule_basis, 'dependency_basis': args.dependency_basis}, ensure_ascii=False)
    reason = args.scope_basis or '同じ版の関連する許可・行動ルールと本人Pin検索が確認済み。未知の不在を推測していない。'
    return {n: {'mode': 'current_scope', 'reason': reason, 'refs': refs, 'records': rows}
            for n, rows in records.items()}


def resume_request(path):
    """Dispatch existing saved requests; never guess a new revision or request."""
    from pma.workmap_canonical_core import CanonicalWorkMapCore
    from pma.workmap_core import _digest
    from tools.apply_workmap_transition import _apply_request
    path = Path(path).resolve()
    bundle = json.loads(path.read_text(encoding='utf-8'))
    if bundle.get('kind') == 'owner-pin-workflow-v1':
        from tools.workmap_pin_workflow import owner_pin_workflow
        return owner_pin_workflow(DB, backend='sqlite', reviewed_pin_move=True, reviewed_owner_pin=True, resume=path)
    if bundle.get('operation') == 'restore':
        return restore_request(resume=path)
    if bundle.get('store_binding') != {'path': str(DB.resolve()), 'backend': 'sqlite'}:
        raise ValueError('保存済み要求はこのMapの保存先と一致していません。')
    q = bundle.get('request', {}); changes = q.get('changes', [])
    if not changes: raise ValueError('保存済み要求の種類を確認できません。')
    op = changes[-1].get('op')
    if op == 'add_work':
        return add_work(DB, backend='sqlite', resume=path, reviewed_owner_add=True)
    if op == 'close':
        from tools.close_workmap_work import close_work
        return close_work(DB, backend='sqlite', resume=path, reviewed_close=True)
    if op == 'append_thread' and q.get('thread_entries', [{}])[0].get('kind') == 'evidence_recorded':
        from tools.record_workmap_evidence import record_evidence
        return record_evidence(DB, backend='sqlite', resume=path)
    if len(changes) != 1 or op not in ('append_thread', 'set_permission', 'add_pin'):
        raise ValueError('この回収入口で扱わない要求です。元の正規入口で確認してください。')
    if bundle.get('request_digest') != _digest(q):
        raise ValueError('保存済み要求の内容が変わっています。元の要求を確認してください。')
    state = SQLiteStore(DB).read().snapshot
    if q.get('project_id') != state['project_id']:
        raise ValueError('保存済み要求は別projectです。')
    observed = CanonicalWorkMapCore(SQLiteStore(DB)).lookup_transition(q['transition_id'])
    if observed.get('status') == 'applied':
        if observed['receipt']['request_digest'] != _digest(q):
            raise ValueError('同じ要求IDの内容が一致しません。')
        return {'result': observed['receipt'], 'readback': {'status': 'confirmed',
                'scope': 'applied history; later current changes are not undone', 'revision': state['revision']}}
    from pma.workmap_close import repository_evidence
    bindings = bundle.get('source_bindings')
    if not isinstance(bindings, dict) or set(bindings) != set(q['source_refs']):
        raise ValueError('この旧要求には根拠の版束縛がありません。現在と元の要求を個別に確認してください。')
    if any(hashlib.sha256(repository_evidence(ref)).hexdigest() != digest for ref, digest in bindings.items()):
        raise ValueError('根拠が変わっています。元の要求の版を差し替えず、再判断してください。')
    r = _apply_request(DB, q, backend='sqlite', evidence_repo=ROOT,
                       reviewed_owner_permission=op == 'set_permission',
                       context_assessments=bundle.get('assessments'))
    found = CanonicalWorkMapCore(SQLiteStore(DB)).lookup_transition(q['transition_id'])
    return {'request_path': str(path), 'result': r,
            'readback': {'status': 'confirmed' if found.get('status') == 'applied' else 'unknown',
                         'scope': 'applied history', 'revision': found['observed_revision']}}


def restore_request(args=None, *, resume=None):
    """Explicit local Owner recovery; no authority is granted to restored Works."""
    from pma.workmap_core import _digest
    if resume:
        path = Path(resume).resolve(); bundle = json.loads(path.read_text(encoding='utf-8'))
        if (bundle.get('operation') != 'restore' or bundle.get('store_binding') != str(DB.resolve())):
            raise ValueError('復旧要求の保存先が一致しません。')
        if DB.exists():
            state = SQLiteStore(DB).read().snapshot
            if _digest(state) != bundle['restored_digest']:
                raise ValueError('復旧先に別の状態があります。上書きせず現在値を確認してください。')
            return {'result': {'result': 'applied'}, 'readback': {'status': 'confirmed', 'revision': state['revision']}}
        args = argparse.Namespace(**bundle['inputs'])
        # Old saved restores retain their original raw/all policy on resume.
        compression = bundle['inputs'].get('compression', 'none')
        retention = bundle['inputs'].get('retention', 'all')
    else:
        compression, retention = NEW_COMPRESSION, NEW_RETENTION
        if DB.exists():
            raise ValueError('現用DBは上書きしません。全アプリを閉じ、元DBとdocsを保全して退避してから復旧してください。')
        source = owner_source(args.owner_instruction, OWNER, args.project_id)
        path = DB.parent / 'transition-requests' / (uuid4().hex + '.restore.json')
        path.parent.mkdir(parents=True, exist_ok=True)
    original, files, thread_status = prepare_restore(args.backup, args.candidate, args.sha256,
                args.candidate_token, args.project_id, backend='sqlite')
    if resume:
        state = dict(original, revision=bundle['new_revision'], recorded_at=bundle['recorded_at'])
        if _digest(state) != bundle['restored_digest']:
            raise ValueError('保存済み復旧要求が元backupと一致しません。')
    else:
        from copy import deepcopy
        state = deepcopy(original)
        state.update(revision=uuid4().hex, recorded_at=datetime.now(timezone.utc).isoformat())
        bundle = {'operation': 'restore', 'store_binding': str(DB.resolve()),
                  'actor': OWNER, 'recorded_at': state['recorded_at'], 'source_revision': original['revision'],
                  'inputs': {**{k: str(getattr(args,k)) for k in ('backup','candidate','sha256','candidate_token','project_id','owner_instruction')},
                             'compression': compression, 'retention': retention},
                  'owner_source': source, 'owner_source_sha256': hashlib.sha256((ROOT/source[5:]).read_bytes()).hexdigest(),
                  'new_revision': state['revision'], 'restored_digest': _digest(state)}
        with path.open('x', encoding='utf-8') as stream:
            json.dump(bundle,stream,ensure_ascii=False,indent=2); stream.write('\n'); stream.flush()
            import os
            os.fsync(stream.fileno())
    if hashlib.sha256((ROOT/bundle['owner_source'][5:]).read_bytes()).hexdigest() != bundle['owner_source_sha256']:
        raise ValueError('復旧のOwner指示が変わっています。再判断してください。')
    result = restore_verified(state, files, DB, ROOT, backend='sqlite',
                              compression=compression, retention=retention)
    return {'request_path': str(path), 'result': {'result': 'applied', **result},
            'readback': {'status': 'confirmed', 'revision': state['revision'], 'thread_status': thread_status},
            'boundary': 'selected backup revision restored; external artifacts and later lost updates are not proved recovered'}


def owner_source(text, actor, work):
    if not text or not text.strip():
        raise ValueError('実際のOwner指示を入力してください。')
    folder = ROOT / 'docs/work-management/evidence'; folder.mkdir(parents=True, exist_ok=True)
    file = folder / ('owner_personal_' + uuid4().hex + '.txt')
    file.write_text('ローカル操作者が確認したOwner指示。認証を証明するものではない。\n'
                    + '操作者: ' + actor + '\n対象: ' + work + '\n原文:\n' + text + '\n', encoding='utf-8')
    return 'repo:' + file.relative_to(ROOT).as_posix()


def append_reason_or_permission(args):
    from tools.apply_workmap_transition import _apply_request
    state = SQLiteStore(DB).read().snapshot
    target = state['aliases'].get(args.work, args.work)
    if target not in state['map']['members']:
        raise ValueError('現在MapにあるWorkを指定してください。')
    source = owner_source(args.owner_instruction, args.actor, target)
    tid = 'transition:' + str(uuid4()); grant = None; assessments = None
    entry = {'id': 'thread:' + tid, 'works': [target], 'kind': 'rationale_added',
             'summary': getattr(args, 'text', 'Ownerの明示的な操作許可'),
             'why': getattr(args, 'text', args.owner_instruction), 'occurred_at': None,
             'recorded_at': datetime.now(timezone.utc).isoformat(), 'actor': args.actor,
             'source_refs': [source], 'transition_id': tid}
    change = {'op': 'append_thread', 'target': target}
    if args.command == 'permit':
        operations = []
        if args.edit: operations += ['update_work']
        if args.record: operations += ['append_thread']
        if args.close: operations += ['close', 'remove_pin', 'append_thread']
        if args.pin: operations += ['add_pin', 'update_pin', 'remove_pin', 'append_thread']
        constraints = ['指定Workの選んだ操作だけ。引受・親替え・他Workへ拡張しない。']
        if args.map_add:
            if operations: raise ValueError('--map-addは他の許可指定と分けてください。')
            operations = ['add_work_in_map']
            constraints = ['map_project_id:' + state['project_id'], 'map_store_path:' + str(DB.resolve()),
                           'このMapへのWork追加だけ。引受や他操作の許可は含めない。']
        if not operations: raise ValueError('--edit / --record / --close / --pin / --map-addを指定してください。')
        grant = {'id': 'permission:personal:' + uuid4().hex, 'targets': [target], 'grantee': args.actor,
                 'grantor_ref': 'principal:owner', 'effect': 'allow', 'operations': list(dict.fromkeys(operations)),
                 'constraints': constraints, 'source_refs': [source]}
        change = {'op': 'set_permission', 'target': target, 'permission_id': grant['id'], 'expected': None, 'value': grant}
        entry['kind'] = 'permission_changed'
    elif args.command == 'pin':
        # First engagement has no active/held source to move. Use ordinary Core
        # add_pin with the explicit Work-specific permission, never a raw write.
        if any(p['actor_id'] == args.actor and p['mode'] == 'active' for p in state['map']['pins']):
            raise ValueError('現在activeのPinがあります。通常の切替を使ってください。')
        now = datetime.now(timezone.utc).isoformat()
        pin = {'id': 'pin:' + str(uuid4()), 'work_id': target, 'actor_id': args.actor,
               'mode': 'active', 'pinned_at': now, 'confirmed_at': now, 'refs': [source]}
        change = {'op': 'add_pin', 'target': target, 'value': pin}
        entry.update(kind='pin_added', summary='本人の最初の引受', why=args.reason)
    request = {'format_version': state['format_version'], 'project_id': state['project_id'],
               'transition_id': tid, 'actor_id': args.actor, 'base_revision': state['revision'],
               'intent': entry['summary'], 'source_refs': [source], 'preconditions': [],
               'changes': [change], 'thread_entries': [entry]}
    if args.command == 'pin':
        assessments = initial_pin_review(state, request, args)
    folder = DB.parent / 'transition-requests'; folder.mkdir(exist_ok=True)
    path = folder / (tid.removeprefix('transition:') + '.json')
    with path.open('x', encoding='utf-8') as stream:
        from pma.workmap_core import _digest
        json.dump({'store_binding': {'path': str(DB.resolve()), 'backend': 'sqlite'}, 'request': request,
                   'request_digest': _digest(request),
                   'source_bindings': {ref: hashlib.sha256((ROOT / ref[5:]).read_bytes()).hexdigest()
                                       for ref in request['source_refs']},
                   'assessments': assessments}, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush()
        import os
        os.fsync(stream.fileno())
    result = _apply_request(DB, request, backend='sqlite', reviewed_owner_permission=grant is not None,
                            context_assessments=assessments, evidence_repo=ROOT)
    after = SQLiteStore(DB).read().snapshot
    present = grant in after['map']['permissions'] if grant else any(
        e['id'] == entry['id'] and e['why'] == entry['why'] and e['works'] == [target]
        for e in after['thread_entries'])
    if args.command == 'pin': present = present and pin in after['map']['pins']
    return {'request_path': str(path), 'result': result,
            'readback': {'status': 'confirmed' if present else 'unavailable', 'revision': after['revision'], 'entry_id': entry['id']}}


def gui(actor):
    import tkinter as tk
    from pma.workmap_viewer import WorkMapViewer
    root = tk.Tk()
    app = WorkMapViewer(root, actor_names={actor: 'Owner'}, operator_id=actor,
                       session_path=ROOT / 'data/viewer-session.json', background_reads=True, task_index_path=None)
    preferred = None; own_unreadable = not DB.exists()
    if DB.exists():
        from pma.workmap_store import StoreError
        try:
            state = validate_snapshot(SQLiteStore(DB).read().snapshot)
            if state['metadata'].get('store_role') != 'canonical_current': raise ValueError('not canonical current')
            preferred = (state['project_id'], DB.resolve())
        except (OSError, ValueError, StoreError):
            own_unreadable = True
    if own_unreadable:
        app.session_writable = False
        app.session_error = 'このフォルダの正本が見つからないか、読めません。以前の接続一覧は保全し、自動接続しません。保存先と復旧手順を確認してください。'
        app.status.configure(text=app.session_error)
    if not own_unreadable: app.restore_session(preferred_source=preferred)
    if DB.exists() and not any(s.path == DB.resolve() for s in app.sources): app.add_source(DB, ROOT)
    root.mainloop()


def main(argv=None):
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
        p.add_argument('--actor', default=OWNER)
        if name == 'add':
            p.add_argument('--parent', required=True)
            p.add_argument('--reason', help='Intent/Originだけで残せない特別な出生理由がある場合のみ')
    show = sub.add_parser('show'); show.add_argument('--work'); show.add_argument('--brief', action='store_true')
    thread = sub.add_parser('thread'); thread.add_argument('--work')
    history = sub.add_parser('history'); history.add_argument('--work')
    resume = sub.add_parser('resume-add'); resume.add_argument('--request', required=True, type=Path)
    resume = sub.add_parser('resume'); resume.add_argument('--request', required=True, type=Path)
    backup = sub.add_parser('backup'); backup.add_argument('--output', required=True, type=Path)
    recover = sub.add_parser('recover-check')
    recover.add_argument('--backup', type=Path, required=True)
    recover.add_argument('--sha256', required=True)
    recover.add_argument('--target', type=Path, required=True, help='新しい隔離確認用DB。現用DBは指定しない。')
    view = sub.add_parser('recovery-show'); view.add_argument('--candidate', type=Path, required=True)
    view.add_argument('--work'); view.add_argument('--thread', action='store_true')
    restore = sub.add_parser('restore')
    for name in ('backup', 'candidate'): restore.add_argument('--' + name, type=Path, required=True)
    for name in ('sha256', 'candidate-token', 'project-id', 'owner-instruction'):
        restore.add_argument('--' + name, required=True)
    p = sub.add_parser('gui'); p.add_argument('--actor', default=OWNER)
    p = sub.add_parser('permit'); p.add_argument('--work', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--owner-instruction', required=True)
    for flag in ('edit', 'record', 'close', 'pin', 'map-add'): p.add_argument('--' + flag, action='store_true')
    p = sub.add_parser('reason'); p.add_argument('--work', required=True); p.add_argument('--text', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--owner-instruction', required=True)
    p = sub.add_parser('result'); p.add_argument('--work', required=True); p.add_argument('--text', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--source', action='append', required=True)
    p = sub.add_parser('pin'); p.add_argument('--work', required=True); p.add_argument('--reason', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--owner-instruction', required=True)
    for field in ('action-rule-basis', 'dependency-basis', 'scope-basis'): p.add_argument('--' + field)
    p = sub.add_parser('close'); p.add_argument('--work', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--owner-instruction', required=True)
    p.add_argument('--outcome', choices=('completed', 'stopped', 'replaced'), required=True)
    p.add_argument('--reason', required=True); p.add_argument('--evidence', action='append', default=[])
    p.add_argument('--successor', action='append', default=[]); p.add_argument('--residual', action='append', default=[])
    p.add_argument('--release-own-pin', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'gui':
            gui(args.actor); return 0
        if args.command in ('init', 'add'):
            if args.command == 'init':
                if DB.exists():
                    current = SQLiteStore(DB).read().snapshot
                    if not current['works'] and not current['receipts'] and current['metadata'].get('basis') == 'new_empty_personal_store':
                        raise ValueError('初期化途中の空DBです。元のdataとdocsを別名へ退避して保全し、新しい空フォルダでinitを行ってください。')
                    raise ValueError('初期化済みです。initの応答が不明なら保存済みの追加要求をresume-add --requestで確認してください。')
                if not all(getattr(args, k).strip() for k in ('name', 'intent', 'origin', 'owner_instruction', 'actor')):
                    raise ValueError('Name / Intent / Origin / Owner指示 / actorは空にできません。')
                DB.parent.mkdir(parents=True, exist_ok=True)
                state = {'format_version': '0.4.2', 'project_id': 'project:' + str(uuid4()),
                    'revision': uuid4().hex, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                    'works': {}, 'aliases': {}, 'display_ids': {}, 'display_id_history': [],
                    'map': {k: [] for k in ('members', 'relations', 'pins', 'permissions', 'action_rules', 'notes')},
                    'retirements': {}, 'thread_entries': [], 'receipts': {},
                    'metadata': {'completeness': 'full', 'store_role': 'canonical_current', 'basis': 'new_empty_personal_store'}}
                SQLiteStore(DB).initialize(validate_snapshot(state),
                                           compression=NEW_COMPRESSION, retention=NEW_RETENTION)
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
                brief=getattr(args, 'brief', False),
                backend='sqlite', expected_project_id=s['project_id'], evidence_repo=ROOT)
            print(result['tree'] if 'tree' in result else json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('status') == 'confirmed' else 2
        if args.command == 'resume-add':
            result = add_work(DB, backend='sqlite', resume=args.request.resolve(), reviewed_owner_add=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('readback', {}).get('status') == 'confirmed' else 2
        if args.command == 'resume':
            result = resume_request(args.request)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('readback', {}).get('status') == 'confirmed' else 2
        if args.command == 'recovery-show':
            result = inspect_recovery(args.candidate, work=args.work, thread=args.thread)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] == 'confirmed' else 2
        if args.command == 'restore':
            result = restore_request(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('readback', {}).get('status') == 'confirmed' else 2
        if args.command in ('permit', 'reason'):
            result = append_reason_or_permission(args)
        elif args.command == 'result':
            from tools.record_workmap_evidence import record_evidence
            result = record_evidence(DB, backend='sqlite', actor=args.actor, work=args.work, summary=args.text, sources=args.source)
        elif args.command == 'pin':
            from tools.workmap_pin_workflow import owner_pin_workflow
            current = SQLiteStore(DB).read().snapshot
            target = current['aliases'].get(args.work, args.work)
            own = [p for p in current['map']['pins'] if p['actor_id'] == args.actor]
            if not any(p['mode'] == 'active' or p['work_id'] == target for p in own):
                result = append_reason_or_permission(args)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result['readback']['status'] == 'confirmed' else 2
            draft = read_result(DB, backend='sqlite', pin_destination=args.work, actor=args.actor, pin_inputs=True,
                                expected_project_id=SQLiteStore(DB).read().snapshot['project_id'], evidence_repo=ROOT)
            if draft['status'] != 'confirmed' or 'inputs_draft' not in draft:
                raise ValueError(draft.get('inputs_draft_error') or draft.get('reason') or 'Pin移動の現在前提を取得できません。')
            packet = draft['inputs_draft']; packet['inputs']['reason'] = args.reason
            for field in ('action_rule_basis', 'dependency_basis', 'scope_basis'):
                if getattr(args, field): packet['inputs'][field] = getattr(args, field)
            result = owner_pin_workflow(DB, backend='sqlite', reviewed_pin_move=True, reviewed_owner_pin=True,
                                        owner_instruction=args.owner_instruction, inputs_packet=packet)
        elif args.command == 'close':
            from tools.close_workmap_work import close_work
            source = owner_source(args.owner_instruction, args.actor, args.work)
            result = close_work(DB, backend='sqlite', actor=args.actor, work=args.work, outcome=args.outcome,
                                reason=args.reason, sources=[source], evidence_refs=args.evidence,
                                successors=args.successor, residuals=args.residual,
                                release_own_pin=args.release_own_pin, reviewed_close=True)
        elif args.command == 'backup':
            result = export_one(DB, args.output.resolve(), backend='sqlite', evidence_repo=ROOT)
        else:
            if args.target.resolve() == DB:
                raise ValueError('復旧確認先には現用DBを指定できません。')
            args.target.resolve().parent.mkdir(parents=True, exist_ok=True)
            result = recover_isolated(args.backup.resolve(), args.target.resolve(), args.sha256, backend='sqlite',
                                      compression=NEW_COMPRESSION, retention=NEW_RETENTION)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if 'readback' in result: return 0 if result['readback']['status'] == 'confirmed' else 2
        if 'result' in result and isinstance(result['result'], dict) and result['result'].get('result') != 'applied': return 2
        return 0 if result.get('management_recovery', 'verified') == 'verified' else 2
    except Exception as exc:
        print(json.dumps({'status': 'unknown_or_rejected', 'reason': str(exc),
            'recovery': '失敗を未実行と決めつけず現在Mapを再読。保存済みのCLI要求がある場合だけ、init/addは resume-add --request <元要求>、その他の対応するCLI操作は resume --request <元要求> で照会する。GUI要求はこのCLIへ流用しない。未初期化ならinit、初期化途中なら元を保全して新しいフォルダで開始する。'}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
