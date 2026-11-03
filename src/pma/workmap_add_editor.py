"""Desktop Work registration through the same normal add_work Core contract."""
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from pma.workmap_add import build_add_request
from pma.workmap_contract import validate_snapshot, inspect_work, rule_requires_disposition, ContextUnknown, SnapshotInvalid
from pma.workmap_core import _digest, TransitionConflict, TransitionDenied, TransitionError
from pma.workmap_editor import _source_path, _current
from pma.workmap_store import StoreError, StoreConflict
from tools.apply_workmap_transition import _apply_request


@dataclass
class PreparedAdd:
    store_path: Path
    evidence_root: Path
    request: dict
    bindings: tuple
    request_digest: str
    request_path: Path | None = None


def prepare_add(source, snapshot, *, parent, name, intent, origin, actor, refs):
    state = validate_snapshot(copy.deepcopy(snapshot))
    if source.project_id is not None and source.project_id != state['project_id']:
        raise ValueError('編集中のMapが違います。読み直してください。')
    if not all(isinstance(v, str) and v.strip() for v in (parent, name, intent, origin, actor)) or not refs:
        raise ValueError('親Work・名前・目的・生まれた理由・登録者・根拠資料を指定してください。')
    bindings = tuple({'ref': ref, 'sha256': hashlib.sha256(_source_path(source.evidence_root, ref).read_bytes()).hexdigest()}
                     for ref in dict.fromkeys(refs))
    request = build_add_request(state, actor=actor.strip(), parent=parent, name=name.strip(),
        intent=intent.strip(), origin=origin.strip(), refs=[b['ref'] + '#sha256=' + b['sha256'] for b in bindings])
    return PreparedAdd(Path(source.path).resolve(), Path(source.evidence_root).resolve(), request, bindings, _digest(request))


def inspect_add_result(prepared):
    """Read the same Transition ID; never create another Work to retry."""
    state = _current(prepared); request = prepared.request
    receipt = state['receipts'].get(request['transition_id'])
    if receipt is None:
        return {'status': 'not_applied', 'revision': state['revision'],
                'message': '現在の正本に、この追加要求の適用記録はありません。最新Mapを確認してから再判断してください。'}
    if receipt['request_digest'] != prepared.request_digest:
        raise TransitionConflict('追加要求IDの内容が一致しません。')
    change = request['changes'][0]; value = change['value']; work = value['work']; key = work['work_key']
    same = (key in state['map']['members'] and state['works'].get(key) == work
            and state['display_ids'].get(key) == value['display_id']
            and any(r['kind'] == 'parent' and r['child'] == key and r['parent'] == change['target']
                    for r in state['map']['relations']))
    return {'status': 'confirmed' if same else 'saved_but_changed', 'revision': state['revision'],
            'work_key': key, 'display_id': state['display_ids'].get(key), 'receipt': copy.deepcopy(receipt),
            'message': '正本へ追加し、Workと親の関係を読み戻しました。' if same else '追加記録はありますが、その後の現在値が変わっています。再確認してください。'}


def save_add(prepared):
    if _digest(prepared.request) != prepared.request_digest:
        return {'status': 'rejected', 'message': '確認後に追加要求が変わりました。内容を確認し直してください。'}
    try:
        state = _current(prepared)
        if prepared.request['transition_id'] in state['receipts']:
            return inspect_add_result(prepared)
        if state['revision'] != prepared.request['base_revision']:
            raise TransitionConflict('正本の版が変わりました。最新Mapを読み直して再判断してください。')
        context = inspect_work(state, prepared.request['changes'][0]['target'])
        chain = {w['work_key'] for w in context['parent_chain']}
        if any(chain.intersection(rule['targets']) and rule_requires_disposition(rule, ['add_work'])
               for rule in state['map']['action_rules']):
            raise TransitionDenied('applicable Action Rule still needs disposition')
        for binding in prepared.bindings:
            if hashlib.sha256(_source_path(prepared.evidence_root, binding['ref']).read_bytes()).hexdigest() != binding['sha256']:
                raise TransitionConflict('根拠資料が確認時から変わりました。')
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
        _apply_request(prepared.store_path, prepared.request, backend='sqlite', evidence_repo=prepared.evidence_root)
        return inspect_add_result(prepared)
    except SnapshotInvalid:
        return {'status': 'unknown', 'message': '正本を読めず追加結果を確認できません。同じ要求の結果を再確認してください。',
                'request_path': str(prepared.request_path) if prepared.request_path else None}
    except (ContextUnknown, TransitionDenied, TransitionConflict, StoreConflict, TransitionError, ValueError) as exc:
        if isinstance(exc, ContextUnknown):
            message = '判断に必要な情報が未確認のため、追加できません。'
        elif isinstance(exc, (TransitionConflict, StoreConflict)):
            message = 'Map・許可・根拠の前提が変わりました。最新Mapを読み直して再確認してください。'
        elif isinstance(exc, TransitionDenied):
            message = 'この登録者の追加許可、または適用される行動ルールを確認できないため、追加できません。'
        else:
            message = '入力または根拠が登録契約に合わないため、追加できません。'
        return {'status': 'rejected', 'message': message, 'detail': str(exc)}
    except (OSError, StoreError):
        try:
            return inspect_add_result(prepared)
        except (OSError, StoreError, TransitionError, ValueError, KeyError):
            return {'status': 'unknown', 'message': '追加結果は未確認です。再登録せず、正本を復旧・再読して同じ要求の結果を確認してください。',
                    'request_path': str(prepared.request_path) if prepared.request_path else None}
