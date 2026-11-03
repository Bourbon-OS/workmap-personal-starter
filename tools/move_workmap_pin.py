"""Prepare/apply a routine own-Pin move through the existing canonical Core.

Meaning review is explicit operator input, not automatic approval. Ordinary
mode uses existing Permissions. The separately reviewed explicit Owner Pin
mode packages temporary move-only grants and their termination via the same
Core. Saved bundles are requests, not another Store; resume after lost output.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pma.workmap_close import repository_evidence
from pma.workmap_contract import ContextUnknown, SnapshotInvalid, rule_requires_disposition, validate_snapshot
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest
from pma.workmap_pin_move import routine_pin_guard, pin_move_scope, move_note_id, is_held_reactivation
from pma.workmap_reapply import reapply_precondition, requests_reapplication, check_reapplication, protected_move_digest
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from tools.apply_workmap_transition import _apply_request


def _protected(snapshot, actor, source, destination, note_id, request=None):
    """Everything a routine move must leave untouched, including other actors."""
    if request is not None and requests_reapplication(request):
        return protected_move_digest(snapshot, request)
    fixed = {k: v for k, v in snapshot.items()
             if k not in ('revision', 'recorded_at', 'map', 'thread_entries', 'receipts')}
    fixed['map'] = {k: v for k, v in snapshot['map'].items() if k not in ('pins', 'notes')}
    fixed['pins'] = [p for p in snapshot['map']['pins']
                     if not (p['actor_id'] == actor and p['work_id'] in (source, destination))]
    fixed['notes'] = [n for n in snapshot['map']['notes'] if n['id'] != note_id]
    return _digest(fixed)


def prepare_move(snapshot, *, actor, destination, reason, sources, remaining=None, first_step=None,
                 unsaved=None, acceptance_basis=None, action_rule_basis=None,
                 dependency_basis=None, scope_basis=None, reusable_refs=(), where=None):
    """Build exact rows/IDs mechanically. Basis statements must be real reviews."""
    snapshot = validate_snapshot(snapshot)
    if snapshot['metadata'].get('store_role') != 'canonical_current':
        raise ValueError('selected Store is not canonical current')
    fields = (actor, destination, reason)
    if not all(isinstance(v, str) and v.strip() for v in fields) or not sources:
        raise ValueError('actor, destination, reason and actual sources are required')
    optional = (remaining, first_step, unsaved, acceptance_basis, action_rule_basis, dependency_basis, scope_basis)
    if any(v is not None and (not isinstance(v, str) or not v.strip()) for v in optional):
        raise ValueError('optional hints/bases must be useful nonempty text or omitted')
    destination = snapshot['aliases'].get(destination, destination)
    if destination not in snapshot['map']['members']:
        raise ValueError('destination is not a current Work')
    source, note_id, guard = routine_pin_guard(snapshot, actor, destination, where)
    returning = source is None
    if returning and (any(v is not None for v in (remaining, first_step, unsaved)) or reusable_refs):
        raise ValueError('held return has no source to leave; resume hints are not rewritten')
    tid = 'transition:' + str(uuid4())
    old = guard['destination_pin']
    if old is not None and where is not None and old.get('where') != where:
        raise ValueError('reactivating a held Pin cannot change its location')
    now = datetime.now(timezone.utc).isoformat()
    active = copy.deepcopy(old) if old else {
        'id': 'pin:' + str(uuid4()), 'work_id': destination, 'actor_id': actor, 'pinned_at': now}
    if old is None and where is not None:
        active['where'] = where
    refs = list(dict.fromkeys(sources))
    active.update(mode='active', confirmed_at=now, refs=refs)
    hints = {n: v for n, v in {'remaining': remaining, 'first_step': first_step, 'unsaved': unsaved}.items() if v is not None}
    if reusable_refs:
        hints['reusable_refs'] = list(reusable_refs)
    note = ({'id': note_id, 'targets': [source], 'basis': 'reported', 'refs': refs,
             'observed_at': now,
             'text': json.dumps({'actor': actor, 'return_work': source, **hints}, ensure_ascii=False)} if hints else None)
    # Use the existing Core classification, not natural-language inference.
    # This records structural facts only; operator review still remains required.
    if action_rule_basis is None:
        has_rules = False
        source_operations = ('update_pin', 'append_thread') + (('set_note',) if note is not None else ())
        destination_operations = ('update_pin' if old else 'add_pin', 'append_thread')
        for context, operations in ((guard['source_context'], source_operations),
                                    (guard['destination_context'], destination_operations)):
            if context is None:
                continue
            rules = context['ancestor_action_rules']
            has_rules = has_rules or bool(rules)
            if any(rule_requires_disposition(rule, operations) for rule in rules):
                raise ValueError('existing Action Rules need an explicit applicability review')
        action_rule_basis = (
            'Recorded endpoint/ancestor rules are review triggers that the existing Core does not '
            'treat as blockers for these Pin-recording operations. They remain unchanged; '
            'this classification certifies neither applicability nor completed observation.'
            if has_rules else 'Applicable endpoint ancestor Action Rule sets were read and are empty.')
    if dependency_basis is None:
        if any(c['incident_dependencies'] for c in (guard['source_context'], guard['destination_context']) if c is not None):
            raise ValueError('existing dependencies need an explicit applicability review')
        dependency_basis = 'Applicable endpoint incident dependency sets were read and are empty.'
    if scope_basis is None:
        from pma.workmap_contract import _collection_status
        keys = {p['work_key'] for c in (guard['source_context'], guard['destination_context']) if c is not None for p in c['parent_chain']}
        if any(_collection_status(snapshot, n, key) != 'confirmed' for key in keys for n in ('pins', 'permissions', 'action_rules')):
            raise ValueError('unknown scope needs positive evidence and an explicit review basis')
        if any(_collection_status(snapshot, 'pins', key) != 'confirmed' for key in snapshot['map']['members']):
            raise ValueError('actor-wide unknown Pin visibility needs an explicit scope review')
        scope_basis = 'Current complete endpoint/ancestor authority records and all actor Pins are bound to this revision; operator reviews grant constraints.'
    acceptance_basis = acceptance_basis or reason
    request = {'format_version': snapshot['format_version'], 'project_id': snapshot['project_id'],
        'transition_id': tid, 'actor_id': actor, 'base_revision': snapshot['revision'],
        'intent': reason, 'source_refs': refs, 'preconditions': [],
        'changes': [{'op': 'move_active_pin', 'target': source, 'expected': guard, 'value': {
            'new_target': destination, 'active_pin': active, 'resume_note': note,
            'acceptance_basis': acceptance_basis, 'action_rule_basis': action_rule_basis,
            'dependency_basis': dependency_basis}}],
        'thread_entries': [{'id': 'thread:' + tid, 'works': [source, destination],
            'kind': 'pin_active_moved', 'summary': 'Move own active Pin', 'why': reason,
            'occurred_at': None, 'recorded_at': now, 'actor': actor,
            'source_refs': refs, 'transition_id': tid}]}
    if returning:
        # Same existing Core operation and technical kind; no new stored shape.
        active['refs'] = list(dict.fromkeys([*old['refs'], *refs]))
        request['changes'] = [{'op': 'update_pin', 'target': destination, 'pin_id': old['id'],
                               'expected': old, 'value': active}]
        request['thread_entries'][0].update(works=[destination], kind='pin_changed',
                                           summary='Reactivate own existing held Pin')
    _, records, required_refs = pin_move_scope(snapshot, request)
    refs = sorted(set(refs) | required_refs | set(active.get('refs', []))
                  | set(old['refs'] if old else [])
                  | set(guard['resume_note']['refs'] if guard['resume_note'] else []))
    if not set(reusable_refs) <= set(refs):
        raise ValueError('reusable references must be included in reviewed source references')
    request['source_refs'] = refs
    request['thread_entries'][0]['source_refs'] = refs
    active['refs'] = refs
    if note is not None:
        note['refs'] = refs
    assessments = {n: {'mode': 'current_scope', 'reason': scope_basis,
                       'refs': refs, 'records': rows} for n, rows in records.items()}
    from pma.workmap_contract import _collection_status
    known = (not returning
             and all(_collection_status(snapshot, 'pins', k) == 'confirmed' for k in snapshot['map']['members'])
             and all(_collection_status(snapshot, n, k) == 'confirmed'
                     for k in pin_move_scope(snapshot, request)[0]
                     for n in ('pins', 'permissions', 'action_rules')))
    if known:
        request['preconditions'] = [reapply_precondition(snapshot)]
        assessments = None
    return {'request': request, 'assessments': assessments, 'request_digest': _digest(request),
            'protected_digest': _protected(snapshot, actor, source, destination, note_id, request),
            'source_bindings': {r: hashlib.sha256(repository_evidence(r)).hexdigest() for r in refs},
            'display': {'from': snapshot['display_ids'][source] if source else None, 'to': snapshot['display_ids'][destination]},
            'scope_counts': {n: len(rows) for n, rows in records.items()}}


def _readback(snapshot, bundle):
    q = bundle['request']; c = q['changes'][0]; v = c['value']; g = c['expected']
    if is_held_reactivation(q):
        entries = [e for e in snapshot['thread_entries'] if e.get('transition_id') == q['transition_id']]
        same = (v in snapshot['map']['pins']
                and _protected(snapshot, q['actor_id'], None, c['target'], None) == bundle['protected_digest']
                and len(entries) == 1 and entries[0]['kind'] == 'pin_changed')
        return {'status': 'confirmed' if same else 'changed_or_unavailable',
                'revision': snapshot['revision'], **bundle['display']}
    held = {**g['source_pin'], 'mode': 'held',
            'refs': list(dict.fromkeys([*g['source_pin']['refs'], *q['source_refs']]))}
    pins = snapshot['map']['pins']
    expected_note = v['resume_note'] if v['resume_note'] is not None else g['resume_note']
    hint_same = (expected_note in snapshot['map']['notes'] if expected_note is not None else
                 not any(n['id'] == move_note_id(c, q['actor_id']) for n in snapshot['map']['notes']))
    same = (held in pins and v['active_pin'] in pins and hint_same
            and _protected(snapshot, q['actor_id'], c['target'], v['new_target'], move_note_id(c, q['actor_id']), q)
            == bundle['protected_digest'])
    entries = [e for e in snapshot['thread_entries'] if e.get('transition_id') == q['transition_id']]
    same = same and len(entries) == 1 and entries[0]['kind'] == 'pin_active_moved'
    return {'status': 'confirmed' if same else 'changed_or_unavailable',
            'revision': snapshot['revision'], **bundle['display']}


def move_pin(store_path, *, backend, reviewed_pin_move=False, resume=None, inputs_packet=None, **meaning):
    started = datetime.now(timezone.utc).isoformat(); t0 = perf_counter(); timings = {}
    path = Path(store_path).resolve()
    if resume is not None:
        if meaning or inputs_packet is not None:
            raise ValueError('resume uses saved inputs; replacement meaning fields are not accepted')
        bundle_path = Path(resume).resolve()
        bundle = json.loads(bundle_path.read_text(encoding='utf-8'))
        if (bundle.get('store_binding') != {'path': str(path), 'backend': backend}
                or bundle.get('request_digest') != _digest(bundle['request'])
                or len(bundle['request']['changes']) != 1
                or not (bundle['request']['changes'][0]['op'] == 'move_active_pin'
                        or is_held_reactivation(bundle['request']))):
            raise ValueError('resume needs the unchanged saved Pin move bundle')
    else:
        if inputs_packet is not None and meaning:
            raise ValueError('input draft cannot be combined with individual meaning fields')
        t = perf_counter()
        snapshot = open_store(path, backend).read().snapshot
        timings['map_read_seconds'] = perf_counter() - t
        t = perf_counter()
        if inputs_packet is not None:
            from tools.workmap_pin_inputs import resolve_pin_inputs
            meaning = resolve_pin_inputs(inputs_packet, snapshot, path, backend)
        bundle = prepare_move(snapshot, **meaning)
        if inputs_packet is not None:
            from tools.workmap_pin_inputs import check_prepared_pin_inputs
            check_prepared_pin_inputs(inputs_packet, snapshot, bundle)
        bundle['store_binding'] = {'path': str(path), 'backend': backend}
        directory = (path if backend == 'text' else path.parent) / 'transition-requests'
        directory.mkdir(exist_ok=True)
        bundle_path = directory / (bundle['request']['transition_id'][11:] + '.pin-move.json')
        with bundle_path.open('x', encoding='utf-8') as stream:
            json.dump(bundle, stream, ensure_ascii=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        timings['preparation_seconds'] = perf_counter() - t
    q = bundle['request']
    report = {'request_path': str(bundle_path), 'transition_id': q['transition_id'],
              'started_at': started, 'scope_counts': bundle['scope_counts'],
              **bundle['display'], 'timings': timings}
    if not reviewed_pin_move:
        report['result'] = {'result': 'prepared', 'reason': 'Operator review is required before applying'}
    else:
        t = perf_counter()
        readback_bundle = bundle
        try:
            store = open_store(path, backend)
            current = store.read().snapshot
            # A committed replay recovers its receipt even if later evidence changes.
            if q['transition_id'] not in current['receipts']:
                if current['revision'] != q['base_revision'] and not requests_reapplication(q):
                    raise TransitionConflict('prepared revision changed; review current Map again')
                if any(hashlib.sha256(repository_evidence(r)).hexdigest() != h
                       for r, h in bundle['source_bindings'].items()):
                    raise TransitionConflict('prepared source evidence changed; review again')
                if requests_reapplication(q):
                    check_reapplication(store, current, q)
            report['result'] = _apply_request(path, q, backend=backend,
                reviewed_pin_move=True, context_assessments=bundle['assessments'])
        except (OSError, StoreError, SnapshotInvalid) as exc:
            report['result'] = {'result': 'unknown', 'reason': str(exc),
                                'recovery': 'Resume the same saved bundle; do not rebuild the request'}
        except (TransitionError, ContextUnknown, ValueError) as exc:
            category = ('conflict' if isinstance(exc, TransitionConflict) else
                        'context_unknown' if isinstance(exc, ContextUnknown) else 'denied')
            report['result'] = {'result': 'rejected', 'category': category, 'reason': str(exc)}
        timings['apply_seconds'] = perf_counter() - t
        if report['result'].get('result') == 'applied':
            t = perf_counter()
            try:
                report['readback'] = _readback(validate_snapshot(open_store(path, backend).read().snapshot), readback_bundle)
            except (OSError, StoreError, SnapshotInvalid) as exc:
                report['readback'] = {'status': 'unknown', 'reason': str(exc)}
            timings['readback_seconds'] = perf_counter() - t
    timings['entrypoint_seconds'] = perf_counter() - t0
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store', required=True, type=Path)
    p.add_argument('--backend', choices=('text', 'sqlite'), required=True)
    for name in ('actor', 'destination', 'reason', 'remaining', 'first-step', 'unsaved',
                 'acceptance-basis', 'action-rule-basis', 'dependency-basis', 'scope-basis', 'where'):
        p.add_argument('--' + name)
    p.add_argument('--source', action='append', default=[])
    p.add_argument('--reusable-ref', action='append', default=[])
    p.add_argument('--reviewed-pin-move', action='store_true',
                   help='Operator reviewed own acceptance, grant constraints, Action Rules, dependencies and current scope; not a grant')
    p.add_argument('--resume', type=Path)
    p.add_argument('--inputs-json', help='Filled input draft from the reader; never a review or grant')
    owner = p.add_mutually_exclusive_group()
    owner.add_argument('--owner-instruction', help='Literal direct Owner Pin-change instruction, not quoted approval')
    owner.add_argument('--owner-source', help='Existing reviewed Owner instruction repository reference')
    p.add_argument('--reviewed-owner-pin', action='store_true',
                   help='Operator checked explicit Owner Pin intent/actor/destination; not authentication or project-work authorization')
    a = p.parse_args()
    if not a.store.is_absolute(): p.error('Store path must be absolute')
    fields = ('actor', 'destination', 'reason', 'remaining', 'first_step', 'unsaved',
              'acceptance_basis', 'action_rule_basis', 'dependency_basis', 'scope_basis', 'where')
    supplied = {n: getattr(a, n) for n in fields if getattr(a, n) is not None}
    if a.source: supplied['sources'] = a.source
    if a.reusable_ref: supplied['reusable_refs'] = a.reusable_ref
    if a.inputs_json is not None and (a.resume is not None or supplied):
        p.error('--inputs-json cannot be combined with --resume or individual meaning fields')
    try:
        packet = json.loads(a.inputs_json) if a.inputs_json is not None else None
        if a.inputs_json is not None and not isinstance(packet, dict):
            raise ValueError('--inputs-json requires an input-draft object')
        if a.resume is None and a.inputs_json is None: supplied.setdefault('sources', [])
        workflow = a.owner_instruction is not None or a.owner_source is not None or a.reviewed_owner_pin
        if workflow:
            from tools.workmap_pin_workflow import owner_pin_workflow
            report = owner_pin_workflow(a.store, backend=a.backend,
                reviewed_pin_move=a.reviewed_pin_move, reviewed_owner_pin=a.reviewed_owner_pin,
                owner_instruction=a.owner_instruction, owner_source=a.owner_source,
                resume=a.resume, inputs_packet=packet, **supplied)
        else:
            report = move_pin(a.store, backend=a.backend, reviewed_pin_move=a.reviewed_pin_move,
                              resume=a.resume, inputs_packet=packet, **supplied)
    except (OSError, ValueError, KeyError, TypeError, StoreError, SnapshotInvalid, TransitionError) as exc:
        p.exit(2, str(exc) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['result'].get('result') == 'applied' and report.get('readback', {}).get('status') == 'confirmed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
