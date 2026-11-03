"""Opt-in, one-hop context over existing visible Work records; never authority.

No new database, text inference or linked source-document reads. The existing
meaning projection may validate its selection Evidence. Ordinary reader/Pin
paths never call this helper; the caller supplies one committed snapshot.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from pma.workmap_notes import recorded_evidence
from pma.workmap_thread_view import meaning_thread

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_BODY_CHARS = 16000


def related_record_view(snapshot: dict, work_key: str, requested: list[dict], *,
                        repo: Path, limit: int = DEFAULT_LIMIT,
                        selection: dict | None = None, requested_projection: str = 'evidence') -> dict:
    """Return explicitly linked, normally visible records in this Work only.

    `confirmed` describes retrieval, not whether a conclusion is still valid.
    Even a complete page cannot prove that an unrecorded correction is absent.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f'related limit must be between 1 and {MAX_LIMIT}')
    if not requested or len(requested) > 20:
        raise ValueError('related reading requires between 1 and 20 selected records')
    if requested_projection not in ('thread', 'evidence'):
        raise ValueError('related reading requires a thread or evidence projection')
    try:
        return _related_view(snapshot, work_key, requested, repo=repo, limit=limit,
                             selection=selection, requested_projection=requested_projection)
    except (KeyError, TypeError, ValueError):
        return {'status': 'unknown', 'entries': [], 'complete': False,
                'reason': 'related record fields are unreadable'}


def _related_view(snapshot, work_key, requested, *, repo, limit, selection, requested_projection):
    if selection is None:
        selection = meaning_thread(snapshot, work_key, repo=repo, include_sources=True)
    raw = {e['id']: e for e in snapshot['thread_entries'] if work_key in e['works']}
    roots = {e['id'] for e in requested}

    def links(entry):
        authored = entry.get('related_entries', [])
        original = raw.get(entry['id'], {}).get('related_entries', [])
        if (not isinstance(authored, list) or not isinstance(original, list)
                or any(not isinstance(i, str) or not i.strip() for i in authored + original)):
            raise ValueError('unreadable related IDs')
        return list(dict.fromkeys(authored + original))

    def metadata(entry):
        source = raw.get(entry['id'])
        if source is None:
            return {'id': entry['id'], 'status': 'unknown'}
        return {k: copy.deepcopy(source[k]) for k in
                ('id', 'kind', 'actor', 'occurred_at', 'recorded_at')}

    visible = [('thread', e) for e in selection.get('thread_entries', [])]
    visible.extend(('evidence', e) for e in recorded_evidence(snapshot, work_key))
    by_projection = {(view, e['id']): e for view, e in visible}
    if (any((requested_projection, i) not in by_projection for i in roots)
            or len(by_projection) != len(visible)):
        return {'status': 'unknown', 'entries': [], 'complete': False,
                'reason': 'selected records are unavailable or ambiguous in visible projections'}

    def source_ids(view, entry):
        ids = selection.get('source_record_ids', {}).get(entry['id'], [entry['id']]) if view == 'thread' else [entry['id']]
        # Approved grouping resolves source IDs to their normal display; it
        # never imports another Work's raw body or treats authored links as aliases.
        return {entry['id']} | {i for i in ids if i in raw}

    root_sources = {i for e in requested for i in source_ids(requested_projection, e)}
    root_links = {i for e in requested for i in set(links(e)) - source_ids(requested_projection, e)}
    matched = [(view, e) for view, e in visible if e['id'] not in roots
               and (source_ids(view, e).intersection(root_links)
                    or (set(links(e)) - source_ids(view, e)).intersection(root_sources))]
    available_ids = {i for view, e in visible for i in source_ids(view, e)}
    unresolved = sorted(root_links - available_ids)
    entries, omitted, used = [], [], 0
    for view, entry in matched:
        body = copy.deepcopy(entry)
        if links(entry):
            body['related_entries'] = links(entry)
        row = {'projection': view, 'record': body, 'record_metadata': metadata(entry),
               'source_record_ids': sorted(source_ids(view, entry))}
        size = len(json.dumps(row, ensure_ascii=False))
        if len(entries) >= limit or used + size > MAX_BODY_CHARS:
            omitted.append(entry['id'])
            continue
        entries.append(row)
        used += size
    state = selection['status']
    return {
        'status': state,
        'complete': state == 'confirmed' and not omitted and not unresolved,
        'requested_metadata': [metadata(e) for e in requested],
        'entries': entries, 'matched_count': len(matched),
        'omitted_ids': omitted[:MAX_LIMIT], 'omitted_count': len(omitted),
        'unresolved_link_ids': unresolved[:MAX_LIMIT], 'unresolved_link_count': len(unresolved),
        'selection_status': state,
        'unreviewed_count': selection.get('unreviewed_count', 0),
        'limits': {'records': limit, 'body_characters': MAX_BODY_CHARS},
        'boundary': 'Explicit one-hop references in the same Work and visible projections only. '
                    'Reviewed source grouping is resolved without merging thread/evidence displays. '
                    'No linked technical prose, other Work bodies or recursive expansion. '
                    'Missing links, withdrawal scope, truth and Owner approval are not inferred. '
                    'Metadata belongs to the anchor record ID; source_record_ids can list further sources. '
                    'Actor is the recorder, not necessarily the speaker or approver; '
                    'recording time is not occurrence time. Omitted/unresolved IDs need explicit retrieval.',
    }
