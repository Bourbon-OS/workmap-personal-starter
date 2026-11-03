"""Sourced current collection confirmation using existing technical Evidence.

The local operator checks facts and authority. Hashes bind that review, rather
than proving truth. This changes current completeness only, not historical data.
"""
import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pma.workmap_contract import (ContextUnknown, _collection_status,
                                  confirm_work_collections, inspect_work)
from pma.workmap_core import TransitionConflict, TransitionDenied, _digest, _nonempty, _timestamp
from pma.workmap_notes import note_work_scope
from pma.workmap_owner_permission import _read, verify_permission_sources

ACTION = 'confirm_current_collections'
NAMES = ('pins', 'permissions', 'action_rules')


@dataclass(frozen=True)
class ReviewedCurrentCollections:
    request_digest: str
    snapshot_digest: str
    repo: Path
    bindings: tuple


def confirmation_detail(request):
    try:
        detail = json.loads(request['thread_entries'][0].get('consequence', '{}'))
        return detail if isinstance(detail, dict) and detail.get('action') == ACTION else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def current_scope(snapshot, work, target, actor):
    # Reuse the existing recording-work/edited-target separation and exact rows.
    scope = note_work_scope(snapshot, work, target, actor)
    scope['collection_statuses'] = {
        key: {name: _collection_status(snapshot, name, key) for name in NAMES}
        for key in scope['purposes']}
    scope['target_records'] = {name: copy.deepcopy([
        item for item in snapshot['map'][name]
        if (item['work_id'] == target if name == 'pins' else target in item['targets'])])
        for name in NAMES}
    scope['completeness'] = copy.deepcopy(snapshot['metadata'].get('completeness'))
    return scope


def make_confirmation_request(snapshot, *, actor, target, work, bases, coverage,
                              authority, scope_reason, sources, transition_id, recorded_at):
    """Ordinary entry assembles internal guards; human and AI supply meanings."""
    detail = {'action': ACTION, 'working_work': work, 'scope_reason': scope_reason,
              'collections': bases, 'coverage': coverage, 'authority': authority,
              'expected': current_scope(snapshot, work, target, actor)}
    return {'format_version': snapshot['format_version'], 'project_id': snapshot['project_id'],
            'transition_id': transition_id, 'actor_id': actor, 'base_revision': snapshot['revision'],
            'intent': 'Record the verified current collections', 'source_refs': list(sources),
            'preconditions': [], 'changes': [{'op': 'append_thread', 'target': target}],
            'thread_entries': [{'id': 'evidence:' + transition_id, 'works': [target],
                'kind': 'evidence_recorded', 'summary': '現在集合の確認結果',
                'why': scope_reason, 'occurred_at': None, 'recorded_at': recorded_at,
                'actor': actor, 'source_refs': list(sources), 'transition_id': transition_id,
                'consequence': json.dumps(detail, ensure_ascii=False, sort_keys=True)}]}


def review_current_collections(snapshot, request, repo):
    """Explicit operator review of instruction coverage, facts and edit authority.

    No auto-confirmation from empty records or Owner unawareness. `coverage`
    describes the applicable period, all current instructions and temporary
    grants/action_rules. Unchecked parts must be left out of `collections`.
    """
    try:
        change, = request['changes']
        entry, = request['thread_entries']
        detail = confirmation_detail(request)
        if (change != {'op': 'append_thread', 'target': change['target']}
                or entry['kind'] != 'evidence_recorded' or entry['works'] != [change['target']]
                or request['preconditions'] != [] or not isinstance(detail, dict)
                or set(detail) != {'action', 'working_work', 'scope_reason', 'collections',
                                   'coverage', 'authority', 'expected'}):
            raise ValueError('one exact technical confirmation is required')
        bases = detail['collections']
        if (not isinstance(bases, dict) or not bases or not set(bases) <= set(NAMES)
                or any(not _nonempty(reason) for reason in bases.values())
                or not _nonempty(detail['scope_reason'])):
            raise ValueError('selected facts, recording scope and edit authority need reasons')
        coverage = detail['coverage']
        if (not isinstance(coverage, dict) or set(coverage) != {'as_of', 'period', 'instructions', 'temporary_authority'}
                or not _timestamp(coverage['as_of'])
                or any(not _nonempty(coverage[n]) for n in ('period', 'instructions', 'temporary_authority'))):
            raise ValueError('current instruction and temporary authority coverage is missing')
        if request['base_revision'] != snapshot['revision']:
            raise TransitionConflict('confirmation revision changed; reread and reassess')
        expected = current_scope(snapshot, detail['working_work'], change['target'], request['actor_id'])
        if detail['expected'] != expected:
            raise TransitionConflict('confirmation facts, scope or authority changed')
        authority = detail['authority']
        if (not isinstance(authority, dict) or set(authority) != {'status', 'reason', 'action_rules'}
                or authority['status'] != 'confirmed' or not _nonempty(authority['reason'])
                or not isinstance(authority['action_rules'], dict)
                or set(authority['action_rules']) != {c['id'] for c in expected['action_rules']}):
            raise TransitionDenied('current edit authority or applicable Action Rules remain unresolved')
        for assessment in authority['action_rules'].values():
            if (not isinstance(assessment, dict) or set(assessment) != {'status', 'reason'}
                    or assessment['status'] != 'compatible' or not _nonempty(assessment['reason'])):
                raise TransitionDenied('an applicable Action Rule blocks or has not been assessed for this edit')
        work_context = inspect_work(snapshot, detail['working_work'])
        if work_context['pin_status'] != 'confirmed' or any(not _nonempty(p) for p in expected['purposes'].values()):
            raise ContextUnknown('accepted recording Work or its purpose is unconfirmed')
        refs = request['source_refs']
        if not isinstance(refs, list) or not refs or len(set(refs)) != len(refs):
            raise ValueError('readable fact and authority sources are required')
        # Bind all applicable recorded restrictions as well as the accepted Pin.
        relevant = set(expected['pin']['refs'])
        for item in expected['permissions']:
            if item.get('grantee') == request['actor_id']:
                relevant.update(item['source_refs'])
        for item in expected['action_rules']:
            relevant.update(item['refs'])
        repo = Path(repo).resolve()
        bindings = tuple((ref, hashlib.sha256(_read(repo, ref)).hexdigest())
                         for ref in dict.fromkeys([*refs, *sorted(relevant)]))
        return ReviewedCurrentCollections(_digest(request), _digest(snapshot), repo, bindings)
    except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
        raise TransitionDenied('current collections need sourced local review of facts and authority') from exc


def apply_current_collections(snapshot, request, review, permission_validator, thread_validator):
    if (not isinstance(review, ReviewedCurrentCollections)
            or review.request_digest != _digest(request) or review.snapshot_digest != _digest(snapshot)):
        raise TransitionDenied('current confirmation review differs from this request or revision')
    verify_permission_sources(review)
    # Core checks the narrow contract too: constructing a review object alone
    # cannot waive unresolved authority, coverage or changed expected records.
    checked = review_current_collections(snapshot, request, review.repo)
    if checked.bindings != review.bindings:
        raise TransitionDenied('current confirmation evidence bindings differ')
    detail = confirmation_detail(request)
    target = request['changes'][0]['target']
    scope = detail['expected']
    if any(p['grantee'] == request['actor_id'] and p['effect'] != 'allow'
           and {'update_work', 'append_thread'}.intersection(p['operations'])
           for p in scope['permissions']):
        raise TransitionDenied('a recorded applicable denial or unknown grant blocks this edit')
    # Neither recording-work engagement nor its permission grants target edits.
    for operation in ('update_work', 'append_thread'):
        permission_validator(snapshot, target, operation, request['actor_id'])
    thread_validator(snapshot, request, request['thread_entries'][0], 'evidence_recorded', [target])
    old = copy.deepcopy(snapshot['metadata'].get('completeness'))
    confirm_work_collections(snapshot, target, detail['collections'])
    entry = copy.deepcopy(request['thread_entries'][0])
    entry['consequence'] = json.dumps({**detail, 'source_revision': request['base_revision'],
        'evidence': [{'ref': ref, 'sha256': digest} for ref, digest in review.bindings], 'old_completeness': old,
        'new_completeness': snapshot['metadata']['completeness']}, ensure_ascii=False, sort_keys=True)
    snapshot['thread_entries'].append(entry)
