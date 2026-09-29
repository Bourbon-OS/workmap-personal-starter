"""Gate a cooperating review runtime after canonical acceptance registration.

This does not intercept arbitrary tool calls or authenticate Owner/runtime IDs.
The coordinator and reviewer must use this gate before substantive work.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pma.workmap_canonical_core import ReviewedDelegation, validate_review_scope
from pma.workmap_contract import inspect_work, require_decision_context, validate_snapshot
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest
from pma.workmap_owner_scope import limited_review_context


def _bound_file(repo: Path, binding: dict, prefix: str, suffix: str) -> Path:
    if not isinstance(binding, dict) or set(binding) != {"ref", "sha256"}:
        raise TransitionDenied("evidence binding is invalid")
    ref = binding["ref"]
    if not isinstance(ref, str) or not ref.startswith(prefix) or not ref.endswith(suffix):
        raise TransitionDenied("evidence is outside the delegated review boundary")
    path = (repo / ref[len("repo:"):]).resolve()
    if path.parent != (repo / "docs/work-management/evidence").resolve():
        raise TransitionDenied("evidence path escapes the evidence directory")
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise TransitionDenied("delegation evidence is unreadable") from exc
    if actual != binding["sha256"]:
        raise TransitionDenied("delegation evidence differs from reviewed bytes")
    return path


def review_delegation(request: dict, repo: Path) -> ReviewedDelegation:
    """Bind bytes after operator review of real acceptance or completed result."""
    changes = request.get("changes") if isinstance(request, dict) else None
    if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict):
        raise TransitionDenied("one delegation operation must be reviewed")
    change = changes[0]
    value = change.get("value")
    if not isinstance(value, dict):
        raise TransitionDenied("delegation value is unreadable")
    prefix = "repo:docs/work-management/evidence/"
    if change.get("op") == "finish_delegation":
        binding = value.get("result_source")
        _bound_file(repo, binding, prefix + "delegation_result_", ".md")
        return ReviewedDelegation(_digest(request), (binding["ref"],))
    if change.get("op") != "start_delegation":
        raise TransitionDenied("local delegation review requires start or finish")
    validate_review_scope(value.get("scope"))
    sources = value.get("owner_sources")
    if not isinstance(sources, list) or not sources:
        raise TransitionDenied("Owner delegation scope is missing")
    for binding in sources:
        _bound_file(repo, binding, prefix + "owner_", ".txt")
    binding = value.get("acceptance")
    path = _bound_file(repo, binding, prefix + "delegation_acceptance_", ".json")
    try:
        acceptance = json.loads(path.read_text(encoding="utf-8"))
        pin = value["pin"]
        expected = {
            "status": "accepted", "actor_id": pin["actor_id"],
            "coordinator": request["actor_id"], "work_id": change["target"],
            "scope": value["scope"],
        }
        if (not isinstance(acceptance, dict)
                or set(acceptance) != set(expected) | {"runtime_id", "response"}
                or any(acceptance[key] != item for key, item in expected.items())
                or not isinstance(acceptance["runtime_id"], str) or not acceptance["runtime_id"].strip()
                or not isinstance(acceptance["response"], str) or not acceptance["response"].strip()):
            raise TransitionDenied("actual acceptance must match actor, Work, and exact scope")
    except (ValueError, KeyError, TypeError) as exc:
        raise TransitionDenied("actual acceptance evidence is unreadable") from exc
    return ReviewedDelegation(_digest(request), tuple([*(source["ref"] for source in sources), binding["ref"]]))


def check_review_gate(store, request: dict, actor_id: str, repo: Path) -> dict:
    """Read back one committed registration and file versions before release.

    Caller must keep the reviewer waiting until ready, then supply this exact
    registration to the reviewer for its own check. Changes require re-evaluation.
    """
    review_delegation(request, repo)
    change = request["changes"][0]
    if change["op"] != "start_delegation":
        raise TransitionDenied("review gate requires an acceptance registration")
    value = change["value"]
    if value["pin"]["actor_id"] != actor_id:
        raise TransitionDenied("release actor differs from accepted reviewer")
    committed = store.read()
    state = validate_snapshot(committed.snapshot)
    if state["metadata"].get("store_role") != "canonical_current":
        raise TransitionDenied("review gate needs the declared canonical Store")
    receipt = state["receipts"].get(request["transition_id"])
    if (not isinstance(receipt, dict) or receipt["request_digest"] != _digest(request)
            or receipt["result"] != "applied" or state["revision"] != receipt["revision"]):
        raise TransitionConflict("registration is absent, different, or no longer the current revision")
    if 'coordinator_scope' in value:
        context = limited_review_context(state, change['target'], request['actor_id'], value['coordinator_scope'])
    else:
        context = inspect_work(state, change["target"])
        require_decision_context(context)
    if value["pin"] not in context["pins"] or value["permission"] not in context["permissions"]:
        raise TransitionDenied("accepted active Pin or exact review Permission is absent")
    matching = [entry for entry in state["thread_entries"] if entry.get("transition_id") == request["transition_id"]]
    if len(matching) != 1 or matching[0]["kind"] != "delegation_accepted":
        raise TransitionDenied("registration Thread is absent")
    history = {"coordinator": request["actor_id"], **value}
    if matching[0].get("consequence") != json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":")):
        raise TransitionDenied("registration Thread scope differs")
    if any(item["effect"] != "allow" for item in context["permissions"]
           if item.get("grantee") == actor_id and "review" in item["operations"]):
        raise TransitionDenied("review is denied or unconfirmed")
    for item in value["scope"]["files"]:
        path = (repo / item["path"]).resolve()
        if not path.is_relative_to(repo.resolve()):
            raise TransitionDenied("review file escapes the repository")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TransitionDenied("review file is unreadable") from exc
        if actual != item["sha256"]:
            raise TransitionConflict("review file changed; re-evaluate the approved scope")
    if store.read().token != committed.token:
        raise TransitionConflict("canonical Store changed during release check")
    return {"status": "ready", "actor_id": actor_id, "work_id": change["target"],
            "revision": state["revision"], "registration": request["transition_id"],
            "scope": value["scope"]}
