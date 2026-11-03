"""Meaning-only Work Thread projection over preserved 0.4.2 raw records.

Selection is reviewed Evidence referenced by a committed record, not another
editable Map. Hashes bind both the Evidence bytes and each source record.
Unreviewed or unavailable material is reported; it is never silently discarded
or promoted into meaning merely because its kind/why field sounds important.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from pma.workmap_core import _digest

PREFIX = "repo:docs/work-management/evidence/work_thread_selection_"
BINDING = re.compile(r"^repo:docs/work-management/evidence/(work_thread_selection_[a-zA-Z0-9_-]+\.json)#sha256=([0-9a-f]{64})$")
CRITERION = "future_decision_why"
# These are execution records by contract. A new insight is recorded using the
# existing rationale path, not inferred from the operation's boilerplate why.
TECHNICAL_KINDS = frozenset({"pin_added", "pin_changed", "pin_removed", "pin_moved",
    "active_pin_moved", "pin_active_moved", "handoff", "permission_changed", "delegation_accepted",
    "delegation_finished", "dependency_disposition_authorized", "owner_scope_accepted",
    "owner_scope_renewed", "evidence_recorded", "work_renamed", "work_added",
    "scope_released", "scope_accepted", "action_rule_changed"})
MEANING_KINDS = frozenset({"rationale_added", "correction", "intent_confirmation",
    "intent_correction", "intent_refinement", "close", "reopen", "reparent"})


def direct_meaning(entry: dict, *, reference_fields: bool = True) -> dict:
    """An explicitly authored reason is available while the Work is alive."""
    result = {k: copy.deepcopy(entry[k]) for k in ("id", "works", "summary", "why", "source_refs")}
    result["related_entries"] = [entry["id"]]
    _preserve_references(result if reference_fields else copy.deepcopy(result), [entry])
    if entry.get("evidence_refs"):
        result["source_refs"] = list(dict.fromkeys(result["source_refs"] + entry["evidence_refs"]))
    if entry["kind"] == "rationale_added" and entry.get("consequence"):
        result["consequence"] = entry["consequence"]
    if entry.get("revisit"):
        result["revisit"] = entry["revisit"]
    if entry["kind"] == "close":
        from pma.workmap_resume import close_history
        history = close_history(entry)
        result["outcome"] = entry.get("outcome")
        if history and "identity" in history:
            result["identity"] = copy.deepcopy(history["identity"])
            result["display_id"] = history["display_id"]
            if history.get("reported_consequence"):
                result["consequence"] = history["reported_consequence"]
        else:
            result["identity_status"] = "unknown"
    return result


class SelectionUnavailable(ValueError):
    pass


def _preserve_references(result: dict, sources: list[dict]) -> None:
    """Keep authored links and close context without importing linked prose."""
    links = list(result["related_entries"])
    for source in sources:
        related = source.get("related_entries", [])
        if (not isinstance(related, list)
                or any(not isinstance(link, str) or not link.strip() for link in related)):
            raise SelectionUnavailable("Thread related entries are unreadable")
        links.extend(related)
    result["related_entries"] = list(dict.fromkeys(links))
    closes = [source for source in sources if source["kind"] == "close"]
    if len(closes) == 1:
        for field in ("successor", "residuals"):
            if field in closes[0]:
                value = closes[0][field]
                if (not isinstance(value, list)
                        or any(not isinstance(item, str) or not item.strip() for item in value)):
                    raise SelectionUnavailable("close references are unreadable")
                result[field] = copy.deepcopy(value)


def selection_refs(entry: dict) -> list[str]:
    if not isinstance(entry, dict):
        return []
    sources, evidence = entry.get("source_refs", []), entry.get("evidence_refs", [])
    if not isinstance(sources, list) or not isinstance(evidence, list):
        raise SelectionUnavailable("evidence references are unreadable")
    return list(dict.fromkeys(r for r in sources + evidence if isinstance(r, str) and r.startswith(PREFIX)))


def load_selection(ref: str, repo: Path, *, snapshot: dict | None = None) -> dict:
    match = BINDING.fullmatch(ref)
    if match is None:
        raise SelectionUnavailable("selection Evidence reference is invalid")
    if snapshot is not None and snapshot.get('format_version') == '0.4.3':
        from pma.workmap_transfer import embedded_evidence
        raw = embedded_evidence(snapshot, ref)
        if raw is not None:
            if hashlib.sha256(raw).hexdigest() != match[2]:
                raise SelectionUnavailable('carried selection bytes changed')
            return json.loads(raw)
    folder = (repo / "docs/work-management/evidence").resolve()
    path = (folder / match[1]).resolve()
    if path.parent != folder:
        raise SelectionUnavailable("selection Evidence escapes its permitted directory")
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != match[2]:
            raise SelectionUnavailable("selection Evidence bytes changed")
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        raise SelectionUnavailable(str(exc)) from exc


def validate_selection(snapshot: dict, evidence: dict, *, publishing: bool = False) -> dict:
    """Validate provenance/coverage, not the truth of the author's judgment."""
    required = {"project_id", "source_revision", "criterion", "classification", "thread_entries"}
    if (not isinstance(evidence, dict) or set(evidence) != required
            or evidence["project_id"] != snapshot["project_id"]
            or evidence["criterion"] != CRITERION
            or not isinstance(evidence["source_revision"], str)
            or not evidence["source_revision"]):
        raise SelectionUnavailable("selection scope or criterion is invalid")
    if publishing and evidence["source_revision"] != snapshot["revision"]:
        raise SelectionUnavailable("selection was reviewed against another revision")
    records = {e["id"]: e for e in snapshot["thread_entries"]}
    classes = evidence["classification"]
    if not isinstance(classes, list) or not isinstance(evidence["thread_entries"], list):
        raise SelectionUnavailable("selection collections are invalid")
    indexed = {}
    for c in classes:
        if (not isinstance(c, dict) or set(c) != {"id", "sha256", "disposition", "reason"}
                or not isinstance(c["id"], str) or c["id"] not in records or c["id"] in indexed
                or c["sha256"] != _digest(records[c["id"]])
                or c["disposition"] not in ("keep", "technical_only")
                or not isinstance(c["reason"], str) or not c["reason"].strip()):
            raise SelectionUnavailable("classification lacks exact unchanged source or reason")
        indexed[c["id"]] = c
    if publishing and set(indexed) != set(records):
        raise SelectionUnavailable("publication must account for every existing raw record")
    included, item_ids = set(), set()
    for e in evidence["thread_entries"]:
        if (not isinstance(e, dict)
                or not {"id", "works", "summary", "why", "related_entries", "source_refs"} <= set(e)
                or not set(e) <= {"id", "works", "summary", "why", "related_entries", "source_refs", "revisit", "consequence", "identity", "display_id", "outcome"}
                or any(not isinstance(e[k], str) or not e[k].strip() for k in ("id", "summary", "why"))
                or e["id"] in item_ids):
            raise SelectionUnavailable("meaning entry has no identity, reason or valid fields")
        links = e["related_entries"]
        if (not isinstance(links, list) or not links or any(not isinstance(i, str) for i in links)
                or len(set(links)) != len(links)
                or any(i not in indexed or indexed[i]["disposition"] != "keep" for i in links)
                or e["id"] not in links):
            raise SelectionUnavailable("meaning entry must trace kept raw records")
        allowed_works = {w for i in links for w in records[i]["works"]}
        if (not isinstance(e["works"], list) or not e["works"]
                or any(not isinstance(w, str) or w not in allowed_works for w in e["works"])):
            raise SelectionUnavailable("meaning Work scope exceeds its source records")
        if (not isinstance(e["source_refs"], list) or not e["source_refs"]
                or any(not isinstance(r, str) or not r.strip() for r in e["source_refs"])):
            raise SelectionUnavailable("meaning entry lacks provenance")
        allowed_refs = {r for i in links for r in records[i].get("source_refs", []) + records[i].get("evidence_refs", [])}
        if not set(e["source_refs"]) <= allowed_refs:
            raise SelectionUnavailable("meaning provenance is absent from its raw source")
        if any(field in e for field in ("identity", "display_id", "outcome")):
            from pma.workmap_resume import close_history
            closes = [records[i] for i in links if records[i]["kind"] == "close"]
            if len(closes) != 1 or not all(f in e for f in ("identity", "display_id", "outcome")):
                raise SelectionUnavailable("past identity must belong to one close")
            closing = closes[0]
            history = close_history(closing)
            identity = e["identity"]
            if (not isinstance(identity, dict) or set(identity) != {"work_key", "name", "intent", "origin"}
                    or identity.get("work_key") != closing["works"][0]
                    or e["outcome"] != closing.get("outcome") or not isinstance(e["display_id"], str)):
                raise SelectionUnavailable("past identity or outcome differs from close")
            source = history.get("identity") if history else None
            if source is None and publishing:
                source = snapshot["works"].get(identity["work_key"])
            if source is not None and identity != source:
                raise SelectionUnavailable("past identity differs from its confirmed source")
            if publishing and e["display_id"] != snapshot["display_ids"][identity["work_key"]]:
                raise SelectionUnavailable("past display address differs from its source")
        for field in ("revisit", "consequence"):
            if field in e and (not isinstance(e[field], str) or not e[field].strip()):
                raise SelectionUnavailable("meaning context is unreadable")
        included.update(links)
        item_ids.add(e["id"])
    if included != {i for i, c in indexed.items() if c["disposition"] == "keep"}:
        raise SelectionUnavailable("a kept source is missing from the meaning projection")
    return evidence


def validate_publication(snapshot: dict, request: dict, repo: Path) -> None:
    if not isinstance(request, dict) or not isinstance(request.get("thread_entries"), list):
        # Leave malformed ordinary requests to the existing Core validator.
        return
    if isinstance(request, dict) and request.get("transition_id") in snapshot["receipts"]:
        # Core still checks the original digest. Already committed requests do
        # not need to reload external Evidence merely to recover their receipt.
        return
    for entry in request.get("thread_entries", []):
        refs = selection_refs(entry)
        if refs:
            if len(refs) != 1:
                raise SelectionUnavailable("one reviewed selection per publication is required")
            validate_selection(snapshot, load_selection(refs[0], repo), publishing=True)


def meaning_thread(snapshot: dict, work_key: str | None = None, *, repo: Path,
                   reference_fields: bool = True, include_sources: bool = False) -> dict:
    records = snapshot["thread_entries"]
    relevant = [e for e in records if work_key is None or work_key in e["works"]]
    bindings = [(e, selection_refs(e)) for e in records if selection_refs(e)]
    if not bindings:
        anchor, refs = {"id": None}, []
        evidence = {"classification": [], "thread_entries": [], "source_revision": snapshot["revision"]}
    else:
        anchor, refs = bindings[-1]
        try:
            if len(refs) != 1:
                raise SelectionUnavailable("latest selection reference is ambiguous")
            evidence = validate_selection(snapshot, load_selection(refs[0], repo, snapshot=snapshot))
        except SelectionUnavailable as exc:
            return {"status": "unknown", "thread_entries": [], "raw_record_count": len(relevant),
                    "unreviewed_count": len(relevant), "reason": str(exc)}
    classified = {c["id"]: c for c in evidence["classification"]}
    # This anchor is the publication/provenance of this projection, not another
    # substantive decision. Every later record needs its own semantic review.
    remaining = [e for e in relevant if e["id"] not in classified and not selection_refs(e)]
    pending = [e["id"] for e in remaining if e["kind"] not in TECHNICAL_KINDS | MEANING_KINDS]
    items = [copy.deepcopy(e) for e in evidence["thread_entries"] if work_key is None or work_key in e["works"]]
    try:
        records_by_id = {e["id"]: e for e in records}
        for item in items:
            _preserve_references(item if reference_fields else copy.deepcopy(item),
                                 [records_by_id[i] for i in item["related_entries"]])
        items.extend(direct_meaning(e, reference_fields=reference_fields) for e in remaining if e["kind"] in MEANING_KINDS)
    except SelectionUnavailable as exc:
        return {"status": "unknown", "thread_entries": [], "raw_record_count": len(relevant),
                "unreviewed_count": len(relevant), "reason": str(exc)}
    result = {"status": "needs_review" if pending else "confirmed", "thread_entries": items,
            "raw_record_count": len(relevant), "unreviewed_count": len(pending),
            "unreviewed_record_ids": pending[:20],
            "technical_only_count": sum(classified[e["id"]]["disposition"] == "technical_only" for e in relevant if e["id"] in classified) + sum(e["kind"] in TECHNICAL_KINDS for e in remaining),
            "selection_source": refs[0] if refs else None, "selection_record": anchor["id"],
            "source_revision": evidence["source_revision"], "observed_revision": snapshot["revision"],
            "rendering": "reviewed meaning projection; raw records preserved"}
    if include_sources:
        # Use the reviewed grouping, before authored reference links were
        # added to the display. A reference to B does not mean A summarizes B.
        sources = {e['id']: list(e['related_entries']) for e in evidence['thread_entries']
                   if work_key is None or work_key in e['works']}
        sources.update({e['id']: [e['id']] for e in remaining if e['kind'] in MEANING_KINDS})
        result['source_record_ids'] = sources
    return result


def project_context(context: dict, snapshot: dict, repo: Path) -> dict:
    """Apply the same separation to nested parent/held contexts as well."""
    result = copy.deepcopy(context)
    key = result.get("work")
    if 'notes' in result:
        from pma.workmap_notes import actor_helpers, recorded_evidence
        result['notes'] = []  # Shared free text is historical, not a second current state.
        result['actor_helpers'] = actor_helpers(snapshot, key)
        result['recorded_evidence'] = recorded_evidence(snapshot, key)
    if key is not None and "thread_entries" in result:
        selection = meaning_thread(snapshot, key, repo=repo)
        result["thread_entries"] = selection.pop("thread_entries")
        result["thread_selection"] = selection
    if "close_entry" in result:
        raw = result.pop("close_entry")
        result["close_record_ref"] = raw["id"]
        result["outcome"] = raw.get("outcome")
        result["reopen_condition"] = raw.get("revisit")
        result["residuals"] = copy.deepcopy(raw.get("residuals", []))
        if "successor" in raw:
            result["successor"] = copy.deepcopy(raw["successor"])
    if isinstance(result.get("parent_now"), dict) and "context" in result["parent_now"]:
        result["parent_now"]["context"] = project_context(result["parent_now"]["context"], snapshot, repo)
    if isinstance(result.get("held_now"), dict):
        for item in result["held_now"].get("works", []):
            item["context"] = project_context(item["context"], snapshot, repo)
    return result
