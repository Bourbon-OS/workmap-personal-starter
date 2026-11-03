"""Bounded 0.4.3 scope movement through existing Core and Store CAS.

Bounded contract: one branch or one whole single-root Map, fresh or explicitly
authorized populated receiver. Whole return is only for a dedicated receiver.
No live remote view, global registry or automatic
resurrection. The caller reviews permission/environment applicability. Source
commit proof comes from an explicit same-host Store or an exported committed
release bound to a caller-supplied file hash. Carried proof is an export-time
fact, not a claim about the source's current state or sender authentication.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pma.workmap_core import (TransitionConflict, TransitionDenied, TransitionError,
                             UnsupportedTransition, _digest, _matching_receipt)

KINDS = frozenset(('scope_released', 'scope_accepted'))
FORMAT = '0.4.3'
RECIPIENT_OBSERVATION = 'recipient_had_current_work'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _records(snapshot):
    # Foreign movement records are immutable provenance, never this Map's
    # membership instruction, marker or proof of its own execution.
    result = []
    for e in snapshot['thread_entries']:
        if e['kind'] not in KINDS:
            continue
        p = _detail(e)['packet']
        side = p['source_map_id'] if e['kind'] == 'scope_released' else p['destination_map_id']
        if side == snapshot.get('map_id'):
            result.append(e)
    return result


def _recipient_observation(record):
    """Read Core's receipt-time fact, never infer it from absent old metadata."""
    from pma.workmap_contract import ContextUnknown
    observations = []
    for text in record.get('assumptions', []):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and RECIPIENT_OBSERVATION in value:
            observations.append(value)
    if (len(observations) != 1 or set(observations[0]) != {RECIPIENT_OBSERVATION}
            or type(observations[0][RECIPIENT_OBSERVATION]) is not bool):
        raise ContextUnknown('receipt-time recipient state is unknown; whole return is not authorized')
    return observations[0][RECIPIENT_OBSERVATION]


def _detail(entry):
    try:
        value = json.loads(entry['consequence'])
        if value['packet']['payload_sha256'] != _digest(value['packet']['payload']):
            raise ValueError('payload hash differs')
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise TransitionError('scope movement history is unreadable') from exc


def historical_identities(snapshot):
    identities = {}
    for entry in _records(snapshot):
        identities.update(copy.deepcopy(_detail(entry)['packet']['payload']['identities']))
    return identities


def all_identities(snapshot):
    from pma.workmap_resume import close_history
    result = historical_identities(snapshot)
    for e in snapshot['thread_entries']:
        if e['kind'] == 'close':
            history = close_history(e)
            if history and 'identity' in history:
                result[history['work']] = copy.deepcopy(history['identity'])
    result.update(copy.deepcopy(snapshot['works']))
    return result


def dependency_history(snapshot, provider):
    """Past external users, not claims about another Map's current Work."""
    result = []
    for entry in _records(snapshot):
        p = _detail(entry)['packet']
        for relation in p['payload'].get('boundary_relations', []):
            if (relation['provider'] == provider
                    and relation['consumer'] not in snapshot['map']['members']):
                result.append({'dependency': copy.deepcopy(relation),
                               'record_id': entry['id'], 'release_id': p['release_id'],
                               'source_map_id': p['source_map_id'],
                               'destination_map_id': p['destination_map_id'],
                               'recorded_at': entry['recorded_at'],
                               'current_status': 'unknown',
                               'boundary': 'Recorded use at movement; neither a live consumer nor an automatic close veto.'})
    return result


def provider_history(snapshot, key):
    """Resolve only a recorded identity; do not read the other Store."""
    identity = historical_identities(snapshot).get(key)
    if identity is None:
        return None
    records = [e for e in _records(snapshot) if key in _detail(e)['packet']['payload']['identities']]
    return {'provider_identity': identity,
            'provider_status': 'detached_history' if moved_out(snapshot, key) else 'external_history',
            'provider_current_status': 'unknown',
            'provider_history_refs': [e['id'] for e in records],
            'boundary': 'Identity at a recorded boundary; current progress and fulfillment are not inferred.'}


def moved_out(snapshot, key):
    moved = False
    for entry in _records(snapshot):
        if key in _detail(entry)['packet']['payload']['owned_keys']:
            moved = entry['kind'] == 'scope_released'
    return moved


def detached_markers(snapshot):
    """Root-only historical labels. Never a current Work or remote status."""
    completed = {_detail(e)['packet']['return_of'] for e in _records(snapshot)
                 if e['kind'] == 'scope_accepted'}
    markers = []
    for e in _records(snapshot):
        if e['kind'] != 'scope_released':
            continue
        p = _detail(e)['packet']
        if p['release_id'] in completed:
            continue
        identity = p['payload']['identities'][p['root']]
        markers.append({'work_key': p['root'], 'name': identity['name'],
                        'intent_at_release': identity['intent'], 'current': False,
                        'status': 'detached_history', 'release_id': p['release_id'],
                        'parent_at_release': p['payload']['original_parent'],
                        'recorded_at': e['recorded_at'], 'reason_ref': e['source_refs'],
                        'boundary': 'This Map released the scope; destination current state is not observed.'})
    return markers


def guard_actor_activation(snapshot, request):
    """Known outgoing main Work prevents an uncoordinated second active Pin.

    This checks this Map's own release history only. General Map-to-Map Pin
    switching remains unsupported; it does not poll another Map each operation.
    """
    active_moves = {m['release_id'] for m in detached_markers(snapshot)}
    actor = request['actor_id']
    for change in request['changes']:
        value = change.get('value', {})
        activating = (change.get('op') in ('move_active_pin', 'accept_owner_scope', 'renew_owner_scope')
                      or change.get('op') in ('add_pin', 'update_pin') and value.get('mode') == 'active')
        if not activating: continue
        for e in _records(snapshot):
            p = _detail(e)['packet']
            if e['kind'] == 'scope_released' and p['release_id'] in active_moves and any(
                    pin['actor_id'] == actor and pin['mode'] == 'active' for pin in p['payload']['map']['pins']):
                raise UnsupportedTransition('main Work moved to another Map; coordinated cross-Map Pin switching is not implemented')


def embedded_evidence(snapshot, ref):
    """Immutable Evidence bytes carried in committed movement records."""
    found = []
    for e in _records(snapshot):
        detail = _detail(e)
        files = detail['packet']['payload']['evidence_files'] + detail.get('projection_files', [])
        for file in files:
            if file['ref'] == ref:
                found.append(file['text'].encode('utf-8'))
    if not found:
        return None
    if any(raw != found[0] for raw in found):
        raise TransitionConflict('same Evidence binding carries different bytes')
    return found[0]


def _evidence_origin(snapshot, ref):
    for e in _records(snapshot):
        d = _detail(e)
        for file in d['packet']['payload']['evidence_files'] + d.get('projection_files', []):
            if file['ref'] == ref:
                return file['source_map_id']
    return snapshot['map_id']


def _file(ref, text, source_map):
    raw = text.encode('utf-8')
    return {'ref': ref, 'source_map_id': source_map, 'sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'text': text}


def _projection_file(evidence, source_map):
    text = _json(evidence)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    ref = 'repo:docs/work-management/evidence/work_thread_selection_transfer_' + digest + '.json#sha256=' + digest
    return _file(ref, text, source_map)


def validate_transfer_history(snapshot):
    """Validate noncurrent identity/provenance without creating a Work graveyard."""
    from pma.workmap_contract import SnapshotInvalid
    from pma.workmap_thread_view import BINDING
    identities, receipts, accepted = {}, {}, set()
    try:
        if not isinstance(snapshot.get('map_id'), str) or not snapshot['map_id'].strip():
            raise ValueError('0.4.3 needs a logical Map identity')
        for e in _records(snapshot):
            d = _detail(e)
            if set(d) != {'packet', 'projection_files'}:
                raise ValueError('movement record fields differ')
            p = d['packet']
            if set(p) != {'source_map_id', 'destination_map_id', 'release_id', 'return_of', 'root', 'base_revision', 'payload', 'payload_sha256'}:
                raise ValueError('packet fields differ')
            if (not all(isinstance(p[k], str) and p[k].strip() for k in
                        ('source_map_id', 'destination_map_id', 'release_id', 'root', 'base_revision'))
                    or p['source_map_id'] == p['destination_map_id']
                    or (p['return_of'] is not None and not isinstance(p['return_of'], str))):
                raise ValueError('movement endpoints differ')
            if p['root'] not in p['payload']['identities']:
                raise ValueError('historical root identity missing')
            payload = p['payload']
            fields = {'members', 'owned_keys', 'map', 'works', 'identities', 'display_ids',
                      'retirements', 'thread_entries', 'receipts', 'completeness', 'meaning',
                      'evidence_files', 'original_parent', 'classification'}
            if (not fields <= set(payload) or not set(payload) - fields <= {'boundary_relations', 'collection_placements'}
                    or payload['members'] != payload['map']['members']
                    or set(payload['works']) != set(payload['members'])
                    or not set(payload['members']) <= set(payload['owned_keys'])
                    or p['root'] not in payload['owned_keys']
                    or not set(payload['owned_keys']) <= set(payload['identities'])
                    or set(payload['display_ids']) != set(payload['identities'])):
                raise ValueError('payload current and historical scope differ')
            members = set(payload['members'])
            plans = payload.get('collection_placements', [])
            if not isinstance(plans, list):
                raise ValueError('placement history is not a list')
            original_rows = {'map': {n: [p['expected'] for p in plans if p['collection'] == n]
                                     for n in ('permissions', 'action_rules')}}
            _placements(original_rows, members, plans, e['source_refs'])
            for plan in plans:
                if plan['destination'] not in payload['map'][plan['collection']]:
                    raise ValueError('partitioned destination record is missing')
            for r in payload.get('boundary_relations', []):
                if (r.get('kind') != 'dependency' or not r.get('id') or not r.get('action')
                        or not r.get('requires') or not isinstance(r.get('refs'), list) or not r['refs']
                        or not {r.get('consumer'), r.get('provider')} <= set(payload['identities'])
                        or len(members.intersection((r['consumer'], r['provider']))) != 1):
                    raise ValueError('boundary Dependency is invalid')
                if r['consumer'] in members and r not in payload['map']['relations']:
                    raise ValueError('moving consumer lost its Dependency')
            for r in payload['map']['relations']:
                if r.get('kind') == 'dependency' and (r.get('consumer') not in members
                        or r.get('provider') not in payload['identities']):
                    raise ValueError('only consumer-owned Dependencies may be carried as current')
            if e['kind'] == 'scope_released' and p['release_id'] != e.get('transition_id'):
                raise ValueError('release ID is not its Transition ID')
            raw_records = {r['id']: r for r in payload['thread_entries']}
            for c in payload['classification']:
                if (c['id'] not in raw_records or c['sha256'] != _digest(raw_records[c['id']])
                        or c['disposition'] not in ('keep', 'technical_only')):
                    raise ValueError('carried classification changed its raw source')
            expected_side = p['source_map_id'] if e['kind'] == 'scope_released' else p['destination_map_id']
            if snapshot['map_id'] != expected_side:
                raise ValueError('movement belongs to another Map')
            if e['kind'] == 'scope_accepted':
                if p['release_id'] in accepted:
                    raise ValueError('release was accepted twice')
                accepted.add(p['release_id'])
            for key, identity in p['payload']['identities'].items():
                if identity.get('work_key') != key:
                    raise ValueError('historical identity differs')
                identities[key] = copy.deepcopy(identity)
            for tid, receipt in p['payload']['receipts'].items():
                if (not isinstance(receipt.get('request_digest'), str)
                        or re.fullmatch('[0-9a-f]{64}', receipt['request_digest']) is None
                        or not _matching_receipt(receipt, tid, receipt.get('request_digest'))):
                    raise ValueError('foreign applied result is invalid')
                if tid in receipts and receipts[tid] != receipt:
                    raise ValueError('foreign result ID conflict')
                if tid in snapshot['receipts'] and snapshot['receipts'][tid] != receipt:
                    raise ValueError('foreign result conflicts with a local applied result')
                receipts[tid] = copy.deepcopy(receipt)
            for file in p['payload']['evidence_files'] + d.get('projection_files', []):
                match = BINDING.fullmatch(file['ref'])
                raw = file['text'].encode('utf-8')
                if (match is None or file['sha256'] != match[2]
                        or hashlib.sha256(raw).hexdigest() != file['sha256']
                        or len(raw) != file['bytes'] or not file['source_map_id']):
                    raise ValueError('carried Evidence differs')
                json.loads(raw)
    except (KeyError, TypeError, ValueError, TransitionError) as exc:
        raise SnapshotInvalid('scope movement history: ' + str(exc)) from exc
    return identities, receipts


def isolated_format(snapshot, map_id):
    """Explicit in-memory candidate conversion, never an automatic live migration."""
    from pma.workmap_contract import validate_snapshot
    validate_snapshot(snapshot)
    if snapshot['format_version'] != '0.4.2':
        raise TransitionConflict('candidate must begin from 0.4.2')
    candidate = copy.deepcopy(snapshot)
    candidate.update(format_version=FORMAT, map_id=map_id)
    return validate_snapshot(candidate)


def empty_receiver(project_id, map_id):
    """New isolated receiver; no fabricated Work, Pin or Owner authority."""
    return {'format_version': FORMAT, 'project_id': project_id, 'map_id': map_id,
            'revision': uuid.uuid4().hex, 'recorded_at': datetime.now(timezone.utc).isoformat(),
            'works': {}, 'aliases': {}, 'display_ids': {}, 'display_id_history': [],
            'map': {'members': [], 'relations': [], 'pins': [], 'permissions': [], 'action_rules': [], 'notes': []},
            'retirements': {}, 'thread_entries': [], 'receipts': {},
            'metadata': {'completeness': 'full', 'store_role': 'canonical_current', 'basis': 'isolated_synthetic_trial'}}


def _projection(snapshot, repo):
    from pma.workmap_thread_view import meaning_thread
    # Transport the reviewed source projection, not its display enrichment.
    # Authored references/close fields remain in raw and are projected on read.
    result = meaning_thread(snapshot, repo=repo, reference_fields=False)
    if result['status'] == 'unknown':
        raise TransitionConflict('meaning Evidence unavailable: ' + result['reason'])
    return result['thread_entries']


def _classes(snapshot, repo):
    from pma.workmap_thread_view import selection_refs, load_selection, validate_selection
    bindings = [selection_refs(e) for e in snapshot['thread_entries'] if selection_refs(e)]
    if not bindings:
        return []
    if len(bindings[-1]) != 1:
        raise TransitionConflict('selection binding is ambiguous')
    doc = validate_selection(snapshot, load_selection(bindings[-1][0], repo, snapshot=snapshot))
    return doc['classification']


def _placements(snapshot, moving, placements, source_refs):
    """Explicit non-widening partitions of existing target-scoped records.

    This is part of one reviewed movement request, not another rule model.
    Semantic applicability of the unchanged statement/constraints still needs
    local review. Splitting a free-form statement by inference is unsupported.
    """
    plans, seen = [], set()
    for plan in placements:
        if (not isinstance(plan, dict) or set(plan) != {'collection', 'id', 'expected', 'source', 'destination', 'reason', 'refs'}
                or plan['collection'] not in ('permissions', 'action_rules')
                or not isinstance(plan['reason'], str) or not plan['reason'].strip()
                or not isinstance(plan['refs'], list) or not plan['refs']
                or not set(plan['refs']) <= set(source_refs)):
            raise TransitionError('placement needs exact records, reason and reviewed sources')
        name, identifier = plan['collection'], plan['id']
        if (name, identifier) in seen:
            raise TransitionError('placement is repeated')
        seen.add((name, identifier))
        original = next((r for r in snapshot['map'][name] if r['id'] == identifier), None)
        if original is None or original != plan['expected']:
            raise TransitionConflict('placement record changed')
        targets = set(original['targets'])
        if not targets.intersection(moving) or targets <= moving:
            raise UnsupportedTransition('only an explicitly shared target set may be partitioned here')
        for side, scope in [('source', targets - moving), ('destination', targets & moving)]:
            row = plan[side]
            if (not isinstance(row, dict) or set(row) != set(original)
                    or not isinstance(row.get('targets'), list) or set(row['targets']) != scope
                    or len(row['targets']) != len(scope)
                    or any(row[k] != original[k] for k in original if k not in ('id', 'targets'))):
                raise TransitionDenied('placement may only narrow targets; operations, actor, source and constraints cannot change')
        destination_id = plan['destination']['id']
        if (plan['source']['id'] != identifier or not isinstance(destination_id, str) or not destination_id.strip()
                or any(r['id'] == destination_id for r in snapshot['map'][name])
                or any(p['collection'] == name and p['destination']['id'] == destination_id for p in plans)):
            raise TransitionConflict('partitioned records need distinct unused IDs')
        plans.append(copy.deepcopy(plan))
    return plans


def prepare_release(snapshot, root, destination_map_id, *, repo: Path, actor,
                    source_refs, return_of=None, target=None, collection_placements=None):
    """Prepare from one revision. Core recomputes payload before release."""
    from pma.workmap_contract import current_subtree, validate_snapshot, _collection_status
    from pma.workmap_thread_view import selection_refs, load_selection
    from pma.workmap_resume import close_history
    validate_snapshot(snapshot)
    if snapshot['format_version'] != FORMAT or destination_map_id == snapshot['map_id']:
        raise UnsupportedTransition('release needs 0.4.3 and another Map')
    incoming = [e for e in _records(snapshot) if e['kind'] == 'scope_accepted' and _detail(e)['packet']['return_of'] is None]
    if return_of is None:
        if target is not None and target != root:
            raise TransitionDenied('initial release authorization target must be its root')
        if incoming or _records(snapshot):
            raise UnsupportedTransition('initial unit is one branch and one roundtrip; nesting/repeated cycles are not implemented')
        members = current_subtree(snapshot, root)
    else:
        matches = [e for e in incoming if _detail(e)['packet']['release_id'] == return_of]
        if (len(matches) != 1 or _detail(matches[0])['packet']['source_map_id'] != destination_map_id
                or _detail(matches[0])['packet']['root'] != root):
            raise TransitionConflict('return does not match the accepted cutout')
        if len(_records(snapshot)) != 1:
            raise UnsupportedTransition('return receiver has other local movements; whole return would mix scopes')
        if _recipient_observation(matches[0]):
            raise UnsupportedTransition('merged recipient cannot return its unrelated existing Work as a whole')
        members = list(snapshot['map']['members'])
    keys = set(members)
    if not keys:
        raise UnsupportedTransition('empty-current return needs a reviewed surviving permission target; not implemented in this unit')
    # Include previously closed descendants as history, without reviving them.
    if return_of is not None:
        owned = set(snapshot['display_ids']) - set(_detail(incoming[0])['packet']['payload']['identities']).difference(
            _detail(incoming[0])['packet']['payload']['owned_keys'])
    else:
        owned = set(keys)
        changed = True
        while changed:
            changed = False
            for e in snapshot['thread_entries']:
                if e['kind'] == 'close':
                    h = close_history(e)
                    if h and h['parent_at_close'] in owned and h['work'] not in owned:
                        owned.add(h['work']); changed = True
    parents = {r['child']: r['parent'] for r in snapshot['map']['relations'] if r['kind'] == 'parent'}
    original_parent = parents.get(root)
    relations, boundary_relations = [], []
    for r in snapshot['map']['relations']:
        endpoints = {r['child'], r['parent']} if r['kind'] == 'parent' else {r['consumer'], r['provider']}
        if not keys.intersection(endpoints):
            continue
        if r['kind'] == 'parent' and r['child'] == root and r['parent'] not in keys:
            continue
        if r['kind'] == 'dependency':
            if not endpoints <= keys:
                boundary_relations.append(copy.deepcopy(r))
            # One current owner: the consumer's Map. The other side retains
            # the recorded boundary, not an independently editable relation.
            if r['consumer'] in keys:
                relations.append(copy.deepcopy(r))
        else:
            if not endpoints <= keys:
                raise UnsupportedTransition('cross-boundary Parent is not a current relationship')
            relations.append(copy.deepcopy(r))
    plans = _placements(snapshot, keys, collection_placements or [], source_refs)
    partitioned = {(p['collection'], p['id']): p for p in plans}
    rows = {'members': members, 'relations': relations}
    for name in ('pins', 'permissions', 'action_rules', 'notes'):
        rows[name] = []
        for item in snapshot['map'][name]:
            targets = {item['work_id']} if name == 'pins' else set(item['targets'])
            if keys.intersection(targets):
                if not targets <= keys:
                    plan = partitioned.get((name, item['id']))
                    if plan is None:
                        raise UnsupportedTransition('cross-boundary ' + name + ' needs explicit placement')
                    rows[name].append(copy.deepcopy(plan['destination']))
                else:
                    rows[name].append(copy.deepcopy(item))
    # Ancestor rules are not silently shed. Their applicable placement needs review.
    ancestors, cursor = set(), root
    while cursor in parents:
        cursor = parents[cursor]; ancestors.add(cursor)
    if any(ancestors.intersection(r['targets'])
           and (('action_rules', r['id']) not in partitioned
                or root not in partitioned[('action_rules', r['id'])]['destination']['targets'])
           for r in snapshot['map']['action_rules']):
        raise UnsupportedTransition('applicable ancestor Action Rule must be placed before release')
    meaning = _projection(snapshot, repo)
    selected = [m for m in meaning if owned.intersection(m['works'])]
    wanted = {i for m in selected for i in m['related_entries']}
    records = {e['id']: e for e in snapshot['thread_entries']}
    pending = list(wanted | {e['id'] for e in snapshot['thread_entries'] if owned.intersection(e['works'])})
    visited = set()
    while pending:
        record_id = pending.pop()
        if record_id in visited or record_id not in records:
            continue
        visited.add(record_id)
        links = records[record_id].get('related_entries', [])
        if not isinstance(links, list) or any(not isinstance(link, str) or not link.strip() for link in links):
            raise TransitionConflict('authored Thread references are unreadable')
        wanted.update(links)
        pending.extend(link for link in links if link not in visited)
    raw = [copy.deepcopy(e) for e in snapshot['thread_entries']
           if return_of is not None or owned.intersection(e['works']) or e['id'] in wanted]
    needed = owned | {k for e in raw for k in e['works']}
    needed.update(k for r in relations + boundary_relations if r['kind'] == 'dependency'
                  for k in (r['consumer'], r['provider']))
    needed.update(k for plan in plans for k in plan['expected']['targets'])
    if original_parent is not None:
        needed.add(original_parent)
    # A previously closed external provider needs its genuine close record,
    # not a fabricated local closure or a silent loss of identity.
    raw_ids = {e['id'] for e in raw}
    for key in sorted(needed):
        close_id = snapshot['retirements'].get(key)
        if close_id and close_id not in raw_ids:
            raw.append(copy.deepcopy(next(e for e in snapshot['thread_entries'] if e['id'] == close_id)))
            raw_ids.add(close_id)
    needed.update(k for e in raw for k in e['works'])
    for e in raw:
        if e['kind'] == 'close':
            h = close_history(e)
            if h:
                if h['parent_at_close']: needed.add(h['parent_at_close'])
                needed.update(p['work_id'] for p in h['actor_held_at_close']['pins'])
    identities = all_identities(snapshot)
    _, prior_results = validate_transfer_history(snapshot)
    all_results = {**prior_results, **snapshot['receipts']}
    receipts = {e['transition_id']: copy.deepcopy(all_results[e['transition_id']]) for e in raw if e.get('transition_id')}
    # Keep original reviewed selection bytes as provenance, with Map+hash binding.
    files = []
    for e in snapshot['thread_entries']:
        for ref in selection_refs(e):
            carried = embedded_evidence(snapshot, ref)
            if carried is None:
                from pma.workmap_thread_view import BINDING
                match = BINDING.fullmatch(ref)
                if match is None: raise TransitionConflict('invalid selection ref')
                carried = (repo / 'docs/work-management/evidence' / match[1]).read_bytes()
                load_selection(ref, repo)  # exact hash check
            files.append(_file(ref, carried.decode('utf-8'), _evidence_origin(snapshot, ref)))
    payload = {'members': members, 'owned_keys': sorted(owned), 'map': rows,
               'works': {k: copy.deepcopy(snapshot['works'][k]) for k in members},
               'identities': {k: copy.deepcopy(identities[k]) for k in needed},
               'display_ids': {k: snapshot['display_ids'][k] for k in needed},
               'retirements': {k: v for k, v in snapshot['retirements'].items() if k in needed and any(e['id'] == v for e in raw)},
               'thread_entries': raw, 'receipts': receipts,
               'completeness': {n: {k: _collection_status(snapshot, n, k) for k in members}
                                for n in ('pins', 'permissions', 'action_rules')},
               'meaning': selected, 'evidence_files': files, 'original_parent': original_parent,
               'boundary_relations': boundary_relations, 'collection_placements': plans}
    raw_ids = {e['id'] for e in raw}
    payload['classification'] = [copy.deepcopy(c) for c in _classes(snapshot, repo) if c['id'] in raw_ids]
    tid = 'transition:' + str(uuid.uuid4())
    packet = {'source_map_id': snapshot['map_id'], 'destination_map_id': destination_map_id,
              'release_id': tid, 'return_of': return_of, 'root': root,
              'base_revision': snapshot['revision'], 'payload': payload, 'payload_sha256': _digest(payload)}
    return _request(snapshot, actor, source_refs, tid, 'release_scope', target or root, packet)


def _request(snapshot, actor, source_refs, tid, op, target, value):
    return {'format_version': FORMAT, 'project_id': snapshot['project_id'], 'transition_id': tid,
            'actor_id': actor, 'base_revision': snapshot['revision'], 'intent': op,
            'source_refs': source_refs, 'preconditions': [],
            'changes': [{'op': op, 'target': target, 'value': value}], 'thread_entries': []}


def release_envelope(source_store, release_id):
    """Re-read after a lost response; recover only a committed release."""
    from pma.workmap_contract import validate_snapshot
    s = validate_snapshot(source_store.read().snapshot)
    return _release_envelope(s, release_id)


def _release_envelope(snapshot, release_id):
    s = snapshot
    matches = [e for e in _records(s) if e['kind'] == 'scope_released' and e['transition_id'] == release_id]
    if len(matches) != 1 or release_id not in s['receipts']:
        raise TransitionConflict('release is not committed; candidate cannot be accepted')
    return {'record': copy.deepcopy(matches[0]), 'receipt': copy.deepcopy(s['receipts'][release_id]),
            'project_id': s['project_id']}


def release_file_ref(sha256):
    return 'source:workmap-release-file#sha256=' + sha256


def _validate_release_file(document):
    """Bind the original request, generated record and applied result.

    This checks consistency, not cryptographic sender authentication. Files
    must come from the reviewed sender/export and an independently supplied
    expected hash; never infer authority from a self-declared result alone.
    """
    try:
        if not isinstance(document, dict) or set(document) != {'request', 'envelope'}:
            raise ValueError('release file fields differ')
        request, envelope = document['request'], document['envelope']
        if not isinstance(envelope, dict) or set(envelope) != {'record', 'receipt', 'project_id'}:
            raise ValueError('release envelope fields differ')
        record, receipt = envelope['record'], envelope['receipt']
        detail = _detail(record)
        if set(detail) != {'packet', 'projection_files'} or detail['projection_files'] != []:
            raise ValueError('release record detail differs')
        p = detail['packet']
        change = request['changes'][0]
        if not isinstance(change['target'], str) or not change['target'].strip():
            raise ValueError('release target is missing')
        expected = _request({'project_id': envelope['project_id'], 'revision': p['base_revision']},
                            record['actor'], record['source_refs'], p['release_id'],
                            'release_scope', change['target'], p)
        if request != expected or not _matching_receipt(receipt, p['release_id'], _digest(request)):
            raise ValueError('original release request and applied result differ')
        expected_record = {
            'id': 'movement:' + p['release_id'], 'works': [p['root']], 'kind': 'scope_released',
            'summary': 'scope_released', 'why': 'Apply the explicitly reviewed scope movement',
            'actor': request['actor_id'], 'occurred_at': None, 'recorded_at': receipt['recorded_at'],
            'source_refs': request['source_refs'], 'transition_id': p['release_id'],
            'consequence': _json(detail),
        }
        if record != expected_record:
            raise ValueError('generated release record differs from the committed request')
        if (not isinstance(envelope['project_id'], str) or not envelope['project_id'].strip()
                or not isinstance(p['source_map_id'], str) or not p['source_map_id'].strip()
                or not isinstance(p['destination_map_id'], str) or not p['destination_map_id'].strip()
                or p['source_map_id'] == p['destination_map_id']):
            raise ValueError('release project or Map identity is invalid')
        return document
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise TransitionConflict('carried release proof is incomplete or inconsistent: ' + str(exc)) from exc


def read_release_file(path, expected_sha256):
    if not isinstance(expected_sha256, str) or re.fullmatch('[0-9a-f]{64}', expected_sha256) is None:
        raise TransitionDenied('carried release needs the expected SHA-256 from the reviewed export')
    try:
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise TransitionConflict('carried release file changed')
        document = json.loads(raw.decode('utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise TransitionConflict('carried release file is unreadable') from exc
    return _validate_release_file(document)


def export_release_file(source_store, request, path):
    """Export only a committed, still-released generation; never a candidate.

    No whole source snapshot is copied. The file contains the existing exact
    request and committed envelope. Its hash must travel as reviewed metadata.
    """
    from pma.workmap_contract import validate_snapshot
    committed = source_store.read()
    state = validate_snapshot(committed.snapshot)
    envelope = _release_envelope(state, request.get('transition_id'))
    _check_live_source(state, envelope)
    document = _validate_release_file({'request': copy.deepcopy(request), 'envelope': envelope})
    if source_store.read().token != committed.token:
        raise TransitionConflict('source changed during release export; reread before exporting')
    raw = (_json(document) + '\n').encode('utf-8')
    path = Path(path)
    if path.exists():
        if path.read_bytes() != raw:
            raise TransitionConflict('export path already contains a different release')
    else:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            import os
            os.fsync(stream.fileno())
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
            'release_id': request['transition_id'], 'source_commit_revision': envelope['receipt']['revision'],
            'source_map_id': state['map_id'],
            'destination_map_id': _detail(envelope['record'])['packet']['destination_map_id'],
            'boundary': 'Committed and still released at export; not a live source claim or sender authentication.'}


def prepare_accept_file(snapshot, path, expected_sha256, *, actor, source_refs, placements=None, target=None):
    document = read_release_file(path, expected_sha256)
    refs = list(dict.fromkeys([*source_refs, release_file_ref(expected_sha256)]))
    return prepare_accept(snapshot, document['envelope'], actor=actor, source_refs=refs,
                          placements=placements, target=target)


def prepare_accept(snapshot, envelope, *, actor, source_refs, placements=None, target=None):
    p = _detail(envelope['record'])['packet']
    return _request(snapshot, actor, source_refs, 'transition:' + str(uuid.uuid4()), 'accept_scope',
                    target or p['root'], {'envelope': copy.deepcopy(envelope), 'placements': placements or {}})


@dataclass(frozen=True)
class ReviewedTransfer:
    request_digest: str


def review_transfer(request):
    """Local operator confirms actual scope, rules and environment applicability.

    This binds that review; it supplies no Permission and is not authentication.
    """
    if request.get('format_version') != FORMAT:
        raise UnsupportedTransition('transfer requires 0.4.3')
    return ReviewedTransfer(_digest(request))


def _check_live_source(s, envelope):
    p = _detail(envelope['record'])['packet']
    actual = _release_envelope(s, p['release_id'])
    if actual != envelope or s['map_id'] != p['source_map_id']:
        raise TransitionConflict('source commit proof differs')
    if any(k in s['map']['members'] or not moved_out(s, k) for k in p['payload']['members']):
        raise TransitionConflict('source release is no longer the current movement generation')
    moving_actors = {pin['actor_id'] for pin in p['payload']['map']['pins'] if pin['mode'] == 'active'}
    if any(pin['actor_id'] in moving_actors and pin['mode'] == 'active' for pin in s['map']['pins']):
        raise TransitionConflict('source actor has another active Work; do not duplicate the main Pin')


def _source_proof(core, envelope, *, source_refs=()):
    source = core.transfer_source_store
    path = core.transfer_source_file
    if source is not None and path is not None:
        raise TransitionDenied('choose source Store or carried file, not both')
    if path is not None:
        expected = core.transfer_source_sha256
        document = read_release_file(path, expected)
        if document['envelope'] != envelope or release_file_ref(expected) not in source_refs:
            raise TransitionConflict('carried proof or its request binding differs')
        return
    if core.transfer_source_sha256 is not None:
        raise TransitionDenied('carried release hash has no file')
    if source is None:
        raise TransitionDenied('accept requires an explicit source Store or reviewed carried release file')
    from pma.workmap_contract import validate_snapshot
    _check_live_source(validate_snapshot(source.read().snapshot), envelope)


def _merge_raw(snapshot, incoming):
    indexed = {e['id']: e for e in snapshot['thread_entries']}
    for e in incoming:
        if e['id'] in indexed:
            if e != indexed[e['id']]:
                raise TransitionConflict('immutable history has same ID and different bytes')
        else:
            snapshot['thread_entries'].append(copy.deepcopy(e)); indexed[e['id']] = e


def _merged_projection(snapshot, old_meaning, new_meaning, known_classes, *, check_classification_conflicts=False):
    from pma.workmap_thread_view import TECHNICAL_KINDS, MEANING_KINDS, selection_refs, validate_selection
    items = {}
    for e in old_meaning + new_meaning:
        if e['id'] in items and items[e['id']] != e:
            raise TransitionConflict('reviewed meaning differs; explicit reconciliation is required')
        items[e['id']] = copy.deepcopy(e)
    kept = {i for e in items.values() for i in e['related_entries']}
    dispositions = {}
    for c in known_classes:
        previous = dispositions.get(c['id'])
        if check_classification_conflicts and previous and (previous['sha256'], previous['disposition']) != (c['sha256'], c['disposition']):
            raise TransitionConflict('meaning selection conflicts between Maps; explicit reconciliation is required')
        if check_classification_conflicts and c['id'] in kept and c['disposition'] != 'keep':
            raise TransitionConflict('meaning selection conflicts with a retained projection; explicit reconciliation is required')
        dispositions[c['id']] = c
    classes = []
    for e in snapshot['thread_entries']:
        if selection_refs(e):
            continue
        if e['id'] in kept:
            disposition = 'keep'
        elif e['kind'] in TECHNICAL_KINDS:
            disposition = 'technical_only'
        elif e['kind'] in MEANING_KINDS:
            # Existing meaning omitted by an earlier reviewed selection stays
            # omitted. Do not turn every inherited operation into a new Why.
            disposition = 'technical_only'
        elif e['id'] in dispositions and dispositions[e['id']]['sha256'] == _digest(e):
            disposition = dispositions[e['id']]['disposition']
        else:
            continue  # genuinely undecided records remain needs_review
        classes.append({'id': e['id'], 'sha256': _digest(e), 'disposition': disposition,
                        'reason': 'Preserve the reviewed source/destination projection and existing technical contract.'})
    evidence = {'project_id': snapshot['project_id'], 'source_revision': snapshot['revision'],
                'criterion': 'future_decision_why', 'classification': classes,
                'thread_entries': list(items.values())}
    validate_selection(snapshot, evidence)
    return evidence


def _historical_addresses(snapshot, payload, source_map):
    for key in payload['identities']:
        if key not in snapshot['display_ids']:
            identifier = 'history:' + source_map + ':' + payload['display_ids'][key]
            if identifier in snapshot['aliases'] and snapshot['aliases'][identifier] != key:
                raise TransitionConflict('historical address binding conflict')
            snapshot['display_ids'][key] = identifier; snapshot['aliases'][identifier] = key


def _place(snapshot, payload, placements, source_map, transition_id):
    from pma.workmap_contract import child_address_prefix
    parents = {r['child']: r['parent'] for r in payload['map']['relations'] if r['kind'] == 'parent'}
    roots = set(payload['members']) - set(parents)
    if set(placements) != roots:
        raise TransitionConflict('placement must cover every evolved current root')
    for root, parent in placements.items():
        if parent is not None:
            snapshot['map']['relations'].append({'id': 'parent:accepted:' + root,
                                                'kind': 'parent', 'child': root, 'parent': parent})
            parents[root] = parent
    pending = set(payload['members'])
    while pending:
        progressed = False
        for key in sorted(pending):
            parent = parents.get(key)
            if parent in pending: continue
            prefix = 'W' if parent is None else child_address_prefix(snapshot['display_ids'][parent])
            index = 1
            while prefix + str(index) in snapshot['aliases'] and snapshot['aliases'][prefix + str(index)] != key:
                index += 1
            address = prefix + str(index)
            old = payload['display_ids'][key]
            snapshot['display_ids'][key] = address; snapshot['aliases'][address] = key
            snapshot['display_id_history'].append({'work_key': key, 'old_display_id': old, 'new_display_id': address,
                                                  'transition_id': transition_id, 'thread_entry_id': 'movement:' + transition_id,
                                                  'from_map_id': source_map, 'to_map_id': snapshot['map_id'],
                                                  'reason': 'scope acceptance; addresses are local to a Map'})
            pending.remove(key); progressed = True
        if not progressed: raise TransitionConflict('placement has a Parent cycle')


def apply_transfer(core, snapshot, request, permission):
    from pma.workmap_contract import (validate_snapshot, inspect_work, require_decision_context,
                                     _collection_status, ContextUnknown)
    from pma.workmap_thread_view import validate_selection
    validate_snapshot(snapshot)
    if snapshot['format_version'] != FORMAT:
        raise UnsupportedTransition('scope movement requires 0.4.3')
    if core.reviewed_transfer is None or core.reviewed_transfer.request_digest != _digest(request):
        raise TransitionDenied('review the exact movement scope and environment first')
    if request['preconditions'] or request['thread_entries']:
        raise TransitionError('movement uses its generated technical record; optional Why uses ordinary rationale')
    change = request['changes'][0]
    if set(change) != {'op', 'target', 'value'}:
        raise TransitionError('movement fields differ')
    target, actor = change['target'], request['actor_id']
    if change['op'] == 'release_scope':
        p = change['value']
        if p['release_id'] != request['transition_id'] or p['source_map_id'] != snapshot['map_id']:
            raise TransitionConflict('release identity differs')
        if p['base_revision'] != request['base_revision'] or p['base_revision'] != snapshot['revision']:
            raise TransitionConflict('release packet revision differs from the current request')
        refreshed = prepare_release(snapshot, p['root'], p['destination_map_id'], repo=core.transfer_evidence_repo,
                                    actor=actor, source_refs=request['source_refs'], return_of=p['return_of'], target=target,
                                    collection_placements=p['payload'].get('collection_placements', []))
        if refreshed['changes'][0]['value']['payload'] != p['payload']:
            raise TransitionConflict('release contents changed')
        permission(snapshot, target, 'release_scope', actor)
        for key in p['payload']['members']:
            require_decision_context(inspect_work(snapshot, key))
            # Root/surviving-target authority does not override a known block
            # on a moving Work. It does not require a new allow on every child.
            for row in snapshot['map']['permissions']:
                if key in row['targets'] and row.get('grantee') == actor and 'release_scope' in row['operations']:
                    if row['effect'] == 'unknown':
                        raise ContextUnknown('moving Work release Permission is unknown')
                    if row['effect'] != 'allow':
                        raise TransitionDenied('moving Work release Permission denies this scope')
        for plan in p['payload'].get('collection_placements', []):
            op = 'set_permission' if plan['collection'] == 'permissions' else 'set_action_rule'
            for key in plan['expected']['targets']:
                require_decision_context(inspect_work(snapshot, key))
                permission(snapshot, key, op, actor)
        if any(pin['actor_id'] != actor for pin in p['payload']['map']['pins']):
            raise TransitionDenied('another actor must explicitly confirm their Pin disposition before this initial route')
        moving = set(p['payload']['members'])
        snapshot['map']['members'] = [k for k in snapshot['map']['members'] if k not in moving]
        snapshot['map']['relations'] = [r for r in snapshot['map']['relations']
                                      if (not moving.intersection({r['child'], r['parent']}) if r['kind'] == 'parent'
                                          else r['consumer'] not in moving)]
        for name in ('pins', 'permissions', 'action_rules', 'notes'):
            snapshot['map'][name] = [r for r in snapshot['map'][name]
                                     if not moving.intersection({r['work_id']} if name == 'pins' else set(r['targets']))]
        for plan in p['payload'].get('collection_placements', []):
            snapshot['map'][plan['collection']].append(copy.deepcopy(plan['source']))
        for key in moving: snapshot['works'].pop(key)
        complete = snapshot['metadata'].get('completeness')
        if isinstance(complete, dict):
            for name in ('pins', 'permissions', 'action_rules'):
                if isinstance(complete.get(name), dict):
                    complete[name]['confirmed_work_keys'] = [k for k in complete[name]['confirmed_work_keys'] if k not in moving]
        detail = {'packet': copy.deepcopy(p), 'projection_files': []}
        kind = 'scope_released'
    else:
        value = change['value']; envelope = value['envelope']; p = _detail(envelope['record'])['packet']
        if set(value) != {'envelope', 'placements'}:
            raise TransitionError('acceptance fields differ')
        _source_proof(core, envelope, source_refs=request['source_refs'])
        if p['destination_map_id'] != snapshot['map_id'] or envelope['project_id'] != snapshot['project_id']:
            raise TransitionConflict('wrong recipient Map or project')
        if any(e['kind'] == 'scope_accepted' and _detail(e)['packet']['release_id'] == p['release_id'] for e in _records(snapshot)):
            raise TransitionConflict('this release was already accepted; recover its original result')
        payload = p['payload']
        recipient_had_current_work = bool(snapshot['map']['members'])
        fresh = not (snapshot['map']['members'] or snapshot['thread_entries'] or snapshot['aliases'])
        if p['return_of'] is None:
            if fresh:
                # A genuinely fresh receiver uses the reviewed transferred
                # grant. A historical empty Map is never treated as fresh.
                permission({'map': payload['map']}, target, 'accept_scope', actor)
                placements = value['placements'] or {p['root']: None}
            else:
                if target not in snapshot['map']['members']:
                    raise TransitionConflict('recipient authorization target is not current')
                permission(snapshot, target, 'accept_scope', actor)
                require_decision_context(inspect_work(snapshot, target))
                if _records(snapshot):
                    raise UnsupportedTransition('independent recipient has prior local movements; repeated/nested merging is not implemented')
                placements = value['placements']
        else:
            matches = [e for e in _records(snapshot) if e['kind'] == 'scope_released' and e['transition_id'] == p['return_of']]
            if len(matches) != 1 or _detail(matches[0])['packet']['destination_map_id'] != p['source_map_id']:
                raise TransitionConflict('return generation or source differs')
            permission(snapshot, target, 'accept_scope', actor)
            if target not in snapshot['map']['members']:
                raise TransitionConflict('return authorization target is not current')
            require_decision_context(inspect_work(snapshot, target))
            placements = value['placements']
        for parent in placements.values():
            if parent is not None:
                if parent not in snapshot['map']['members']:
                    raise TransitionConflict('placement Parent is not current')
                permission(snapshot, parent, 'accept_scope', actor)
                require_decision_context(inspect_work(snapshot, parent))
        # An incoming active Pin may collide with an unknown actor assignment
        # anywhere in this recipient, not just at the placement Parent.
        if any(pin['mode'] == 'active' for pin in payload['map']['pins']):
            for key in snapshot['map']['members']:
                if _collection_status(snapshot, 'pins', key) != 'confirmed':
                    raise ContextUnknown('recipient active Pin exclusivity is unknown')
        if set(payload['members']).intersection(snapshot['map']['members']):
            raise TransitionConflict('current Work identity already exists in recipient')
        local_identities = all_identities(snapshot)
        if p['return_of'] is None:
            for key, identity in payload['identities'].items():
                if key in local_identities and local_identities[key] != identity:
                    raise TransitionConflict('shared Work identity differs at independent acceptance')
        else:
            for key in payload['owned_keys']:
                if key in local_identities and local_identities[key]['origin'] != payload['identities'][key]['origin']:
                    raise TransitionConflict('Work Origin conflicts with shared identity')
        old_meaning = _projection(snapshot, core.transfer_evidence_repo)
        old_classes = _classes(snapshot, core.transfer_evidence_repo)
        _merge_raw(snapshot, payload['thread_entries'])
        _historical_addresses(snapshot, payload, p['source_map_id'])
        snapshot['works'].update(copy.deepcopy(payload['works']))
        snapshot['map']['members'].extend(payload['members'])
        for name in ('relations', 'pins', 'permissions', 'action_rules', 'notes'):
            existing = {r['id'] for r in snapshot['map'][name]}
            if any(r['id'] in existing for r in payload['map'][name]):
                raise TransitionConflict('current collection ID conflicts at acceptance')
            snapshot['map'][name].extend(copy.deepcopy(payload['map'][name]))
        snapshot['retirements'].update(copy.deepcopy(payload['retirements']))
        _place(snapshot, payload, placements, p['source_map_id'], request['transition_id'])
        # Never turn an unknown sender collection into a confirmed recipient.
        if snapshot['metadata'].get('completeness') == 'full':
            snapshot['metadata']['completeness'] = {n: {'confirmed_work_keys': [k for k in snapshot['map']['members'] if k not in payload['members']]}
                                                     for n in ('pins', 'permissions', 'action_rules')}
        for n in ('pins', 'permissions', 'action_rules'):
            scope = snapshot['metadata']['completeness'].get(n)
            if scope == 'full':
                scope = snapshot['metadata']['completeness'][n] = {'confirmed_work_keys': [k for k in snapshot['map']['members'] if k not in payload['members']]}
            if not isinstance(scope, dict):
                scope = snapshot['metadata']['completeness'][n] = {'confirmed_work_keys': []}
            scope['confirmed_work_keys'] += [k for k in payload['members'] if payload['completeness'][n][k] == 'confirmed' and k not in scope['confirmed_work_keys']]
        # Independent Maps' reviewed exclusions must not be decided by union.
        # Dedicated returns retain the original contract: transport-generated
        # omission of an external Parent's Why is not a new editorial decision
        # overriding that Parent's local rationale when it returns home.
        merged = _merged_projection(snapshot, old_meaning, payload['meaning'], old_classes + payload['classification'],
                                    check_classification_conflicts=p['return_of'] is None)
        file = _projection_file(merged, snapshot['map_id'])
        detail = {'packet': copy.deepcopy(p), 'projection_files': [file]}
        kind = 'scope_accepted'
    record = {'id': 'movement:' + request['transition_id'], 'works': [p['root']], 'kind': kind,
              'summary': kind, 'why': 'Apply the explicitly reviewed scope movement',
              'actor': actor, 'occurred_at': None, 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'source_refs': copy.deepcopy(request['source_refs']), 'transition_id': request['transition_id'],
              'consequence': _json(detail)}
    if kind == 'scope_accepted':
        record['assumptions'] = [_json({RECIPIENT_OBSERVATION: recipient_had_current_work})]
        record['evidence_refs'] = [file['ref']]
        record['source_refs'] = list(dict.fromkeys(record['source_refs'] + envelope['record']['source_refs']))
    snapshot['thread_entries'].append(record)
    if kind == 'scope_accepted':
        validate_selection(snapshot, merged)
