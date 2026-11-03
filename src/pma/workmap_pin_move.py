"""One atomic own-actor move, preserving held work and its resume context."""

from __future__ import annotations

import copy
import json
import hashlib
from dataclasses import dataclass

from pma.workmap_contract import ContextUnknown, _collection_status, inspect_work, require_decision_context
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest, _nonempty, _refs, _timestamp


@dataclass(frozen=True)
class ReviewedPinMove:
    """Operator reviewed acceptance, Action Rules, grant constraints and dependencies.

    A bounded trust input, not Owner authentication or new permission.
    """
    request_digest: str


def is_initial_pin(request):
    changes = request.get('changes', [])
    if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict) or changes[0].get('op') != 'add_pin':
        return False
    pin = changes[0].get('value', {})
    return (isinstance(pin, dict) and pin.get('mode') == 'active' and pin.get('actor_id') == request.get('actor_id')
            and pin.get('work_id') == changes[0].get('target'))


def initial_pin_scope(snapshot, request):
    """The same actor/ancestor/dependency premises as a routine return."""
    c = request['changes'][0]; target = c['target']; actor = request['actor_id']
    context = inspect_work(snapshot, target)
    keys = {w['work_key'] for w in context['parent_chain']}
    if any(p['actor_id'] == actor and (p['mode'] == 'active' or p['work_id'] == target)
           for p in snapshot['map']['pins']):
        raise TransitionConflict('initial Pin requires no own active or existing target Pin')
    locations = {p['where'] for p in snapshot['map']['pins'] if p['work_id'] == target and 'where' in p}
    if c['value'].get('where'): locations.add(c['value']['where'])
    rows = {'pins': [p for p in snapshot['map']['pins'] if p['actor_id'] == actor
                    or p['work_id'] == target or p.get('where') in locations],
            **{n: [r for r in snapshot['map'][n] if keys.intersection(r['targets'])]
               for n in ('permissions', 'action_rules')}}
    dependencies = [d for d in snapshot['map']['relations'] if d['kind'] == 'dependency'
                    and target in (d['consumer'], d['provider'])]
    refs = {ref for p in rows['pins'] for ref in p['refs']}
    refs.update(ref for p in rows['permissions'] for ref in p['source_refs'])
    refs.update(ref for r in rows['action_rules'] for ref in r['refs'])
    refs.update(ref for d in dependencies for ref in d['refs'])
    return tuple(sorted(keys)), rows, refs


def require_initial_pin_context(snapshot, request, review=None):
    keys, records, _ = initial_pin_scope(snapshot, request)
    target = request['changes'][0]['target']; actor = request['actor_id']
    if review is None and any(_collection_status(snapshot, 'pins', k) != 'confirmed'
                              for k in snapshot['map']['members']):
        raise ContextUnknown("actor's full active Pin search requires complete current Pin visibility")
    for key in keys:
        require_decision_context(inspect_work(snapshot, key), needs_pin=key == target,
                                 needs_parent_intent=False, operation_review=review)
    if any(p['grantee'] == actor and p['effect'] != 'allow'
           and set(p['operations']).intersection(('add_pin', 'append_thread'))
           for p in records['permissions']):
        raise TransitionDenied('applicable initial Pin denial still applies')
    try:
        basis = json.loads(request['thread_entries'][0].get('assumptions', '{}'))
    except (ValueError, TypeError):
        basis = {}
    if not isinstance(basis, dict): basis = {}
    from pma.workmap_contract import rule_requires_disposition
    if any(rule_requires_disposition(r, ('add_pin', 'append_thread')) for r in records['action_rules']):
        if review is None or not _nonempty(basis.get('action_rule_basis')):
            raise TransitionDenied('initial Pin Action Rules need an explicit applicability review')
    dependencies = [d for d in snapshot['map']['relations'] if d['kind'] == 'dependency'
                    and target in (d['consumer'], d['provider'])]
    if dependencies and (review is None or not _nonempty(basis.get('dependency_basis'))):
        raise TransitionDenied('initial Pin dependencies need an explicit applicability review')


def move_note_id(change, actor):
    """No new hint means preserve the existing hint, never silently clear it."""
    note = change['value']['resume_note'] or change.get('expected', {}).get('resume_note')
    return note['id'] if note else 'note:pin-entry:' + hashlib.sha256((actor + '\0' + change['target']).encode()).hexdigest()[:24]


def is_held_reactivation(request):
    """Only the existing own held->active update; never generic update_pin."""
    changes = request.get('changes', [])
    if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict) or changes[0].get('op') != 'update_pin':
        return False
    c = changes[0]; old, new = c.get('expected'), c.get('value')
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    mutable = {'mode', 'confirmed_at', 'refs'}
    return (old.get('mode') == 'held' and new.get('mode') == 'active'
            and old.get('actor_id') == request.get('actor_id')
            and old.get('work_id') == c.get('target') and old.get('id') == c.get('pin_id')
            and {k: v for k, v in old.items() if k not in mutable}
            == {k: v for k, v in new.items() if k not in mutable}
            and _refs(new.get('refs')) and set(old.get('refs', [])) <= set(new['refs']))


def observe_reactivation_guard(snapshot, destination, actor, where=None):
    """Read the same actor/ancestor/shared scope for an explicit held return."""
    if destination not in snapshot['map']['members']:
        raise TransitionConflict('destination is not a current Work')
    if any(p['actor_id'] == actor and p['mode'] == 'active' for p in snapshot['map']['pins']):
        raise TransitionConflict('held return requires no own active Pin')
    held = [p for p in snapshot['map']['pins'] if p['actor_id'] == actor and p['work_id'] == destination]
    if len(held) != 1 or held[0]['mode'] != 'held':
        raise TransitionConflict('explicit destination needs exactly one existing own held Pin')
    if where is not None and held[0].get('where') != where:
        raise TransitionConflict('reactivating a held Pin cannot change its location')
    context = inspect_work(snapshot, destination)
    chain = {p['work_key'] for p in context['parent_chain']}
    rows = {n: copy.deepcopy([r for r in snapshot['map'][n]
                             if (r['work_id'] == destination if n == 'pins' else destination in r['targets'])])
            for n in ('pins', 'permissions', 'action_rules')}
    locations = {p['where'] for p in rows['pins'] if 'where' in p}
    endpoint = {**{n: copy.deepcopy(context[n]) for n in ('parent_chain', 'dependencies')}, **rows,
                'incident_dependencies': copy.deepcopy([d for d in snapshot['map']['relations'] if d['kind'] == 'dependency' and destination in (d['consumer'], d['provider'])]),
                'shared_where_pins': copy.deepcopy([p for p in snapshot['map']['pins'] if p.get('where') in locations]),
                'ancestor_action_rules': copy.deepcopy([c for c in snapshot['map']['action_rules'] if chain & set(c['targets'])]),
                'notes': copy.deepcopy([n for n in snapshot['map']['notes'] if destination in n['targets']])}
    return {'source_context': None, 'destination_context': endpoint, 'actor_active_pins': [],
            'source_pin': None, 'destination_pin': copy.deepcopy(held[0]), 'resume_note': None}


def routine_pin_guard(snapshot, actor, destination, where=None):
    """One selector used by reader, inputs and preparation; no auto destination."""
    active = [p for p in snapshot['map']['pins'] if p['actor_id'] == actor and p['mode'] == 'active']
    if not active:
        return None, None, observe_reactivation_guard(snapshot, destination, actor, where)
    if len(active) != 1:
        raise TransitionConflict('own active Pin is ambiguous')
    source = active[0]['work_id']
    note = 'note:pin-entry:' + hashlib.sha256((actor + '\0' + source).encode()).hexdigest()[:24]
    return source, note, observe_move_guard(snapshot, source, destination, actor, note, where)


def observe_move_guard(snapshot: dict, source: str, destination: str, actor: str, note_id: str,
                       destination_where: str | None = None) -> dict:
    """Prepare recorded premises only; this cannot authorize a move or confirm a set."""
    if source == destination or any(k not in snapshot["map"]["members"] for k in (source, destination)):
        raise TransitionConflict("move requires two distinct current Works")
    contexts = []
    for key in (source, destination):
        context = inspect_work(snapshot, key)
        chain = {p["work_key"] for p in context["parent_chain"]}
        # A recorded projection must not turn unknown into an empty set. The
        # ordinary reader still returns None; these exact rows are review input.
        rows = {n: copy.deepcopy([r for r in snapshot['map'][n]
                                 if (r['work_id'] == key if n == 'pins' else key in r['targets'])])
                for n in ('pins', 'permissions', 'action_rules')}
        locations = {p["where"] for p in rows["pins"] if "where" in p}
        if key == destination and destination_where:
            locations.add(destination_where)
        contexts.append({**{n: copy.deepcopy(context[n]) for n in ("parent_chain", "dependencies")}, **rows,
                         "incident_dependencies": copy.deepcopy([d for d in snapshot["map"]["relations"] if d["kind"] == "dependency" and key in (d["consumer"], d["provider"])]),
                         "shared_where_pins": copy.deepcopy([p for p in snapshot["map"]["pins"] if p.get("where") in locations]),
                         "ancestor_action_rules": copy.deepcopy([c for c in snapshot["map"]["action_rules"] if chain & set(c["targets"])]),
                         "notes": copy.deepcopy([n for n in snapshot["map"]["notes"] if key in n["targets"]])})
    active = [p for p in snapshot["map"]["pins"] if p["actor_id"] == actor and p["mode"] == "active"]
    held = [p for p in snapshot["map"]["pins"] if p["actor_id"] == actor and p["work_id"] == destination]
    notes = [n for n in snapshot["map"]["notes"] if n["id"] == note_id]
    if len(active) != 1 or active[0]["work_id"] != source or len(held) > 1 or len(notes) > 1:
        raise TransitionConflict("own source active Pin, destination Pin or resume note is ambiguous")
    return {"source_context": contexts[0], "destination_context": contexts[1],
            "actor_active_pins": copy.deepcopy(active), "source_pin": copy.deepcopy(active[0]),
            "destination_pin": copy.deepcopy(held[0]) if held else None,
            "resume_note": copy.deepcopy(notes[0]) if notes else None}


def pin_move_scope(snapshot: dict, request: dict) -> tuple:
    """Exact recorded review scope; positive evidence of coverage remains necessary."""
    if is_initial_pin(request):
        return initial_pin_scope(snapshot, request)
    c = request['changes'][0]
    v, actor = c['value'], request['actor_id']
    if c.get('op') == 'update_pin':
        if not is_held_reactivation(request):
            raise TransitionDenied('only own held->active may use the routine return scope')
        guard = observe_reactivation_guard(snapshot, c['target'], actor, v.get('where'))
        if c['expected'] != guard['destination_pin']:
            raise TransitionConflict('held Pin differs from reviewed current value')
        _pin(v, c['target'], actor, 'active')
    else:
        guard = observe_move_guard(snapshot, c['target'], v['new_target'], actor,
                                   move_note_id(c, actor), v['active_pin'].get('where'))
    contexts = [context for context in (guard['source_context'], guard['destination_context']) if context is not None]
    keys = {p['work_key'] for context in contexts for p in context['parent_chain']}
    relevant_pins = [p for context in contexts for p in context['pins'] + context['shared_where_pins']]
    records = {'pins': [p for p in snapshot['map']['pins'] if p['actor_id'] == actor or p in relevant_pins],
               **{n: [r for r in snapshot['map'][n] if keys.intersection(r['targets'])]
                  for n in ('permissions', 'action_rules')}}
    refs = {ref for p in records['pins'] for ref in p['refs']}
    refs.update(ref for p in records['permissions'] for ref in p['source_refs'])
    refs.update(ref for c in records['action_rules'] for ref in c['refs'])
    refs.update(ref for context in contexts for d in context['incident_dependencies'] for ref in d['refs'])
    return tuple(sorted(keys)), records, refs


def move_guard(snapshot: dict, source: str, destination: str, actor: str, note_id: str,
               destination_where: str | None = None, *, operation_review=None) -> dict:
    if operation_review is None:
        if any(_collection_status(snapshot, "pins", key) != "confirmed" for key in snapshot["map"]["members"]):
            raise ContextUnknown("actor's full active Pin search requires complete current Pin visibility")
    else:
        from pma.workmap_decision_context import ReviewedDecisionContext
        if (not isinstance(operation_review, ReviewedDecisionContext)
                or operation_review.snapshot_digest != _digest(snapshot)
                or operation_review.target != source or destination not in operation_review.related_targets):
            raise TransitionDenied('Pin move needs its exact current operation scope')
    guard = observe_move_guard(snapshot, source, destination, actor, note_id, destination_where)
    for key in (source, destination):
        context = inspect_work(snapshot, key)
        require_decision_context(context, operation_review=operation_review)
        for p in context['parent_chain']:
            if (_collection_status(snapshot, 'action_rules', p['work_key']) != 'confirmed'
                    and not (operation_review is not None
                             and operation_review.permits(inspect_work(snapshot, p['work_key']), 'action_rules'))):
                raise ContextUnknown('applicable ancestor Action Rules are unconfirmed')
    return guard


def require_pin_move_context(snapshot, request, review):
    """Known restrictions on either endpoint are never waived by a scope review."""
    c = request['changes'][0]
    v = c['value']
    if is_held_reactivation(request):
        from pma.workmap_decision_context import ReviewedDecisionContext
        if (not isinstance(review, ReviewedDecisionContext) or review.snapshot_digest != _digest(snapshot)
                or review.target != c['target']):
            raise TransitionDenied('held return needs its exact current operation scope')
        pin_move_scope(snapshot, request)
        context = inspect_work(snapshot, c['target'])
        require_decision_context(context, operation_review=review)
        for p in context['parent_chain']:
            if (_collection_status(snapshot, 'action_rules', p['work_key']) != 'confirmed'
                    and not review.permits(inspect_work(snapshot, p['work_key']), 'action_rules')):
                raise ContextUnknown('applicable ancestor Action Rules are unconfirmed')
        chain = {p['work_key'] for p in context['parent_chain']}
        operations = ('update_pin', 'append_thread')
        from pma.workmap_contract import rule_requires_disposition
        if any(chain.intersection(r['targets']) and rule_requires_disposition(r, operations)
               for r in snapshot['map']['action_rules']):
            raise TransitionDenied('applicable Action Rule still needs disposition')
        if any(chain.intersection(p['targets']) and p['grantee'] == request['actor_id']
               and set(operations).intersection(p['operations']) and p['effect'] != 'allow'
               for p in snapshot['map']['permissions']):
            raise TransitionDenied('applicable denial still blocks this operation')
        return
    guard = move_guard(snapshot, c['target'], v['new_target'], request['actor_id'],
                       move_note_id(c, request['actor_id']), v['active_pin'].get('where'), operation_review=review)
    source_ops = ('update_pin', 'append_thread') + (('set_note',) if v['resume_note'] is not None else ())
    for context, operations in ((guard['source_context'], source_ops),
            (guard['destination_context'], ('update_pin' if guard['destination_pin'] else 'add_pin', 'append_thread'))):
        chain = {p['work_key'] for p in context['parent_chain']}
        from pma.workmap_contract import rule_requires_disposition
        if any(rule_requires_disposition(r, operations) for r in context['ancestor_action_rules']):
            raise TransitionDenied('applicable Action Rule still needs disposition')
        if any(chain.intersection(p['targets']) and p.get('grantee') == request['actor_id']
               and set(operations).intersection(p['operations']) and p['effect'] != 'allow'
               for p in snapshot['map']['permissions']):
            raise TransitionDenied('applicable denial still blocks this operation')


def resume_context(note: dict, source: str, actor: str) -> dict:
    try:
        data = json.loads(note["text"])
        if (not {"id", "targets", "text", "refs", "basis"} <= set(note)
                or not set(note) <= {"id", "targets", "text", "refs", "basis", "observed_at"}
                or not _nonempty(note["id"]) or note["targets"] != [source] or note["basis"] != "reported"
                or not _refs(note["refs"]) or not note["refs"]
                or ("observed_at" in note and not _timestamp(note["observed_at"]))
                or not {"actor", "return_work"} <= set(data)
                or not set(data) <= {"actor", "return_work", "remaining", "first_step", "reusable_refs", "unsaved"}
                or data["actor"] != actor or data["return_work"] != source
                or any(not _nonempty(data[n]) for n in ("remaining", "first_step", "unsaved") if n in data)
                or not isinstance(data.get("reusable_refs", []), list)
                or any(not _nonempty(ref) for ref in data.get("reusable_refs", []))
                or not set(data.get("reusable_refs", [])) <= set(note["refs"])
                or not any(data.get(n) for n in ("remaining", "first_step", "unsaved", "reusable_refs"))):
            raise ValueError()
        return data
    except (KeyError, TypeError, ValueError) as exc:
        raise TransitionError("helper note needs its actor/Work, sources and at least one useful hint") from exc


def _pin(pin: dict, work: str, actor: str, mode: str) -> None:
    required = {"id", "work_id", "actor_id", "mode", "pinned_at", "confirmed_at", "refs"}
    if (not isinstance(pin, dict) or not required <= set(pin) or not set(pin) <= required | {"where", "release"}
            or not _nonempty(pin["id"]) or pin["work_id"] != work or pin["actor_id"] != actor or pin["mode"] != mode
            or not _timestamp(pin["pinned_at"]) or not _timestamp(pin["confirmed_at"])
            or not _refs(pin["refs"]) or not pin["refs"]
            or any(not _nonempty(pin[n]) for n in ("where", "release") if n in pin)):
        raise TransitionError("move Pin identity, mode, acceptance refs or time is invalid")


def move_history(entry: dict) -> dict:
    """Reader checks retain the two sides and restart material after later moves."""
    try:
        h = json.loads(entry["consequence"])
        if isinstance(h, dict) and 'condition_basis' in h:
            if 'action_rule_basis' in h:
                raise ValueError()
            h['action_rule_basis'] = h.pop('condition_basis')
        if not isinstance(h, dict) or set(h) != {"from", "to", "old_source_pin", "new_source_pin", "old_destination_pin", "new_destination_pin", "old_resume_note", "resume_note", "acceptance_basis", "action_rule_basis", "dependency_basis", "decision_context"}:
            raise ValueError()
        source, destination, actor = h["from"], h["to"], entry["actor"]
        if source == destination or entry["works"] != [source, destination] or any(not _nonempty(h[n]) for n in ("acceptance_basis", "action_rule_basis", "dependency_basis")):
            raise ValueError()
        _pin(h["old_source_pin"], source, actor, "active")
        _pin(h["new_source_pin"], source, actor, "held")
        _pin(h["new_destination_pin"], destination, actor, "active")
        if (h["new_source_pin"] != {**h["old_source_pin"], "mode": "held", "refs": list(dict.fromkeys([*h["old_source_pin"]["refs"], *entry["source_refs"]]))}
                or h["decision_context"]["source_pin"] != h["old_source_pin"]
                or h["decision_context"]["actor_active_pins"] != [h["old_source_pin"]]
                or h["decision_context"]["destination_pin"] != h["old_destination_pin"]
                or h["decision_context"]["resume_note"] != h["old_resume_note"]):
            raise ValueError()
        old = h["old_destination_pin"]
        if old is not None:
            _pin(old, destination, actor, "held")
            mutable = {"mode", "confirmed_at", "refs"}
            if {k:v for k,v in h["new_destination_pin"].items() if k not in mutable} != {k:v for k,v in old.items() if k not in mutable}:
                raise ValueError()
            if not set(old["refs"]) <= set(h["new_destination_pin"]["refs"]):
                raise ValueError()
        elif h["new_destination_pin"]["id"] == h["old_source_pin"]["id"]:
            raise ValueError()
        context = h["decision_context"]
        if set(context) != {"source_context", "destination_context", "actor_active_pins", "source_pin", "destination_pin", "resume_note"}:
            raise ValueError()
        for name, work, old_pin in (("source_context", source, h["old_source_pin"]), ("destination_context", destination, old)):
            c = context[name]
            if isinstance(c, dict) and 'conditions' in c:
                if 'action_rules' in c or 'ancestor_action_rules' in c:
                    raise ValueError()
                # Validate the former saved context on a copy; stored JSON
                # remains verbatim and current requests never use these names.
                c = {('action_rules' if key == 'conditions' else
                      'ancestor_action_rules' if key == 'ancestor_conditions' else key): val
                     for key, val in c.items()}
            fields = {"parent_chain", "pins", "permissions", "action_rules", "dependencies", "incident_dependencies", "shared_where_pins", "ancestor_action_rules", "notes"}
            if not isinstance(c, dict) or set(c) != fields or any(not isinstance(c[n], list) for n in fields):
                raise ValueError()
            if not c["parent_chain"] or c["parent_chain"][-1]["work_key"] != work or any(p["intent_status"] != "confirmed" or not _nonempty(p["intent"]) for p in c["parent_chain"]):
                raise ValueError()
            own = [p for p in c["pins"] if p["actor_id"] == actor]
            if (old_pin is not None and old_pin not in own) or (old_pin is None and own):
                raise ValueError()
        selected = h['resume_note'] or h['old_resume_note']
        previous = [n for n in context["source_context"]["notes"] if selected is not None and n["id"] == selected["id"]]
        if previous != ([h["old_resume_note"]] if h["old_resume_note"] is not None else []):
            raise ValueError()
        if h['resume_note'] is not None:
            resume_context(h["resume_note"], source, actor)
        if h["old_resume_note"] is not None:
            resume_context(h["old_resume_note"], source, actor)
            if h['resume_note'] is not None and (h["old_resume_note"]["id"] != h["resume_note"]["id"] or not set(h["old_resume_note"]["refs"]) <= set(h["resume_note"]["refs"])):
                raise ValueError()
        if (not set(entry["source_refs"]) <= set(h["new_destination_pin"]["refs"])
                or (h['resume_note'] is not None and not set(entry["source_refs"]) <= set(h["resume_note"]["refs"]))):
            raise ValueError()
        return h
    except (KeyError, TypeError, ValueError) as exc:
        raise TransitionError("atomic Pin move history is unreadable") from exc


def apply_pin_move(snapshot: dict, request: dict, change: dict, entry: dict,
                   reviewed: ReviewedPinMove | None, permission_check, thread_check, operation_review=None) -> None:
    if set(change) != {"op", "target", "expected", "value"}:
        raise TransitionError("exact move fields are required")
    v = change["value"]
    if not isinstance(v, dict) or set(v) != {"new_target", "active_pin", "resume_note", "acceptance_basis", "action_rule_basis", "dependency_basis"}:
        raise TransitionError("move requires accepted destination and resume material")
    source, destination, actor = change["target"], v["new_target"], request["actor_id"]
    if not _nonempty(destination) or (v['resume_note'] is not None and not isinstance(v["resume_note"], dict)) or not isinstance(v["active_pin"], dict):
        raise TransitionError("destination and resume note are invalid")
    _pin(v["active_pin"], destination, actor, "active")
    guard = move_guard(snapshot, source, destination, actor, move_note_id(change, actor), v["active_pin"].get("where"),
                       operation_review=operation_review)
    if change["expected"] != guard:
        raise TransitionConflict("Pin, authority, Action Rules, purpose, dependencies or resume context changed")
    if not isinstance(reviewed, ReviewedPinMove) or reviewed.request_digest != _digest(request):
        raise TransitionDenied("exact acceptance, Action Rules and dependency review is required; it grants no permission")
    source_ops = ('update_pin', 'append_thread') + (('set_note',) if v['resume_note'] is not None else ())
    for work, operations in ((source, source_ops),
                             (destination, ("update_pin" if guard["destination_pin"] else "add_pin", "append_thread"))):
        for op in operations:
            permission_check(snapshot, work, op, actor)
    active_pin = v["active_pin"]
    _pin(active_pin, destination, actor, "active")
    if guard["destination_pin"] is None and any(p["id"] == active_pin["id"] for p in snapshot["map"]["pins"]):
        raise TransitionConflict("new destination Pin ID already exists")
    old_pin = guard["source_pin"]
    held = {**copy.deepcopy(old_pin), "mode": "held", "refs": list(dict.fromkeys([*old_pin["refs"], *request["source_refs"]]))}
    required = {"id", "works", "kind", "summary", "why", "occurred_at", "recorded_at", "actor", "source_refs", "transition_id"}
    if not isinstance(entry, dict) or not required <= set(entry) or not set(entry) <= required | {"consequence"}:
        raise TransitionError("move Thread cannot include unrelated outcomes or changes")
    # The request's refs are exact; unrelated additional Thread refs would alter
    # the history oracle and claim unreviewed evidence.
    if entry["source_refs"] != request["source_refs"]:
        raise TransitionError("move Thread sources differ from reviewed sources")
    history = {"from": source, "to": destination, "old_source_pin": old_pin, "new_source_pin": held,
               "old_destination_pin": guard["destination_pin"], "new_destination_pin": active_pin,
               "old_resume_note": guard["resume_note"], "resume_note": v["resume_note"],
               **{n:v[n] for n in ("acceptance_basis", "action_rule_basis", "dependency_basis")}, "decision_context": guard}
    encoded = json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if "consequence" in entry and entry["consequence"] != encoded:
        raise TransitionError("supplied move history differs from actual changes")
    entry = {**copy.deepcopy(entry), "consequence": encoded}
    move_history(entry)
    thread_check(snapshot, request, entry, "pin_active_moved", [source, destination])
    pins, notes = snapshot["map"]["pins"], snapshot["map"]["notes"]
    pins[pins.index(old_pin)] = held
    if guard["destination_pin"] is None:
        pins.append(copy.deepcopy(active_pin))
    else:
        pins[pins.index(guard["destination_pin"])] = copy.deepcopy(active_pin)
    if v['resume_note'] is None:
        pass  # Read/re-acceptance is never disposal of an earlier helper.
    elif guard["resume_note"] is None:
        notes.append(copy.deepcopy(v["resume_note"]))
    else:
        notes[notes.index(guard["resume_note"])] = copy.deepcopy(v["resume_note"])
    snapshot["thread_entries"].append(entry)
