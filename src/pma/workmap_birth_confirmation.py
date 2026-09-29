"""Repair a proven native birth's omitted collection confirmation, not close rules.

The explicit local review binds Owner authority to one metadata-only repair.
No Permission, Pin, Condition, Work, or general legacy default is changed.
"""
import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pma.workmap_contract import validate_snapshot, confirm_new_work_collections
from pma.workmap_core import TransitionDenied, _digest
from pma.workmap_owner_permission import _read, verify_permission_sources


@dataclass(frozen=True)
class ReviewedBirthConfirmation:
    request_digest: str
    repo: Path
    bindings: tuple


def review_birth_confirmation(request, repo):
    """Caller reviews the exact Owner direction; file hashes are not authority."""
    try:
        change, = request['changes']
        entry, = request['thread_entries']
        detail = json.loads(entry['consequence'])
        if (set(change) != {'op', 'target'} or change['op'] != 'append_thread'
                or entry['kind'] != 'evidence_recorded'
                or entry['works'] != [change['target']]
                or set(detail) != {'birth_request_ref', 'retained_snapshot_tokens', 'owner_ref'}
                or not detail['owner_ref'].startswith('repo:docs/work-management/evidence/owner_')
                or not detail['owner_ref'].endswith('.txt')
                or not {detail['birth_request_ref'], detail['owner_ref']} <= set(request['source_refs'])
                or not isinstance(detail['retained_snapshot_tokens'], list)
                or not detail['retained_snapshot_tokens']
                or len(set(detail['retained_snapshot_tokens'])) != len(detail['retained_snapshot_tokens'])):
            raise ValueError('one sourced native-birth repair required')
        repo = Path(repo).resolve()
        bindings = tuple((r, hashlib.sha256(_read(repo, r)).hexdigest()) for r in request['source_refs'])
        return ReviewedBirthConfirmation(_digest(request), repo, bindings)
    except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
        raise TransitionDenied('native birth confirmation needs exact local Owner review') from exc


def apply_birth_confirmation(snapshot, request, review, store, thread_validator):
    if not isinstance(review, ReviewedBirthConfirmation) or review.request_digest != _digest(request):
        raise TransitionDenied('birth confirmation review differs from request')
    verify_permission_sources(review)
    entry = request['thread_entries'][0]
    target = request['changes'][0]['target']
    try:
        detail = json.loads(entry['consequence'])
        birth = json.loads(_read(review.repo, detail['birth_request_ref']))
        change, = birth['changes']
        receipt = snapshot['receipts'][birth['transition_id']]
        if (change['op'] != 'add_work' or birth['project_id'] != snapshot['project_id']
                or receipt['request_digest'] != _digest(birth) or receipt['result'] != 'applied'
                or target not in snapshot['map']['members'] or target in snapshot['retirements']
                or change['value']['work'] != snapshot['works'][target]
                or change['value']['work']['work_key'] != target):
            raise ValueError('not a proven current native birth')
        retained = {token: validate_snapshot(store.read_retained_snapshot(token))
                    for token in store.retained_snapshot_tokens()}
        relevant = {token: state for token, state in retained.items()
                    if birth['transition_id'] in state['receipts']}
        if set(detail['retained_snapshot_tokens']) != set(relevant):
            raise ValueError('request omits or adds retained birth-to-current versions')
        versions = list(relevant.values())
        born, = [s for s in versions if s['revision'] == receipt['revision']]
        if not any(_digest(s) == _digest(snapshot) for s in versions):
            raise ValueError('history does not reach this exact current revision')
        fields = lambda s: {n: [x for x in s['map'][n]
                               if x.get('work_id') == target or target in x.get('targets', [])]
                            for n in ('pins', 'permissions', 'conditions')}
        parent = lambda s: [r for r in s['map']['relations'] if r['kind'] == 'parent' and r['child'] == target]
        if (any(fields(born).values()) or len(parent(born)) != 1
                or parent(born)[0]['parent'] != change['target']
                or born['display_ids'][target] != change['value']['display_id']):
            raise ValueError('birth collections or identity are not empty/known')
        groups = {}
        for state in versions:
            if (state['project_id'] != snapshot['project_id']
                    or target not in state['map']['members'] or target in state['retirements']
                    or state['works'][target] != snapshot['works'][target]
                    or parent(state) != parent(born)
                    or state['display_ids'][target] != born['display_ids'][target]
                    or any(snapshot['receipts'].get(tid) != r for tid, r in state['receipts'].items())
                    or state['receipts'].get(birth['transition_id']) != receipt):
                raise ValueError('history identity, branch or receipts differ')
            groups.setdefault(len(state['receipts']), []).append(state)
        if sorted(groups) != list(range(len(born['receipts']), len(snapshot['receipts']) + 1)):
            raise ValueError('a result revision is missing')
        previous = None
        for count in sorted(groups):
            states = groups[count]
            state = states[0]
            if any(s['receipts'] != state['receipts'] or fields(s) != fields(state)
                   or s['thread_entries'] != state['thread_entries'] for s in states):
                raise ValueError('same-receipt versions disagree')
            if previous is not None:
                added = set(state['receipts']) - set(previous['receipts'])
                if (len(added) != 1 or not set(previous['receipts']) < set(state['receipts'])
                        or state['thread_entries'][:len(previous['thread_entries'])] != previous['thread_entries']):
                    raise ValueError('history is not continuous')
                result = state['receipts'][next(iter(added))]
                if not any(s['revision'] == result['revision'] for s in states):
                    raise ValueError('the committed result version is missing')
            previous = state
        if fields(previous) != fields(snapshot):
            raise ValueError('history does not explain current collections')
    except (KeyError, ValueError, TypeError, OSError, AttributeError) as exc:
        raise TransitionDenied('native birth history cannot fully reconstruct current state') from exc
    thread_validator(snapshot, request, entry, 'evidence_recorded', [target])
    old = copy.deepcopy(snapshot['metadata'].get('completeness'))
    confirm_new_work_collections(snapshot, target)
    record = copy.deepcopy(entry)
    record['consequence'] = json.dumps({**detail, 'old_completeness': old,
        'new_completeness': snapshot['metadata']['completeness']}, ensure_ascii=False, sort_keys=True)
    snapshot['thread_entries'].append(record)
