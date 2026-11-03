"""Gate a cooperating review runtime after canonical acceptance registration.

This does not intercept arbitrary tool calls or authenticate Owner/runtime IDs.
The coordinator and reviewer must use this gate before substantive work.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from pma.workmap_canonical_core import ReviewedDelegation, validate_review_scope
from pma.workmap_contract import ContextUnknown, _collection_status, inspect_work, require_decision_context, validate_snapshot
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest
from pma.workmap_owner_scope import context_guard, limited_review_context, scope_history
from pma.workmap_store import snapshot_object_id


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


def _fixed_review_context(state: dict, request: dict, *, allow_action_rules: bool = False) -> tuple[set, set]:
    """Conservative authority/relationship scope, not the review's subject data."""
    change = request['changes'][0]
    value, target = change['value'], change['target']
    if 'coordinator_scope' in value:
        raise TransitionDenied('fixed-file continuation does not extend legacy limited scopes')
    context = inspect_work(state, target)
    require_decision_context(context)
    actors = {request['actor_id'], value['pin']['actor_id']}
    related = {w['work_key'] for w in context['parent_chain']}
    for key in related:
        require_decision_context(inspect_work(state, key))
    # Unknown membership of any participant's Pin search cannot be absence.
    if any(_collection_status(state, 'pins', key) != 'confirmed' for key in state['map']['members']):
        raise ContextUnknown('fixed-file continuation needs complete participant Pin visibility')
    for actor in actors:
        if not any(p['actor_id'] == actor and p['work_id'] == target and p['mode'] == 'active'
                   for p in state['map']['pins']):
            raise TransitionDenied('both accepted participants need active Pins on the review Work')
    # A -> X through one location and X -> Y through another still relates Y.
    # Resolve the finite dependency/location component to a fixed point; never
    # infer independence merely because a Work is more than one hop away.
    while True:
        previous = frozenset(related)
        related.update(r['parent'] for r in state['map']['relations']
                       if r['kind'] == 'parent' and r['child'] in previous)
        related.update(end for r in state['map']['relations'] if r['kind'] == 'dependency'
                       and previous.intersection({r['consumer'], r['provider']})
                       for end in (r['consumer'], r['provider']))
        locations = {p['where'] for p in state['map']['pins']
                     if p.get('where') and (p['actor_id'] in actors or p['work_id'] in related)}
        related.update(p['work_id'] for p in state['map']['pins'] if p.get('where') in locations)
        if related == previous:
            break
    for key in related:
        if key not in state['map']['members']:
            raise ContextUnknown('fixed-file continuation does not assess closed dependency context')
        require_decision_context(inspect_work(state, key))
    if any(related.intersection(p['targets']) and p.get('grantee') in actors
           and p['effect'] != 'allow' and set(p['operations']) & {'review', 'add_pin', 'remove_pin', 'append_thread'}
           for p in state['map']['permissions']):
        raise TransitionDenied('related review authority is denied or unknown')
    if not allow_action_rules and any(related.intersection(r['targets']) for r in state['map']['action_rules']):
        raise ContextUnknown('fixed-file continuation does not assess related Action Rules')
    return related, actors


def _check_fixed_continuation(base: dict, current: dict, request: dict) -> None:
    """Prove only fresh unrelated Owner-scope additions; never rewrite a request."""
    related, actors = _fixed_review_context(base, request)
    now_related, now_actors = _fixed_review_context(current, request)
    if (related, actors) != (now_related, now_actors):
        raise TransitionConflict('review relationships or participant scope changed')
    if (any(current['receipts'].get(tid) != receipt for tid, receipt in base['receipts'].items())
            or current['thread_entries'][:len(base['thread_entries'])] != base['thread_entries']):
        raise TransitionConflict('original committed review history is not retained')
    rebuilt = copy.deepcopy(base)
    added_receipts = set()
    for entry in current['thread_entries'][len(base['thread_entries']):]:
        if (entry['kind'] != 'owner_scope_accepted' or related.intersection(entry['works'])
                or entry['actor'] in actors):
            raise TransitionConflict('intervening operation needs a new review judgment')
        history = scope_history(entry)
        if (set(history) != {'pin', 'permission', 'owner_sources', 'prior_context'}
                or history['prior_context'] != context_guard(rebuilt)):
            raise TransitionConflict('intervening scope is not an exact fresh unrelated addition')
        pin, permission = history['pin'], history['permission']
        if (pin['work_id'] in related or pin['actor_id'] in actors
                or any(p['id'] == pin['id'] for p in rebuilt['map']['pins'])
                or any(p['id'] == permission['id'] for p in rebuilt['map']['permissions'])):
            raise TransitionConflict('intervening scope overlaps review participants or identity')
        tid = entry.get('transition_id')
        receipt = current['receipts'].get(tid)
        if tid in rebuilt['receipts'] or tid in added_receipts or not receipt:
            raise TransitionConflict('intervening scope lacks its unique committed receipt')
        added_receipts.add(tid)
        rebuilt['map']['pins'].append(copy.deepcopy(pin))
        rebuilt['map']['permissions'].append(copy.deepcopy(permission))
    if set(current['receipts']) - set(base['receipts']) != added_receipts:
        raise TransitionConflict('intervening receipts are not fully explained')
    for field in set(base) - {'revision', 'recorded_at', 'thread_entries', 'receipts'}:
        if rebuilt[field] != current[field]:
            raise TransitionConflict('current data is not fully explained by unrelated registrations')


def check_review_gate(store, request: dict, actor_id: str, repo: Path, *,
                      reviewed_fixed_files: bool = False, registration_token: str | None = None,
                      reviewed_current_map: bool = False, reconsidered_token: str | None = None) -> dict:
    """Read back one committed registration and file versions before release.

    The optional local operator input covers fixed subject files only, never
    current-Map subject data. Current-Map continuation is a separate opt-in.
    Neither grants anything. Saved snapshot tokens reuse
    the existing retained Store objects; no new canonical field or ledger.
    """
    review_delegation(request, repo)
    change = request["changes"][0]
    if change["op"] != "start_delegation":
        raise TransitionDenied("review gate requires an acceptance registration")
    value = change["value"]
    if value["pin"]["actor_id"] != actor_id:
        raise TransitionDenied("release actor differs from accepted reviewer")
    if not isinstance(reviewed_fixed_files, bool):
        raise TransitionDenied('fixed-file scope must be explicit operator input')
    if not isinstance(reviewed_current_map, bool) or (reviewed_fixed_files and reviewed_current_map):
        raise TransitionDenied('choose one explicit review subject mode')
    if reconsidered_token is not None and not reviewed_current_map:
        raise TransitionDenied('reconsidered subject requires current-Map review mode')
    continuation = reviewed_fixed_files or reviewed_current_map
    if registration_token is not None and not continuation:
        raise TransitionDenied('a retained registration token requires an explicit continuation mode')
    if continuation and 'coordinator_scope' in value:
        raise TransitionDenied('review continuation does not extend legacy limited scopes')
    base, base_token = None, registration_token
    attempts = 2 if continuation else 1
    for attempt in range(attempts):
        review_delegation(request, repo)
        committed = store.read()
        state = validate_snapshot(committed.snapshot)
        if state['metadata'].get('store_role') != 'canonical_current':
            raise TransitionDenied('review gate needs the declared canonical Store')
        receipt = state['receipts'].get(request['transition_id'])
        if (not isinstance(receipt, dict) or receipt['request_digest'] != _digest(request)
                or receipt['result'] != 'applied'):
            raise TransitionConflict('registration is absent or different')
        if base is None:
            if reviewed_current_map and state['revision'] == receipt['revision']:
                # Same-version fast path: no retained lookup or delta assessment.
                if base_token is not None and base_token != snapshot_object_id(state):
                    raise TransitionConflict('registration token differs from current registration')
                base, base_token = state, snapshot_object_id(state)
            elif base_token is not None:
                if (not isinstance(base_token, str) or len(base_token) != 64
                        or any(c not in '0123456789abcdef' for c in base_token)):
                    raise TransitionDenied('registration token must be an existing snapshot hash')
                base = validate_snapshot(store.read_retained_snapshot(base_token))
                if snapshot_object_id(base) != base_token:
                    raise TransitionConflict('retained registration snapshot hash differs')
            elif state['revision'] == receipt['revision']:
                # Text Store's publication token hashes its head, while SQLite
                # hashes the snapshot itself. Retained lookup always uses the
                # snapshot content hash, never the adapter's CAS token.
                base, base_token = copy.deepcopy(state), snapshot_object_id(state)
            else:
                raise TransitionConflict('registration is no longer current; retained registration token required')
            if (base['project_id'] != state['project_id'] or base['revision'] != receipt['revision']
                    or base['receipts'].get(request['transition_id']) != receipt
                    or base['metadata'].get('store_role') != 'canonical_current'):
                raise TransitionConflict('retained snapshot is not the committed registration version')
        if state['revision'] != receipt['revision'] and not continuation:
            raise TransitionConflict('registration is absent, different, or no longer the current revision')
        if reviewed_fixed_files:
            _check_fixed_continuation(base, state, request)
        result = _check_current_registration(state, request, actor_id, repo)
        if reviewed_current_map:
            from pma.workmap_current_review import assess_current_review, acknowledge_current_subject
            assessment = ({'status': 'ready', 'impact': 'unchanged', 'changes': [], 'history': [],
                           'review_token': snapshot_object_id(state)} if state['revision'] == receipt['revision']
                          else assess_current_review(base, state, request, scope_context=_fixed_review_context))
            result = {**result, **acknowledge_current_subject(store, reconsidered_token, state, assessment, request,
                                                             scope_context=_fixed_review_context)}
        if store.read().token == committed.token:
            return {**result, 'registration_token': base_token,
                    'reviewed_fixed_files': reviewed_fixed_files,
                    **({'reviewed_current_map': True} if reviewed_current_map else {})}
        if attempt + 1 == attempts:
            raise TransitionConflict('canonical Store changed during release check')
    raise TransitionConflict('canonical Store changed during release check')


def _check_current_registration(state: dict, request: dict, actor_id: str, repo: Path) -> dict:
    change = request['changes'][0]
    value = change['value']
    if state["metadata"].get("store_role") != "canonical_current":
        raise TransitionDenied("review gate needs the declared canonical Store")
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
    return {"status": "ready", "actor_id": actor_id, "work_id": change["target"],
            "revision": state["revision"], "registration": request["transition_id"],
            "scope": value["scope"]}
