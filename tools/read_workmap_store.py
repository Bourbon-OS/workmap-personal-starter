"""Read a committed Work Map for an AI or Steward, without a human portal."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

from pma.workmap_contract import SnapshotInvalid, _collection_status, inspect_work, validate_snapshot
from pma.workmap_core import TransitionError
from pma.workmap_pin_move import routine_pin_guard, pin_move_scope
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from pma.workmap_resume import closed_identity, inspect_retired_work
from pma.workmap_thread_view import meaning_thread, project_context
from pma.workmap_references import current_reference_status


def id_title_tree(snapshot: dict) -> str:
    """Render current members only, using one validated Store revision."""
    display = snapshot["display_ids"]
    parents = {item["child"]: item["parent"] for item in snapshot["map"]["relations"] if item["kind"] == "parent"}
    children: dict[str | None, list[str]] = {}
    for key in snapshot["map"]["members"]:
        children.setdefault(parents.get(key), []).append(key)
    order = lambda key: tuple(int(part) for part in display[key][1:].split("."))
    for group in children.values():
        group.sort(key=order)
    lines: list[str] = []

    def visit(key: str, prefix: str, last: bool, root: bool = False) -> None:
        marker = "" if root else ("└─ " if last else "├─ ")
        lines.append(f"{prefix}{marker}{display[key]}  {snapshot['works'][key]['name']}")
        descendants = children.get(key, [])
        next_prefix = prefix + ("" if root else ("   " if last else "│  "))
        for index, child in enumerate(descendants):
            visit(child, next_prefix, index == len(descendants) - 1)

    for root in children.get(None, []):
        visit(root, "", True, True)
    return "\n".join(lines)


def pin_move_context(snapshot: dict, actor: str, destination: str,
                     destination_where: str | None = None) -> dict:
    """Project existing move scope once, without preparing or authorizing a move."""
    destination = snapshot['aliases'].get(destination, destination)
    source, note_id, guard = routine_pin_guard(snapshot, actor, destination, destination_where)
    if (guard['destination_pin'] is not None and destination_where is not None
            and guard['destination_pin'].get('where') != destination_where):
        raise ValueError('reactivating a held Pin cannot change its location')
    # Only the shape consumed by the existing read-only collector, not a
    # Transition request, scope assessment, Permission, or review attestation.
    scope_input = {'actor_id': actor, 'changes': [{'target': source, 'value': {
        'new_target': destination, 'resume_note': {'id': note_id},
        'active_pin': {'where': destination_where} if destination_where is not None else {}}}]}
    if source is None:
        held = guard['destination_pin']
        scope_input['changes'] = [{'op': 'update_pin', 'target': destination, 'pin_id': held['id'],
                                   'expected': held, 'value': {**held, 'mode': 'active'}}]
    keys, rows, refs = pin_move_scope(snapshot, scope_input)
    records = {n: {r['id']: copy.deepcopy(r) for r in items} for n, items in rows.items()}
    records.update(dependencies={}, notes={})
    endpoints = {}
    for label in ('source', 'destination'):
        context = guard[label + '_context']
        if context is None:
            endpoints[label] = None
            continue
        # Preserve provider/close information from inspect_work as well as
        # incoming dependencies, but emit each relation only once.
        for d in context['incident_dependencies']:
            records['dependencies'].setdefault(d['id'], copy.deepcopy(d))
        for d in context['dependencies']:
            records['dependencies'][d['id']] = copy.deepcopy(d)
        endpoints[label] = {
            'work_key': source if label == 'source' else destination,
            'parent_chain': [p['work_key'] for p in context['parent_chain']],
            **{n + '_ids': [r['id'] for r in context[n]] for n in
               ('pins', 'permissions', 'action_rules', 'shared_where_pins',
                'ancestor_action_rules', 'incident_dependencies', 'notes')},
        }
    related = set(keys) | {p['work_id'] for p in rows['pins']}
    related.update(k for d in records['dependencies'].values() for k in (d['consumer'], d['provider']))
    # Ancestor restrictions are in the scope records. Do not pull every
    # ancestor's general progress notes into a routine move; include endpoint
    # notes and the actor's other held-work restart material instead.
    current_related = {source, destination} | {p['work_id'] for p in rows['pins'] if p['actor_id'] == actor}
    for n in snapshot['map']['notes']:
        if current_related.intersection(n['targets']):
            records['notes'][n['id']] = copy.deepcopy(n)
    works = {}
    for key in sorted(related):
        current = key in snapshot['map']['members']
        identity = snapshot['works'][key] if current else closed_identity(snapshot, key)
        status = 'current' if current else 'closed_history'
        if not current and snapshot['format_version'] == '0.4.3':
            from pma.workmap_transfer import provider_history, moved_out
            past = provider_history(snapshot, key)
            if past and (moved_out(snapshot, key) or identity is None):
                identity, status = past['provider_identity'], past['provider_status']
        works[key] = {'display_id': snapshot['display_ids'][key],
                      'status': status,
                      'identity': copy.deepcopy(identity),
                      'intent_status': 'confirmed' if identity and identity.get('intent') is not None else 'unknown'}
    unknown_pins = [k for k in snapshot['map']['members']
                    if _collection_status(snapshot, 'pins', k) != 'confirmed']
    view = {
        'actor_id': actor, 'source': source, 'destination': destination,
        'destination_where': destination_where, 'scope_work_keys': list(keys),
        'works': works, 'endpoints': endpoints, 'records': records,
        'actor_pin_ids': [p['id'] for p in rows['pins'] if p['actor_id'] == actor],
        'source_pin_id': guard['source_pin']['id'] if guard['source_pin'] else None,
        'destination_pin_id': guard['destination_pin']['id'] if guard['destination_pin'] else None,
        'routine_resume_note_id': guard['resume_note']['id'] if guard['resume_note'] else None,
        'collection_visibility': {k: {n: _collection_status(snapshot, n, k)
                                    for n in ('pins', 'permissions', 'action_rules')} for k in keys},
        'actor_pin_visibility': {'status': 'unknown' if unknown_pins else 'confirmed',
                                 'unconfirmed_work_keys': unknown_pins},
    }
    # Deduplicate reference text only. Records/constraints are lossless; these
    # explicitly named *_ref_ids are view links, never Store source references.
    ref_fields = {'refs': 'ref_ids', 'source_refs': 'source_ref_ids', 'evidence_refs': 'evidence_ref_ids'}
    def collect(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ref_fields and isinstance(v, list): refs.update(v)
                else: collect(v)
        elif isinstance(value, list):
            for v in value: collect(v)
    collect(view)
    ref_ids = {r: 'r' + str(i) for i, r in enumerate(sorted(refs), 1)}
    def encode(value):
        if isinstance(value, dict):
            return {ref_fields.get(k, k): [ref_ids[r] for r in v]
                    if k in ref_fields and isinstance(v, list) else encode(v)
                    for k, v in value.items()}
        return [encode(v) for v in value] if isinstance(value, list) else value
    return {**encode(view), 'references': {i: r for r, i in ref_ids.items()},
            'review': {'authorization': 'not_evaluated', 'current_scope': 'not_reviewed',
                       'evidence_contents_versions_applicability': 'not_reviewed'},
            'boundary': 'Recorded review inputs only. Unknown is not absence; references are not verified approval. Recheck the current revision before applying.'}


def brief_work_context(context: dict, snapshot: dict, repo: Path,
                       selected_ids: frozenset[str] = frozenset()) -> dict:
    """A read-only index, never a saved summary or an authorization decision."""
    key = context['work']
    current = key in snapshot['map']['members']
    identity = snapshot['works'][key] if current else context['identity']
    chain = context.get('parent_chain', [])
    scope_keys = [p['work_key'] for p in chain]
    selection = copy.deepcopy(context['thread_selection'])
    # Selection audit is recoverable from the existing --thread view. Keep
    # status, both revisions and all pending-review information at first read;
    # uncertain/failed selection must also retain its diagnostic provenance.
    deferred_selection_audit = selection.get('status') == 'confirmed'
    if deferred_selection_audit:
        for field in ('raw_record_count', 'technical_only_count', 'selection_source',
                      'selection_record', 'rendering'):
            selection.pop(field, None)

    def index(entries):
        return [copy.deepcopy(e) if e['id'] in selected_ids else
                {f: copy.deepcopy(e[f]) for f in
                 ('id', 'summary', 'related_entries') if f in e}
                for e in entries]

    def relation_index(items):
        return [{f: item[f] for f in ('work_key', 'display_id', 'name')} for item in items]

    view = {
        'project_id': snapshot['project_id'], 'revision': snapshot['revision'],
        'work': {'work_key': key, 'display_id': snapshot['display_ids'][key],
                 'status': 'current' if current else 'closed_history',
                 'identity': copy.deepcopy(identity),
                 'identity_status': 'confirmed' if identity else 'unknown',
                 'intent_status': 'confirmed' if identity and identity.get('intent') is not None else 'unknown'},
        'ancestors': [{f: copy.deepcopy(p[f]) for f in
                       ('work_key', 'display_id', 'name', 'intent', 'intent_status')}
                      for p in chain[:-1]],
        'collections': {},
        'map_visibility': {n: {
            'unconfirmed_work_keys': [k for k in snapshot['map']['members']
                                     if _collection_status(snapshot, n, k) != 'confirmed']}
            for n in ('pins', 'permissions', 'action_rules')},
        'dependencies': copy.deepcopy(context.get('dependencies', [])),
        'incoming_dependencies': [copy.deepcopy(r) for r in snapshot['map']['relations']
                                  if r['kind'] == 'dependency' and r['provider'] == key],
        'external_dependency_history': copy.deepcopy(context.get('external_dependency_history', [])),
        'children': relation_index(context.get('children', [])),
        'siblings': relation_index(context.get('siblings', [])),
        'thread': {**selection, 'entries': index(context['thread_entries'])},
        'evidence': {'scope': 'all explicit results in recorded_evidence projection; not all technical records or current status',
                     'entries': index(context.get('recorded_evidence', []))},
        'actor_helpers': copy.deepcopy(context.get('actor_helpers', [])),
        'work_provenance': copy.deepcopy(context.get('work_provenance')),
        'retrieve': {'work': snapshot['display_ids'][key], 'expected_revision': snapshot['revision'],
                     'detail': '--work', 'meaning': '--thread [--entry <id>]',
                     'results': '--evidence [--entry <id>]', 'technical': '--history'},
        'omitted': ['unrequested meaning and result bodies and their provenance (fetch indexed entry IDs at expected_revision)', 'ancestor Origin',
                    'related Work purposes/details', 'technical history/receipts'],
        'boundary': 'Pin, Permission and Action Rule values cover only Work keys in collections; tree, siblings and map_visibility do not expand that scope. Recorded inputs only; not an authorization or completion assessment. Unknown is not absence. Re-read the current revision before a decision or write.',
    }
    for n, visibility in view['map_visibility'].items():
        visibility['status'] = 'unknown' if visibility['unconfirmed_work_keys'] else 'confirmed'
    if deferred_selection_audit:
        view['omitted'].append(
            'confirmed selection audit/counts; recover with --thread at expected_revision')
    for scope_key in scope_keys:
        view['collections'][scope_key] = {n: {
            'status': _collection_status(snapshot, n, scope_key),
            'recorded': [copy.deepcopy(item) for item in snapshot['map'][n]
                         if (item['work_id'] == scope_key if n == 'pins' else scope_key in item['targets'])]}
            for n in ('pins', 'permissions', 'action_rules')}
    if not current:
        raw = next(e for e in snapshot['thread_entries'] if e['id'] == context['close_record_ref'])
        view['close'] = {f: copy.deepcopy(raw[f]) for f in
                         ('id', 'summary', 'why', 'outcome', 'successor', 'residuals', 'revisit', 'source_refs', 'evidence_refs') if f in raw}
        view['parent_at_close'] = copy.deepcopy(context['parent_at_close'])
        parent = context.get('parent_now')
        view['parent_now'] = ({'status': parent['status'],
                               'work_key': parent.get('work_key', parent.get('context', {}).get('work'))}
                              if parent else None)
        view['held_now'] = {'status': context['held_now']['status'],
                            'works': [{'work_key': x['pin']['work_id'],
                                       'display_id': snapshot['display_ids'][x['pin']['work_id']]}
                                      for x in context['held_now']['works']]}
        view['omitted'].append('historical authority and current parent/held details (read their Work IDs)')

    # Reference IDs belong only to this response. Original references are kept
    # in the same response and must be resolved before any Store operation.
    fields = {'refs': 'ref_ids', 'source_refs': 'source_ref_ids', 'evidence_refs': 'evidence_ref_ids'}
    refs = set()
    def collect(value):
        if isinstance(value, dict):
            for name, item in value.items():
                if name in fields and isinstance(item, list):
                    refs.update(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(view)
    from pma.workmap_references import inspect_references
    reference_rows = {row['ref']: row for row in inspect_references(sorted(refs), repo)}
    deferred_pin_sources = False
    for collections in view['collections'].values():
        pins = collections['pins']
        if pins['status'] != 'confirmed':
            continue
        for pin in pins['recorded']:
            sources = pin.get('refs', [])
            if sources and all(reference_rows[ref]['availability'] == 'available' for ref in sources):
                del pin['refs']
                deferred_pin_sources = True
    if deferred_pin_sources:
        view['omitted'].append(
            'Pin source lists with available paths (not verified contents/validity); '
            'read each Pin work_id with --work <ID/key> --expected-revision <revision> without --brief')
        refs.clear()
        collect(view)
    ids = {ref: 'r' + str(i) for i, ref in enumerate(sorted(refs), 1)}
    def encode(value):
        if isinstance(value, dict):
            return {fields.get(name, name): [ids[r] for r in item]
                    if name in fields and isinstance(item, list) else encode(item)
                    for name, item in value.items()}
        return [encode(item) for item in value] if isinstance(value, list) else value
    references = {}
    for ref in sorted(refs):
        row = reference_rows[ref]
        item = {f: row[f] for f in ('ref', 'availability', 'version_status')}
        if row['availability'] != 'available':
            item['reason'] = row['reason']
        references[ids[row['ref']]] = item
    return {**encode(view), 'references': references,
            'reference_boundary': 'Availability checks file existence only; contents, revision and applicability remain unverified.'}


def _grouped_original_lookup_hint(snapshot: dict, work_key: str, selected_ids: list[str],
                                  selection: dict, sources: dict | None, repo: Path) -> dict | None:
    """Suggest explicit reads only after a failed, same-Work meaning lookup."""
    if selection['status'] != 'confirmed' or not 1 <= len(selected_ids) <= 20:
        return None
    visible = {e['id'] for e in selection['thread_entries']}
    raw_counts: dict[str, int] = {}
    for entry in snapshot['thread_entries']:
        if work_key in entry['works']:
            raw_counts[entry['id']] = raw_counts.get(entry['id'], 0) + 1
    if any(i in visible or raw_counts.get(i) != 1 for i in selected_ids):
        return None
    if sources is None:
        # Ordinary views and successful lookups do not load this mapping.
        # The related route already supplied it from the same selection read.
        checked = meaning_thread(snapshot, work_key, repo=repo, include_sources=True)
        if checked['status'] != 'confirmed' or checked['thread_entries'] != selection['thread_entries']:
            return None
        sources = checked.get('source_record_ids', {})
    if not isinstance(sources, dict):
        return None
    grouped_ids = []
    for original in selected_ids:
        matches = [key for key, ids in sources.items()
                   if key in visible and isinstance(ids, list) and len(ids) > 1 and original in ids]
        if len(matches) != 1:
            return None
        grouped_ids.append(matches[0])
    return {
        'kind': 'grouped_original_requested_as_meaning', 'entry_ids': selected_ids,
        'view': '--history', 'work': work_key, 'expected_revision': snapshot['revision'],
        'grouped_lookup': {'view': '--thread', 'entry_ids': list(dict.fromkeys(grouped_ids)), 'related': True},
        'message': '指定IDは、まとめた表示に含まれる元の記録です。読取許可内なら、同じWork・同じ版で'
                   '--history --entry に元IDを一件ずつ指定して本文を確認できます。訂正・補足も確認したい時は、'
                   '対応する表示IDを--thread --entryに指定し、--relatedを付けられます。'
                   '本文は未取得のままであり、許可や内容の採否を示す案内ではありません。'}


def read_result(directory: Path, display_id: str | None = None, *, tree: bool = False,
                history: bool = False, thread: bool = False, expected_project_id: str | None = None,
                evidence_repo: Path | None = None, backend: str = 'text', entry_id: str | list[str] | None = None,
                pin_destination: str | None = None, actor: str | None = None,
                destination_where: str | None = None, pin_inputs: bool = False,
                brief: bool = False, evidence: bool = False,
                expected_revision: str | None = None, subtree: bool = False,
                expected_scope: str | None = None, related: bool = False,
                related_limit: int | None = None) -> dict:
    if related and (not display_id or entry_id is None or not (evidence or thread)
                    or brief or tree or history or subtree or pin_destination is not None):
        raise ValueError('related reading requires selected entries in one Work evidence/thread view')
    if related_limit is not None and (not related or type(related_limit) is not int
                                      or not 1 <= related_limit <= 50):
        raise ValueError('related limit requires --related and a value between 1 and 50')
    if subtree and (not display_id or tree or brief or evidence or history or thread
                    or pin_destination is not None or entry_id is not None):
        raise ValueError('subtree requires one Work and cannot combine other views')
    if expected_scope is not None:
        if (not subtree or expected_revision is not None or not isinstance(expected_scope, str)
                or len(expected_scope) != 64 or any(c not in '0123456789abcdef' for c in expected_scope)):
            raise ValueError('expected scope requires subtree and one SHA-256; not a global revision')
    if expected_revision is not None and (not isinstance(expected_revision, str) or not expected_revision.strip()):
        raise ValueError('expected revision must be nonempty')
    if tree and (display_id is not None or history or thread or evidence or pin_destination is not None or entry_id is not None) and not brief:
        raise ValueError('tree combines with one Work only in brief mode')
    if (brief or evidence) and (not display_id or (tree and evidence) or history or thread or pin_destination is not None or (brief and evidence)):
        raise ValueError('brief/evidence requires one Work and cannot be combined with another view')
    selected_ids = []
    if entry_id is not None:
        if isinstance(entry_id, str):
            selected_ids = [entry_id]
        elif isinstance(entry_id, list) and (brief or (display_id and (evidence or thread))):
            selected_ids = entry_id
        else:
            raise ValueError('multiple entry IDs require brief or one Work evidence/thread mode')
        if (not selected_ids or any(not isinstance(i, str) or not i.strip() for i in selected_ids)
                or len(set(selected_ids)) != len(selected_ids)):
            raise ValueError('entry IDs must be nonempty and distinct')
    if related and len(selected_ids) > 20:
        raise ValueError('related reading accepts at most 20 selected entries')
    if pin_destination is not None:
        if not actor or not actor.strip() or not pin_destination.strip():
            raise ValueError('Pin review requires an actor and destination')
        if destination_where is not None and not destination_where.strip():
            raise ValueError('prospective destination location must be nonempty')
        if display_id is not None or tree or history or thread or entry_id is not None:
            raise ValueError('Pin review cannot be combined with another reader view')
    elif actor is not None or destination_where is not None or pin_inputs:
        raise ValueError('actor/location requires Pin review mode')
    if entry_id is not None and not (history or thread or evidence or brief):
        raise ValueError('entry lookup requires explicit brief, thread, evidence or history mode')
    try:
        store = open_store(directory, backend)
        committed = store.read(read_only=True) if backend == 'sqlite' and (pin_destination is not None or brief or evidence or subtree) else store.read()
        snapshot = validate_snapshot(committed.snapshot)
        if snapshot["metadata"].get("store_role") != "canonical_current":
            return {"status": "unknown", "reason": "selected Store is not declared canonical current"}
        if expected_project_id is not None and snapshot["project_id"] != expected_project_id:
            return {"status": "unknown", "reason": "selected Store belongs to another project"}
        if expected_revision is not None and snapshot['revision'] != expected_revision:
            return {'status': 'unknown', 'revision': snapshot['revision'],
                    'expected_revision': expected_revision,
                    'reason': 'Store revision changed; re-read and reconsider before combining results'}
        if pin_destination is not None:
            try:
                context = pin_move_context(snapshot, actor, pin_destination, destination_where)
            except (ValueError, TransitionError) as exc:
                return {'status': 'unknown', 'revision': snapshot['revision'], 'reason': str(exc)}
            result = {'status': 'confirmed', 'view': 'pin_move_review', 'read_only': True,
                    'project_id': snapshot['project_id'], 'revision': snapshot['revision'],
                    'store_token': committed.token, 'recorded_at': snapshot['recorded_at'],
                    'context': context}
            if pin_inputs:
                from tools.workmap_pin_inputs import pin_inputs_draft
                try:
                    result['inputs_draft'] = pin_inputs_draft(snapshot, directory, backend,
                        actor=actor, destination=pin_destination, where=destination_where)
                except (OSError, ValueError, TransitionError) as exc:
                    # Keep the recorded context visible, even if a bound draft is unavailable.
                    result['inputs_draft_error'] = str(exc)
            return result
        if tree and not brief:
            result = {"status": "confirmed", "store_token": committed.token, "revision": snapshot["revision"], "tree": id_title_tree(snapshot)}
            if snapshot['format_version'] == '0.4.3':
                from pma.workmap_transfer import detached_markers
                result['detached_markers'] = detached_markers(snapshot)
            return result
        repo = evidence_repo or Path(__file__).resolve().parents[1]
        work_key = snapshot["aliases"].get(display_id, display_id if display_id in snapshot['display_ids'] else None) if display_id is not None else None
        if display_id is not None and work_key is None:
            return {"status": "unknown", "reason": "display ID is not in the committed alias index", "revision": snapshot["revision"]}
        if subtree:
            if work_key not in snapshot['map']['members']:
                return {'status': 'unknown', 'reason': 'subtree root is not a current Work',
                        'revision': snapshot['revision']}
            from pma.workmap_subtree import subtree_view, subtree_digest
            view = subtree_view(snapshot, work_key, repo=repo)
            digest = subtree_digest(view, snapshot['project_id'])
            if expected_scope is not None and expected_scope != digest:
                return {'status': 'unknown', 'reason': 'selected scope changed; read current scope and reconsider',
                        'revision': snapshot['revision'], 'scope_digest': digest}
            return {'status': 'confirmed', 'view': 'subtree', 'read_only': True,
                    'project_id': snapshot['project_id'], 'revision': snapshot['revision'],
                    'store_token': committed.token, 'recorded_at': snapshot['recorded_at'],
                    'scope_digest': digest,
                    'context': {k: v for k, v in view.items() if k != 'history_bindings'},
                    'boundary': 'Scope equality covers this read projection only, not write authority. '
                                'Revision remains the exact provenance of this single snapshot. '
                                'Not a portable writable Map or a cross-PC merge result.'}
        if work_key is not None and work_key not in snapshot['map']['members'] and snapshot['format_version'] == '0.4.3':
            from pma.workmap_transfer import moved_out, historical_identities
            past = historical_identities(snapshot)
            if (moved_out(snapshot, work_key) or work_key in past and work_key not in snapshot['retirements']) and not (thread or history or evidence):
                return {'status': 'confirmed', 'revision': snapshot['revision'], 'current': False,
                        'view': 'detached_history', 'identity_at_release': past.get(work_key),
                        'boundary': 'Historical identity only; no destination current Work, Pin or Permission is inferred.'}
        if evidence:
            from pma.workmap_notes import recorded_evidence
            records = recorded_evidence(snapshot, work_key)
            if entry_id is not None:
                if any(sum(e['id'] == i for e in records) != 1 for i in selected_ids):
                    return {'status': 'unknown', 'revision': snapshot['revision'],
                            'reason': 'result ID is absent or ambiguous in the requested explicit Evidence projection'}
                records = [e for e in records if e['id'] in selected_ids]
            result = {'status': 'confirmed', 'revision': snapshot['revision'], 'work': work_key,
                    'evidence': records,
                    'boundary': 'All requested explicit result records, not all technical history or current status. Referenced contents/versions/applicability are not automatically verified.'}
            if related:
                from pma.workmap_related_records import related_record_view
                result['related_records'] = related_record_view(snapshot, work_key, records,
                    repo=repo, limit=related_limit if related_limit is not None else 10)
            return result
        if history:
            records = [e for e in snapshot["thread_entries"] if work_key is None or work_key in e["works"]]
            if entry_id is not None:
                records = [e for e in records if e['id'] == entry_id]
                if len(records) != 1:
                    return {'status': 'unknown', 'revision': snapshot['revision'],
                            'reason': 'raw entry is absent from the requested history scope'}
            tids = {e.get("transition_id") for e in records}
            imported = {}
            if snapshot['format_version'] == '0.4.3':
                from pma.workmap_transfer import validate_transfer_history
                _, imported = validate_transfer_history(snapshot)
                imported = {k: v for k, v in imported.items() if (work_key is None and entry_id is None) or k in tids}
            return {"status": "confirmed", "revision": snapshot["revision"], "raw_records": records,
                    'imported_results': imported,
                    "receipts": {k: v for k, v in snapshot["receipts"].items() if (work_key is None and entry_id is None) or k in tids},
                    "receipt_scope": "all" if work_key is None and entry_id is None else "linked records only; use unscoped history for all receipts"}
        if thread or display_id is None:
            selection_sources = None
            if related:
                selection = meaning_thread(snapshot, work_key, repo=repo, include_sources=True)
                selection_sources = selection.pop('source_record_ids', {})
            else:
                selection = meaning_thread(snapshot, work_key, repo=repo)
            related_view = None
            if entry_id is not None:
                if (selection['status'] == 'unknown'
                        or any(sum(e['id'] == i for e in selection['thread_entries']) != 1 for i in selected_ids)):
                    incomplete = {**selection, 'thread_entries': [],
                                  'entry_scope': 'no bodies returned; requested selection incomplete; full Work status/counts retained'}
                    result = {'status': 'unknown', 'revision': snapshot['revision'], 'thread': incomplete,
                              'reason': 'meaning ID is absent, ambiguous, or selection is unavailable'}
                    if selection['status'] != 'unknown' and work_key is not None:
                        from pma.workmap_notes import recorded_evidence
                        records = recorded_evidence(snapshot, work_key)
                        result_ids = [i for i in selected_ids
                                      if not any(e['id'] == i for e in selection['thread_entries'])
                                      and sum(e['id'] == i for e in records) == 1]
                        if result_ids:
                            result['lookup_hint'] = {
                                'kind': 'result_requested_as_meaning', 'entry_ids': result_ids,
                                'view': '--evidence', 'work': work_key,
                                'expected_revision': snapshot['revision'],
                                'message': '指定したIDは結果の記録です。--threadではなく--evidenceで、'
                                           '同じWork・同じ版から取得してください。これは判断理由の欠落を示しません。'}
                        else:
                            hint = _grouped_original_lookup_hint(snapshot, work_key, selected_ids,
                                                                 selection, selection_sources, repo)
                            if hint is not None:
                                result['lookup_hint'] = hint
                    return result
                chosen = [e for e in selection['thread_entries'] if e['id'] in selected_ids]
                if related:
                    from pma.workmap_related_records import related_record_view
                    related_view = related_record_view(snapshot, work_key, chosen, repo=repo,
                        limit=related_limit if related_limit is not None else 10,
                        selection={**selection, 'source_record_ids': selection_sources},
                        requested_projection='thread')
                selection['thread_entries'] = chosen
                selection['entry_scope'] = ('one meaning item' if len(selected_ids) == 1 else 'selected meaning items') + '; selection status/counts still describe the full requested Work scope'
            result = {"status": selection["status"], "revision": snapshot["revision"], "thread": selection}
            if related_view is not None:
                result['related_records'] = related_view
            if display_id is None and not thread:
                current_map = copy.deepcopy(snapshot['map'])
                members = set(current_map['members'])
                # A historical restart note is not a current instruction. Keep
                # raw storage and references to past providers unchanged.
                current_map['notes'] = []
                from pma.workmap_notes import actor_helpers, recorded_evidence
                result['actor_helpers'] = actor_helpers(snapshot)
                result['recorded_evidence'] = recorded_evidence(snapshot)
                result.update(project_id=snapshot["project_id"], works={k: snapshot["works"][k] for k in snapshot["map"]["members"]},
                              map=current_map, display_ids={k: snapshot["display_ids"][k] for k in snapshot["map"]["members"]},
                              completeness=snapshot["metadata"].get("completeness"))
                if snapshot['format_version'] == '0.4.3':
                    from pma.workmap_transfer import detached_markers
                    result.update(map_id=snapshot['map_id'], detached_markers=detached_markers(snapshot))
                result['reference_status'] = current_reference_status(result, repo)
            return result
        context = inspect_work(snapshot, work_key) if work_key in snapshot["map"]["members"] else inspect_retired_work(snapshot, work_key)
        context = project_context(context, snapshot, repo)
        if brief:
            available = context['thread_entries'] + context.get('recorded_evidence', [])
            if any(sum(e['id'] == i for e in available) != 1 for i in selected_ids):
                return {'status': 'unknown', 'revision': snapshot['revision'],
                        'reason': 'requested ID is absent, ambiguous or unavailable in the target Work meaning/result projections'}
            result = {'status': 'confirmed', 'view': 'work_brief', 'store_token': committed.token,
                      'context': brief_work_context(context, snapshot, repo, frozenset(selected_ids))}
            if tree:
                result['tree'] = id_title_tree(snapshot)
            return result
        context['reference_status'] = current_reference_status(context, repo)
        return {"status": "confirmed", "store_token": committed.token, "context": context}
    except (OSError, StoreError, SnapshotInvalid) as exc:
        return {"status": "unknown", "reason": f"committed Work Map is unreadable: {type(exc).__name__}: {exc}"}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Read one committed Work Map revision")
    parser.add_argument("--store", type=Path, required=True, help="absolute directory of the canonical Store")
    parser.add_argument("--backend", choices=("text", "sqlite"), default="text")
    parser.add_argument("--work", help="display ID or stable work_key for a bounded context")
    parser.add_argument("--tree", action="store_true", help="current ID/title tree; combine with --work and --brief for one snapshot")
    views = parser.add_mutually_exclusive_group()
    views.add_argument("--thread", action="store_true", help="show reviewed meaning only, with unknown/pending selection visible")
    views.add_argument("--history", action="store_true", help="explicit raw technical/provenance history and receipts")
    views.add_argument('--brief', action='store_true', help='Work purpose/safety inputs and indexes; bodies fetched when needed')
    views.add_argument('--evidence', action='store_true', help='explicit recorded results for one Work, not current status')
    views.add_argument('--subtree', action='store_true', help='partial Map with ancestors, boundary links and reason/result indexes')
    views.add_argument('--pin-destination', help='read-only own-Pin move context for this destination; not a prepared request')
    parser.add_argument('--actor', help='actor identity for --pin-destination')
    parser.add_argument('--destination-where', help='prospective shared location for a new destination Pin')
    parser.add_argument('--pin-inputs', action='store_true',
                        help='include an editable blank input draft; not a request or completed review')
    parser.add_argument('--entry', action='append', help='one projected ID; repeat with one Work --brief/--thread/--evidence to read selected bodies from the same snapshot')
    parser.add_argument('--related', action='store_true',
                        help='opt in to one-hop same-Work references and recording metadata for selected entries')
    parser.add_argument('--related-limit', type=int, help='maximum additional records, 1..50 (default 10; body size also bounded)')
    parser.add_argument('--expected-revision', help='reject staged retrieval if the Store revision has changed')
    parser.add_argument('--expected-scope', help='subtree read projection SHA-256; never write authority')
    parser.add_argument('--compact', action='store_true', help='omit JSON formatting whitespace; preserve all values and statuses')
    parser.add_argument('--evidence-repo', type=Path, help='root resolving the snapshot-bound Thread selection')
    parser.add_argument("--expected-project", default="project:ashiba-steward-development")
    args = parser.parse_args()
    if not args.store.is_absolute():
        parser.error("--store must be an absolute path")
    if args.entry and not (args.history or args.thread or args.evidence or args.brief):
        parser.error('--entry requires --brief, --thread, --evidence or --history')
    if args.entry and len(args.entry) > 1 and not (args.brief or (args.work and (args.thread or args.evidence))):
        parser.error('repeated --entry requires --brief or one Work --thread/--evidence')
    if (args.brief or args.evidence) and not args.work:
        parser.error('--brief/--evidence requires --work')
    if args.tree and (args.work or args.thread or args.history or args.evidence or args.entry) and not args.brief:
        parser.error("--tree combines with --work only with --brief")
    if args.pin_destination is not None:
        if not args.actor or not args.actor.strip() or not args.pin_destination.strip():
            parser.error('--pin-destination requires a nonempty --actor and destination')
        if args.work or args.tree or args.entry:
            parser.error('--pin-destination cannot be combined with another view')
        if args.destination_where is not None and not args.destination_where.strip():
            parser.error('--destination-where must be nonempty')
    elif args.actor is not None or args.destination_where is not None or args.pin_inputs:
        parser.error('--actor/--destination-where requires --pin-destination')
    entries = args.entry if args.entry and (args.brief or len(args.entry) > 1) else args.entry[0] if args.entry else None
    if args.related and (not args.work or not args.entry or not (args.evidence or args.thread)
                         or args.tree or args.pin_destination is not None):
        parser.error('--related requires --work, --entry and --evidence or --thread')
    if args.related and len(args.entry) > 20:
        parser.error('--related accepts at most 20 selected entries')
    if args.related_limit is not None and (not args.related or not 1 <= args.related_limit <= 50):
        parser.error('--related-limit requires --related and a value between 1 and 50')
    result = read_result(args.store, args.work, tree=args.tree, thread=args.thread, history=args.history, expected_project_id=args.expected_project, backend=args.backend, entry_id=entries, evidence_repo=args.evidence_repo, pin_destination=args.pin_destination, actor=args.actor, destination_where=args.destination_where, pin_inputs=args.pin_inputs, brief=args.brief, evidence=args.evidence, expected_revision=args.expected_revision, subtree=args.subtree, expected_scope=args.expected_scope, related=args.related, related_limit=args.related_limit)
    json.dump(result, sys.stdout, ensure_ascii=False,
              indent=None if args.compact else 2,
              separators=(',', ':') if args.compact else None)
    sys.stdout.write("\n")
    return 0 if (result["status"] == "confirmed" and 'inputs_draft_error' not in result
                 and result.get('related_records', {}).get('status', 'confirmed') == 'confirmed') else 2


if __name__ == "__main__":
    raise SystemExit(main())
