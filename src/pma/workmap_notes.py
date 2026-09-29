"""Exact, Work-scoped current note changes with retained old/new meaning."""

import copy
import json
from datetime import datetime

from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _nonempty, _refs, _timestamp


def validate_note(note: dict, target: str) -> None:
    fields = {"id", "targets", "text", "refs", "basis"}
    if (not isinstance(note, dict) or not fields <= set(note) or not set(note) <= fields | {"observed_at"}
            or not _nonempty(note["id"]) or note["targets"] != [target] or not _nonempty(note["text"])
            or not _refs(note["refs"]) or note["basis"] not in ("fact", "reported", "proposal", "inference", "unknown")
            or ("observed_at" in note and not _timestamp(note["observed_at"]))):
        raise TransitionError("note needs one Work, explicit basis, text and sources")


def note_history(entry: dict) -> dict:
    try:
        history = json.loads(entry["consequence"])
        if not isinstance(history, dict) or set(history) != {"old_note", "new_note"} or len(entry["works"]) != 1:
            raise ValueError()
        target = entry["works"][0]
        validate_note(history["new_note"], target)
        if history["old_note"] is not None:
            validate_note(history["old_note"], target)
            if history["old_note"]["id"] != history["new_note"]["id"]:
                raise ValueError()
        if not set(entry["source_refs"]) <= set(history["new_note"]["refs"]):
            raise ValueError()
        return history
    except (KeyError, TypeError, ValueError) as exc:
        raise TransitionError("note change history is unreadable") from exc


def apply_note(snapshot, request, change, entry, permission_check, thread_check, operation_review=None):
    if set(change) != {"op", "target", "expected", "value"}:
        raise TransitionError("exact note change fields are required")
    target, actor = change["target"], request["actor_id"]
    from pma.workmap_decision_context import require_operation_context
    require_operation_context(snapshot, request, operation_review)
    # Review can establish this operation's recorded scope without claiming
    # historical completeness. Actual accepted participation is still required.
    pins = [p for p in snapshot['map']['pins'] if p['work_id'] == target]
    if not any(p["actor_id"] == actor and p["mode"] == "active" for p in pins):
        raise TransitionDenied("note writer needs its accepted active Pin on this Work")
    for operation in ("set_note", "append_thread"):
        permission_check(snapshot, target, operation, actor)
    new = change["value"]
    validate_note(new, target)
    notes = snapshot["map"]["notes"]
    existing = [n for n in notes if n["id"] == new["id"]]
    old = existing[0] if existing else None
    if len(existing) > 1 or change["expected"] != old:
        raise TransitionConflict("note differs from expected current value")
    if old is not None:
        validate_note(old, target)
        if old.get("observed_at"):
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
    thread_check(snapshot, request, entry, "note_changed", [target])
    if old is None:
        notes.append(copy.deepcopy(new))
    else:
        notes[notes.index(old)] = copy.deepcopy(new)
    snapshot["thread_entries"].append(entry)
