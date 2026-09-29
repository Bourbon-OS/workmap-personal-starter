"""Retain close-time context and expose current material for a new judgment."""

import copy
import json

from pma.workmap_contract import _collection_status, inspect_work, validate_snapshot
from pma.workmap_core import TransitionError, _nonempty
from pma.workmap_pin_move import _pin

CONTRACT = "close_context_v1"


def actor_held(snapshot, actor):
    visibility = "confirmed" if all(_collection_status(snapshot, "pins", key) == "confirmed" for key in snapshot["map"]["members"]) else "unknown"
    pins = [p for p in snapshot["map"]["pins"] if p["actor_id"] == actor and p["mode"] == "held"]
    return {"status": visibility, "pins": copy.deepcopy(pins)}


def retain_close_context(snapshot, entry):
    target = entry["works"][0]
    parent = next((r["parent"] for r in snapshot["map"]["relations"] if r["kind"] == "parent" and r["child"] == target), None)
    history = {"contract": CONTRACT, "work": target, "parent_at_close": parent,
               "identity": copy.deepcopy(snapshot["works"][target]),
               "display_id": snapshot["display_ids"][target],
               "actor_held_at_close": actor_held(snapshot, entry["actor"]),
               "reported_consequence": entry.get("consequence")}
    result = {**copy.deepcopy(entry), "consequence": json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}
    close_history(result)
    return result


def close_history(entry):
    """Old close entries remain readable, with missing context explicitly unknown."""
    try:
        history = json.loads(entry.get("consequence", ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(history, dict) or history.get("contract") != CONTRACT:
        return None
    try:
        fields = {"contract", "work", "parent_at_close", "actor_held_at_close", "reported_consequence"}
        if (set(history) not in (fields, fields | {"identity", "display_id"})
                or entry["works"] != [history["work"]]
                or (history["parent_at_close"] is not None and (not _nonempty(history["parent_at_close"]) or history["parent_at_close"] == history["work"]))
                or (history["reported_consequence"] is not None and not isinstance(history["reported_consequence"], str))):
            raise ValueError()
        if "identity" in history:
            identity = history["identity"]
            if (not isinstance(identity, dict) or set(identity) != {"work_key", "name", "intent", "origin"}
                    or identity["work_key"] != history["work"]
                    or any(not _nonempty(identity[k]) for k in ("name", "intent"))
                    or not isinstance(identity["origin"], dict)
                    or not _nonempty(identity["origin"].get("summary"))
                    or not isinstance(identity["origin"].get("refs"), list)
                    or not identity["origin"]["refs"]
                    or any(not _nonempty(r) for r in identity["origin"]["refs"])
                    or not _nonempty(history["display_id"])):
                raise ValueError()
        held = history["actor_held_at_close"]
        if (not isinstance(held, dict) or set(held) != {"status", "pins"}
                or held["status"] not in ("confirmed", "unknown") or not isinstance(held["pins"], list)):
            raise ValueError()
        identities = []
        for pin in held["pins"]:
            if not isinstance(pin, dict) or pin.get("work_id") == history["work"]:
                raise ValueError()
            _pin(pin, pin.get("work_id"), entry["actor"], "held")
            identities.append(pin["id"])
        if len(set(identities)) != len(identities):
            raise ValueError()
        return history
    except (TypeError, KeyError, ValueError) as exc:
        raise TransitionError("close context is unreadable") from exc


def closed_identity(snapshot, work_key, repo=None):
    """Recover past identity from Thread, never from a noncurrent Work copy."""
    closing = next((e for e in snapshot["thread_entries"]
                    if e["id"] == snapshot["retirements"].get(work_key)), None)
    if closing is None:
        return None
    history = close_history(closing)
    if history is not None and "identity" in history:
        return copy.deepcopy(history["identity"])
    # Old close records have no identity. A reviewed projection can supplement
    # them without rewriting the original. Missing evidence stays unknown.
    from pathlib import Path
    from pma.workmap_thread_view import meaning_thread
    view = meaning_thread(snapshot, work_key, repo=repo or Path(__file__).resolve().parents[2])
    if view["status"] == "unknown":
        return None
    for item in view["thread_entries"]:
        if closing["id"] in item["related_entries"] and "identity" in item:
            return copy.deepcopy(item["identity"])
    return None


def inspect_retired_work(snapshot, work_key):
    validate_snapshot(snapshot)
    if work_key in snapshot["map"]["members"] or work_key not in snapshot["retirements"]:
        raise KeyError(work_key)
    closing = next(e for e in snapshot["thread_entries"] if e["id"] == snapshot["retirements"][work_key])
    history = close_history(closing)
    parent = {"status": "unknown", "work_key": None}
    parent_now = None
    if history is not None:
        key = history["parent_at_close"]
        parent = {"status": "confirmed", "work_key": key}
        if key in snapshot["map"]["members"]:
            parent_now = {"status": "current", "context": inspect_work(snapshot, key)}
        elif key is not None:
            parent_now = {"status": "closed_history", "work_key": key, "display_id": snapshot["display_ids"][key]}
    held = actor_held(snapshot, closing["actor"])
    identity = closed_identity(snapshot, work_key)
    return {"status": "closed_history", "project_id": snapshot["project_id"], "revision": snapshot["revision"],
            "work": work_key, "identity": identity, "identity_status": "confirmed" if identity else "unknown",
            "display_id": snapshot["display_ids"][work_key], "close_entry": copy.deepcopy(closing),
            "parent_at_close": parent, "parent_now": parent_now, "actor": closing["actor"],
            "held_at_close": copy.deepcopy(history["actor_held_at_close"]) if history else {"status": "unknown", "pins": []},
            "held_now": {"status": held["status"], "works": [{"pin": p, "context": inspect_work(snapshot, p["work_id"])} for p in held["pins"]]},
            "notes": copy.deepcopy([n for n in snapshot["map"]["notes"] if work_key in n["targets"]]),
            "thread_entries": copy.deepcopy([e for e in snapshot["thread_entries"] if work_key in e["works"]])}
