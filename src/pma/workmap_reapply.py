"""Bounded Pin-move reapplication from a retained, committed base snapshot."""

import copy

from pma.workmap_contract import validate_snapshot, inspect_work, _collection_status
from pma.workmap_core import TransitionConflict, TransitionError, UnsupportedTransition, _nonempty, _digest
from pma.workmap_pin_move import move_guard, _pin, move_note_id
from pma.workmap_store import snapshot_object_id

PROJECTION = "pin_move_reapply_v1"


def protected_move_digest(snapshot, request):
    """Readback protection for this move, excluding only its own allowed effects.

    The writer still proves the entire intervening delta and uses Store CAS.
    This digest is not a replacement for that proof or for Permission checks.
    """
    c = request['changes'][0]
    actor, source, destination = request['actor_id'], c['target'], c['value']['new_target']
    contexts = [inspect_work(snapshot, k) for k in (source, destination)]
    keys = {p['work_key'] for ctx in contexts for p in ctx['parent_chain']}
    dependencies = [d for d in snapshot['map']['relations']
                    if d['kind'] == 'dependency' and {source, destination}.intersection((d['consumer'], d['provider']))]
    endpoints = {k for d in dependencies for k in (d['consumer'], d['provider'])}
    locations = {p['where'] for p in snapshot['map']['pins']
                 if p['work_id'] in (source, destination) and 'where' in p}
    if c['value']['active_pin'].get('where'):
        locations.add(c['value']['active_pin']['where'])
    pins = [p for p in snapshot['map']['pins']
            if (p['work_id'] in (source, destination) or p.get('where') in locations or p['actor_id'] == actor)
            and not (p['actor_id'] == actor and p['work_id'] in (source, destination))]
    related = keys | endpoints | {p['work_id'] for p in pins}
    note_id = move_note_id(c, actor)
    return _digest({'project_id': snapshot['project_id'], 'store_role': snapshot['metadata'].get('store_role'),
        'contexts': [{k: ctx[k] for k in ('parent_chain', 'dependencies')} for ctx in contexts],
        'dependencies': dependencies, 'pins': pins,
        'authority': {n: [r for r in snapshot['map'][n] if keys.intersection(r['targets'])]
                      for n in ('permissions', 'action_rules')},
        'visibility': {k: {n: _collection_status(snapshot, n, k) for n in ('pins', 'permissions', 'action_rules')}
                       for k in sorted(keys)},
        'actor_pins_complete': all(_collection_status(snapshot, 'pins', k) == 'confirmed' for k in snapshot['map']['members']),
        'notes': [n for n in snapshot['map']['notes'] if n['id'] != note_id and related.intersection(n['targets'])],
        'history': [{'id': e['id'], 'sha256': _digest(e)} for e in snapshot['thread_entries']
                    if e.get('transition_id') != request['transition_id'] and related.intersection(e['works'])]})


def reapply_precondition(snapshot: dict) -> dict:
    """Caller uses a snapshot read from the sole committed Store."""
    validate_snapshot(snapshot)
    return {"selector": "base_revision", "projection": PROJECTION,
            "expected": {"object_sha256": snapshot_object_id(snapshot)}}


def requests_reapplication(request: dict) -> bool:
    preconditions = request.get("preconditions")
    return (isinstance(preconditions, list) and len(preconditions) == 1
            and isinstance(preconditions[0], dict) and preconditions[0].get("projection") == PROJECTION)


def check_reapplication(store, current: dict, request: dict) -> None:
    """Check original guard and retained meaning history, including ABA changes.

    No request field is rewritten: the receipt and operator review continue to
    bind the original request even when the internal CAS revision changes.
    """
    changes = request.get("changes")
    if (not requests_reapplication(request) or not isinstance(changes, list) or len(changes) != 1
            or not isinstance(changes[0], dict) or changes[0].get("op") != "move_active_pin"):
        raise UnsupportedTransition("automatic reapplication is limited to one guarded own-actor Pin move")
    guard = request["preconditions"][0]
    expected = guard.get("expected")
    if (set(guard) != {"selector", "projection", "expected"} or guard["selector"] != "base_revision"
            or not isinstance(expected, dict) or set(expected) != {"object_sha256"}):
        raise TransitionError("reapplication requires an exact retained base reference")
    object_id = expected["object_sha256"]
    if not isinstance(object_id, str) or len(object_id) != 64 or any(c not in "0123456789abcdef" for c in object_id):
        raise TransitionError("invalid base object reference")
    base = current if current["revision"] == request["base_revision"] else validate_snapshot(store.read_retained_snapshot(object_id))
    if (snapshot_object_id(base) != object_id or base["revision"] != request["base_revision"]
            or base["project_id"] != current["project_id"]):
        raise TransitionConflict("retained base identity or content differs")
    if base is not current:
        # A prepared object has a fresh receipt not present in committed history.
        # Initial imports without a commit receipt cannot be proven this way.
        anchors = [(tid, r) for tid, r in base["receipts"].items() if r["revision"] == base["revision"]]
        if len(anchors) != 1 or current["receipts"].get(anchors[0][0]) != anchors[0][1]:
            raise TransitionConflict("base revision has no retained committed receipt; re-evaluate from current state")
        if (any(current["receipts"].get(tid) != r for tid, r in base["receipts"].items())
                or current["thread_entries"][:len(base["thread_entries"])] != base["thread_entries"]):
            raise TransitionConflict("base meaning history is not retained unchanged")
    change = changes[0]
    value = change.get("value")
    if not isinstance(value, dict) or 'resume_note' not in value or (value['resume_note'] is not None and not isinstance(value['resume_note'], dict)) or not isinstance(value.get("active_pin"), dict):
        raise TransitionError("movement context is missing")
    if any(not _nonempty(item) for item in (change.get("target"), value.get("new_target"), request.get("actor_id"), move_note_id(change, request.get('actor_id')))):
        raise TransitionError("movement identity is missing")
    _pin(value["active_pin"], value["new_target"], request["actor_id"], "active")
    try:
        args = (change["target"], value["new_target"], request["actor_id"], move_note_id(change, request['actor_id']), value["active_pin"].get("where"))
        prior_guard = move_guard(base, *args)
        current_guard = move_guard(current, *args)
    except KeyError as exc:
        raise TransitionError("movement identity is missing") from exc
    if change.get("expected") != prior_guard or current_guard != prior_guard:
        raise TransitionConflict("related Pin, permission, action_rule, purpose, dependency, WHERE or resume context changed")
    relevant = set()
    for name in ("source_context", "destination_context"):
        context = prior_guard[name]
        relevant.update(p["work_key"] for p in context["parent_chain"])
        relevant.update(p["work_id"] for p in context["shared_where_pins"])
        for dependency in context["incident_dependencies"]:
            relevant.update((dependency["consumer"], dependency["provider"]))
    reconstructed = copy.deepcopy(base)
    for entry in current["thread_entries"][len(base["thread_entries"]):]:
        if relevant.intersection(entry["works"]):
            raise TransitionConflict("related meaning history changed after the original judgment")
        # Only effects whose complete delta we can prove are automatically
        # classified as unrelated. Other/unknown semantic operations stop.
        if entry["kind"] == "note_changed":
            from pma.workmap_notes import note_history
            history = note_history(entry)
            if history['new_note'] is None:
                raise TransitionConflict('intervening note disposal needs a new judgment')
            notes = reconstructed["map"]["notes"]
            matching = [n for n in notes if n["id"] == history["new_note"]["id"]]
            expected_old = [] if history["old_note"] is None else [history["old_note"]]
            if matching != expected_old:
                raise TransitionConflict("intervening note history does not match its prior value")
            if matching:
                notes[notes.index(matching[0])] = copy.deepcopy(history["new_note"])
            else:
                notes.append(copy.deepcopy(history["new_note"]))
        elif entry['kind'] == 'pin_active_moved':
            from pma.workmap_pin_move import move_history
            history = move_history(entry)
            if entry['actor'] == request['actor_id']:
                raise TransitionConflict('the moving actor changed another Pin')
            if move_guard(reconstructed, *args) != prior_guard:
                raise TransitionConflict('related premises changed during intervening history')
            pins = reconstructed['map']['pins']
            old_source, old_dest = history['old_source_pin'], history['old_destination_pin']
            if old_source not in pins or ([p for p in pins if p['id'] == history['new_destination_pin']['id']]
                                         != ([old_dest] if old_dest else [])):
                raise TransitionConflict('intervening Pin history differs from its prior state')
            pins[pins.index(old_source)] = copy.deepcopy(history['new_source_pin'])
            if old_dest:
                pins[pins.index(old_dest)] = copy.deepcopy(history['new_destination_pin'])
            else:
                pins.append(copy.deepcopy(history['new_destination_pin']))
            note = history['resume_note'] or history['old_resume_note']
            if note:
                notes = reconstructed['map']['notes']
                matches = [n for n in notes if n['id'] == note['id']]
                if matches != ([history['old_resume_note']] if history['old_resume_note'] else []):
                    raise TransitionConflict('intervening Pin helper history differs')
                if history['resume_note'] is not None:
                    if matches:
                        notes[notes.index(matches[0])] = copy.deepcopy(note)
                    else:
                        notes.append(copy.deepcopy(note))
            if move_guard(reconstructed, *args) != prior_guard:
                raise TransitionConflict('intervening Pin move affected shared scope')
        elif entry["kind"] != "rationale_added":
            raise TransitionConflict("intervening semantic operation needs a new judgment")
    for field in set(base) - {"revision", "recorded_at", "thread_entries", "receipts"}:
        if reconstructed[field] != current[field]:
            raise TransitionConflict("current changes are not fully explained by unrelated retained history")
