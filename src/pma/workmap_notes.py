"""Exact, Work-scoped helper-note changes with retained technical history."""

import copy
import json
from datetime import datetime

from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _nonempty, _refs, _timestamp


WORK_CONTEXT_PROJECTION = "note_work_context"


def note_work_scope(snapshot, work, target, actor):
    """Bind existing Work/Pin/authority records; creates no new stored entity."""
    from pma.workmap_contract import inspect_work
    if work not in snapshot['map']['members'] or target not in snapshot['map']['members']:
        raise TransitionDenied('note work and target must both be current')
    contexts = [inspect_work(snapshot, key) for key in dict.fromkeys((work, target))]
    keys = {p['work_key'] for c in contexts for p in c['parent_chain']}
    active = [p for p in snapshot['map']['pins'] if p['actor_id'] == actor and p['mode'] == 'active']
    if len(active) != 1 or active[0]['work_id'] != work:
        raise TransitionDenied('note writer needs its accepted active Pin on the declared working Work')
    records = {name: [r for r in snapshot['map'][name]
                     if (r['work_id'] == work if name == 'pins' else keys.intersection(r['targets']))]
               for name in ('pins', 'permissions', 'action_rules')}
    return {'target': target, 'pin': copy.deepcopy(active[0]),
            'purposes': {p['work_key']: p['intent'] for c in contexts for p in c['parent_chain']},
            **copy.deepcopy(records)}


def note_work_precondition(snapshot, work, target, actor, reason):
    """Caller has checked why this target belongs to the accepted recording work."""
    if not _nonempty(reason):
        raise TransitionError('note work scope reason is required')
    return {'selector': work, 'projection': WORK_CONTEXT_PROJECTION,
            'expected': note_work_scope(snapshot, work, target, actor), 'reason': reason}


def declared_note_work(snapshot, request):
    """Note operations accept this existing context, without retry rebinding."""
    guards = request.get('preconditions')
    if guards == []:
        return request['changes'][0]['target']
    if (not isinstance(guards, list) or len(guards) != 1
            or not isinstance(guards[0], dict)
            or set(guards[0]) != {'selector', 'projection', 'expected', 'reason'}
            or guards[0]['projection'] != WORK_CONTEXT_PROJECTION
            or not _nonempty(guards[0]['selector']) or not _nonempty(guards[0]['reason'])):
        raise TransitionError('exact note working context is required')
    action_rule = guards[0]
    actual = note_work_scope(snapshot, action_rule['selector'], request['changes'][0]['target'], request['actor_id'])
    if action_rule['expected'] != actual:
        raise TransitionConflict('note working Pin, purpose or authority changed')
    return action_rule['selector']


def note_scope_records(snapshot, request):
    work = declared_note_work(snapshot, request)
    scope = note_work_scope(snapshot, work, request['changes'][0]['target'], request['actor_id'])
    return {name: scope[name] for name in ('pins', 'permissions', 'action_rules')}


def note_scope_refs(snapshot, request):
    work = declared_note_work(snapshot, request)
    scope = note_work_scope(snapshot, work, request['changes'][0]['target'], request['actor_id'])
    return set(scope['pin']['refs']).union(*(p['source_refs'] for p in scope['permissions']
                                            if p.get('grantee') == request['actor_id']))


def validate_note(note: dict, target: str) -> None:
    fields = {"id", "targets", "text", "refs", "basis"}
    if (not isinstance(note, dict) or not fields <= set(note) or not set(note) <= fields | {"observed_at"}
            or not _nonempty(note["id"]) or note["targets"] != [target] or not _nonempty(note["text"])
            or not _refs(note["refs"]) or note["basis"] not in ("fact", "reported", "proposal", "inference", "unknown")
            or ("observed_at" in note and not _timestamp(note["observed_at"]))):
        raise TransitionError("note needs one Work, explicit basis, text and sources")


def actor_helpers(snapshot, work_key=None):
    """Past actor-specific recovery aids, never shared current-state assertions."""
    from pma.workmap_pin_move import resume_context
    result = []
    for note in snapshot['map']['notes']:
        if len(note['targets']) != 1 or note['targets'][0] not in snapshot['map']['members']:
            continue
        target = note['targets'][0]
        if work_key is not None and target != work_key:
            continue
        try:
            data = json.loads(note['text'])
            resume_context(note, target, data['actor'])
        except (KeyError, TypeError, ValueError, TransitionError):
            continue
        recorded = note.get('observed_at')
        if recorded is None:
            for e in reversed(snapshot['thread_entries']):
                try:
                    h = json.loads(e.get('consequence', 'null'))
                except (ValueError, TypeError):
                    continue
                if isinstance(h, dict) and (h.get('new_note') == note or h.get('resume_note') == note):
                    recorded = e['recorded_at']; break
        result.append({'note': copy.deepcopy(note), 'hint': data, 'recorded_at': recorded,
                       'applicability': 'not_established',
                       'meaning': 'Past recovery aid; reread current Work, Pin, Permission, Action Rules and results.'})
    return result


def recorded_evidence(snapshot, work_key=None):
    """All explicit result records, not the latest record as a current status."""
    result = []
    for e in snapshot['thread_entries']:
        if e['kind'] != 'evidence_recorded' or (work_key is not None and work_key not in e['works']):
            continue
        # Note/confirmation/selection machinery remains in technical history.
        # Explicit human-readable results use the existing append_thread path.
        if e.get('consequence') is not None or any('work_thread_selection_' in r for r in e['source_refs']):
            continue
        result.append({k: copy.deepcopy(e[k]) for k in ('id', 'works', 'summary', 'why', 'recorded_at', 'source_refs')})
    return result


def note_history(entry: dict) -> dict:
    try:
        history = json.loads(entry["consequence"])
        if not isinstance(history, dict) or set(history) != {"old_note", "new_note"} or len(entry["works"]) != 1:
            raise ValueError()
        target = entry["works"][0]
        if history["new_note"] is not None:
            validate_note(history["new_note"], target)
        if history["old_note"] is not None:
            validate_note(history["old_note"], target)
            if history["new_note"] is not None and history["old_note"]["id"] != history["new_note"]["id"]:
                raise ValueError()
        if history["old_note"] is None and history["new_note"] is None:
            raise ValueError()
        if history["new_note"] is not None and not set(entry["source_refs"]) <= set(history["new_note"]["refs"]):
            raise ValueError()
        return history
    except (KeyError, TypeError, ValueError) as exc:
        raise TransitionError("note change history is unreadable") from exc


def apply_note(snapshot, request, change, entry, permission_check, thread_check, operation_review=None):
    removing = change.get('op') == 'remove_note'
    if set(change) != ({"op", "target", "expected"} if removing else {"op", "target", "expected", "value"}):
        raise TransitionError("exact note change fields are required")
    target, actor = change["target"], request["actor_id"]
    from pma.workmap_decision_context import require_operation_context
    work = declared_note_work(snapshot, request)
    scoped = request['preconditions'] != []
    require_operation_context(snapshot, request, operation_review, needs_pin=work == target)
    if scoped:
        from pma.workmap_contract import inspect_work, require_decision_context
        if operation_review is None:
            raise TransitionDenied('declared note work scope needs a bound operation review')
        require_decision_context(inspect_work(snapshot, work), operation_review=operation_review)
    # Review can establish this operation's recorded scope without claiming
    # historical completeness. Actual accepted participation is still required.
    pins = [p for p in snapshot['map']['pins'] if p['work_id'] == work]
    if not any(p["actor_id"] == actor and p["mode"] == "active" for p in pins):
        raise TransitionDenied("note writer needs its accepted active Pin on the working Work")
    for operation in (change['op'], "append_thread"):
        permission_check(snapshot, target, operation, actor)
    new = None if removing else change["value"]
    selected = change['expected'] if removing else new
    validate_note(selected, target)
    notes = snapshot["map"]["notes"]
    existing = [n for n in notes if n["id"] == selected["id"]]
    old = existing[0] if existing else None
    if len(existing) > 1 or change["expected"] != old or (removing and old is None):
        raise TransitionConflict("note differs from expected current value")
    if old is not None:
        validate_note(old, target)
        if not removing and old.get("observed_at"):
            if not new.get("observed_at"):
                raise TransitionConflict("dated observation cannot be replaced by an undated one")
            if datetime.fromisoformat(new["observed_at"].replace("Z", "+00:00")) < datetime.fromisoformat(old["observed_at"].replace("Z", "+00:00")):
                raise TransitionConflict("older observation must be kept separately, not replace this current note")
    required = {"id", "works", "kind", "summary", "why", "occurred_at", "recorded_at", "actor", "source_refs", "transition_id"}
    if not isinstance(entry, dict) or not required <= set(entry) or not set(entry) <= required | {"consequence"}:
        raise TransitionError("note Thread has unrelated fields")
    encoded = json.dumps({"old_note": old, "new_note": new}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if "consequence" in entry and entry["consequence"] != encoded:
        raise TransitionError("note history differs from actual change")
    entry = {**copy.deepcopy(entry), "consequence": encoded}
    note_history(entry)
    # Scoped routine observations are existing technical evidence, not automatic
    # meaning entries. A separate rationale can be authored when needed.
    thread_check(snapshot, request, entry, "evidence_recorded" if scoped or removing else "note_changed", [target])
    if scoped:
        entry['assumptions'] = json.dumps({'note_work_context': copy.deepcopy(request['preconditions'][0])},
                                         ensure_ascii=False, sort_keys=True)
    if removing:
        notes.remove(old)
    elif old is None:
        notes.append(copy.deepcopy(new))
    else:
        notes[notes.index(old)] = copy.deepcopy(new)
    snapshot["thread_entries"].append(entry)
