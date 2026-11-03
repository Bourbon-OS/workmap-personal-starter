"""One authorized format adoption through the existing Canonical Core/Store.

This is not a raw editor or a routine alias for legacy writes. Retained snapshot
bytes, request digests and historical Thread records remain untouched.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import uuid

from pma.workmap_contract import validate_snapshot, inspect_work, require_decision_context
from pma.workmap_core import (TransitionConflict, TransitionDenied, TransitionError,
                             _digest, _matching_receipt)
from pma.workmap_legacy import CURRENT_FORMAT, LEGACY_FORMAT, current_shape
from pma.workmap_store import StoreConflict


def apply_adoption(core, request):
    from pma.workmap_canonical_core import _permission, _thread
    from pma.workmap_close import repository_evidence
    import hashlib

    fields = {'format_version', 'project_id', 'transition_id', 'actor_id',
              'base_revision', 'intent', 'source_refs', 'preconditions',
              'changes', 'thread_entries'}
    if not isinstance(request, dict) or set(request) != fields or request['format_version'] != CURRENT_FORMAT:
        raise TransitionError('Action Rule adoption requires an exact current-format request')
    if (not isinstance(request['transition_id'], str) or not request['transition_id'].strip()
            or not isinstance(request['changes'], list) or len(request['changes']) != 1
            or not isinstance(request['thread_entries'], list) or len(request['thread_entries']) != 1
            or request['preconditions'] != [] or not request['intent']):
        raise TransitionError('one exact Action Rule adoption and its rationale are required')
    change = request['changes'][0]
    if (not isinstance(change, dict) or set(change) != {'op', 'target', 'expected', 'value'}
            or change['op'] != 'adopt_action_rules'):
        raise TransitionError('not an Action Rule adoption')
    committed = core.store.read()
    raw = committed.snapshot
    request_hash = _digest(request)
    tid = request['transition_id']
    if tid in raw.get('receipts', {}):
        validate_snapshot(raw)
        if not _matching_receipt(raw['receipts'][tid], tid, request_hash):
            raise TransitionConflict('Transition ID was used for different content')
        return copy.deepcopy(raw['receipts'][tid])
    if (raw.get('format_version') != LEGACY_FORMAT
            or request['project_id'] != raw.get('project_id')
            or request['base_revision'] != raw.get('revision')
            or change['expected'] != {'store_token': committed.token, 'source_format': LEGACY_FORMAT}):
        raise TransitionConflict('adoption source changed or is not the reviewed former format')
    snapshot = validate_snapshot(current_shape(raw))
    if snapshot['metadata'].get('store_role') != 'canonical_current':
        raise TransitionDenied('only the declared canonical source may be adopted')
    target, actor = change['target'], request['actor_id']
    context = inspect_work(snapshot, target)
    require_decision_context(context)
    if not any(p['actor_id'] == actor and p['mode'] == 'active' for p in context['pins']):
        raise TransitionDenied('adoption requires the accepting actor active on this Work')
    _permission(snapshot, target, 'adopt_action_rules', actor)
    # Do not treat an unseen ancestor set or an applicable rule as harmless.
    from pma.workmap_contract import _collection_status, rule_requires_disposition
    chain = {p['work_key'] for p in context['parent_chain']}
    applicable = [p for p in snapshot['map']['permissions']
                  if chain.intersection(p['targets']) and p.get('grantee') == actor
                  and 'adopt_action_rules' in p['operations']]
    if any(p['effect'] != 'allow' for p in applicable):
        raise TransitionDenied('applicable denial or unknown Permission blocks adoption')
    if any(not set(p['source_refs']).issubset(request['source_refs']) for p in applicable):
        raise TransitionDenied('adoption must bind the applicable Permission sources')
    if any(_collection_status(snapshot, 'action_rules', key) != 'confirmed' for key in chain):
        raise TransitionDenied('applicable ancestor Action Rule completeness is unknown')
    if any(chain.intersection(r['targets']) and rule_requires_disposition(r, ['adopt_action_rules'])
           for r in snapshot['map']['action_rules']):
        raise TransitionDenied('applicable Action Rule needs disposition before adoption')
    value = change['value']
    if not isinstance(value, dict) or set(value) != {'rule_reviews', 'evidence'}:
        raise TransitionError('adoption needs exact rule reviews and versioned evidence')
    rules = snapshot['map']['action_rules']
    reviews, evidence = value['rule_reviews'], value['evidence']
    if (not isinstance(reviews, list) or len(reviews) != len(rules)
            or any(not isinstance(v, dict) or set(v) != {'id', 'basis', 'refs'}
                   or not isinstance(v['basis'], str) or not v['basis'].strip()
                   or not isinstance(v['refs'], list) or not v['refs']
                   or not set(v['refs']).issubset(request['source_refs']) for v in reviews)
            or len({r['id'] for r in reviews}) != len(reviews)
            or {r['id'] for r in reviews} != {r['id'] for r in rules}):
        raise TransitionDenied('every source rule needs a sourced applicability review; no inferred rules')
    by_id = {r['id']: r for r in reviews}
    if any(not set(r['refs']).issubset(by_id[r['id']]['refs']) for r in rules):
        raise TransitionDenied('each rule review must bind the original rule sources')
    if (not isinstance(evidence, list) or not evidence
            or any(not isinstance(v, dict) or set(v) != {'ref', 'sha256'}
                   or v['ref'] not in request['source_refs'] for v in evidence)
            or {v['ref'] for v in evidence} != set(request['source_refs'])):
        raise TransitionDenied('every adoption source requires an exact evidence version')

    def verify_sources():
        for item in evidence:
            if hashlib.sha256(repository_evidence(item['ref'])).hexdigest() != item['sha256']:
                raise TransitionConflict('adoption source evidence changed')

    verify_sources()
    from pma.workmap_thread_view import validate_publication, SelectionUnavailable
    try:
        validate_publication(snapshot, request, Path(__file__).resolve().parents[2])
    except SelectionUnavailable as exc:
        raise TransitionError(str(exc)) from exc
    entry = request['thread_entries'][0]
    linked = entry.get('works') if isinstance(entry, dict) else None
    if (not isinstance(linked, list) or not linked or linked[0] != target
            or len(set(linked)) != len(linked)
            or any(key not in snapshot['map']['members'] for key in linked)):
        raise TransitionError('adoption rationale needs its Work and current related Works')
    _thread(snapshot, request, entry, 'rationale_added', linked)
    snapshot['thread_entries'].append(copy.deepcopy(entry))
    snapshot['revision'] = uuid.uuid4().hex
    snapshot['recorded_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    snapshot['thread_entries'][-1]['recorded_at'] = snapshot['recorded_at']
    receipt = {'transition_id': tid, 'request_digest': request_hash,
               'revision': snapshot['revision'], 'recorded_at': snapshot['recorded_at'], 'result': 'applied'}
    snapshot['receipts'][tid] = receipt
    validate_snapshot(snapshot)
    # Conversion changes only the declared collection/control identifiers.
    expected = current_shape(raw)
    for key in snapshot:
        if key not in {'revision', 'recorded_at', 'thread_entries', 'receipts'} and snapshot[key] != expected[key]:
            raise TransitionError('adoption changed content beyond the reviewed format conversion')
    verify_sources()
    try:
        core.store.commit_validated(committed.token, snapshot)
    except StoreConflict as exc:
        latest = core.store.read().snapshot.get('receipts', {}).get(tid)
        if _matching_receipt(latest, tid, request_hash):
            return copy.deepcopy(latest)
        raise TransitionConflict('canonical source changed before adoption') from exc
    return receipt
