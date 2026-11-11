"""Controlled single-host cutover primitive; not an authorization entry point.

The caller must authorize the exact stores, quiesce cooperating writers, review
the plan, and change the reader/writer locator only after verified completion.
No automatic fallback, unavailable-source recovery, or distributed guarantee.
"""
from copy import deepcopy
from datetime import datetime, timezone
import os
from uuid import uuid4, uuid5, NAMESPACE_URL

from pma.workmap_contract import validate_snapshot
from pma.workmap_core import _digest
from pma.workmap_store import LocalTextStore, StoreConflict
from pma.workmap_sqlite_store import SQLiteStore


def _location(store) -> dict:
    if isinstance(store, LocalTextStore):
        backend, path = 'text', store.directory
    elif isinstance(store, SQLiteStore):
        backend, path = 'sqlite', store.path
    else:
        raise ValueError('cutover requires an explicit local Store implementation')
    return {'backend': backend, 'path': os.path.normcase(str(path.resolve()))}


def prepare_cutover(source, target, *, reason_refs: list[str]) -> dict:
    """Read only: fix the exact old root and a byte-equivalent recovery candidate."""
    if not isinstance(reason_refs, list) or not reason_refs or any(not isinstance(r, str) or not r.strip() for r in reason_refs):
        raise ValueError('reviewable cutover reasons required')
    source_location, target_location = _location(source), _location(target)
    if source_location['path'] == target_location['path']:
        raise ValueError('source and target must be separate')
    old, candidate = source.read(), target.read()
    original = validate_snapshot(old.snapshot)
    restored = validate_snapshot(candidate.snapshot)
    if original['metadata'].get('store_role') != 'canonical_current':
        raise ValueError('source must be current')
    if restored['metadata'].get('store_role') != 'recovery_candidate':
        raise ValueError('target must be an isolated recovery candidate')
    comparable = deepcopy(restored)
    comparable['metadata']['store_role'] = 'canonical_current'
    comparable['metadata'].pop('recovery_source_sha256', None)
    baseline = deepcopy(original)
    baseline['metadata'].pop('recovery_source_sha256', None)
    if comparable != baseline:
        raise StoreConflict('candidate is not the exact source revision and content')
    now = datetime.now(timezone.utc).isoformat()
    cutover_id = uuid4().hex
    marker = {'id': cutover_id, 'source_token': old.token, 'target_token': candidate.token,
              'source_location': source_location, 'target_location': target_location,
              'source_revision': original['revision'], 'reason_refs': list(reason_refs)}
    frozen = deepcopy(original)
    frozen.update(revision=uuid4().hex, recorded_at=now)
    frozen['metadata'].update(store_role='cutover_source', store_cutover=marker)
    active = deepcopy(restored)
    active.update(revision=uuid4().hex, recorded_at=now)
    active['metadata'].update(store_role='canonical_current', store_cutover=marker)
    return {'source_token': old.token, 'target_token': candidate.token,
            'source_snapshot': original, 'candidate_snapshot': restored,
            'frozen_snapshot': frozen, 'active_snapshot': active}


def _validated_plan(source, target, plan, reviewed_digest):
    if _digest(plan) != reviewed_digest:
        raise ValueError('plan differs from reviewed content')
    frozen, active = plan['frozen_snapshot'], plan['active_snapshot']
    original, restored = plan['source_snapshot'], plan['candidate_snapshot']
    for snapshot in (original, restored, frozen, active):
        validate_snapshot(snapshot)
    marker = frozen['metadata'].get('store_cutover')
    if (not isinstance(marker, dict) or not marker.get('reason_refs')
            or marker.get('source_location') != _location(source)
            or marker.get('target_location') != _location(target)
            or marker.get('source_token') != plan['source_token']
            or marker.get('target_token') != plan['target_token']
            or marker.get('source_revision') != original['revision']
            or original['metadata'].get('store_role') != 'canonical_current'
            or restored['metadata'].get('store_role') != 'recovery_candidate'
            or frozen['revision'] == original['revision']
            or active['revision'] in (original['revision'], frozen['revision'])):
        raise ValueError('invalid cutover plan')
    comparable, baseline = deepcopy(restored), deepcopy(original)
    comparable['metadata']['store_role'] = 'canonical_current'
    for snapshot in (comparable, baseline):
        snapshot['metadata'].pop('recovery_source_sha256', None)
    if comparable != baseline:
        raise ValueError('plan candidate differs from original')
    for before, after, role in ((original, frozen, 'cutover_source'), (restored, active, 'canonical_current')):
        expected = deepcopy(before)
        expected.update(revision=after['revision'], recorded_at=after['recorded_at'])
        expected['metadata'].update(store_role=role, store_cutover=marker)
        if expected != after:
            raise ValueError('cutover may only change revision, time and physical provenance')
    return frozen, active


def execute_cutover(source, target, plan: dict, *, reviewed_digest: str,
                    fault_at: str | None = None) -> dict:
    """CAS old to noncurrent first, then CAS candidate to a fresh revision.

    A digest fixes reviewed input; it is not Owner identity or permission.
    Re-execute this same plan after an uncertain result. Any other root stops
    for reread and new judgment; never force the old bytes over a newer root.
    """
    if fault_at not in (None, 'after_source', 'after_target'):
        raise ValueError('unsupported fault hook')
    frozen, active = _validated_plan(source, target, plan, reviewed_digest)
    old, candidate = source.read(), target.read()
    # Exact plans come from prepare_cutover; reject changed states on retry.
    source_ready = old.token == plan['source_token'] and old.snapshot == plan['source_snapshot']
    source_frozen = old.snapshot == frozen
    target_ready = candidate.token == plan['target_token'] and candidate.snapshot == plan['candidate_snapshot']
    target_active = candidate.snapshot == active
    if source_frozen and target_active:
        return {'result': 'verified', 'revision': active['revision']}
    if not (source_ready or source_frozen) or not target_ready:
        raise StoreConflict('cutover state changed; reread and reassess')
    if source_ready:
        source.commit_validated(old.token, frozen)
    if source.read().snapshot != frozen:
        raise StoreConflict('old source is not confirmed stopped')
    if fault_at == 'after_source':
        raise OSError('simulated lost response after source stopped')
    target.commit_validated(candidate.token, active)
    if fault_at == 'after_target':
        raise OSError('simulated lost response after target activated')
    if source.read().snapshot != frozen or target.read().snapshot != active:
        raise StoreConflict('cutover readback changed; reread and reassess')
    return {'result': 'verified', 'revision': active['revision']}


def _cancellation_snapshots(plan, reviewed_digest):
    """The same deterministic roots for cancellation and its catalog audit."""
    frozen, active = plan['frozen_snapshot'], plan['active_snapshot']
    marker = frozen['metadata']['store_cutover']
    rejected = deepcopy(active)
    restored = deepcopy(plan['source_snapshot'])
    for state, role, suffix in ((rejected,'recovery_candidate','target'),(restored,'canonical_current','source')):
        state.update(revision=uuid5(NAMESPACE_URL, reviewed_digest+'/rollback/'+suffix).hex,
                     recorded_at=active['recorded_at'])
        state['metadata'].update(store_role=role,store_cutover={**marker,'outcome':'cancelled'})
        validate_snapshot(state)
    return rejected, restored


def cancel_cutover(source, target, plan: dict, *, reviewed_digest: str,
                   fault_at: str | None = None) -> dict:
    """Return only while neither side has progressed beyond this plan.

    Stop the provisional target before reactivating source at a fresh revision.
    Unknown/unrelated newer writes require rereading, never restoring stale data.
    """
    if fault_at not in (None, 'after_target_stopped'):
        raise ValueError('invalid rollback hook')
    frozen, active = _validated_plan(source, target, plan, reviewed_digest)
    marker = frozen['metadata']['store_cutover']
    if marker['source_location'] != _location(source) or marker['target_location'] != _location(target):
        raise ValueError('rollback Store locations differ from the plan')
    old, new = source.read(), target.read()
    rejected, restored = _cancellation_snapshots(plan, reviewed_digest)
    if old.snapshot == restored and new.snapshot in (plan['candidate_snapshot'],rejected):
        return {'result':'reverted','revision':restored['revision']}
    if old.snapshot == plan['source_snapshot'] and new.snapshot == plan['candidate_snapshot']:
        return {'result':'not_started','revision':old.snapshot['revision']}
    if old.snapshot != frozen or new.snapshot not in (plan['candidate_snapshot'],active,rejected):
        raise StoreConflict('rollback would lose a newer or unknown state; reread and reassess')
    if new.snapshot == active:
        target.commit_validated(new.token,rejected)
    if target.read().snapshot not in (plan['candidate_snapshot'],rejected):
        raise StoreConflict('target is not confirmed stopped')
    if fault_at == 'after_target_stopped':
        raise OSError('simulated response loss during rollback')
    source.commit_validated(old.token,restored)
    if source.read().snapshot != restored:
        raise StoreConflict('rollback readback differs')
    return {'result':'reverted','revision':restored['revision']}
