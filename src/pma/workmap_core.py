"""Experimental P1 Core slices for sourced corrections and later rationale.

Each narrowly defined Transition publishes its explanation and receipt in one
Store snapshot. This is not a general Work Map writer or an identity boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone

from pma.workmap_store import LocalTextStore, StoreConflict
from pma.workmap_contract import SnapshotInvalid, inspect_work, require_decision_context, validate_snapshot


class TransitionError(Exception):
    """The requested Work transition cannot be committed."""


class UnsupportedTransition(TransitionError):
    """The experimental Core does not implement this operation."""


class TransitionConflict(TransitionError):
    """The request's relevant prior state is no longer current."""


class TransitionDenied(TransitionError):
    """The committed Permission set does not allow this operation."""


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _refs(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(_nonempty(item) for item in value)


def _origin(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"summary", "refs"}
        and (value["summary"] is None or _nonempty(value["summary"]))
        and _refs(value["refs"])
    )


def _digest(value: dict) -> str:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TransitionError("transition is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _timestamp(value: object) -> bool:
    if not _nonempty(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _matching_receipt(value: object, transition_id: str, request_hash: str) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"transition_id", "request_digest", "revision", "recorded_at", "result"}
        and value.get("transition_id") == transition_id
        and value.get("request_digest") == request_hash
        and _nonempty(value.get("revision"))
        and _timestamp(value.get("recorded_at"))
        and value.get("result") == "applied"
    )


class WorkMapCore:
    """Controlled P1 entry for permitted origin correction or later rationale.

    The caller must already have established the actor's identity. All writers
    in this trial must use this Core; direct Store access is outside its claim.
    """

    def __init__(self, store: LocalTextStore, *, reviewed_decision_context=None):
        self.store = store
        self.reviewed_decision_context = reviewed_decision_context

    def lookup_transition(self, transition_id: str) -> dict:
        """Read a retained receipt and its Thread from one committed root.

        ``unknown`` does not prove that a request was never submitted. The
        caller is responsible for authorizing access to this project snapshot.
        """
        if not _nonempty(transition_id):
            raise TransitionError("transition_id is required")
        try:
            snapshot = validate_snapshot(self.store.read().snapshot)
        except SnapshotInvalid as exc:
            raise TransitionError("committed transition evidence is incomplete") from exc
        receipts = snapshot.get("receipts")
        entries = snapshot.get("thread_entries")
        revision = snapshot.get("revision")
        if not isinstance(receipts, dict) or not isinstance(entries, list) or not _nonempty(revision):
            raise TransitionError("committed transition index is unavailable")
        matching = [
            entry for entry in entries
            if isinstance(entry, dict) and entry.get("transition_id") == transition_id
        ]
        if transition_id not in receipts:
            if matching:
                raise TransitionError("Thread exists without its receipt")
            return {"status": "unknown", "observed_revision": revision}
        receipt = receipts[transition_id]
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"transition_id", "request_digest", "revision", "recorded_at", "result"}
            or receipt.get("transition_id") != transition_id
            or not isinstance(receipt.get("request_digest"), str)
            or len(receipt["request_digest"]) != 64
            or any(ch not in "0123456789abcdef" for ch in receipt["request_digest"])
            or not _nonempty(receipt.get("revision"))
            or not _timestamp(receipt.get("recorded_at"))
            or receipt.get("result") != "applied"
            or any(entry.get("recorded_at") != receipt["recorded_at"] for entry in matching)
        ):
            raise TransitionError("committed transition evidence is incomplete")
        return {
            "status": "applied",
            "observed_revision": revision,
            "receipt": copy.deepcopy(receipt),
            "thread_entries": copy.deepcopy(matching),
        }

    def apply(self, transition: dict) -> dict:
        if not isinstance(transition, dict):
            raise TransitionError("transition must be an object")
        request_hash = _digest(transition)
        transition_id = transition.get("transition_id")
        if not _nonempty(transition_id):
            raise TransitionError("transition_id is required")

        attempts = self._attempt_limit(transition)
        for attempt in range(attempts):
            committed = self.store.read()
            try:
                snapshot = validate_snapshot(committed.snapshot)
            except SnapshotInvalid as exc:
                if "Permission" in str(exc):
                    raise TransitionDenied("committed Permission is unreadable") from exc
                raise TransitionError("committed snapshot is unreadable") from exc
            receipts = snapshot.get("receipts")
            if not isinstance(receipts, dict):
                raise TransitionError("committed receipts are unavailable")
            if transition_id in receipts:
                prior = receipts[transition_id]
                if not _matching_receipt(prior, transition_id, request_hash):
                    raise TransitionConflict("transition_id was used for different content")
                return copy.deepcopy(prior)

            prior_entry_count = len(snapshot["thread_entries"])
            self._validate_and_apply(snapshot, transition)
            if self.reviewed_decision_context is not None:
                from pma.workmap_decision_context import retain_context_assessment
                for entry in snapshot['thread_entries'][prior_entry_count:]:
                    retain_context_assessment(entry, self.reviewed_decision_context)
            snapshot["revision"] = uuid.uuid4().hex
            snapshot["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for entry in snapshot["thread_entries"][prior_entry_count:]:
                entry["recorded_at"] = snapshot["recorded_at"]
            receipt = {
                "transition_id": transition_id,
                "request_digest": request_hash,
                "revision": snapshot["revision"],
                "recorded_at": snapshot["recorded_at"],
                "result": "applied",
            }
            receipts[transition_id] = receipt
            validate_snapshot(snapshot)
            self._before_commit(transition)
            try:
                self.store.commit_validated(committed.token, snapshot)
            except StoreConflict as exc:
                latest = validate_snapshot(self.store.read().snapshot).get("receipts", {}).get(transition_id)
                if _matching_receipt(latest, transition_id, request_hash):
                    return copy.deepcopy(latest)
                if attempt + 1 < attempts:
                    continue
                raise TransitionConflict("committed root changed; retry bound reached") from exc
            return copy.deepcopy(receipt)

    def _before_commit(self, transition: dict) -> None:
        """Subclass check for external premises immediately before publication."""
        if self.reviewed_decision_context is not None:
            from pma.workmap_decision_context import verify_context_sources
            verify_context_sources(self.reviewed_decision_context)

    def _attempt_limit(self, transition: dict) -> int:
        return 1

    def _validate_and_apply(self, snapshot: dict, transition: dict) -> None:
        if set(transition) != {
            "format_version", "project_id", "transition_id", "actor_id", "base_revision",
            "intent", "source_refs", "preconditions", "changes", "thread_entries",
        }:
            raise TransitionError("unexpected or missing transition fields")
        if transition.get("format_version") != "0.4.1" or snapshot.get("format_version") != "0.4.1":
            raise UnsupportedTransition("unsupported format")
        if transition.get("project_id") != snapshot.get("project_id"):
            raise TransitionConflict("project identity differs")
        if transition.get("base_revision") != snapshot.get("revision"):
            raise TransitionConflict("base revision changed")
        if not _nonempty(transition.get("actor_id")) or not _nonempty(transition.get("intent")):
            raise TransitionError("actor and intent are required")
        if not _refs(transition.get("source_refs")):
            raise TransitionError("transition needs a source reference")
        if transition.get("preconditions") != []:
            raise UnsupportedTransition("only a single exact-value guard is supported")
        changes = transition.get("changes")
        entries = transition.get("thread_entries")
        if not isinstance(changes, list) or len(changes) != 1 or not isinstance(entries, list) or len(entries) != 1:
            raise UnsupportedTransition("one supported change and one Thread entry are required")
        change, entry = changes[0], entries[0]
        if not isinstance(change, dict):
            raise TransitionError("change must be an object")
        operation = change.get("op")
        if operation == "update_work":
            if set(change) != {"op", "target", "expected", "value"}:
                raise TransitionError("unexpected change fields")
            if not isinstance(change.get("value"), dict) or set(change["value"]) != {"origin"} or not _origin(change["value"]["origin"]):
                raise UnsupportedTransition("only a sourced origin correction is supported")
            expected_kind = "correction"
        elif operation == "append_thread":
            if set(change) != {"op", "target"}:
                raise TransitionError("unexpected rationale change fields")
            expected_kind = "evidence_recorded" if isinstance(entry, dict) and entry.get("kind") == "evidence_recorded" else "rationale_added"
        else:
            raise UnsupportedTransition("operation is not supported")
        work_key = change.get("target")
        works = snapshot.get("works")
        current_map = snapshot.get("map")
        if not isinstance(works, dict) or not isinstance(current_map, dict) or work_key not in works:
            raise TransitionConflict("target Work is unavailable")
        members = current_map.get("members")
        if not isinstance(members, list) or work_key not in members:
            raise TransitionConflict("target Work is not current")
        work = works[work_key]
        if not isinstance(work, dict) or work.get("work_key") != work_key:
            raise TransitionConflict("Work identity changed")
        # A sourced rationale or Origin correction can be recorded for a Work
        # whose own Intent is known even when a parent Intent awaits recovery.
        # Placement and completion changes still require the full parent chain.
        from pma.workmap_decision_context import require_operation_context
        require_operation_context(snapshot, transition, self.reviewed_decision_context,
                                  needs_pin=False, needs_parent_intent=False)
        if operation == "update_work":
            if change.get("expected") != {"origin": work.get("origin")}:
                raise TransitionConflict("Work origin changed")
            if change["value"]["origin"] == work["origin"]:
                raise TransitionError("correction does not change the origin")
            if not set(transition["source_refs"]).issubset(change["value"]["origin"]["refs"]):
                raise TransitionError("corrected origin omits the request source")
        permissions = current_map.get("permissions")
        if not isinstance(permissions, list):
            raise TransitionDenied("Permission set is unavailable")
        if any(
            not isinstance(permission, dict)
            or not isinstance(permission.get("targets"), list)
            or not isinstance(permission.get("operations"), list)
            for permission in permissions
        ):
            raise TransitionDenied("Permission set is malformed")
        applicable = [
            permission for permission in permissions
            if isinstance(permission, dict)
            and work_key in permission.get("targets", [])
            and operation in permission.get("operations", [])
            and permission.get("grantee") == transition["actor_id"]
        ]
        if any(permission.get("effect") != "allow" for permission in applicable) or not applicable:
            raise TransitionDenied("transition is not permitted")
        if any(not _refs(permission.get("source_refs")) or not _nonempty(permission.get("grantor_ref")) for permission in applicable):
            raise TransitionDenied("Permission provenance is incomplete")
        if not isinstance(entry, dict) or set(entry) != {
            "id", "works", "kind", "summary", "why", "occurred_at",
            "recorded_at", "actor", "source_refs", "transition_id",
        } or not _nonempty(entry.get("id")) or entry.get("kind") != expected_kind:
            raise TransitionError("a matching Thread entry is required")
        if entry.get("works") != [work_key] or entry.get("actor") != transition["actor_id"] or entry.get("transition_id") != transition["transition_id"]:
            raise TransitionError("Thread attribution differs")
        if not _nonempty(entry.get("summary")) or not _nonempty(entry.get("why")) or not _refs(entry.get("source_refs")):
            raise TransitionError("Thread reason and source are required")
        if entry.get("occurred_at") is not None and not _nonempty(entry["occurred_at"]):
            raise TransitionError("Thread occurrence time is invalid")
        if not _timestamp(entry.get("recorded_at")):
            raise TransitionError("Thread recorded time is required")
        if not set(transition["source_refs"]).issubset(entry["source_refs"]):
            raise TransitionError("Thread omits the request source")
        if not isinstance(snapshot.get("thread_entries"), list) or any(
            isinstance(old, dict) and old.get("id") == entry["id"] for old in snapshot["thread_entries"]
        ):
            raise TransitionConflict("Thread identity is unavailable or reused")
        if operation == "update_work":
            entry = {**copy.deepcopy(entry), "consequence": json.dumps(
                {"old_origin": work["origin"], "new_origin": change["value"]["origin"]},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"))}
            work["origin"] = copy.deepcopy(change["value"]["origin"])
        snapshot["thread_entries"].append(copy.deepcopy(entry))
