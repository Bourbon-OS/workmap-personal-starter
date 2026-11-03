"""Editable routine-Pin inputs, not a request, grant or cached review."""
from __future__ import annotations

import hashlib
from pathlib import Path

from pma.workmap_close import repository_evidence
from pma.workmap_contract import validate_snapshot
from pma.workmap_core import TransitionConflict, TransitionError, _digest
from pma.workmap_pin_move import routine_pin_guard, pin_move_scope, resume_context

KIND = 'pin-move-inputs-v1'
GUIDE_REF = 'repo:docs/work-management/PIN_ROUTINE_ENTRY_2026-10-02.md'
RESUME_FIELDS = ('remaining', 'first_step', 'unsaved')
TEXT_FIELDS = ('reason', *RESUME_FIELDS, 'acceptance_basis', 'action_rule_basis',
               'dependency_basis', 'scope_basis')
LIST_FIELDS = ('sources', 'reusable_refs', 'reuse_resume_fields')


def pin_inputs_draft(snapshot, store_path, backend, *, actor, destination, where=None):
    """Bind a blank input draft to one current read; preserve old text as suggestions."""
    snapshot = validate_snapshot(snapshot)
    path = Path(store_path)
    if not path.is_absolute() or backend not in ('text', 'sqlite'):
        raise ValueError('input draft requires an absolute Store path and explicit backend')
    if snapshot['metadata'].get('store_role') != 'canonical_current':
        raise ValueError('selected Store is not canonical current')
    if (not isinstance(actor, str) or not actor.strip()
            or not isinstance(destination, str) or not destination.strip()
            or (where is not None and (not isinstance(where, str) or not where.strip()))):
        raise ValueError('input draft needs actor/destination and a nonempty optional location')
    destination = snapshot['aliases'].get(destination, destination)
    if destination not in snapshot['map']['members']:
        raise ValueError('destination is not a current Work')
    source, note_id, guard = routine_pin_guard(snapshot, actor, destination, where)
    held = guard['destination_pin']
    if held is not None and where is not None and held.get('where') != where:
        raise ValueError('reactivating a held Pin cannot change its location')
    scope = {'actor_id': actor, 'changes': [{'target': source, 'value': {
        'new_target': destination, 'resume_note': {'id': note_id},
        'active_pin': {'where': where} if where is not None else {}}}]}
    if source is None:
        scope['changes'] = [{'op': 'update_pin', 'target': destination, 'pin_id': held['id'],
                             'expected': held, 'value': {**held, 'mode': 'active'}}]
    _, _, refs = pin_move_scope(snapshot, scope)
    note = guard['resume_note']
    refs = refs | {GUIDE_REF} | set(held['refs'] if held else []) | set(note['refs'] if note else [])
    # Integrity only: hashing never means the operator has understood/appraised evidence.
    evidence = {ref: hashlib.sha256(repository_evidence(ref)).hexdigest() for ref in sorted(refs)}
    suggestions = {'status': 'absent', 'note_id': note_id, 'values': {}, 'review': 'not_reviewed'}
    if note is not None:
        try:
            old = resume_context(note, source, actor)
            suggestions.update(status='recorded', basis='reported',
                               values={n: old.get(n) for n in (*RESUME_FIELDS, 'reusable_refs')})
        except TransitionError:
            suggestions.update(status='invalid', reason='Existing note is not valid reusable resume material')
    return {'kind': KIND, 'binding': {
        'store': {'path': str(path.resolve()), 'backend': backend},
        'project_id': snapshot['project_id'], 'revision': snapshot['revision'],
        'snapshot_digest': _digest(snapshot), 'evidence_digest': _digest(evidence),
        'actor': actor, 'source': source, 'destination': destination, 'where': where},
        # Ordinary input is reason + sources. Optional review/resume fields may
        # be added explicitly; do not present them all as a blank checklist.
        'inputs': {'reason': None, 'sources': [GUIDE_REF]},
        'resume_suggestions': suggestions}


def resolve_pin_inputs(packet, snapshot, store_path, backend):
    """Validate current bindings before any request/Owner capture; return existing CLI meaning."""
    if (not isinstance(packet, dict) or set(packet) != {'kind', 'binding', 'inputs', 'resume_suggestions'}
            or packet['kind'] != KIND or not isinstance(packet['binding'], dict)
            or set(packet['binding']) != {'store', 'project_id', 'revision', 'snapshot_digest',
                'evidence_digest', 'actor', 'source', 'destination', 'where'}):
        raise ValueError('expected an unchanged draft envelope with editable inputs only')
    binding = packet['binding']
    if binding['store'] != {'path': str(Path(store_path).resolve()), 'backend': backend}:
        raise TransitionConflict('draft belongs to another Store/backend')
    current = pin_inputs_draft(snapshot, store_path, backend, actor=binding['actor'],
                               destination=binding['destination'], where=binding['where'])
    if binding != current['binding'] or packet['resume_suggestions'] != current['resume_suggestions']:
        raise TransitionConflict('draft revision, scope or evidence changed; read and review current inputs again')
    fields = packet['inputs']
    if (not isinstance(fields, dict) or not {'reason', 'sources'} <= set(fields)
            or not set(fields) <= set(TEXT_FIELDS + LIST_FIELDS)):
        raise ValueError('only the documented text, source and resume-selection inputs are accepted')
    lists = {name: fields.get(name, []) for name in LIST_FIELDS}
    for name in LIST_FIELDS:
        items = lists[name]
        if (not isinstance(items, list) or any(not isinstance(v, str) or not v.strip() for v in items)
                or len(items) != len(set(items))):
            raise ValueError(name + ' must be a list of distinct nonempty strings')
    reuse = lists['reuse_resume_fields']
    if not set(reuse) <= set(RESUME_FIELDS):
        raise ValueError('only remaining/first_step/unsaved text may be explicitly reused')
    # Absent optional fields keep the same meaning as the old null/empty slots.
    values = {n: fields.get(n) for n in TEXT_FIELDS}
    for name in reuse:
        if values[name] is not None or current['resume_suggestions']['status'] != 'recorded' or not current['resume_suggestions']['values'].get(name):
            raise ValueError('reuse requires a valid recorded suggestion and no conflicting explicit value')
        values[name] = current['resume_suggestions']['values'][name]
    if (not isinstance(values['reason'], str) or not values['reason'].strip() or not fields['sources']
            or any(v is not None and (not isinstance(v, str) or not v.strip()) for v in values.values())):
        raise ValueError('fill reason/sources; optional hints and review bases must be useful or omitted')
    # The ordinary mover still assembles full references and checks Core authority.
    return {**values, 'actor': binding['actor'], 'destination': binding['destination'],
            'where': binding['where'], 'sources': list(lists['sources']),
            'reusable_refs': list(lists['reusable_refs'])}


def check_prepared_pin_inputs(packet, snapshot, bundle):
    """Do not bind a prepared request to evidence changed since draft validation."""
    request = bundle['request']
    guard = request['changes'][0]['expected']
    _, _, refs = pin_move_scope(snapshot, request)
    if request['changes'][0]['op'] == 'update_pin':
        refs = refs | {GUIDE_REF} | set(guard['refs'])
    else:
        refs = (refs | {GUIDE_REF} | set(guard['destination_pin']['refs'] if guard['destination_pin'] else [])
                | set(guard['resume_note']['refs'] if guard['resume_note'] else []))
    hashes = {ref: bundle['source_bindings'][ref] if ref in bundle['source_bindings'] else
              hashlib.sha256(repository_evidence(ref)).hexdigest() for ref in sorted(refs)}
    if _digest(hashes) != packet['binding']['evidence_digest']:
        raise TransitionConflict('draft evidence changed during preparation; review again')
