"""Bounded Owner-instructed Pin workflow, behind the existing routine entry.

This packages local operator review; it does not authenticate an Owner or
authorize project work. Each mutation still uses the existing canonical Core.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from time import sleep
from uuid import uuid4

from pma.workmap_contract import ContextUnknown, SnapshotInvalid, inspect_work
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest
from pma.workmap_reapply import requests_reapplication
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from tools.apply_workmap_transition import _apply_request
from tools.move_workmap_pin import prepare_move, repository_evidence, _readback

REPO = Path(__file__).resolve().parents[1]
KIND = 'owner-pin-workflow-v1'


def capture_instruction(instruction, actor, destination, *, repo=REPO):
    """Keep literal input apart from the operator's mechanically limited scope."""
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError('literal Owner Pin instruction is required')
    text = ('Literal Owner instruction, attributed by the local operator:\n'
            + instruction + '\n\nOperator-limited scope (not another Owner quote):\n'
            + json.dumps({'actor': actor, 'destination': destination,
                'scope': 'own Pin move, intrinsic resume note, technical history, temporary move authority and its termination only; no project-work authority'},
                ensure_ascii=False, sort_keys=True) + '\n')
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    relative = 'docs/work-management/evidence/owner_pin_instruction_' + digest + '.txt'
    path = Path(repo) / relative
    # Repeated identical source input reuses the same immutable evidence, not a grant.
    try:
        with path.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != text.encode('utf-8'):
            raise TransitionConflict('captured instruction was changed; do not overwrite it')
    return 'repo:' + relative


def _projection(snapshot):
    return {k: v for k, v in snapshot.items()
            if k not in ('revision', 'recorded_at', 'thread_entries', 'receipts')}


def _save(path, bundle):
    bundle['checkpoint_digest'] = _digest({k: v for k, v in bundle.items() if k != 'checkpoint_digest'})
    temporary = path.with_name(path.name + '.' + str(uuid4()) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(bundle, stream, ensure_ascii=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        # Windows scanners may briefly hold the old checkpoint without delete
        # sharing. Retry only the same atomic replacement, never delete first.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4: raise
                sleep(0.02)
    finally:
        if temporary.exists(): temporary.unlink()


def _permission_assessments(snapshot, request, reason):
    target = request['changes'][0]['target']
    chain = {w['work_key'] for w in inspect_work(snapshot, target)['parent_chain']}
    return {n: {'mode': 'current_scope', 'reason': reason, 'refs': request['source_refs'],
                'records': [r for r in snapshot['map'][n]
                    if (r['work_id'] == target if n == 'pins' else chain.intersection(r['targets']))]}
            for n in ('pins', 'permissions', 'action_rules')}


def _missing_operations(snapshot, work, actor, operations):
    chain = {w['work_key'] for w in inspect_work(snapshot, work)['parent_chain']}
    permissions = [p for p in snapshot['map']['permissions'] if p['grantee'] == actor]
    from pma.workmap_contract import rule_requires_disposition
    if any(chain.intersection(c['targets']) and rule_requires_disposition(c, operations) for c in snapshot['map']['action_rules']):
        raise TransitionDenied('applicable Action Rule needs disposition before a routine workflow')
    if any(chain.intersection(p['targets']) and p['effect'] != 'allow'
           and set(p['operations']).intersection((*operations, 'remove_permission')) for p in permissions):
        raise TransitionDenied('existing denial/unknown cannot be replaced by an Owner-instruction workflow')
    return [op for op in operations if not any(work in p['targets'] and op in p['operations']
                and p['effect'] == 'allow' for p in permissions)]


def _permission_step(snapshot, grant, *, remove, workflow, sources, reason):
    suffix = ('end:' if remove else 'grant:') + grant['id'][11:]
    tid = 'transition:' + suffix
    change = {'op': 'remove_permission' if remove else 'set_permission',
              'target': grant['targets'][0], 'permission_id': grant['id'],
              'expected': grant if remove else None}
    if not remove: change['value'] = grant
    q = {'format_version': snapshot['format_version'], 'project_id': snapshot['project_id'],
        'transition_id': tid, 'actor_id': workflow['meaning']['actor'],
        'base_revision': snapshot['revision'], 'intent': 'End only this temporary Pin authority' if remove else 'Record explicit limited Owner Pin instruction',
        'source_refs': sources, 'preconditions': [], 'changes': [change],
        'thread_entries': [{'id': 'thread:' + tid, 'works': grant['targets'],
            'kind': 'permission_changed', 'summary': 'Temporary Pin authority ended' if remove else 'Explicit limited Pin authority recorded',
            'why': reason, 'actor': workflow['meaning']['actor'], 'occurred_at': None,
            'recorded_at': datetime.now(timezone.utc).isoformat(), 'source_refs': sources, 'transition_id': tid}]}
    return q, _permission_assessments(snapshot, q, workflow['meaning']['scope_basis'])


def _expected_after(snapshot, request):
    """An oracle for just these three operations; Core remains the executor."""
    after = copy.deepcopy(_projection(snapshot)); m = after['map']; c = request['changes'][0]
    if c['op'] == 'set_permission': m['permissions'].append(c['value'])
    elif c['op'] == 'remove_permission':
        m['permissions'] = [p for p in m['permissions'] if p['id'] != c['permission_id']]
    elif c['op'] == 'update_pin':
        m['pins'] = [c['value'] if p['id'] == c['pin_id'] else p for p in m['pins']]
    else:
        g, v = c['expected'], c['value']
        held = {**g['source_pin'], 'mode': 'held',
                'refs': list(dict.fromkeys([*g['source_pin']['refs'], *request['source_refs']]))}
        m['pins'] = [held if p['id'] == held['id'] else
                     v['active_pin'] if g['destination_pin'] and p['id'] == g['destination_pin']['id'] else p
                     for p in m['pins']]
        if g['destination_pin'] is None: m['pins'].append(v['active_pin'])
        if v['resume_note'] is not None:
            old_note = next((n for n in m['notes'] if n['id'] == v['resume_note']['id']), None)
            if old_note is None:
                m['notes'].append(v['resume_note'])
            else:
                m['notes'][m['notes'].index(old_note)] = v['resume_note']
    return _digest(after)


def owner_pin_workflow(store_path, *, backend, reviewed_pin_move=False,
                       reviewed_owner_pin=False, owner_instruction=None,
                       owner_source=None, resume=None, inputs_packet=None, **meaning):
    """One routine call: source -> narrow grants if needed -> move -> grant end.

    The Owner flag attests to an explicit Pin-change instruction, not to arbitrary
    text containing approval words. Stop on changed premises; never rebase silently.
    A saved checkpoint/receipt recovers lost output, including between the stages.
    """
    if not (reviewed_pin_move and reviewed_owner_pin):
        raise TransitionDenied('explicit Owner Pin attribution and current-scope operator review are both required')
    path = Path(store_path).resolve(); t0 = perf_counter()
    if resume is not None:
        if meaning or inputs_packet is not None or owner_instruction is not None or owner_source is not None:
            raise ValueError('resume cannot replace the saved instruction or meaning')
        checkpoint = Path(resume).resolve()
        b = json.loads(checkpoint.read_text(encoding='utf-8'))
        if (b.get('kind') != KIND or b.get('store_binding') != {'path': str(path), 'backend': backend}
                or b.get('checkpoint_digest') != _digest({k: v for k, v in b.items() if k != 'checkpoint_digest'})):
            raise ValueError('resume requires the unchanged workflow and original Store/backend')
    else:
        if inputs_packet is not None and meaning:
            raise ValueError('input draft cannot be combined with individual meaning fields')
        if (owner_instruction is None) == (owner_source is None):
            raise ValueError('supply one literal instruction or an existing reviewed Owner source')
        snapshot = open_store(path, backend).read().snapshot
        if inputs_packet is not None:
            from tools.workmap_pin_inputs import resolve_pin_inputs
            meaning = resolve_pin_inputs(inputs_packet, snapshot, path, backend)
        # Capture the literal source; validate the complete plan before any Map mutation.
        owner_ref = owner_source
        if owner_ref is None:
            owner_ref = capture_instruction(owner_instruction, meaning['actor'], meaning['destination'])
        if not (owner_ref.startswith('repo:docs/work-management/evidence/owner_') and owner_ref.endswith('.txt')):
            raise ValueError('Owner evidence must be an existing repository Owner source')
        repository_evidence(owner_ref)
        meaning = {**meaning, 'sources': list(dict.fromkeys([*meaning.get('sources', []), owner_ref]))}
        initial = prepare_move(snapshot, **meaning)
        if initial['assessments'] is not None:
            meaning.update(scope_basis=initial['assessments']['pins']['reason'])
        else:
            # prepare_move omits assessments only after its complete-scope checks.
            # Keep that checked basis for temporary grants; unknown still needs review.
            meaning['scope_basis'] = meaning.get('scope_basis') or (
                'Ordinary Pin preparation checked complete endpoint/ancestor records and '
                'all actor Pins at this revision; operator reviews grant constraints.')
        if inputs_packet is not None:
            from tools.workmap_pin_inputs import check_prepared_pin_inputs
            check_prepared_pin_inputs(inputs_packet, snapshot, initial)
        c = initial['request']['changes'][0]
        returning = c['op'] == 'update_pin'
        source = None if returning else c['target']
        destination = c['target'] if returning else c['value']['new_target']
        wf = str(uuid4()); grants = []
        endpoints = [(destination, ('update_pin', 'append_thread'))] if returning else [
            (source, ('update_pin',) + (('set_note',) if c['value']['resume_note'] is not None else ()) + ('append_thread',)),
            (destination, ('update_pin' if c['expected']['destination_pin'] else 'add_pin', 'append_thread'))]
        for work, ops in endpoints:
            missing = _missing_operations(snapshot, work, meaning['actor'], ops)
            if missing:
                grants.append({'id': 'permission:pin-workflow:' + wf + ':' + str(len(grants)),
                    'targets': [work], 'grantee': meaning['actor'], 'grantor_ref': 'principal:owner',
                    'operations': [*missing, 'remove_permission'], 'effect': 'allow',
                    'constraints': ['Only this own Pin move/resume note/history and ending this temporary grant; no project-work authority',
                        'Workflow ' + wf + '; endpoints ' + (source or '(no active Pin)') + ' -> ' + destination],
                    'source_refs': sorted(initial['source_bindings'])})
        b = {'kind': KIND, 'store_binding': {'path': str(path), 'backend': backend},
             'workflow_id': wf, 'meaning': meaning, 'owner_source': owner_ref,
             'evidence_bindings': initial['source_bindings'], 'original_protected_digest': initial['protected_digest'],
             'frontier_digest': _digest(_projection(snapshot)), 'frontier_revision': snapshot['revision'],
             'grants': grants, 'plan': [('grant', i) for i in range(len(grants))] + [('move', None)]
                    + [('end', i) for i in reversed(range(len(grants)))],
             'steps': [], 'cursor': 0, 'move_bundle': None, 'display': initial['display']}
        directory = (path if backend == 'text' else path.parent) / 'transition-requests'
        directory.mkdir(exist_ok=True)
        checkpoint = directory / (wf + '.pin-workflow.json'); _save(checkpoint, b)
    report = {'request_path': str(checkpoint), 'workflow_id': b['workflow_id'],
              **b['display'], 'stage_timings': []}
    try:
        while b['cursor'] < len(b['plan']):
            index = b['cursor']; kind, grant_index = b['plan'][index]
            current = open_store(path, backend).read().snapshot
            step = b['steps'][index] if index < len(b['steps']) else None
            replay = step is not None and step['request']['transition_id'] in current['receipts']
            if not replay:
                if current['revision'] != b['frontier_revision'] or _digest(_projection(current)) != b['frontier_digest']:
                    raise TransitionConflict('workflow premises changed; review current Map, do not rebase')
                if any(hashlib.sha256(repository_evidence(r)).hexdigest() != h
                       for r, h in b['evidence_bindings'].items()):
                    raise TransitionConflict('workflow source evidence changed; review again')
            if step is None:
                if kind == 'move':
                    bundle = prepare_move(current, **b['meaning']); b['move_bundle'] = bundle
                    q, assessments = bundle['request'], bundle['assessments']
                else:
                    q, assessments = _permission_step(current, b['grants'][grant_index],
                        remove=kind == 'end', workflow=b, sources=sorted(b['evidence_bindings']),
                        reason=b['meaning']['reason'])
                step = {'request': q, 'assessments': assessments,
                        'expected_after': _expected_after(current, q)}
                b['steps'].append(step); _save(checkpoint, b)
            q = step['request']; t = perf_counter()
            result = _apply_request(path, q, backend=backend, context_assessments=step['assessments'],
                reviewed_owner_permission=kind == 'grant', reviewed_pin_move=kind == 'move')
            report['stage_timings'].append({'stage': kind, 'seconds': perf_counter() - t, 'receipt_replay': replay})
            if result.get('result') != 'applied':
                raise TransitionError('workflow stage did not return an applied receipt')
            after = open_store(path, backend).read().snapshot
            if after['revision'] != result['revision'] or _digest(_projection(after)) != step['expected_after']:
                raise TransitionConflict('stage receipt recovered but current state changed; do not continue')
            b['frontier_digest'] = step['expected_after']; b['frontier_revision'] = after['revision']
            b['cursor'] += 1; _save(checkpoint, b)
        final = open_store(path, backend).read().snapshot
        readback_state = final
        if requests_reapplication(b['move_bundle']['request']):
            # Only this workflow's exact grant/end records are allowed effects.
            # Keep all other history in the original protection comparison.
            allowed_ids = set()
            for step in b['steps']:
                q = step['request']
                if q['changes'][0]['op'] not in ('set_permission', 'remove_permission'):
                    continue
                receipt = final['receipts'].get(q['transition_id'], {})
                expected = {**q['thread_entries'][0], 'recorded_at': receipt.get('recorded_at')}
                actual = [e for e in final['thread_entries'] if e['id'] == expected['id']]
                # Core may add its reviewed authority/context metadata. The request
                # fields and receipt must still match; no other record is excluded.
                if (receipt.get('request_digest') != _digest(q) or len(actual) != 1
                        or any(actual[0].get(k) != v for k, v in expected.items())):
                    raise TransitionConflict('workflow authority history changed; do not confirm the move')
                allowed_ids.add(expected['id'])
            readback_state = {**final, 'thread_entries': [e for e in final['thread_entries'] if e['id'] not in allowed_ids]}
        report['readback'] = _readback(readback_state, {**b['move_bundle'],
                                'protected_digest': b['original_protected_digest']})
        report['result'] = {'result': 'applied', 'transition_id': b['move_bundle']['request']['transition_id']}
    except (OSError, StoreError, SnapshotInvalid) as exc:
        report['result'] = {'result': 'unknown', 'reason': str(exc)}
    except (TransitionError, ContextUnknown, ValueError) as exc:
        report['result'] = {'result': 'rejected', 'category': 'conflict' if isinstance(exc, TransitionConflict) else 'denied', 'reason': str(exc)}
    try:
        final = open_store(path, backend).read().snapshot
        present = {p['id'] for p in final['map']['permissions']}
        report['temporary_permissions_remaining'] = [g['id'] for g in b['grants'] if g['id'] in present]
    except (OSError, StoreError, SnapshotInvalid):
        report['temporary_permissions_remaining'] = 'unknown'
    if report['result']['result'] != 'applied':
        report['recovery'] = 'Resume this checkpoint after lost output; changed premises require fresh operator review, not a new automatic move'
    report['timings'] = {'workflow_seconds': perf_counter() - t0}
    return report
