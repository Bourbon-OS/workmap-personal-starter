"""Record a fresh Owner scope without asserting incomplete collections are empty.

This grants repository inspection and isolated tests, never ordinary Map edits.
The trusted caller reviews attribution, action_rules and acceptance separately.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass

from pma.workmap_contract import ContextUnknown, inspect_work
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest, _nonempty, _timestamp

OPERATIONS = ["review", "run_isolated_tests", "append_scoped_thread"]
CONSTRAINTS = [
    "Repository evidence and isolated synthetic P1 tests only",
    "No ordinary canonical Map edits or completeness confirmation",
    "No publication, external service, production changes or P2 success claim",
]
DEVELOP_OPERATIONS = ["review", "implement_isolated_core", "run_isolated_tests", "append_scoped_thread"]
DEVELOP_CONSTRAINTS = [
    "Repository implementation/evidence and isolated synthetic P1 tests only",
    *CONSTRAINTS[1:],
]
STORE_OPERATIONS = [*DEVELOP_OPERATIONS, "backup_store", "migrate_store", "cutover_store", "recover_store"]
STORE_CONSTRAINTS = [
    "Single Windows host; nonsynced local Store only",
    "One reviewed text-to-SQLite cutover and verified rollback/recovery; preserve all Work data and history",
    "Keep Core/adapter boundary and unknown context; no unrelated Map changes, external publication or P2 claim",
]


@dataclass(frozen=True)
class ReviewedOwnerScope:
    """Exact request reviewed by the local operator; not authentication."""

    request_digest: str
    source_refs: tuple[str, ...]


def context_guard(snapshot: dict) -> dict:
    return {**{field: copy.deepcopy(snapshot["map"][field]) for field in ("pins", "permissions", "action_rules")},
            "completeness": copy.deepcopy(snapshot["metadata"].get("completeness")),
            "purposes": {k: w["intent"] for k, w in snapshot["works"].items()},
            "parents": copy.deepcopy([r for r in snapshot["map"]["relations"] if r["kind"] == "parent"])}


def scope_history(entry: dict) -> dict:
    try:
        history = json.loads(entry["consequence"])
        basic = {"pin", "permission", "owner_sources", "prior_context"}
        if set(history) not in (basic, basic | {"from_scope", "resume_note"}):
            raise ValueError()
        pin, grant, sources = history["pin"], history["permission"], history["owner_sources"]
        prior = history["prior_context"]
        # This is a validation/evaluation copy of an immutable historical
        # record, never a legacy spelling accepted by a current writer.
        if isinstance(prior, dict) and 'conditions' in prior:
            if 'action_rules' in prior:
                raise ValueError()
            history = copy.deepcopy(history)
            prior = history['prior_context']
            prior['action_rules'] = prior.pop('conditions')
            complete = prior.get('completeness')
            if isinstance(complete, dict) and 'conditions' in complete:
                if 'action_rules' in complete:
                    raise ValueError()
                complete['action_rules'] = complete.pop('conditions')
        if (set(prior) != {"pins", "permissions", "action_rules", "completeness", "purposes", "parents"}
                or any(not isinstance(prior[n], list) for n in ("pins", "permissions", "action_rules"))
                or not isinstance(prior["purposes"], dict) or not isinstance(prior["parents"], list)
                or set(pin) != {"id", "work_id", "actor_id", "mode", "pinned_at", "confirmed_at", "refs"}
                or set(grant) != {"id", "targets", "grantee", "effect", "operations", "constraints", "grantor_ref", "source_refs"}
                or not _nonempty(pin["id"]) or not _nonempty(grant["id"])
                or not _timestamp(pin["pinned_at"]) or not _timestamp(pin["confirmed_at"])
                or len(entry["works"]) != 1 or pin["actor_id"] != entry["actor"]
                or pin["work_id"] != entry["works"][0] or pin["mode"] != "active"
                or grant["targets"] != entry["works"] or grant["grantee"] != entry["actor"]
                or grant["effect"] != "allow"
                or (grant["operations"], grant["constraints"]) not in (
                    (OPERATIONS, CONSTRAINTS), (DEVELOP_OPERATIONS, DEVELOP_CONSTRAINTS), (STORE_OPERATIONS, STORE_CONSTRAINTS))
                or grant["grantor_ref"] != "principal:owner"
                or not isinstance(sources, list) or not sources
                or any(set(s) != {"ref", "sha256"} or not _nonempty(s["ref"])
                       or re.fullmatch(r"[0-9a-f]{64}", s["sha256"]) is None for s in sources)
                or grant["source_refs"] != [s["ref"] for s in sources]
                or not set(grant["source_refs"]) <= set(entry["source_refs"])
                or not set(grant["source_refs"]) <= set(pin["refs"])):
            raise ValueError()
        return history
    except (KeyError, TypeError, ValueError) as exc:
        raise TransitionError("Owner scope history is unreadable") from exc


def limited_review_context(snapshot: dict, target: str, actor: str, scope_thread: str) -> dict:
    """Known records for an exact fresh Owner-approved review, not full context.

    The caller must separately validate the reviewed delegation request and its
    actual acceptance. Existing owner scope alone never grants delegation.
    """
    if not isinstance(scope_thread, str) or not scope_thread:
        raise TransitionDenied('exact coordinator scope is required')
    check_scope(snapshot, target, actor, scope_thread)
    context = inspect_work(snapshot, target)
    for name in ('pins', 'permissions', 'action_rules'):
        context[name] = copy.deepcopy([item for item in snapshot['map'][name]
            if (item['work_id'] == target if name == 'pins' else target in item['targets'])])
    return context


def _current_scope_binding(snapshot: dict, target: str, actor: str, scope_thread: str):
    """Resolve the same current Pin/grant; this is not permission to reuse a review."""
    context = inspect_work(snapshot, target)
    if target not in snapshot["map"]["members"] or any(p["intent_status"] != "confirmed" for p in context["parent_chain"]):
        raise ContextUnknown("scope requires a current Work and known purpose chain")
    entries = [e for e in snapshot["thread_entries"] if e["id"] == scope_thread and e["kind"] in ("owner_scope_accepted", "owner_scope_renewed")]
    if len(entries) != 1 or entries[0]["works"] != [target] or entries[0]["actor"] != actor:
        raise TransitionDenied("exact committed Owner scope is required")
    history = scope_history(entries[0])
    pin, grant = history["pin"], history["permission"]
    if pin not in snapshot["map"]["pins"] or grant not in snapshot["map"]["permissions"]:
        raise TransitionDenied("Owner scope Pin or Permission changed")
    return context, history, entries[0]


def check_scope(snapshot: dict, target: str, actor: str, scope_thread: str) -> dict:
    context, history, entry = _current_scope_binding(snapshot, target, actor, scope_thread)
    pin, grant = history["pin"], history["permission"]
    chain = {p["work_key"] for p in context["parent_chain"]}
    scope_index = snapshot["thread_entries"].index(entry)
    if any(e["kind"] in {"permission_changed", "handoff"} and chain & set(e["works"])
           for e in snapshot["thread_entries"][scope_index + 1:]):
        # Revoke/regrant may restore identical bytes. Retained meaning history
        # still invalidates this earlier authority, even when values match.
        raise ContextUnknown("authority changed after scope acceptance; review it again")
    prior = history["prior_context"]
    parents = {r["child"]: r["parent"] for r in prior["parents"]}
    old_chain, key = [], target
    while key not in old_chain:
        old_chain.append(key)
        if key not in parents:
            break
        key = parents[key]
    if (old_chain[::-1] != [p["work_key"] for p in context["parent_chain"]]
            or any(prior["purposes"].get(p["work_key"]) != p["intent"] for p in context["parent_chain"])):
        raise ContextUnknown("Work purpose or parent chain changed; review the limited scope again")
    relevant = lambda action_rules: [c for c in action_rules if chain & set(c["targets"])]
    if relevant(snapshot["map"]["action_rules"]) != relevant(history["prior_context"]["action_rules"]):
        raise ContextUnknown("recorded applicable Action Rules changed; review the limited scope again")
    if any(p["actor_id"] == actor and p["mode"] == "active" and p != pin for p in snapshot["map"]["pins"]):
        raise TransitionConflict("another recorded active Pin exists for this actor")
    if any(target in p["targets"] and p.get("grantee") == actor and p["effect"] != "allow"
           and set(p["operations"]) & set(grant["operations"]) for p in snapshot["map"]["permissions"]):
        raise TransitionDenied("recorded denial or unknown scope authority remains")
    return {"status": "ready_limited", "pin": copy.deepcopy(pin), "permission": copy.deepcopy(grant),
            "collection_status": {name: context[f"{name}_status"] for name in ("pin", "permission", "action_rule")},
            "scope": copy.deepcopy(grant["constraints"])}


def apply_owner_scope(snapshot: dict, transition: dict, change: dict, entry: dict,
                      reviewed: ReviewedOwnerScope | None, thread_validator) -> None:
    target, actor = change["target"], transition["actor_id"]
    context = inspect_work(snapshot, target)
    if target not in snapshot["map"]["members"] or any(p["intent_status"] != "confirmed" for p in context["parent_chain"]):
        raise ContextUnknown("scope requires a current Work and known purpose chain")
    if set(change) != {"op", "target", "expected", "value"} or change["expected"] != context_guard(snapshot):
        raise TransitionConflict("exact stored collections and completeness guard are required")
    value = change["value"]
    if change["op"] == "append_scoped_thread":
        if not isinstance(value, dict) or set(value) != {"scope_thread"}:
            raise TransitionError("only a committed scope reference may be supplied")
        scope = check_scope(snapshot, target, actor, value["scope_thread"])
        if not set(scope["permission"]["source_refs"]) <= set(transition["source_refs"]):
            raise TransitionError("scope result omits Owner provenance")
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if kind not in ("scoped_rationale_added", "rationale_added", "evidence_recorded"):
            raise TransitionError("scope result must declare rationale, evidence or unresolved legacy meaning")
        thread_validator(snapshot, transition, entry, kind, [target])
        snapshot["thread_entries"].append(copy.deepcopy(entry))
        return
    renewing = change["op"] == "renew_owner_scope"
    fields = {"pin", "permission", "owner_sources"} | ({"scope_thread"} if renewing else set())
    switching = isinstance(value, dict) and 'from_scope' in value
    if switching:
        if renewing:
            raise TransitionDenied('scope renewal cannot switch Works')
        fields |= {'from_scope', 'resume_note'}
    if not isinstance(value, dict) or set(value) != fields:
        raise TransitionError("exact accepted Pin and limited Owner Permission are required")
    if reviewed is None or reviewed.request_digest != _digest(transition):
        raise TransitionDenied("Owner attribution, scope and actor acceptance need separate review")
    history = scope_history(entry)
    if ({k: history[k] for k in ("pin", "permission", "owner_sources")} != {k: value[k] for k in ("pin", "permission", "owner_sources")}
            or history["prior_context"] != change["expected"]
            or tuple(s["ref"] for s in value["owner_sources"]) != reviewed.source_refs):
        raise TransitionDenied("scope history or reviewed Owner sources differ")
    thread_validator(snapshot, transition, entry, "owner_scope_renewed" if renewing else "owner_scope_accepted", [target])
    pin, grant = value["pin"], value["permission"]
    old_source_pin = None
    if switching:
        from pma.workmap_pin_move import resume_context
        # scope_history already validates the existing exact review/development/
        # Store profiles. A fresh Owner-directed move is not Store-specific.
        active = [p for p in snapshot['map']['pins'] if p['actor_id'] == actor and p['mode'] == 'active']
        if len(active) != 1 or active[0]['work_id'] == target:
            raise TransitionConflict('one known different own source Pin is required')
        old_source_pin = active[0]
        check_scope(snapshot, old_source_pin['work_id'], actor, value['from_scope'])
        # The separately reviewed, exact new request must be authorized by its
        # Owner sources. One instruction may cover renewal and the next Work;
        # a different pathname is neither necessary nor proof of new authority.
        if any(history.get(k) != value[k] for k in ('from_scope', 'resume_note')):
            raise TransitionDenied('scope switch restart history differs')
        resume_context(value['resume_note'], old_source_pin['work_id'], actor)
        if any(n['id'] == value['resume_note']['id'] for n in snapshot['map']['notes']):
            raise TransitionConflict('restart note already exists')
        if not set(grant['source_refs']) <= set(value['resume_note']['refs']):
            raise TransitionDenied('restart note omits fresh Owner source')
    if renewing:
        # Renewal records a NEW separately reviewed Owner instruction against
        # current premises. Requiring reuse of the stale review here would make
        # the requested re-evaluation impossible after a legitimate reparent.
        _, prior_scope, _ = _current_scope_binding(snapshot, target, actor, value["scope_thread"])
        old_pin, old_grant = prior_scope["pin"], prior_scope["permission"]
        chain = {p['work_key'] for p in context['parent_chain']}
        old_parents = {r['child']: r['parent'] for r in prior_scope['prior_context']['parents']}
        old_chain, key = set(), target
        while key not in old_chain:
            old_chain.add(key)
            if key not in old_parents:
                break
            key = old_parents[key]
        relevant = lambda items, keys: [c for c in items if keys.intersection(c['targets'])]
        if relevant(snapshot['map']['action_rules'], chain) != relevant(prior_scope['prior_context']['action_rules'], old_chain):
            raise ContextUnknown('changed applicable Action Rules need separate disposition before renewal')
        if any(p['actor_id'] == actor and p['mode'] == 'active' and p != old_pin for p in snapshot['map']['pins']):
            raise TransitionConflict('another recorded active Pin exists for this actor')
        if (any(pin[k] != old_pin[k] for k in old_pin if k != "refs")
                or pin["refs"] != list(dict.fromkeys([*old_pin["refs"], *grant["source_refs"]]))
                or any(grant[k] != old_grant[k] for k in ("id", "targets", "grantee", "effect", "grantor_ref"))
                or not set(old_grant["source_refs"]) < set(grant["source_refs"])):
            raise TransitionDenied("renewal must preserve own Pin and grant identity and add fresh Owner evidence")
    else:
        if any(p["id"] == pin["id"] or (p["actor_id"] == actor and p["mode"] == "active" and p != old_source_pin) for p in snapshot["map"]["pins"]):
            raise TransitionConflict("Pin identity exists or this actor already has a recorded active Pin")
        if any(p["id"] == grant["id"] for p in snapshot["map"]["permissions"]):
            raise TransitionConflict("Permission identity exists")
    current_chain = {p['work_key'] for p in context['parent_chain']}
    if any(current_chain.intersection(p["targets"]) and p.get("grantee") == actor and p["effect"] != "allow"
           and set(p["operations"]) & set(grant["operations"]) for p in snapshot["map"]["permissions"]):
        raise TransitionDenied("recorded denial or unknown scope authority must be resolved")
    if renewing:
        snapshot["map"]["pins"][snapshot["map"]["pins"].index(old_pin)] = copy.deepcopy(pin)
        snapshot["map"]["permissions"][snapshot["map"]["permissions"].index(old_grant)] = copy.deepcopy(grant)
    else:
        if switching:
            held = {**copy.deepcopy(old_source_pin), 'mode': 'held'}
            snapshot['map']['pins'][snapshot['map']['pins'].index(old_source_pin)] = held
            snapshot['map']['notes'].append(copy.deepcopy(value['resume_note']))
        snapshot["map"]["pins"].append(copy.deepcopy(pin))
        snapshot["map"]["permissions"].append(copy.deepcopy(grant))
    snapshot["thread_entries"].append(copy.deepcopy(entry))
