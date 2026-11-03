"""Read and validate one committed Work Map snapshot for AI clients.

This module contains no human view and never infers missing current values from
titles or Thread prose. The physical Store may change; the read contract does not.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re


class SnapshotInvalid(ValueError):
    """The committed bytes cannot be treated as a Work Map snapshot."""


class ContextUnknown(ValueError):
    """A requested decision needs current information that is not confirmed."""


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _refs(value: object) -> bool:
    return isinstance(value, list) and all(_nonempty(item) for item in value)


def _time(value: object) -> bool:
    if not _nonempty(value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _fail(message: str) -> None:
    raise SnapshotInvalid(message)


def _collection_status(snapshot: dict, name: str, work_key: str) -> str:
    completeness = snapshot["metadata"].get("completeness")
    if completeness == "full":
        return "confirmed"
    if isinstance(completeness, dict):
        scope = completeness.get(name)
        if scope == "full":
            return "confirmed"
        if isinstance(scope, dict) and work_key in scope.get("confirmed_work_keys", []):
            return "confirmed"
    return "unknown"


def confirm_new_work_collections(snapshot: dict, work_key: str) -> None:
    """A Core-created identity has no unobserved pre-birth relationships.

    Confirm only this Work, never its migrated parent or siblings.
    """
    confirm_work_collections(snapshot, work_key, ('pins', 'permissions', 'action_rules'))


def confirm_work_collections(snapshot: dict, work_key: str, names) -> None:
    """Mark only reviewed current collections; callers establish the evidence."""
    names = tuple(names)
    if work_key not in snapshot['map']['members'] or not names or any(
            name not in ('pins', 'permissions', 'action_rules') for name in names):
        raise SnapshotInvalid('confirmation needs current Work and selected collections')
    metadata = snapshot['metadata']
    if metadata.get('completeness') == 'full':
        return
    completeness = metadata.setdefault('completeness', {})
    if completeness is None:
        completeness = metadata['completeness'] = {}
    for name in names:
        scope = completeness.get(name)
        if scope == 'full':
            continue
        if not isinstance(scope, dict):
            scope = completeness[name] = {'confirmed_work_keys': []}
        if work_key not in scope['confirmed_work_keys']:
            scope['confirmed_work_keys'].append(work_key)


def child_address_prefix(parent_id: str) -> str:
    """W0 and its non-reused common-parent address W00 have flat children."""
    return 'W' if parent_id in ('W0', 'W00') else parent_id + '.'


def address_matches(parent_id: str | None, identifier: str) -> bool:
    if not isinstance(identifier, str):
        return False
    pattern = r'W[0-9]+' if parent_id is None else re.escape(child_address_prefix(parent_id)) + r'[1-9][0-9]*'
    return re.fullmatch(pattern, identifier) is not None


def current_subtree(snapshot: dict, target: str) -> list[str]:
    """Follow current Parent edges; addresses are labels, not membership."""
    if target not in snapshot['map']['members']:
        raise SnapshotInvalid('subtree target is not current')
    children = {}
    for r in snapshot['map']['relations']:
        if r['kind'] == 'parent': children.setdefault(r['parent'], []).append(r['child'])
    result = [target]
    for key in result:
        for child in sorted(children.get(key, []), key=lambda k: snapshot['display_ids'][k]):
            if child in result: raise SnapshotInvalid('Parent cycle or duplicate child')
            result.append(child)
    return result


def subtree_addresses(snapshot: dict, target: str, new_id: str) -> dict[str, str]:
    parents = {r['child']: r['parent'] for r in snapshot['map']['relations'] if r['kind'] == 'parent'}
    mapped = {target: new_id}
    for key in current_subtree(snapshot, target)[1:]:
        parent = parents[key]
        old_prefix = child_address_prefix(snapshot['display_ids'][parent])
        local = snapshot['display_ids'][key][len(old_prefix):]
        mapped[key] = child_address_prefix(mapped[parent]) + local
    return mapped


def validate_snapshot(snapshot: object) -> dict:
    """Check structure and relationships before any value is used as current.

    Null Intent is a known lack of evidence. Missing Intent or a missing Pin
    collection makes the snapshot unreadable as a complete current value.
    """
    if not isinstance(snapshot, dict):
        _fail("snapshot is not an object")
    required = {
        "format_version", "project_id", "revision", "recorded_at", "works",
        "aliases", "display_ids", "display_id_history", "map", "retirements",
        "thread_entries", "receipts", "metadata",
    }
    if not required <= snapshot.keys() or snapshot.get("format_version") not in ("0.4.2", "0.4.3"):
        _fail("required snapshot fields or format are missing")
    transfer_identities, imported_receipts = {}, {}
    if snapshot['format_version'] == '0.4.3':
        from pma.workmap_transfer import validate_transfer_history
        transfer_identities, imported_receipts = validate_transfer_history(snapshot)
    if ('conditions' in snapshot.get('map', {})
            or isinstance(snapshot.get('metadata', {}).get('completeness'), dict)
            and 'conditions' in snapshot['metadata']['completeness']):
        _fail('legacy collection names are not a current Action Rule format')
    if not _nonempty(snapshot["project_id"]) or not _nonempty(snapshot["revision"]) or not _time(snapshot["recorded_at"]):
        _fail("project identity, revision, or recorded time is invalid")
    works = snapshot["works"]
    aliases = snapshot["aliases"]
    display_ids = snapshot["display_ids"]
    current = snapshot["map"]
    if not all(isinstance(part, dict) for part in (works, aliases, display_ids, current, snapshot["retirements"], snapshot["receipts"], snapshot["metadata"])):
        _fail("snapshot object collections are invalid")
    if not isinstance(snapshot["display_id_history"], list) or not isinstance(snapshot["thread_entries"], list):
        _fail("snapshot history collections are invalid")
    # Local reference resolution only: closed identities live in close Thread.
    # Legacy 0.4.2 snapshots may still contain noncurrent Work bytes; they are
    # accepted as historical compatibility data, not a second current set.
    works = dict(works)
    for key, identity in transfer_identities.items():
        works.setdefault(key, identity)
    from pma.workmap_resume import close_history
    from pma.workmap_core import TransitionError
    for entry in snapshot["thread_entries"]:
        if isinstance(entry, dict) and entry.get("kind") == "close":
            try:
                history = close_history(entry)
            except TransitionError as exc:
                _fail(str(exc))
            if history and "identity" in history:
                key = history["work"]
                if key not in works:
                    works[key] = history["identity"]
    for key, work in works.items():
        if not _nonempty(key) or not isinstance(work, dict) or work.get("work_key") != key:
            _fail("Work identity is invalid")
        if not _nonempty(work.get("name")) or "intent" not in work or (work["intent"] is not None and not _nonempty(work["intent"])):
            _fail(f"Work name or Intent is unreadable: {key}")
        origin = work.get("origin")
        if not isinstance(origin, dict) or "summary" not in origin or not _refs(origin.get("refs")):
            _fail(f"Work Origin is unreadable: {key}")
        if origin["summary"] is not None and not _nonempty(origin["summary"]):
            _fail(f"Work Origin is invalid: {key}")
    if set(display_ids) != set(works) or len(set(display_ids.values())) != len(display_ids):
        _fail("current display IDs do not cover unique Work identities")
    if any(not _nonempty(identifier) or aliases.get(identifier) != key for key, identifier in display_ids.items()):
        _fail("current display ID alias is missing")
    if any(key not in works for key in aliases.values()):
        _fail("an alias points outside Work records")
    for name in ("members", "relations", "pins", "permissions", "action_rules", "notes"):
        if not isinstance(current.get(name), list):
            _fail(f"current {name} collection is missing or unreadable")
    members = current["members"]
    if len(members) != len(set(members)) or any(key not in snapshot["works"] for key in members):
        _fail("current members are invalid")
    member_set = set(members)
    if any(key not in member_set and key not in snapshot["retirements"] and key not in transfer_identities for key in works):
        _fail("noncurrent identity has no close history; pre-Work records are not supported")
    completeness = snapshot["metadata"].get("completeness")
    if completeness is not None and completeness != "full":
        if not isinstance(completeness, dict):
            _fail("completeness declaration is invalid")
        for name in ("pins", "permissions", "action_rules"):
            scope = completeness.get(name)
            if scope in (None, "unknown", "full"):
                continue
            if not isinstance(scope, dict) or set(scope) != {"confirmed_work_keys"} or not _refs(scope["confirmed_work_keys"]) or len(scope["confirmed_work_keys"]) != len(set(scope["confirmed_work_keys"])) or any(key not in member_set for key in scope["confirmed_work_keys"]):
                _fail(f"{name} completeness declaration is invalid")
    parents: dict[str, str] = {}
    relation_ids: set[str] = set()
    for relation in current["relations"]:
        if not isinstance(relation, dict) or not _nonempty(relation.get("id")):
            _fail("relation is unreadable")
        if relation["id"] in relation_ids:
            _fail("relation ID is reused")
        relation_ids.add(relation["id"])
        if relation.get("kind") == "parent":
            child, parent = relation.get("child"), relation.get("parent")
            if child not in member_set or parent not in member_set or child == parent or child in parents:
                _fail("Parent reference, uniqueness, or membership is invalid")
            parents[child] = parent
        elif relation.get("kind") == "dependency":
            if relation.get("consumer") not in member_set or relation.get("provider") not in works or not _nonempty(relation.get("action")) or not _nonempty(relation.get("requires")) or not _refs(relation.get("refs")) or not relation["refs"]:
                _fail("Dependency reference is invalid")
        else:
            _fail("relation kind is unsupported")
    for child in parents:
        seen = {child}
        cursor = child
        while cursor in parents:
            cursor = parents[cursor]
            if cursor in seen:
                _fail("Parent cycle exists")
            seen.add(cursor)
    for key in member_set:
        identifier = display_ids[key]
        if key not in parents:
            if re.fullmatch(r"W[0-9]+", identifier) is None:
                _fail("root display ID is invalid")
            continue
        parent_id = display_ids[parents[key]]
        if not address_matches(parent_id, identifier):
            _fail("display ID does not match current Parent")
    active: set[str] = set()
    for pin in current["pins"]:
        if not isinstance(pin, dict) or pin.get("work_id") not in member_set or not _nonempty(pin.get("id")) or not _nonempty(pin.get("actor_id")) or pin.get("mode") not in ("active", "held") or not _time(pin.get("pinned_at")) or not _time(pin.get("confirmed_at")) or not _refs(pin.get("refs")) or not pin["refs"]:
            _fail("Pin is unreadable or has no acceptance evidence")
        if pin["mode"] == "active":
            if pin["actor_id"] in active:
                _fail("one actor has multiple active Pins")
            active.add(pin["actor_id"])
    permission_ids: set[str] = set()
    for permission in current["permissions"]:
        if not isinstance(permission, dict) or not _nonempty(permission.get("id")) or not _refs(permission.get("targets")) or not _refs(permission.get("operations")) or permission.get("effect") not in ("allow", "deny", "unknown") or not _refs(permission.get("source_refs")) or not permission["source_refs"] or not _nonempty(permission.get("grantor_ref")) or any(target not in works for target in permission["targets"]):
            _fail("Permission or its source is unreadable")
        if permission["id"] in permission_ids:
            _fail("Permission ID is reused")
        permission_ids.add(permission["id"])
    for action_rule in current["action_rules"]:
        if not isinstance(action_rule, dict) or not _nonempty(action_rule.get("id")) or not _refs(action_rule.get("targets")) or any(target not in works for target in action_rule["targets"]) or not _nonempty(action_rule.get("statement")) or not _refs(action_rule.get("refs")) or not action_rule["refs"]:
            _fail("Action Rule is unreadable")
    for note in current["notes"]:
        if not isinstance(note, dict) or not _nonempty(note.get("id")) or not _refs(note.get("targets")) or any(target not in works for target in note["targets"]) or not _nonempty(note.get("text")) or not _refs(note.get("refs")) or note.get("basis") not in ("fact", "reported", "proposal", "inference", "unknown"):
            _fail("current note is unreadable")
    thread_ids: dict[str, dict] = {}
    for entry in snapshot["thread_entries"]:
        if not isinstance(entry, dict) or not _nonempty(entry.get("id")) or entry["id"] in thread_ids or not _refs(entry.get("works")) or not entry["works"] or any(key not in works for key in entry["works"]) or not _nonempty(entry.get("kind")) or not _nonempty(entry.get("summary")) or not _nonempty(entry.get("actor")) or not _refs(entry.get("source_refs")) or not _time(entry.get("recorded_at")):
            _fail("Thread entry is unreadable or reused")
        if entry.get("why") is not None and not _nonempty(entry["why"]):
            _fail("Thread reason is unreadable")
        if entry["kind"] in ("action_rule_transferred", "condition_transferred"):
            try:
                history = json.loads(entry.get("consequence", ""))
            except (ValueError, TypeError):
                _fail("Action Rule transfer history is unreadable")
            # The former names occur only inside immutable historical records.
            old_field, new_field = (('old_condition', 'new_condition')
                                    if entry['kind'] == 'condition_transferred'
                                    else ('old_action_rule', 'new_action_rule'))
            if not isinstance(history, dict) or set(history) != {old_field, new_field, "from_work", "to_work"}:
                _fail("Action Rule transfer history is incomplete")
            old, new = history[old_field], history[new_field]
            if (not isinstance(old, dict) or not isinstance(new, dict)
                    or not _nonempty(old.get("id")) or not _nonempty(old.get("statement"))
                    or not _refs(old.get("refs")) or not old["refs"]
                    or old.get("targets") != [history["from_work"]]
                    or new.get("targets") != [history["to_work"]]
                    or history["from_work"] == history["to_work"]
                    or entry["works"] != [history["from_work"], history["to_work"]]
                    or {key: val for key, val in old.items() if key != "targets"} != {key: val for key, val in new.items() if key != "targets"}
                    or not set(old["refs"]).issubset(entry["source_refs"])):
                _fail("Action Rule transfer history scope or original meaning differs")
        if entry["kind"] in ("intent_confirmation", "intent_correction", "intent_refinement"):
            try:
                history = json.loads(entry.get("consequence", ""))
            except (ValueError, TypeError):
                _fail("Intent history is unreadable")
            fields = {"kind", "reason", "same_work_basis", "owner_sources", "old_intent", "new_intent", "old_source_refs"}
            if (not isinstance(history, dict) or set(history) != fields
                    or history["kind"] not in ("confirmation", "correction", "refinement")
                    or entry["kind"] != "intent_" + history["kind"]
                    or not _nonempty(history["new_intent"])
                    or history["old_intent"] == history["new_intent"]
                    or (history["old_intent"] is None) != (history["kind"] == "confirmation")
                    or (history["old_intent"] is not None and not _nonempty(history["old_intent"]))
                    or not _nonempty(history["reason"]) or not _nonempty(history["same_work_basis"])
                    or entry["why"] != history["reason"] or not _refs(history["old_source_refs"])):
                _fail("Intent history is unreadable")
            sources = history["owner_sources"]
            if (not isinstance(sources, list) or not sources
                    or any(not isinstance(source, dict) or set(source) != {"ref", "sha256"}
                           or not _nonempty(source["ref"]) or source["ref"] not in entry["source_refs"]
                           or not isinstance(source["sha256"], str)
                           or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
                           for source in sources)):
                _fail("Intent history evidence is unreadable")
        if entry["kind"] in ("delegation_accepted", "delegation_finished"):
            try:
                history = json.loads(entry.get("consequence", ""))
            except (ValueError, TypeError):
                _fail("delegation history is unreadable")
            fields = ({"coordinator", "pin", "permission", "scope", "owner_sources", "acceptance"}
                      if entry["kind"] == "delegation_accepted"
                      else {"registration", "result_source", "outcome", "pin", "permission"})
            allowed = (fields, fields | {'coordinator_scope'}) if entry['kind'] == 'delegation_accepted' else (fields,)
            if not isinstance(history, dict) or set(history) not in allowed or len(entry["works"]) != 1:
                _fail("delegation history is incomplete")
            if 'coordinator_scope' in history:
                prior = next((e for e in snapshot['thread_entries'][:snapshot['thread_entries'].index(entry)]
                              if e['id'] == history['coordinator_scope']), None)
                if (prior is None or prior['kind'] not in ('owner_scope_accepted', 'owner_scope_renewed')
                        or prior['works'] != entry['works'] or prior['actor'] != entry['actor']):
                    _fail('delegation lacks its prior coordinator scope')
            pin, permission = history["pin"], history["permission"]
            if (not isinstance(pin, dict) or not isinstance(permission, dict)
                    or pin.get("work_id") != entry["works"][0]
                    or not _nonempty(pin.get("actor_id")) or pin.get("mode") != "active"
                    or permission.get("grantee") != pin["actor_id"]
                    or permission.get("targets") != entry["works"]
                    or permission.get("operations") != ["review"] or permission.get("effect") != "allow"):
                _fail("delegation history actor or scope is unreadable")
            if entry["kind"] == "delegation_accepted":
                sources = history["owner_sources"]
                if (history["coordinator"] != entry["actor"] or not isinstance(history["scope"], dict)
                        or history["scope"].get("mode") != "read_only"
                        or not isinstance(sources, list) or not sources):
                    _fail("delegation acceptance history is unreadable")
                sources = [*sources, history["acceptance"]]
            else:
                if not _nonempty(history["registration"]) or history["outcome"] not in ("review_completed", "cancelled"):
                    _fail("delegation result history is unreadable")
                sources = [history["result_source"]]
            if any(not isinstance(source, dict) or set(source) != {"ref", "sha256"}
                   or source["ref"] not in entry["source_refs"]
                   or not isinstance(source["sha256"], str)
                   or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None for source in sources):
                _fail("delegation history evidence is unreadable")
        if entry["kind"] in ("dependency_disposition_authorized", "dependency_fulfilled"):
            try:
                history = json.loads(entry.get("consequence", ""))
            except (ValueError, TypeError):
                _fail("dependency disposition history is unreadable")
            authorized = entry["kind"] == "dependency_disposition_authorized"
            fields = ({"dependency", "permission", "owner_sources"} if authorized
                      else {"dependency", "permission", "authorization", "outcome", "fulfillment_basis", "evidence"})
            if not isinstance(history, dict) or set(history) != fields:
                _fail("dependency disposition history is incomplete")
            dependency, permission = history["dependency"], history["permission"]
            if (not isinstance(dependency, dict) or dependency.get("kind") != "dependency"
                    or not _nonempty(dependency.get("id")) or not _nonempty(dependency.get("action"))
                    or not _nonempty(dependency.get("requires")) or not _refs(dependency.get("refs"))
                    or not dependency["refs"] or entry["works"] != [dependency.get("consumer"), dependency.get("provider")]
                    or not isinstance(permission, dict) or not _nonempty(permission.get("id"))
                    or permission.get("grantee") != entry["actor"] or permission.get("targets") != [dependency["consumer"]]
                    or permission.get("operations") != ["remove_relation"] or permission.get("effect") != "allow"
                    or permission.get("grantor_ref") != "principal:owner" or not _refs(permission.get("source_refs"))
                    or not permission["source_refs"]):
                _fail("dependency disposition history scope is unreadable")
            digest = hashlib.sha256(json.dumps(dependency, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            if permission.get("constraints") != ["dependency_id:" + dependency["id"], "dependency_sha256:" + digest]:
                _fail("dependency disposition history authority scope differs")
            sources = history["owner_sources" if authorized else "evidence"]
            if (not isinstance(sources, list) or not sources
                    or any(not isinstance(source, dict) or set(source) != {"ref", "sha256"}
                           or source["ref"] not in entry["source_refs"] or not isinstance(source["sha256"], str)
                           or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None for source in sources)
                    or not set(dependency["refs"]).issubset(entry["source_refs"])
                    or not set(permission["source_refs"]).issubset(entry["source_refs"])):
                _fail("dependency disposition history evidence is unreadable")
            if authorized:
                if permission["source_refs"] != [source["ref"] for source in sources]:
                    _fail("dependency disposition Owner provenance differs")
            elif (history["outcome"] != "fulfilled" or not _nonempty(history["authorization"])
                  or not _nonempty(history["fulfillment_basis"]) or entry.get("why") != history["fulfillment_basis"]):
                _fail("dependency fulfillment history basis is unreadable")
        if entry["kind"] == "close":
            from pma.workmap_resume import close_history
            from pma.workmap_core import TransitionError
            try:
                history = close_history(entry)
            except TransitionError as exc:
                _fail(str(exc))
            if history is not None and (history["parent_at_close"] is not None and history["parent_at_close"] not in works
                                        or any(p["work_id"] not in works for p in history["actor_held_at_close"]["pins"])):
                _fail("close context points to an unknown Work")
        if entry["kind"] == "note_changed":
            from pma.workmap_notes import note_history
            from pma.workmap_core import TransitionError
            try:
                note_history(entry)
            except TransitionError as exc:
                _fail(str(exc))
        if entry["kind"] == "pin_active_moved":
            from pma.workmap_pin_move import move_history
            from pma.workmap_core import TransitionError
            try:
                move_history(entry)
            except TransitionError:
                _fail("atomic Pin move history is unreadable")
        if entry["kind"] in ("owner_scope_accepted", "owner_scope_renewed"):
            from pma.workmap_owner_scope import scope_history
            from pma.workmap_core import TransitionError
            try:
                scope_history(entry)
            except TransitionError:
                _fail("Owner scope history is unreadable")
        thread_ids[entry["id"]] = entry
    for entry in thread_ids.values():
        if entry["kind"] == "dependency_fulfilled":
            history = json.loads(entry["consequence"])
            grants = [recorded for recorded in thread_ids.values()
                      if recorded["kind"] == "dependency_disposition_authorized"
                      and recorded.get("transition_id") == history["authorization"]]
            if (len(grants) != 1 or grants[0]["actor"] != entry["actor"]
                    or any(json.loads(grants[0]["consequence"])[name] != history[name] for name in ("dependency", "permission"))):
                _fail("dependency fulfillment lacks its exact committed authorization")
    transition_entries: dict[str, list[dict]] = {}
    for entry in thread_ids.values():
        transition_id = entry.get("transition_id")
        if transition_id is not None:
            if not _nonempty(transition_id):
                _fail("Thread transition identity is unreadable")
            transition_entries.setdefault(transition_id, []).append(entry)
    for transition_id, receipt in snapshot["receipts"].items():
        if not _nonempty(transition_id) or not isinstance(receipt, dict) or set(receipt) != {"transition_id", "request_digest", "revision", "recorded_at", "result"} or receipt.get("transition_id") != transition_id or not _nonempty(receipt.get("revision")) or not _time(receipt.get("recorded_at")) or receipt.get("result") != "applied":
            _fail("receipt is unreadable")
        digest = receipt["request_digest"]
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            _fail("receipt digest is invalid")
        linked = transition_entries.get(transition_id, [])
        # A receipt proves commit/retry identity, not the need for a meaning
        # Thread. Operation-specific writers enforce required explanations.
        if any(entry["recorded_at"] != receipt["recorded_at"] for entry in linked):
            _fail("receipt and Thread do not share one committed transition")
    for transition_id, entries in transition_entries.items():
        if transition_id in snapshot['receipts']:
            continue
        prior = imported_receipts.get(transition_id)
        if prior is None or any(e['recorded_at'] != prior['recorded_at'] for e in entries):
            _fail('Thread has no local receipt or valid imported committed result')
    if any(transition_id not in snapshot["receipts"] and transition_id not in imported_receipts for transition_id in transition_entries):
        _fail("Thread transition has no receipt")
    for key, entry_id in snapshot["retirements"].items():
        if key not in works or entry_id not in thread_ids or thread_ids[entry_id]["kind"] != "close" or key not in thread_ids[entry_id]["works"]:
            _fail("retirement lacks a matching close Thread")
    return snapshot


def inspect_work(snapshot: dict, work_key: str) -> dict:
    """Return a bounded AI-facing context from one already validated revision."""
    validate_snapshot(snapshot)
    if work_key not in snapshot["map"]["members"]:
        raise KeyError(work_key)
    parents = {item["child"]: item["parent"] for item in snapshot["map"]["relations"] if item["kind"] == "parent"}
    chain = [work_key]
    while chain[-1] in parents:
        chain.append(parents[chain[-1]])
    chain.reverse()
    display = snapshot["display_ids"]
    works = snapshot["works"]
    current = snapshot["map"]
    members = set(current["members"])

    def related_work(key: str) -> dict:
        return {"work_key": key, "display_id": display[key], "name": works[key]["name"], "status": "current", "closure": "not_closed"}

    def provider_context(key: str) -> dict:
        from pma.workmap_resume import closed_identity
        if key not in members and snapshot['format_version'] == '0.4.3':
            from pma.workmap_transfer import provider_history, moved_out
            history = provider_history(snapshot, key)
            if history is not None and (moved_out(snapshot, key) or key not in snapshot['retirements']):
                return {"provider_display_id": display[key],
                        "provider_name": history['provider_identity']['name'], **history}
        identity = works[key] if key in members else closed_identity(snapshot, key)
        result = {"provider_display_id": display[key], "provider_name": identity["name"] if identity else None,
                  "provider_status": "current" if key in members else "closed_history"}
        if key not in members:
            closing = next(e for e in snapshot['thread_entries'] if e['id'] == snapshot['retirements'][key])
            result['provider_identity'] = identity
            result['provider_close'] = {k: closing[k] for k in
                ('id', 'outcome', 'why', 'residuals', 'revisit', 'source_refs', 'evidence_refs') if k in closing}
            # These are reviewed past versions, never proof of current availability
            # or applicability to the consumer's required scope.
            try:
                basis = json.loads(closing.get('assumptions', ''))['close_basis']['expected']
                refs = set(closing.get('source_refs', []) + closing.get('evidence_refs', []))
                refs.update(ref for r in basis.get('retained_dependencies', []) if r['provider'] == key for ref in r['refs'])
                result['provider_close']['reviewed_versions'] = [v for v in basis['evidence'] if v['ref'] in refs]
            except (ValueError, TypeError, KeyError):
                result['provider_close']['reviewed_versions'] = None
        return result

    external_dependency_history = []
    if snapshot['format_version'] == '0.4.3':
        from pma.workmap_transfer import dependency_history
        external_dependency_history = dependency_history(snapshot, work_key)

    return {
        "project_id": snapshot["project_id"],
        "revision": snapshot["revision"],
        "recorded_at": snapshot["recorded_at"],
        "work": work_key,
        "work_provenance": snapshot["metadata"].get("work_sources", {}).get(work_key) if isinstance(snapshot["metadata"].get("work_sources"), dict) else None,
        "parent_chain": [{"work_key": key, "display_id": display[key], "name": works[key]["name"], "intent": works[key]["intent"], "intent_status": "confirmed" if works[key]["intent"] is not None else "unknown", "origin": works[key]["origin"]} for key in chain],
        "pin_status": _collection_status(snapshot, "pins", work_key),
        "pins": [pin for pin in current["pins"] if pin["work_id"] == work_key] if _collection_status(snapshot, "pins", work_key) == "confirmed" else None,
        "permission_status": _collection_status(snapshot, "permissions", work_key),
        "permissions": [item for item in current["permissions"] if work_key in item["targets"]] if _collection_status(snapshot, "permissions", work_key) == "confirmed" else None,
        "action_rule_status": _collection_status(snapshot, "action_rules", work_key),
        "action_rules": [item for item in current["action_rules"] if work_key in item["targets"]] if _collection_status(snapshot, "action_rules", work_key) == "confirmed" else None,
        "notes": [item for item in current["notes"] if work_key in item["targets"]],
        "dependencies": [{**item, **provider_context(item["provider"])} for item in current["relations"] if item["kind"] == "dependency" and item["consumer"] == work_key],
        "external_dependency_history": external_dependency_history,
        "children": [related_work(child) for child, parent in parents.items() if parent == work_key],
        "siblings": [related_work(child) for child, parent in parents.items() if child != work_key and parent == parents.get(work_key)],
        "thread_entries": [item for item in snapshot["thread_entries"] if work_key in item["works"]],
    }


def rule_requires_disposition(rule, operations):
    """Review triggers remain visible; they neither grant nor prohibit routine recording.

    Unknown/free-form applicability stays conservative. No natural-language
    inference, automatic execution, expiry or rule consumption occurs here.
    """
    recording = {'add_work', 'move_active_pin', 'add_pin', 'update_pin', 'set_note', 'remove_note',
                 'append_thread', 'set_permission', 'remove_permission', 'adopt_action_rules'}
    return not (rule.get('time_role') == 'review_trigger'
                and bool(rule.get('applies_to')) and set(operations) <= recording)


def require_decision_context(context: dict, *, needs_pin: bool = True, needs_parent_intent: bool = True, operation_review=None) -> None:
    """Fail closed before a change that depends on Intent and current authority."""
    required_chain = context["parent_chain"] if needs_parent_intent else context["parent_chain"][-1:]
    if any(item["intent_status"] != "confirmed" for item in required_chain):
        raise ContextUnknown("Work or parent Intent is unconfirmed")
    def assessed(collection):
        from pma.workmap_decision_context import ReviewedDecisionContext
        return isinstance(operation_review, ReviewedDecisionContext) and operation_review.permits(context, collection)

    if needs_pin and context["pin_status"] != "confirmed" and not assessed('pins'):
        raise ContextUnknown("current Pin set is unconfirmed")
    if context["permission_status"] != "confirmed" and not assessed('permissions'):
        raise ContextUnknown("current Permission set is unconfirmed")
    if context["action_rule_status"] != "confirmed" and not assessed('action_rules'):
        raise ContextUnknown("current Action Rule set is unconfirmed")
