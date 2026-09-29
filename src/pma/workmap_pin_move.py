"""One atomic own-actor move, preserving held work and its resume context."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

from pma.workmap_contract import ContextUnknown, _collection_status, inspect_work, require_decision_context
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest, _nonempty, _refs, _timestamp


@dataclass(frozen=True)
class ReviewedPinMove:
    """Operator reviewed acceptance, Conditions, grant constraints and dependencies.

    A bounded trust input, not Owner authentication or new permission.
    """
    request_digest: str


def move_guard(snapshot: dict, source: str, destination: str, actor: str, note_id: str,
               destination_where: str | None = None) -> dict:
    if source == destination or any(k not in snapshot["map"]["members"] for k in (source, destination)):
        raise TransitionConflict("move requires two distinct current Works")
    if any(_collection_status(snapshot, "pins", key) != "confirmed" for key in snapshot["map"]["members"]):
        raise ContextUnknown("actor's full active Pin search requires complete current Pin visibility")
    contexts = []
    for key in (source, destination):
        context = inspect_work(snapshot, key)
        require_decision_context(context)
        chain = {p["work_key"] for p in context["parent_chain"]}
        if any(_collection_status(snapshot, "conditions", p) != "confirmed" for p in chain):
            raise ContextUnknown("applicable ancestor Conditions are unconfirmed")
        locations = {p["where"] for p in context["pins"] if "where" in p}
        if key == destination and destination_where:
            locations.add(destination_where)
        contexts.append({**{n: copy.deepcopy(context[n]) for n in ("parent_chain", "pins", "permissions", "conditions", "dependencies")},
                         "incident_dependencies": copy.deepcopy([d for d in snapshot["map"]["relations"] if d["kind"] == "dependency" and key in (d["consumer"], d["provider"])]),
                         "shared_where_pins": copy.deepcopy([p for p in snapshot["map"]["pins"] if p.get("where") in locations]),
                         "ancestor_conditions": copy.deepcopy([c for c in snapshot["map"]["conditions"] if chain & set(c["targets"])]),
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


def resume_context(note: dict, source: str, actor: str) -> dict:
    try:
        data = json.loads(note["text"])
        if (not {"id", "targets", "text", "refs", "basis"} <= set(note)
                or not set(note) <= {"id", "targets", "text", "refs", "basis", "observed_at"}
                or not _nonempty(note["id"]) or note["targets"] != [source] or note["basis"] != "reported"
                or not _refs(note["refs"]) or not note["refs"]
                or ("observed_at" in note and not _timestamp(note["observed_at"]))
                or set(data) != {"actor", "return_work", "remaining", "first_step", "reusable_refs", "unsaved"}
                or data["actor"] != actor or data["return_work"] != source
                or any(not _nonempty(data[n]) for n in ("remaining", "first_step", "unsaved"))
                or not isinstance(data["reusable_refs"], list) or any(not _nonempty(ref) for ref in data["reusable_refs"])
                or not set(data["reusable_refs"]) <= set(note["refs"])):
            raise ValueError()
        return data
    except (KeyError, TypeError, ValueError) as exc:
        raise TransitionError("resume note must preserve remaining work, first step, references and unsaved scope") from exc


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
        if not isinstance(h, dict) or set(h) != {"from", "to", "old_source_pin", "new_source_pin", "old_destination_pin", "new_destination_pin", "old_resume_note", "resume_note", "acceptance_basis", "condition_basis", "dependency_basis", "decision_context"}:
            raise ValueError()
        source, destination, actor = h["from"], h["to"], entry["actor"]
        if source == destination or entry["works"] != [source, destination] or any(not _nonempty(h[n]) for n in ("acceptance_basis", "condition_basis", "dependency_basis")):
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
            fields = {"parent_chain", "pins", "permissions", "conditions", "dependencies", "incident_dependencies", "shared_where_pins", "ancestor_conditions", "notes"}
            if not isinstance(c, dict) or set(c) != fields or any(not isinstance(c[n], list) for n in fields):
                raise ValueError()
            if not c["parent_chain"] or c["parent_chain"][-1]["work_key"] != work or any(p["intent_status"] != "confirmed" or not _nonempty(p["intent"]) for p in c["parent_chain"]):
                raise ValueError()
            own = [p for p in c["pins"] if p["actor_id"] == actor]
            if (old_pin is not None and old_pin not in own) or (old_pin is None and own):
                raise ValueError()
        previous = [n for n in context["source_context"]["notes"] if n["id"] == h["resume_note"]["id"]]
        if previous != ([h["old_resume_note"]] if h["old_resume_note"] is not None else []):
            raise ValueError()
        resume_context(h["resume_note"], source, actor)
        if h["old_resume_note"] is not None:
            resume_context(h["old_resume_note"], source, actor)
            if h["old_resume_note"]["id"] != h["resume_note"]["id"] or not set(h["old_resume_note"]["refs"]) <= set(h["resume_note"]["refs"]):
                raise ValueError()
        if (not set(entry["source_refs"]) <= set(h["new_destination_pin"]["refs"])
                or not set(entry["source_refs"]) <= set(h["resume_note"]["refs"])):
            raise ValueError()
        return h
    except (KeyError, TypeError, ValueError) as exc:
        raise TransitionError("atomic Pin move history is unreadable") from exc


def apply_pin_move(snapshot: dict, request: dict, change: dict, entry: dict,
                   reviewed: ReviewedPinMove | None, permission_check, thread_check) -> None:
    if set(change) != {"op", "target", "expected", "value"}:
        raise TransitionError("exact move fields are required")
    v = change["value"]
    if not isinstance(v, dict) or set(v) != {"new_target", "active_pin", "resume_note", "acceptance_basis", "condition_basis", "dependency_basis"}:
        raise TransitionError("move requires accepted destination and resume material")
    source, destination, actor = change["target"], v["new_target"], request["actor_id"]
    if not _nonempty(destination) or not isinstance(v["resume_note"], dict) or not isinstance(v["active_pin"], dict):
        raise TransitionError("destination and resume note are invalid")
    _pin(v["active_pin"], destination, actor, "active")
    guard = move_guard(snapshot, source, destination, actor, v["resume_note"].get("id"), v["active_pin"].get("where"))
    if change["expected"] != guard:
        raise TransitionConflict("Pin, authority, Conditions, purpose, dependencies or resume context changed")
    if not isinstance(reviewed, ReviewedPinMove) or reviewed.request_digest != _digest(request):
        raise TransitionDenied("exact acceptance, Conditions and dependency review is required; it grants no permission")
    for work, operations in ((source, ("update_pin", "set_note", "append_thread")),
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
               **{n:v[n] for n in ("acceptance_basis", "condition_basis", "dependency_basis")}, "decision_context": guard}
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
    if guard["resume_note"] is None:
        notes.append(copy.deepcopy(v["resume_note"]))
    else:
        notes[notes.index(guard["resume_note"])] = copy.deepcopy(v["resume_note"])
    snapshot["thread_entries"].append(entry)
