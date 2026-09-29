"""Controlled P1 transitions for a canonical local Work Map snapshot.

The physical Store remains the sole current value. This module deliberately
rejects unsupported operations and incomplete decision context. Actor identity
must be established by the caller; an actor_id string is not authentication.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from pma.workmap_contract import ContextUnknown, inspect_work, require_decision_context, validate_snapshot, confirm_new_work_collections, address_matches, current_subtree, subtree_addresses
from pma.workmap_birth_confirmation import apply_birth_confirmation
from pma.workmap_owner_permission import validate_owner_permission, verify_permission_sources
from pma.workmap_owner_scope import ReviewedOwnerScope, apply_owner_scope, limited_review_context
from pma.workmap_pin_move import ReviewedPinMove, apply_pin_move
from pma.workmap_owner_add import ReviewedOwnerAdd, validate_owner_add, verify_owner_add_sources, is_root_add
from pma.workmap_reapply import requests_reapplication, check_reapplication
from pma.workmap_notes import apply_note
from pma.workmap_resume import retain_close_context, closed_identity
from pma.workmap_close import verify_close, verify_evidence, repository_evidence, bind_close_basis, review_close
from pma.workmap_core import (
    TransitionConflict,
    TransitionDenied,
    TransitionError,
    UnsupportedTransition,
    WorkMapCore,
    _digest,
    _nonempty,
    _refs,
    _timestamp,
)


def _permission(snapshot: dict, work_key: str, operation: str, actor: str) -> None:
    applicable = [item for item in snapshot["map"]["permissions"] if work_key in item["targets"] and operation in item["operations"] and item.get("grantee") == actor]
    if not applicable or any(item["effect"] != "allow" for item in applicable):
        raise TransitionDenied("operation is not permitted for this actor and Work")


def _thread(snapshot: dict, transition: dict, entry: dict, kind: str, works: list[str]) -> None:
    required = {"id", "works", "kind", "summary", "why", "occurred_at", "recorded_at", "actor", "source_refs", "transition_id"}
    optional = {"evidence_refs", "assumptions", "consequence", "revisit", "related_entries", "outcome", "successor", "residuals"}
    if not isinstance(entry, dict) or not required <= set(entry) or not set(entry) <= required | optional:
        raise TransitionError("Thread fields are missing or unexpected")
    if not isinstance(entry, dict) or entry.get("kind") != kind or entry.get("works") != works or entry.get("actor") != transition["actor_id"] or entry.get("transition_id") != transition["transition_id"]:
        raise TransitionError("a matching Thread entry is required")
    if not _nonempty(entry.get("id")) or not _nonempty(entry.get("summary")) or not _nonempty(entry.get("why")) or not _refs(entry.get("source_refs")) or not _timestamp(entry.get("recorded_at")) or (entry["occurred_at"] is not None and not _timestamp(entry["occurred_at"])):
        raise TransitionError("Thread reason, source, or time is missing")
    if not set(transition["source_refs"]).issubset(entry["source_refs"]):
        raise TransitionError("Thread omits the transition source")
    if any(item.get("id") == entry["id"] for item in snapshot["thread_entries"]):
        raise TransitionConflict("Thread ID has already been used")


def _parent_map(snapshot: dict) -> dict[str, str]:
    return {item["child"]: item["parent"] for item in snapshot["map"]["relations"] if item["kind"] == "parent"}


def _register_work(snapshot, transition, change, entry, owner_review):
    current, works = snapshot['map'], snapshot['works']
    members, actor, target = current['members'], transition['actor_id'], change['target']
    root = is_root_add(change)
    value = change.get("value")
    if not isinstance(value, dict) or set(value) != ({"work", "display_id", "parent"} if root else {"work", "display_id"}) or not isinstance(value["work"], dict):
        raise TransitionError("new Work and display ID are required")
    work = value["work"]
    key, display_id = work.get("work_key"), value["display_id"]
    parent_id = None if root else snapshot["display_ids"][target]
    address_ok = address_matches(parent_id, display_id)
    if not _nonempty(key) or key in snapshot["display_ids"] or not _nonempty(display_id) or display_id in snapshot["aliases"] or not address_ok:
        raise TransitionConflict("new Work identity or child address is unavailable")
    if not _nonempty(work.get("name")) or not _nonempty(work.get("intent")) or not isinstance(work.get("origin"), dict) or not _nonempty(work["origin"].get("summary")) or not _refs(work["origin"].get("refs")) or not set(transition["source_refs"]).issubset(work["origin"]["refs"]):
        raise TransitionError("new Work meaning or Origin is unconfirmed")
    if entry is not None:
        if entry.get('kind') not in ('work_added', 'rationale_added'):
            raise TransitionError('Work registration only accepts an optional rationale or legacy technical record')
        _thread(snapshot, transition, entry, entry['kind'], ([key] if root else [key, target]))
    if entry is None or entry['kind'] == 'rationale_added':
        technical = {
            'id': 'registration:' + transition['transition_id'],
            'works': ([key] if root else [key, target]), 'kind': 'work_added',
            'summary': 'Work registered', 'why': 'Apply the authorized add_work request',
            'occurred_at': None, 'recorded_at': datetime.now(timezone.utc).isoformat(),
            'actor': actor, 'source_refs': copy.deepcopy(transition['source_refs']),
            'transition_id': transition['transition_id'],
        }
        if owner_review is not None:
            technical['consequence'] = json.dumps(owner_review.detail, ensure_ascii=False, sort_keys=True)
        _thread(snapshot, transition, technical, 'work_added', ([key] if root else [key, target]))
        if entry is not None and technical['id'] == entry['id']:
            raise TransitionConflict('registration and rationale IDs collide')
        snapshot['thread_entries'].append(technical)
    works[key] = copy.deepcopy(work)
    snapshot["aliases"][display_id] = key
    snapshot["display_ids"][key] = display_id
    members.append(key)
    if not root:
        current["relations"].append({"id": f"parent:{key}", "kind": "parent", "child": key, "parent": target})
    confirm_new_work_collections(snapshot, key)


@dataclass(frozen=True)
class ReviewedOwnerHandoff:
    """Caller's separate review of an exact request and attributed sources.

    This is a local trust input, not Owner authentication. Never construct it
    merely because a transition contains an Owner-shaped source_ref.
    """

    request_digest: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class ReviewedOwnerIntent:
    """Separate local review of attribution, same Work, and exact change scope.

    The reviewer must read the evidence and assess purpose continuity. A label,
    hash, or this trust input does not authenticate Owner or prove semantics.
    """

    request_digest: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class ReviewedDelegation:
    """Local review of exact Owner scope and actual runtime acceptance/result.

    Evidence bytes and this input do not authenticate the actor. The caller
    must observe the responding runtime context, not infer acceptance from
    dispatch, and must observe completion before releasing its Pin.
    """

    request_digest: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class ReviewedDependencyDisposition:
    """Exact local review of Owner scope or dependency fulfillment evidence.

    The caller must assess attribution and whether the cited results fulfill
    this requirement. Hashes bind versions, not identity or semantic truth.
    """

    request_digest: str
    source_refs: tuple[str, ...]


def _evidence_binding(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == {"ref", "sha256"}
            and _nonempty(value["ref"]) and isinstance(value["sha256"], str)
            and len(value["sha256"]) == 64
            and all(ch in "0123456789abcdef" for ch in value["sha256"]))


def validate_review_scope(scope: object) -> None:
    """A bounded read-only question against exact repository file versions."""
    if (not isinstance(scope, dict) or set(scope) != {"mode", "model", "question", "files"}
            or scope["mode"] != "read_only" or not _nonempty(scope["model"])
            or not _nonempty(scope["question"]) or not isinstance(scope["files"], list)
            or not scope["files"]):
        raise TransitionError("a bounded read-only review scope is required")
    paths = []
    for item in scope["files"]:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not _evidence_binding({"ref": item.get("path"), "sha256": item.get("sha256")})):
            raise TransitionError("review file version is invalid")
        path = item["path"]
        if (path.startswith(("/", "\\")) or "\\" in path or ":" in path
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise TransitionError("review file must be a repository-relative path")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise TransitionError("review file scope is duplicated")


def _dependency_disposition(snapshot: dict, transition: dict, change: dict, entry: dict,
                            review: ReviewedDependencyDisposition | None, *, authorize: bool) -> None:
    target, actor = change["target"], transition["actor_id"]
    if target not in snapshot["map"]["members"]:
        raise TransitionConflict("dependency consumer Work is not current")
    context = inspect_work(snapshot, target)
    require_decision_context(context)
    expected, value = change["expected"], change["value"]
    if (not isinstance(expected, dict) or set(expected) != {"dependency", "pins", "permissions", "conditions"}
            or not isinstance(expected["dependency"], dict) or not isinstance(value, dict)):
        raise TransitionError("exact dependency and decision context are required")
    for name in ("pins", "permissions", "conditions"):
        if expected[name] != context[name]:
            raise TransitionConflict("dependency decision context changed")
    dependency = expected["dependency"]
    matches = [item for item in snapshot["map"]["relations"] if item["id"] == dependency.get("id")]
    if matches != [dependency]:
        raise TransitionConflict("dependency differs from the expected current value")
    if dependency.get("kind") != "dependency" or dependency.get("consumer") != target:
        raise TransitionDenied("only the consumer's exact Dependency may be disposed")
    if not any(pin["actor_id"] == actor and pin["mode"] == "active" for pin in context["pins"]):
        raise TransitionDenied("dependency disposition needs the consumer actor's active Pin")
    _permission(snapshot, target, "append_thread", actor)
    scope = ["dependency_id:" + dependency["id"], "dependency_sha256:" + _digest(dependency)]
    if authorize:
        if set(value) != {"permission", "owner_sources"}:
            raise TransitionError("dependency authorization fields are invalid")
        sources = value["owner_sources"]
        permission = value["permission"]
        if any(item["effect"] != "allow" for item in context["permissions"]
               if item.get("grantee") == actor and "remove_relation" in item["operations"]):
            raise TransitionDenied("recorded denial or unknown removal authority must be resolved")
        kind = "dependency_disposition_authorized"
    else:
        if set(value) != {"outcome", "fulfillment_basis", "evidence"} or value["outcome"] != "fulfilled" or not _nonempty(value["fulfillment_basis"]):
            raise TransitionDenied("only evidenced fulfillment is supported for dependency removal")
        sources = value["evidence"]
        permission = next((item for item in context["permissions"] if item["id"] == change["permission_id"]), None)
        _permission(snapshot, target, "remove_relation", actor)
        if entry.get("why") != value["fulfillment_basis"]:
            raise TransitionError("Thread must preserve the exact fulfillment basis")
        kind = "dependency_fulfilled"
    if not isinstance(sources, list) or not sources or any(not _evidence_binding(item) for item in sources):
        raise TransitionDenied("versioned dependency authority or fulfillment evidence is missing")
    refs = tuple(item["ref"] for item in sources)
    if (len(refs) != len(set(refs)) or not set(refs).issubset(transition["source_refs"])
            or not isinstance(review, ReviewedDependencyDisposition)
            or review.request_digest != _digest(transition) or review.source_refs != refs):
        raise TransitionDenied("exact dependency disposition needs separate local evidence review")
    if (not isinstance(permission, dict)
            or set(permission) != {"id", "targets", "operations", "grantee", "effect", "constraints", "source_refs", "grantor_ref"}
            or not _nonempty(permission["id"]) or permission["targets"] != [target]
            or permission["operations"] != ["remove_relation"] or permission["grantee"] != actor
            or permission["effect"] != "allow" or permission["grantor_ref"] != "principal:owner"
            or permission["constraints"] != scope or not _refs(permission["source_refs"])):
        raise TransitionDenied("an exact one-dependency Owner grant is required")
    if not set(dependency["refs"]).issubset(transition["source_refs"]) or not set(permission["source_refs"]).issubset(transition["source_refs"]):
        raise TransitionError("Thread sources must retain dependency and authority provenance")
    prior_authorizations = []
    for recorded in snapshot["thread_entries"]:
        if recorded["kind"] == "dependency_disposition_authorized":
            history = json.loads(recorded["consequence"])
            if history["permission"]["id"] == permission["id"]:
                prior_authorizations.append((recorded, history))
    if authorize:
        if permission["source_refs"] != list(refs):
            raise TransitionDenied("grant must retain the exact reviewed Owner sources")
        if prior_authorizations or any(item["id"] == permission["id"] for item in snapshot["map"]["permissions"]):
            raise TransitionConflict("dependency grant ID has already been used")
        history = {"dependency": dependency, **copy.deepcopy(value)}
    else:
        if (len(prior_authorizations) != 1 or prior_authorizations[0][0]["actor"] != actor
                or prior_authorizations[0][1]["dependency"] != dependency
                or prior_authorizations[0][1]["permission"] != permission):
            raise TransitionDenied("removal needs its previously committed exact authorization")
        history = {"dependency": dependency, "permission": permission,
                   "authorization": prior_authorizations[0][0]["transition_id"], **copy.deepcopy(value)}
    if entry.get("consequence") != json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":")):
        raise TransitionError("Thread must retain exact dependency, authority, evidence, and disposition")
    _thread(snapshot, transition, entry, kind, [target, dependency["provider"]])
    if authorize:
        snapshot["map"]["permissions"].append(copy.deepcopy(permission))
    else:
        snapshot["map"]["relations"].remove(dependency)
        snapshot["map"]["permissions"].remove(permission)


def _delegation(snapshot: dict, transition: dict, change: dict, entry: dict,
                review: ReviewedDelegation | None, *, finish: bool) -> None:
    target, actor = change["target"], transition["actor_id"]
    if target not in snapshot["map"]["members"]:
        raise TransitionConflict("delegation Work is not current")
    value = change['value']
    if not isinstance(value, dict):
        raise TransitionError('delegation value is unreadable')
    scope_thread = value.get('coordinator_scope')
    if finish:
        original = next((e for e in snapshot['thread_entries'] if e.get('transition_id') == value.get('registration')
                         and e['kind'] == 'delegation_accepted'), None)
        if original is not None:
            scope_thread = json.loads(original['consequence']).get('coordinator_scope')
    if scope_thread is not None:
        context = limited_review_context(snapshot, target, actor, scope_thread)
        expected = {name: snapshot['map'][name] for name in ('pins', 'permissions', 'conditions')}
    else:
        context = inspect_work(snapshot, target)
        require_decision_context(context)
        expected = {name: context[name] for name in ('pins', 'permissions', 'conditions')}
    if change["expected"] != expected:
        raise TransitionConflict("delegation Pin, Permission, or Condition changed")
    if not any(pin["actor_id"] == actor and pin["mode"] == "active" for pin in context["pins"]):
        raise TransitionDenied("coordinator needs its own active Pin on this Work")
    if scope_thread is None:
        for operation in ("add_pin", "remove_pin", "append_thread"):
            _permission(snapshot, target, operation, actor)
    value = change["value"]
    if not isinstance(value, dict):
        raise TransitionError("delegation value is unreadable")
    if finish:
        if set(value) != {"registration", "result_source", "outcome"} or value["outcome"] not in ("review_completed", "cancelled"):
            raise TransitionError("delegation finish needs a result and disposition")
        registration = next((item for item in snapshot["thread_entries"]
                             if item.get("transition_id") == value["registration"]
                             and item["kind"] == "delegation_accepted"), None)
        if registration is None or registration["actor"] != actor or registration["works"] != [target]:
            raise TransitionDenied("only the original coordinator may finish this delegation")
        try:
            history = json.loads(registration["consequence"])
            pin, permission = history["pin"], history["permission"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TransitionError("delegation registration is unreadable") from exc
        if (pin not in context["pins"] or permission not in context["permissions"]
                or not _evidence_binding(value["result_source"])):
            raise TransitionConflict("delegated Pin/Permission changed or result is missing")
        refs = (value["result_source"]["ref"],)
        history = {**copy.deepcopy(value), "pin": pin, "permission": permission}
        kind = "delegation_finished"
    else:
        fields = {"pin", "permission", "scope", "owner_sources", "acceptance"}
        if set(value) not in (fields, fields | {'coordinator_scope'}):
            raise TransitionError("delegation registration fields are invalid")
        validate_review_scope(value["scope"])
        sources = value["owner_sources"]
        if (not isinstance(sources, list) or not sources
                or any(not _evidence_binding(item) for item in sources)
                or not _evidence_binding(value["acceptance"])):
            raise TransitionDenied("Owner scope and actual acceptance evidence are required")
        refs = tuple([*(item["ref"] for item in sources), value["acceptance"]["ref"]])
        pin, permission = value["pin"], value["permission"]
        if (not isinstance(pin, dict)
                or set(pin) != {"id", "work_id", "actor_id", "mode", "pinned_at", "confirmed_at", "refs"}
                or not _nonempty(pin["id"]) or pin["work_id"] != target
                or not _nonempty(pin["actor_id"]) or pin["actor_id"] == actor
                or pin["mode"] != "active" or not _timestamp(pin["pinned_at"])
                or pin["confirmed_at"] != pin["pinned_at"]
                or not _refs(pin["refs"]) or not set(refs).issubset(pin["refs"])):
            raise TransitionDenied("exact accepted reviewer Pin is required")
        if any(item["id"] == pin["id"] or item["actor_id"] == pin["actor_id"] for item in snapshot["map"]["pins"]):
            raise TransitionConflict("reviewer context already has a Pin or Pin ID was used")
        if (not isinstance(permission, dict)
                or set(permission) != {"id", "targets", "operations", "grantee", "effect", "constraints", "source_refs", "grantor_ref"}
                or not _nonempty(permission["id"]) or permission["targets"] != [target]
                or permission["grantee"] != pin["actor_id"] or permission["effect"] != "allow"
                or permission["operations"] != ["review"]
                or permission["constraints"] != ["read_only", "scope_sha256:" + _digest(value["scope"])]
                or permission["source_refs"] != [item["ref"] for item in sources]
                or permission["grantor_ref"] != "principal:owner"):
            raise TransitionDenied("delegation grants only the exact read-only review")
        if any(item["id"] == permission["id"] or item.get("grantee") == pin["actor_id"] for item in snapshot["map"]["permissions"]):
            raise TransitionConflict("reviewer already has Permission or Permission ID was used")
        history = {"coordinator": actor, **copy.deepcopy(value)}
        kind = "delegation_accepted"
    if (len(refs) != len(set(refs)) or not set(refs).issubset(transition["source_refs"])
            or not isinstance(review, ReviewedDelegation)
            or review.request_digest != _digest(transition) or review.source_refs != refs):
        raise TransitionDenied("delegation needs separate exact local review of runtime evidence")
    if entry.get("consequence") != json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":")):
        raise TransitionError("Thread must retain exact delegation scope, evidence, and disposition")
    _thread(snapshot, transition, entry, kind, [target])
    if finish:
        snapshot["map"]["pins"].remove(pin)
        snapshot["map"]["permissions"].remove(permission)
    else:
        snapshot["map"]["pins"].append(copy.deepcopy(pin))
        snapshot["map"]["permissions"].append(copy.deepcopy(permission))


def _intent_update(snapshot: dict, transition: dict, change: dict, entry: dict,
                   review: ReviewedOwnerIntent | None) -> None:
    target = change["target"]
    if target not in snapshot["map"]["members"]:
        raise TransitionConflict("target Work is not current")
    work = snapshot["works"][target]
    context = inspect_work(snapshot, target)
    # Bind even incomplete collections without asserting their completeness.
    permissions = [item for item in snapshot["map"]["permissions"] if target in item["targets"]]
    conditions = [item for item in snapshot["map"]["conditions"] if target in item["targets"]]
    expected = {
        "intent": work["intent"], "permissions": permissions, "conditions": conditions,
        "permission_status": context["permission_status"],
        "condition_status": context["condition_status"],
    }
    if change["expected"] != expected:
        raise TransitionConflict("Intent or correction authority/context changed")
    value = change["value"]
    if (not isinstance(value, dict) or set(value) != {"intent", "intent_change"}
            or not _nonempty(value["intent"]) or value["intent"] == work["intent"]):
        raise TransitionError("a classified, sourced new Intent is required")
    detail = value["intent_change"]
    if (not isinstance(detail, dict)
            or set(detail) != {"kind", "reason", "same_work_basis", "owner_sources"}
            or not _nonempty(detail["reason"]) or not _nonempty(detail["same_work_basis"])):
        raise TransitionError("Intent change classification and continuity basis are required")
    kind = detail["kind"]
    if kind == "material_change":
        raise TransitionDenied("material_change requires disposition of the old Work and creation of a new Work")
    if kind not in ("confirmation", "correction", "refinement"):
        raise TransitionError("Intent change kind is unsupported")
    if (work["intent"] is None) != (kind == "confirmation"):
        raise TransitionError("unknown Intent needs confirmation; known Intent needs correction or refinement")
    sources = detail["owner_sources"]
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(item, dict) or set(item) != {"ref", "sha256"}
                   or not _nonempty(item["ref"])
                   or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                   or any(ch not in "0123456789abcdef" for ch in item["sha256"])
                   for item in sources)):
        raise TransitionDenied("Intent Owner evidence is missing or invalid")
    refs = tuple(item["ref"] for item in sources)
    if (len(set(refs)) != len(refs) or not set(refs).issubset(transition["source_refs"])
            or not isinstance(review, ReviewedOwnerIntent)
            or review.request_digest != _digest(transition) or review.source_refs != refs):
        raise TransitionDenied("exact Intent update needs separate local review of Owner and same-purpose evidence")
    if any(item["effect"] != "allow" and "update_work" in item["operations"] for item in permissions):
        raise TransitionDenied("recorded denial or unknown authority must be resolved before Intent update")
    if kind != "confirmation":
        if context["permission_status"] != "confirmed" or context["condition_status"] != "confirmed":
            raise ContextUnknown("Permission or Condition for Intent revision is unconfirmed")
        _permission(snapshot, target, "update_work", transition["actor_id"])
    # Only first confirmation may use the exact reviewed Owner decision as
    # authority in an incomplete migration. It creates no general Permission,
    # clears no Condition, and permits no other operation on this Work.
    source_map = snapshot["metadata"].get("work_sources", {})
    if not isinstance(source_map, dict) or not isinstance(source_map.get(target, {}), dict):
        raise TransitionError("existing Work provenance is unreadable")
    old_sources = source_map.get(target, {}).get("intent", [])
    if not isinstance(old_sources, list) or any(not _nonempty(ref) for ref in old_sources):
        raise TransitionError("existing Intent provenance is unreadable")
    history = {
        **copy.deepcopy(detail), "old_intent": work["intent"], "new_intent": value["intent"],
        "old_source_refs": copy.deepcopy(old_sources),
    }
    recorded_history = json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if entry.get("consequence") != recorded_history or entry.get("why") != detail["reason"]:
        raise TransitionError("Thread must retain the exact old/new Intent, kind, reason, continuity basis, and evidence")
    _thread(snapshot, transition, entry, f"intent_{kind}", [target])
    work["intent"] = value["intent"]
    sources_by_work = snapshot["metadata"].setdefault("work_sources", {})
    sources_by_work.setdefault(target, {})["intent"] = list(refs)


def _handoff(snapshot: dict, transition: dict, change: dict, entry: dict,
             review: ReviewedOwnerHandoff | None) -> None:
    expected, value = change["expected"], change["value"]
    if (not isinstance(expected, dict) or set(expected) != {"pin", "permission", "conditions"}
            or not isinstance(value, dict) or set(value) != {"to_actor", "owner_sources"}):
        raise TransitionError("handoff prior values or receiving scope fields are invalid")
    target = change["target"]
    if target not in snapshot["map"]["members"]:
        raise TransitionConflict("handoff Work is not current")
    context = inspect_work(snapshot, target)
    # Administrative transfer preserves the known Work's purpose and scope.
    # Parent Intent is not inferred and purpose-dependent operations stay gated.
    require_decision_context(context, needs_parent_intent=False)
    if context["conditions"] != expected["conditions"]:
        raise TransitionConflict("handoff Conditions differ from expected values")
    sources = value["owner_sources"]
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(item, dict) or set(item) != {"ref", "sha256"}
                   or not _nonempty(item["ref"])
                   or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                   or any(ch not in "0123456789abcdef" for ch in item["sha256"])
                   for item in sources)):
        raise TransitionDenied("handoff Owner evidence is missing or invalid")
    refs = tuple(item["ref"] for item in sources)
    if (len(set(refs)) != len(refs) or not set(refs).issubset(transition["source_refs"])
            or not isinstance(review, ReviewedOwnerHandoff)
            or review.request_digest != _digest(transition) or review.source_refs != refs):
        raise TransitionDenied("exact handoff needs separate local review of Owner evidence")
    pins, permissions = snapshot["map"]["pins"], snapshot["map"]["permissions"]
    old_pin, old_permission = expected["pin"], expected["permission"]
    if not isinstance(old_pin, dict) or not isinstance(old_permission, dict):
        raise TransitionConflict("handoff requires exact current Pin and Permission")
    matches = [item for item in pins if item["id"] == old_pin.get("id")]
    grants = [item for item in permissions if item["id"] == old_permission.get("id")]
    if matches != [old_pin] or grants != [old_permission]:
        raise TransitionConflict("handoff Pin or Permission differs from expected current value")
    receiving_actor = value["to_actor"]
    if (old_pin["work_id"] != target or old_pin["mode"] != "active"
            or old_permission["targets"] != [target]
            or old_permission.get("grantee") != old_pin["actor_id"]
            or old_permission["effect"] != "allow"
            or old_permission["grantor_ref"] != "principal:owner"
            or not _nonempty(receiving_actor) or receiving_actor == old_pin["actor_id"]
            or transition["actor_id"] != receiving_actor):
        raise TransitionDenied("handoff must transfer the same Owner scope to the receiving actor")
    if not set(old_permission["source_refs"]).issubset(refs):
        raise TransitionDenied("existing Owner grant evidence must also be reviewed")
    if not set(refs).difference(old_permission["source_refs"]):
        raise TransitionDenied("a separate Owner handoff source is required")
    if any(item["actor_id"] == receiving_actor and item["mode"] == "active" for item in pins):
        raise TransitionConflict("receiving actor already has an active Pin")
    _thread(snapshot, transition, entry, "handoff", [target])
    new_pin, new_permission = copy.deepcopy(old_pin), copy.deepcopy(old_permission)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_pin.update(actor_id=receiving_actor, pinned_at=now, confirmed_at=now)
    new_pin["refs"] = list(dict.fromkeys([*old_pin["refs"], *refs]))
    new_permission["grantee"] = receiving_actor
    new_permission["source_refs"] = list(dict.fromkeys([*old_permission["source_refs"], *refs]))
    pins[pins.index(old_pin)] = new_pin
    permissions[permissions.index(old_permission)] = new_permission


class CanonicalWorkMapCore(WorkMapCore):
    """A narrow writer; incomplete state blocks ordinary Map changes.

    Explicitly reviewed Owner instructions have separate additive repair paths.
    """

    def __init__(self, store, *, reviewed_owner_handoff: ReviewedOwnerHandoff | None = None,
                 reviewed_owner_intent: ReviewedOwnerIntent | None = None,
                 reviewed_delegation: ReviewedDelegation | None = None,
                 reviewed_dependency_disposition: ReviewedDependencyDisposition | None = None,
                 reviewed_owner_scope: ReviewedOwnerScope | None = None,
                 reviewed_pin_move: ReviewedPinMove | None = None,
                 reviewed_owner_add: ReviewedOwnerAdd | None = None,
                 reviewed_owner_permission=None,
                 reviewed_birth_confirmation=None,
                 reviewed_decision_context=None,
                 reviewed_close=None, close_evidence_resolver=repository_evidence):
        super().__init__(store, reviewed_decision_context=reviewed_decision_context)
        self.reviewed_owner_handoff = reviewed_owner_handoff
        self.reviewed_owner_intent = reviewed_owner_intent
        self.reviewed_delegation = reviewed_delegation
        self.reviewed_dependency_disposition = reviewed_dependency_disposition
        self.reviewed_owner_scope = reviewed_owner_scope
        self.reviewed_pin_move = reviewed_pin_move
        self.reviewed_owner_add = reviewed_owner_add
        self.reviewed_owner_permission = reviewed_owner_permission
        self.reviewed_birth_confirmation = reviewed_birth_confirmation
        self.reviewed_close = reviewed_close
        self.close_evidence_resolver = close_evidence_resolver

    def _before_commit(self, transition):
        super()._before_commit(transition)
        if self.reviewed_birth_confirmation is not None:
            verify_permission_sources(self.reviewed_birth_confirmation)
        if self.reviewed_owner_permission is not None:
            verify_permission_sources(self.reviewed_owner_permission)
        if self.reviewed_owner_add is not None:
            verify_owner_add_sources(self.reviewed_owner_add)
        if any(c.get('op') == 'close' for c in transition['changes']):
            verify_evidence(transition, self.close_evidence_resolver)

    def _close_batch(self, snapshot, transition):
        """Validate a bounded disposition on a private candidate; publish once.

        The provisional receipt below is only for validating intermediate raw
        references. It never reaches the Store; apply() creates the sole receipt.
        """
        changes, entries = transition['changes'], transition['thread_entries']
        if (not isinstance(entries, list) or any(not isinstance(c, dict) for c in changes)
                or changes[-1].get('op') != 'close'
                or any(c.get('op') not in ('reparent', 'remove_pin', 'close') for c in changes)):
            raise UnsupportedTransition('close batch only supports reparent, own Pin removal and close')
        verify_close(snapshot, transition, self.reviewed_close, self.close_evidence_resolver)
        for entry in entries:
            _thread(snapshot, transition, entry, entry.get('kind'), entry.get('works'))
        parent = changes[-1]['target']
        parents = _parent_map(snapshot)
        def in_scope(key):
            while key != parent and key in parents:
                key = parents[key]
            return key == parent
        required = [(c['op'], c.get('target')) for c in changes if c['op'] != 'remove_pin']
        if len(set(required)) != len(required):
            raise TransitionError('duplicate semantic operation in close batch')
        if len(entries) != len(required):
            raise TransitionError('close batch needs exactly the reparent and close reasons')
        chosen = []
        # Every operation uses authority already present at the original root.
        for change in changes:
            target, op = change.get('target'), change['op']
            if target not in snapshot['map']['members'] or not in_scope(target):
                raise TransitionDenied('batch operation is outside the closing Work subtree')
            require_decision_context(inspect_work(snapshot, target))
            _permission(snapshot, target, op, transition['actor_id'])
            if op == 'reparent' and change.get('new_parent') is not None:
                destination = change['new_parent']
                if not _nonempty(destination):
                    raise TransitionError('reparent destination is unreadable')
                if destination not in snapshot['map']['members']:
                    raise TransitionConflict('reparent destination is not current')
                require_decision_context(inspect_work(snapshot, destination))
                _permission(snapshot, destination, op, transition['actor_id'])
            if op == 'remove_pin':
                pin = next((p for p in snapshot['map']['pins'] if p['id'] == change.get('pin_id')), None)
                if pin is None or pin['actor_id'] != transition['actor_id']:
                    raise TransitionDenied('batch cannot release another actor Pin')
                close_entry = next(e for e in entries if e.get('kind') == 'close' and e.get('works') == [parent])
                entry = {k: copy.deepcopy(close_entry[k]) for k in (
                    'id', 'works', 'kind', 'summary', 'why', 'occurred_at', 'recorded_at', 'actor', 'source_refs', 'transition_id')}
                entry.update(id=close_entry['id'] + ':release:' + pin['id'], works=[target], kind='pin_removed',
                             summary='closeに伴う本人Pinの解除', why='確認済みclose要求に含まれる本人の関与終了')
            else:
                matches = [e for e in entries if e.get('kind') == op and e.get('works') == [target]]
                if len(matches) != 1:
                    raise TransitionError('each reparent or close needs its corresponding reason')
                entry = copy.deepcopy(matches[0])
            chosen.append(entry)
        initial_count = len(snapshot['thread_entries'])
        tid = transition['transition_id']
        snapshot['receipts'][tid] = {'transition_id': tid, 'request_digest': _digest(transition),
            'revision': snapshot['revision'], 'recorded_at': snapshot['recorded_at'], 'result': 'applied'}
        for change, entry in zip(changes, chosen):
            entry['recorded_at'] = snapshot['recorded_at']
            sub = {**copy.deepcopy(transition), 'changes': [copy.deepcopy(change)],
                   'thread_entries': [entry], 'preconditions': []}
            core = CanonicalWorkMapCore(self.store, close_evidence_resolver=self.close_evidence_resolver)
            if change['op'] == 'close':
                retained = transition['preconditions'][0]['expected'].get('retained_dependencies', [])
                sub = bind_close_basis(snapshot, sub, resolver=self.close_evidence_resolver,
                    retained_dependency_ids=[r['id'] for r in retained if r['provider'] == change['target']])
                core.reviewed_close = review_close(sub)  # derived only from the verified complete batch
            core._validate_and_apply(snapshot, sub)
        for entry in snapshot['thread_entries'][initial_count:]:
            if entry['kind'] == 'close':
                audit = json.loads(entry['assumptions'])
                audit['close_basis'] = copy.deepcopy(transition['preconditions'][0])
                entry['assumptions'] = json.dumps(audit, ensure_ascii=False, sort_keys=True)
        del snapshot['receipts'][tid]

    def _attempt_limit(self, transition: dict) -> int:
        return 4 if requests_reapplication(transition) else 1

    @staticmethod
    def _confirm_pin(snapshot: dict, transition: dict, change: dict) -> None:
        """Record the caller's actual validity confirmation, not a heartbeat.

        The authenticated local caller must have assessed the source evidence.
        A timestamp, source reference or receipt alone does not prove validity.
        No new storage fields or meaning Thread are needed for this operation.
        """
        if set(change) != {"op", "target", "pin_id", "expected", "value"}:
            raise TransitionError("Pin confirmation fields are missing or unexpected")
        target = change["target"]
        if target not in snapshot["map"]["members"]:
            raise TransitionConflict("confirmation target is not current")
        context = inspect_work(snapshot, target)
        require_decision_context(context)
        actor = transition["actor_id"]
        _permission(snapshot, target, "update_pin", actor)
        old = next((p for p in snapshot["map"]["pins"] if p["id"] == change["pin_id"]), None)
        if old is None or old["work_id"] != target or old != change["expected"]:
            raise TransitionConflict("Pin differs from the expected current value")
        if old["actor_id"] != actor:
            raise TransitionDenied("another actor's validity cannot be confirmed through this route")
        new = change["value"]
        if (not isinstance(new, dict) or set(new) != set(old)
                or any(new[key] != value for key, value in old.items() if key != "confirmed_at")):
            raise TransitionError("only confirmed_at may change without a meaning Thread")
        value = new.get("confirmed_at")
        if not _timestamp(value):
            raise TransitionError("validity confirmation time is invalid")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed <= datetime.fromisoformat(old["confirmed_at"].replace("Z", "+00:00")):
            raise TransitionError("validity confirmation time must advance")
        snapshot["map"]["pins"][snapshot["map"]["pins"].index(old)] = copy.deepcopy(new)

    def _validate_and_apply(self, snapshot: dict, transition: dict) -> None:
        if self.reviewed_decision_context is not None:
            from pma.workmap_decision_context import require_operation_context
            # Verify even for a supplied review on an unsupported/batch request.
            # No derived request inherits it.
            require_operation_context(snapshot, transition, self.reviewed_decision_context,
                                      needs_pin=False, needs_parent_intent=False)
        if not isinstance(transition, dict) or set(transition) != {
            "format_version", "project_id", "transition_id", "actor_id", "base_revision",
            "intent", "source_refs", "preconditions", "changes", "thread_entries",
        }:
            raise TransitionError("transition fields are missing or unexpected")
        if transition["format_version"] != "0.4.1" or transition["project_id"] != snapshot["project_id"]:
            raise TransitionConflict("format or project identity differs")
        reapplication = requests_reapplication(transition)
        if reapplication:
            check_reapplication(self.store, snapshot, transition)
        elif transition["base_revision"] != snapshot["revision"]:
            raise TransitionConflict("base revision changed; re-evaluate relevant current values")
        if not _nonempty(transition["actor_id"]) or not _nonempty(transition["intent"]) or not _refs(transition["source_refs"]) or not transition["source_refs"]:
            raise TransitionError("actor, change Intent, or source is missing")
        if isinstance(transition['changes'], list) and len(transition['changes']) > 1:
            self._close_batch(snapshot, transition)
            return
        is_close = (isinstance(transition['changes'], list) and len(transition['changes']) == 1
                    and isinstance(transition['changes'][0], dict) and transition['changes'][0].get('op') == 'close')
        if transition["preconditions"] != [] and not reapplication and not is_close:
            raise UnsupportedTransition("declared preconditions are not implemented")
        changes, entries = transition["changes"], transition["thread_entries"]
        if (isinstance(changes, list) and len(changes) == 1 and isinstance(changes[0], dict)
                and changes[0].get("op") == "update_pin" and entries == []):
            self._confirm_pin(snapshot, transition, changes[0])
            return
        adding = (isinstance(changes, list) and len(changes) == 1
                  and isinstance(changes[0], dict) and changes[0].get('op') == 'add_work')
        if not isinstance(changes, list) or len(changes) != 1 or not isinstance(entries, list) or len(entries) not in ((0, 1) if adding else (1,)) or not isinstance(changes[0], dict):
            raise UnsupportedTransition("one semantic change and one Thread entry are required")
        change, entry = changes[0], entries[0] if entries else None
        operation, target = change.get("op"), change.get("target")
        if not _nonempty(target):
            raise TransitionError("change target is missing")
        if operation == "set_note":
            validate_snapshot(snapshot)
            apply_note(snapshot, transition, change, entry, _permission, _thread, self.reviewed_decision_context)
            return
        if operation == "move_active_pin":
            validate_snapshot(snapshot)
            apply_pin_move(snapshot, transition, change, entry, self.reviewed_pin_move, _permission, _thread)
            return
        if operation in ("accept_owner_scope", "renew_owner_scope", "append_scoped_thread"):
            validate_snapshot(snapshot)
            apply_owner_scope(snapshot, transition, change, entry, self.reviewed_owner_scope, _thread)
            return
        if operation == "append_thread" or (operation == "update_work" and isinstance(change.get("value"), dict) and set(change["value"]) == {"origin"}):
            if operation == 'append_thread' and self.reviewed_birth_confirmation is not None:
                validate_snapshot(snapshot)
                apply_birth_confirmation(snapshot, transition, self.reviewed_birth_confirmation, self.store, _thread)
                return
            WorkMapCore._validate_and_apply(self, snapshot, transition)
            return
        supported = {"transfer_condition", "authorize_dependency_disposition", "remove_relation", "start_delegation", "finish_delegation", "handoff", "update_work", "add_work", "add_pin", "update_pin", "remove_pin", "set_permission", "remove_permission", "set_condition", "remove_condition", "add_relation", "reparent", "close", "reopen"}
        if operation not in supported:
            raise UnsupportedTransition("operation is not implemented")
        expected_fields = {
            "transfer_condition": {"op", "target", "expected", "value"},
            "authorize_dependency_disposition": {"op", "target", "expected", "value"},
            "remove_relation": {"op", "target", "permission_id", "expected", "value"},
            "start_delegation": {"op", "target", "expected", "value"},
            "finish_delegation": {"op", "target", "expected", "value"},
            "handoff": {"op", "target", "expected", "value"},
            "update_work": {"op", "target", "expected", "value"},
            "add_work": {"op", "target", "value"},
            "add_pin": {"op", "target", "value"},
            "update_pin": {"op", "target", "pin_id", "expected", "value"},
            "remove_pin": {"op", "target", "pin_id", "expected"},
            "set_permission": {"op", "target", "permission_id", "expected", "value"},
            "remove_permission": {"op", "target", "permission_id", "expected"},
            "set_condition": {"op", "target", "condition_id", "expected", "value"},
            "remove_condition": {"op", "target", "condition_id", "expected"},
            "add_relation": {"op", "target", "value"},
            "reparent": {"op", "target", "expected_parent", "new_parent", "new_display_id"},
            "close": {"op", "target"},
            "reopen": {"op", "target", "parent"},
        }
        if set(change) != expected_fields[operation]:
            raise TransitionError("change fields are missing or unexpected")
        validate_snapshot(snapshot)
        current = snapshot["map"]
        members = current["members"]
        works = snapshot["works"]
        actor = transition["actor_id"]
        if operation == "transfer_condition":
            value, expected = change.get("value"), change.get("expected")
            if (not isinstance(entry, dict) or not isinstance(value, dict) or set(value) != {"new_target", "placement_basis", "placement_refs"}
                    or not isinstance(expected, dict) or set(expected) != {"condition", "source_context", "destination_context"}):
                raise TransitionError("Condition transfer scope and prior context are required")
            destination = value["new_target"]
            if target not in members or destination not in members or target == destination:
                raise TransitionConflict("Condition transfer requires distinct current source and destination Works")
            if (not _nonempty(value["placement_basis"]) or not _refs(value["placement_refs"])
                    or not value["placement_refs"] or not set(value["placement_refs"]).issubset(transition["source_refs"])
                    or entry.get("why") != value["placement_basis"]):
                raise TransitionError("Condition placement needs a reviewed scope basis and traceable sources")
            contexts = {key: inspect_work(snapshot, key) for key in (target, destination)}
            for key, name in ((target, "source_context"), (destination, "destination_context")):
                context = contexts[key]
                require_decision_context(context)
                guarded = {field: context[field] for field in ("pins", "permissions", "conditions")}
                if expected[name] != guarded:
                    raise TransitionConflict("Condition transfer context differs from the expected value")
            if not any(pin["actor_id"] == actor and pin["mode"] == "active" for pin in contexts[target]["pins"]):
                raise TransitionDenied("Condition transfer needs the actor's own active source Pin")
            for key, grant in ((target, "remove_condition"), (target, "append_thread"), (destination, "set_condition")):
                _permission(snapshot, key, grant, actor)
            old = expected["condition"]
            if not isinstance(old, dict) or old.get("targets") != [target]:
                raise TransitionError("only one source Work's exclusive Condition can be transferred")
            matches = [item for item in current["conditions"] if item["id"] == old.get("id")]
            if matches != [old]:
                raise TransitionConflict("Condition differs from the expected current value")
            if not set(old["refs"]).issubset(transition["source_refs"]):
                raise TransitionError("Condition transfer omits the original source")
            new = copy.deepcopy(old)
            new["targets"] = [destination]
            history = {"old_condition": old, "new_condition": new, "from_work": target, "to_work": destination}
            if entry.get("consequence") != json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":")):
                raise TransitionError("Condition transfer needs exact old/new scope history")
            _thread(snapshot, transition, entry, "condition_transferred", [target, destination])
            current["conditions"][current["conditions"].index(matches[0])] = new
        elif operation in ("authorize_dependency_disposition", "remove_relation"):
            _dependency_disposition(snapshot, transition, change, entry, self.reviewed_dependency_disposition,
                                    authorize=operation == "authorize_dependency_disposition")
        elif operation in ("start_delegation", "finish_delegation"):
            _delegation(snapshot, transition, change, entry, self.reviewed_delegation,
                        finish=operation == "finish_delegation")
        elif operation == "handoff":
            _handoff(snapshot, transition, change, entry, self.reviewed_owner_handoff)
        elif operation == "update_work":
            value = change.get("value")
            if isinstance(value, dict) and set(value) == {"name"}:
                if target not in members:
                    raise TransitionConflict("target Work is not current")
                require_decision_context(inspect_work(snapshot, target))
                _permission(snapshot, target, "update_work", actor)
                if change["expected"] != {"name": works[target]["name"]}:
                    raise TransitionConflict("Work name differs from expected value")
                if not _nonempty(value["name"]) or value["name"] == works[target]["name"]:
                    raise TransitionError("new Work name must be nonempty and changed")
                _thread(snapshot, transition, entry, "work_renamed", [target])
                entry = {**copy.deepcopy(entry), "consequence": json.dumps(
                    {"old_name": works[target]["name"], "new_name": value["name"]},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"))}
                works[target]["name"] = value["name"]
            else:
                _intent_update(snapshot, transition, change, entry, self.reviewed_owner_intent)
        elif operation == 'add_work' and is_root_add(change):
            if self.reviewed_owner_add is None:
                raise TransitionDenied('root registration requires exact Owner review')
            validate_owner_add(snapshot, transition, self.reviewed_owner_add)
            _register_work(snapshot, transition, change, entry, self.reviewed_owner_add)
        elif operation == "reopen":
            if target in members or target not in snapshot["retirements"]:
                raise TransitionConflict("Work has no confirmed prior close")
            parent = change.get("parent")
            if parent not in members:
                raise TransitionConflict("reopen Parent is not current")
            require_decision_context(inspect_work(snapshot, parent))
            identity = closed_identity(snapshot, target)
            if identity is None or not _nonempty(identity.get("intent")):
                raise ContextUnknown("reopen Work identity or Intent is unavailable from Thread")
            _permission(snapshot, parent, operation, actor)
            _thread(snapshot, transition, entry, "reopen", [target])
            works[target] = identity
            members.append(target)
            current["relations"].append({"id": f"parent:{target}", "kind": "parent", "child": target, "parent": parent})
        else:
            if target not in members:
                raise TransitionConflict("target Work is not current")
            if operation == 'add_work' and self.reviewed_owner_add is not None:
                validate_owner_add(snapshot, transition, self.reviewed_owner_add)
            elif operation == 'set_permission' and self.reviewed_owner_permission is not None:
                entry = {**entry, "consequence": validate_owner_permission(snapshot, transition, self.reviewed_owner_permission, self.reviewed_decision_context)}
            else:
                from pma.workmap_decision_context import require_operation_context
                require_operation_context(snapshot, transition, self.reviewed_decision_context,
                                          needs_pin=operation not in ("set_permission", "remove_permission"))
                _permission(snapshot, target, operation, actor)
            if operation == "add_work":
                _register_work(snapshot, transition, change, entry, self.reviewed_owner_add)
            elif operation in ("add_pin", "update_pin", "remove_pin"):
                pins = current["pins"]
                if operation == "add_pin":
                    pin = change.get("value")
                    if not isinstance(pin, dict) or pin.get("work_id") != target or pin.get("actor_id") != actor or not _refs(pin.get("refs")) or not set(transition["source_refs"]).issubset(pin["refs"]) or any(item["id"] == pin.get("id") for item in pins):
                        raise TransitionError("accepted Pin identity or evidence is missing")
                    _thread(snapshot, transition, entry, "pin_added", [target])
                    pins.append(copy.deepcopy(pin))
                else:
                    pin_id = change.get("pin_id")
                    old = next((item for item in pins if item["id"] == pin_id), None)
                    if old is None or old["work_id"] != target or old != change.get("expected"):
                        raise TransitionConflict("Pin differs from the expected current value")
                    if old["actor_id"] != actor:
                        raise TransitionDenied("another actor's Pin needs an explicit repair route")
                    _thread(snapshot, transition, entry, "pin_changed" if operation == "update_pin" else "pin_removed", [target])
                    if operation == "update_pin":
                        replacement = change.get("value")
                        if not isinstance(replacement, dict) or any(replacement.get(field) != old[field] for field in ("id", "work_id", "actor_id")):
                            raise TransitionError("Pin identity cannot change through update_pin")
                        if {key: value for key, value in replacement.items() if key != "confirmed_at"} == {key: value for key, value in old.items() if key != "confirmed_at"}:
                            raise UnsupportedTransition("freshness-only Pin updates use an empty meaning Thread list")
                        pins[pins.index(old)] = copy.deepcopy(replacement)
                    else:
                        pins.remove(old)
            elif operation in ("set_permission", "remove_permission"):
                permissions = current["permissions"]
                permission_id = change["permission_id"]
                old = next((item for item in permissions if item["id"] == permission_id), None)
                if old != change["expected"]:
                    raise TransitionConflict("Permission differs from the expected value")
                if old is not None and old["targets"] != [target]:
                    raise TransitionDenied("another Work's Permission cannot be changed")
                _thread(snapshot, transition, entry, "permission_changed", [target])
                if old is not None:
                    permissions.remove(old)
                if operation == "set_permission":
                    value = change["value"]
                    if not isinstance(value, dict) or value.get("id") != permission_id or value.get("targets") != [target] or not _refs(value.get("source_refs")) or not set(transition["source_refs"]).issubset(value["source_refs"]):
                        raise TransitionError("Permission target or source is invalid")
                    permissions.append(copy.deepcopy(value))
            elif operation in ("set_condition", "remove_condition"):
                conditions = current["conditions"]
                condition_id = change.get("condition_id")
                old = next((item for item in conditions if item["id"] == condition_id), None)
                if old != change.get("expected"):
                    raise TransitionConflict("Condition differs from the expected value")
                _thread(snapshot, transition, entry, "condition_changed", [target])
                if old is not None:
                    conditions.remove(old)
                if operation == "set_condition":
                    value = change.get("value")
                    if not isinstance(value, dict) or value.get("id") != condition_id or target not in value.get("targets", []) or not set(transition["source_refs"]).issubset(value.get("refs", [])):
                        raise TransitionError("Condition target or source is invalid")
                    conditions.append(copy.deepcopy(value))
            elif operation == "add_relation":
                relation = change.get("value")
                if not isinstance(relation, dict) or relation.get("kind") != "dependency" or relation.get("consumer") != target or any(item["id"] == relation.get("id") for item in current["relations"]):
                    raise TransitionError("only a new sourced Dependency is supported here")
                _thread(snapshot, transition, entry, "dependency_added", [target])
                current["relations"].append(copy.deepcopy(relation))
            elif operation == "reparent":
                parents = _parent_map(snapshot)
                new_parent = change.get("new_parent")
                new_id = change.get("new_display_id")
                old_id = snapshot["display_ids"][target]
                parent_id = snapshot["display_ids"].get(new_parent)
                address_ok = address_matches(parent_id, new_id)
                if parents.get(target) != change.get('expected_parent') or new_parent == parents.get(target) or (new_parent is not None and new_parent not in members) or not address_ok:
                    raise TransitionConflict('re-parent prior value, Parent, or new address is invalid')
                descendants = current_subtree(snapshot, target)
                if new_parent in descendants:
                    raise TransitionConflict('re-parent destination belongs to the moved subtree')
                from pma.workmap_decision_context import require_reparent_context
                require_reparent_context(snapshot, transition, self.reviewed_decision_context)
                if new_parent is not None:
                    require_decision_context(inspect_work(snapshot, new_parent), operation_review=self.reviewed_decision_context)
                    _permission(snapshot, new_parent, operation, actor)
                _thread(snapshot, transition, entry, 'reparent', [target])
                mapped = subtree_addresses(snapshot, target, new_id)
                if len(set(mapped.values())) != len(mapped) or any(v in snapshot['aliases'] for v in mapped.values()):
                    raise TransitionConflict('new subtree address has already been used')
                for key, replacement in mapped.items():
                    former = snapshot['display_ids'][key]
                    snapshot['display_ids'][key] = replacement
                    snapshot['aliases'][replacement] = key
                    snapshot['display_id_history'].append({'work_key': key, 'old_display_id': former, 'new_display_id': replacement, 'transition_id': transition['transition_id'], 'thread_entry_id': entry['id'], 'reason': entry['why']})
                current["relations"] = [item for item in current["relations"] if not (item["kind"] == "parent" and item["child"] == target)]
                if new_parent is not None:
                    current["relations"].append({"id": f"parent:{target}:{transition['transition_id']}", "kind": "parent", "child": target, "parent": new_parent})
            elif operation == "close":
                if works[target]["intent"] is None or works[target]["origin"]["summary"] is None:
                    raise ContextUnknown("Work Intent or Origin is unconfirmed")
                if any(item["kind"] == "parent" and item["parent"] == target for item in current["relations"]):
                    raise TransitionConflict("current Child remains")
                if any(item["work_id"] == target for item in current["pins"]):
                    raise TransitionConflict("current Pin remains")
                if any(item["kind"] == "dependency" and item["consumer"] == target for item in current["relations"]):
                    raise TransitionConflict("current Dependency needs disposition")
                if any(target in item["targets"] for item in current["conditions"]):
                    raise TransitionConflict("current Condition needs disposition")
                if any(target in item["targets"] and any(key != target for key in item["targets"])
                       for item in current["permissions"]):
                    raise TransitionConflict("shared Permission needs explicit disposition before close")
                if entry.get("outcome") not in ("completed", "stopped", "replaced") or not isinstance(entry.get("residuals"), list):
                    raise TransitionError("close outcome and residual disposition are required")
                if entry["outcome"] == "replaced" and (not _refs(entry.get("successor")) or not entry["successor"]):
                    raise TransitionError("replaced Work needs a named successor")
                _thread(snapshot, transition, entry, "close", [target])
                verify_close(snapshot, transition, self.reviewed_close, self.close_evidence_resolver)
                entry = {**copy.deepcopy(entry), 'assumptions': json.dumps({
                    'close_basis': transition['preconditions'][0],
                    'reported_assumptions': entry.get('assumptions')}, ensure_ascii=False, sort_keys=True)}
                entry = retain_close_context(snapshot, entry)
                members.remove(target)
                current["relations"] = [item for item in current["relations"] if not (item["kind"] == "parent" and item["child"] == target)]
                current["permissions"] = [item for item in current["permissions"] if target not in item["targets"]]
                completeness = snapshot["metadata"].get("completeness")
                if isinstance(completeness, dict):
                    for name in ("pins", "permissions", "conditions"):
                        scope = completeness.get(name)
                        if isinstance(scope, dict):
                            scope["confirmed_work_keys"] = [key for key in scope["confirmed_work_keys"] if key != target]
                snapshot["retirements"][target] = entry["id"]
                del works[target]
        if entry is not None:
            snapshot["thread_entries"].append(copy.deepcopy(entry))
