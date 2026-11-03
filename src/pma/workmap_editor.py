"""Single-field desktop edits through the existing Core; no alternate writer."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pma.workmap_contract import validate_snapshot, inspect_work, ContextUnknown, SnapshotInvalid
from pma.workmap_core import _digest, TransitionConflict, TransitionDenied, TransitionError
from pma.workmap_sqlite_store import SQLiteStore
from pma.workmap_store import StoreError, StoreConflict
from tools.apply_workmap_transition import _apply_request

FIELDS = {'名前': 'name', 'Intent（目的）': 'intent', 'Origin（生まれた理由）': 'origin'}
KINDS = {'誤記録の訂正': 'correction', '同じ目的の明確化': 'refinement',
         '未確認目的の確定': 'confirmation', '別の目的への変更（新Workが必要）': 'material_change'}


def _source_path(repo, ref):
    parts = PurePosixPath(ref.removeprefix('repo:')).parts
    root = Path(repo).resolve()
    if (not ref.startswith('repo:') or not parts or parts[0] != 'docs'
            or '#' in ref or any(p in ('.', '..') or ':' in p or '\\' in p for p in parts)):
        raise ValueError('根拠は、このMapの資料フォルダ内の文書を選んでください。')
    path = (root / '/'.join(parts)).resolve()
    if not path.is_relative_to(root / 'docs') or path.suffix.lower() not in ('.txt', '.md', '.json'):
        raise ValueError('根拠資料がこのMapの文書範囲外です。')
    return path


def source_ref(repo, filename):
    path = Path(filename).resolve(); root = Path(repo).resolve()
    if not path.is_relative_to(root):
        raise ValueError('別Mapの根拠資料は使えません。')
    ref = 'repo:' + path.relative_to(root).as_posix()
    _source_path(root, ref)
    return ref


@dataclass
class PreparedEdit:
    """Transient reviewed request, not another Store or a mutable current Work."""
    store_path: Path
    evidence_root: Path
    request: dict
    bindings: tuple
    field: str
    old_value: object
    new_value: object
    owner_reviewed: bool
    request_digest: str
    request_path: Path | None = None


def prepare_edit(source, snapshot, work_key, *, field, value, actor, reason, refs,
                 kind=None, same_work_basis='', owner_reviewed=False):
    state = validate_snapshot(copy.deepcopy(snapshot))
    if state['metadata'].get('store_role') != 'canonical_current':
        raise ValueError('現在の正本以外は編集できません。')
    if source.project_id is not None and source.project_id != state['project_id']:
        raise ValueError('編集中のMapが違います。読み直してください。')
    if work_key not in state['map']['members']:
        raise ValueError('現在Mapに存在するWorkだけを編集できます。')
    if field not in ('name', 'intent', 'origin') or not isinstance(value, str) or not value.strip():
        raise ValueError('変更項目と空でない変更後の値が必要です。')
    if not isinstance(actor, str) or not actor.strip() or not reason.strip() or not refs:
        raise ValueError('操作する担当、変更理由、根拠資料を指定してください。')
    work = state['works'][work_key]; value = value.strip(); reason = reason.strip()
    old = work[field]
    if value == (old.get('summary') if field == 'origin' else old):
        raise ValueError('現在の値と同じです。変更はありません。')
    bindings = tuple({'ref': ref, 'sha256': hashlib.sha256(_source_path(source.evidence_root, ref).read_bytes()).hexdigest()}
                     for ref in dict.fromkeys(refs))
    version_refs = [b['ref'] + '#sha256=' + b['sha256'] for b in bindings]
    tid = 'transition:' + str(uuid4()); now = datetime.now(timezone.utc).isoformat()
    entry = {'id': 'thread:' + tid, 'works': [work_key], 'kind': 'work_renamed' if field == 'name' else 'correction',
             'summary': {'name': 'Workの名前を更新', 'origin': 'Workの出自を根拠付きで訂正', 'intent': 'WorkのIntentを更新'}[field],
             'why': reason, 'occurred_at': None, 'recorded_at': now, 'actor': actor.strip(),
             'source_refs': version_refs, 'transition_id': tid}
    expected = {field: copy.deepcopy(old)}
    new = {'summary': value, 'refs': version_refs} if field == 'origin' else value
    change_value = {field: new}
    if field == 'intent':
        if kind == 'material_change':
            raise ValueError('目的そのものが変わる場合は、元Workを処遇して新Workを作ります。')
        if kind not in ('confirmation', 'correction', 'refinement'):
            raise ValueError('Intent変更の種類を選んでください。')
        if not owner_reviewed or not same_work_basis.strip():
            raise ValueError('Ownerの判断根拠と、同じWorkで扱う理由の確認が必要です。')
        detail = {'kind': kind, 'reason': reason, 'same_work_basis': same_work_basis.strip(), 'owner_sources': list(bindings)}
        c = inspect_work(state, work_key)
        expected = {'intent': old, 'permissions': copy.deepcopy(c['permissions']),
                    'action_rules': copy.deepcopy(c['action_rules']),
                    'permission_status': c['permission_status'], 'action_rule_status': c['action_rule_status']}
        change_value = {'intent': new, 'intent_change': detail}
        entry['kind'] = 'intent_' + kind
        entry['source_refs'] = list(dict.fromkeys([b['ref'] for b in bindings] + version_refs))
        history = {**detail, 'old_intent': old, 'new_intent': new,
                   'old_source_refs': state['metadata'].get('work_sources', {}).get(work_key, {}).get('intent', [])}
        entry['consequence'] = json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    request = {'format_version': state['format_version'], 'project_id': state['project_id'],
               'transition_id': tid, 'actor_id': actor.strip(), 'base_revision': state['revision'],
               'intent': '選択したWorkの' + field + 'を根拠付きで更新する',
               'source_refs': entry['source_refs'], 'preconditions': [],
               'changes': [{'op': 'update_work', 'target': work_key, 'expected': expected, 'value': change_value}],
               'thread_entries': [entry]}
    if field == 'intent':
        from tools.apply_workmap_transition import review_owner_intent
        try:
            review_owner_intent(request, Path(source.evidence_root))
        except TransitionDenied as exc:
            raise ValueError('今回のOwner判断を docs/work-management/evidence/owner_任意名.txt に記録して選んでください。'
                             '変更前後・理由・同じ目的である根拠を含めます。名前やhashだけでは内容の妥当性を証明しません。') from exc
    return PreparedEdit(Path(source.path).resolve(), Path(source.evidence_root).resolve(), request,
                        bindings, field, copy.deepcopy(old), copy.deepcopy(new), owner_reviewed, _digest(request))


def _current(prepared):
    state = validate_snapshot(SQLiteStore(prepared.store_path).read(read_only=True).snapshot)
    if state['project_id'] != prepared.request['project_id'] or state['metadata'].get('store_role') != 'canonical_current':
        raise TransitionConflict('選択した保存先のMapが変わっています。')
    return state


def inspect_edit_result(prepared):
    """Re-read the exact submitted ID; never invent or resend a new request."""
    state = _current(prepared); request = prepared.request
    receipt = state['receipts'].get(request['transition_id'])
    if receipt is None:
        return {'status': 'not_applied', 'revision': state['revision'],
                'message': '現在の正本に、この要求の適用記録はありません。再適用前に現在値を確認してください。'}
    if receipt['request_digest'] != prepared.request_digest:
        raise TransitionConflict('保存要求IDの内容が一致しません。')
    key = request['changes'][0]['target']
    same = key in state['map']['members'] and state['works'][key][prepared.field] == prepared.new_value
    return {'status': 'confirmed' if same else 'saved_but_changed', 'revision': state['revision'],
            'receipt': copy.deepcopy(receipt),
            'message': '正本へ保存し、変更結果を読み戻しました。' if same else '保存記録はありますが、その後の現在値が変わっています。再確認してください。'}


def save_edit(prepared):
    """Persist the usual request file, apply once, then check the Canonical Store."""
    if _digest(prepared.request) != prepared.request_digest:
        return {'status': 'rejected', 'message': '確認後に保存要求が変わりました。内容を確認し直してください。'}
    try:
        state = _current(prepared)
        if prepared.request['transition_id'] in state['receipts']:
            return inspect_edit_result(prepared)
        if state['revision'] != prepared.request['base_revision']:
            raise TransitionConflict('正本の版が変わりました。最新の内容を読み直して再判断してください。')
        for binding in prepared.bindings:
            if hashlib.sha256(_source_path(prepared.evidence_root, binding['ref']).read_bytes()).hexdigest() != binding['sha256']:
                raise TransitionConflict('根拠資料が確認時から変わりました。変更内容を再確認してください。')
        folder = prepared.store_path.parent / 'transition-requests'; folder.mkdir(exist_ok=True)
        path = folder / (prepared.request['transition_id'].removeprefix('transition:') + '.json')
        text = json.dumps(prepared.request, ensure_ascii=False, indent=2) + '\n'
        if path.exists():
            if path.read_text(encoding='utf-8') != text:
                raise TransitionConflict('保存済み要求の内容が変わっています。')
        else:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(text); stream.flush(); os.fsync(stream.fileno())
        prepared.request_path = path
        _apply_request(prepared.store_path, prepared.request, backend='sqlite',
                       reviewed_owner_intent=prepared.field == 'intent' and prepared.owner_reviewed,
                       evidence_repo=prepared.evidence_root)
        return inspect_edit_result(prepared)
    except SnapshotInvalid:
        # Invalid current bytes after commit do not prove the write failed.
        return {'status': 'unknown', 'message': '現在の正本を読めず、保存結果を確認できません。正本を復旧・再読して同じ要求IDを確認してください。',
                'request_path': str(prepared.request_path) if prepared.request_path else None}
    except (ContextUnknown, TransitionDenied, TransitionConflict, StoreConflict, TransitionError, ValueError) as exc:
        if isinstance(exc, ContextUnknown):
            message = '判断に必要な情報が未確認のため、保存できません。'
        elif isinstance(exc, (TransitionConflict, StoreConflict)):
            message = '現在値・許可・根拠の前提が変わっています。正本を読み直して再確認してください。'
        elif isinstance(exc, TransitionDenied):
            message = 'この担当への許可、行動ルール、またはOwner判断の根拠を確認できないため、保存できません。'
        else:
            message = '変更内容または根拠が更新契約に合わないため、保存できません。'
        return {'status': 'rejected', 'message': message, 'detail': str(exc),
                'request_path': str(prepared.request_path) if prepared.request_path else None}
    except (OSError, StoreError) as exc:
        # A lost response might follow a commit. Read, never guess or auto-retry.
        try:
            result = inspect_edit_result(prepared)
            if result['status'] in ('confirmed', 'saved_but_changed'):
                return result
            return {**result, 'message': '保存処理が中断しました。現在の正本に適用記録はありません。内容を確認し直してください。'}
        except (OSError, StoreError, TransitionError, ValueError, KeyError):
            return {'status': 'unknown', 'message': '保存結果を確認できません。正本を復旧・再読して、同じ要求IDの結果を確認してください。',
                    'request_path': str(prepared.request_path) if prepared.request_path else None}
