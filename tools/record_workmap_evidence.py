"""Record one observed result through the existing Work Map Core.

The caller supplies the result and sources, not a completion decision. IDs,
recording time, persistence and exact readback are mechanical. Actor identity
continues to rely on the local operator boundary; this command grants nothing.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pma.workmap_contract import ContextUnknown, SnapshotInvalid, validate_snapshot
from pma.workmap_canonical_core import CanonicalWorkMapCore
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, UnsupportedTransition, _digest
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from tools.apply_workmap_transition import _apply_request


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} is required')
    return value


def _related_ids(values):
    if not isinstance(values, (list, tuple)) or len(values) > 50:
        raise ValueError('related entries must be a list of at most 50 record IDs')
    return list(dict.fromkeys(_text(value, 'related entry') for value in values))


def _check_request(request):
    """A saved request is never an entry point for another kind of operation."""
    if not isinstance(request, dict) or set(request) != {
        'format_version', 'project_id', 'transition_id', 'actor_id', 'base_revision',
        'intent', 'source_refs', 'preconditions', 'changes', 'thread_entries',
    }:
        raise ValueError('saved evidence request has invalid fields')
    changes, entries = request['changes'], request['thread_entries']
    if (not isinstance(changes, list) or len(changes) != 1
            or not isinstance(changes[0], dict) or set(changes[0]) != {'op', 'target'}
            or changes[0]['op'] != 'append_thread'
            or not isinstance(entries, list) or len(entries) != 1):
        raise ValueError('only one evidence append is supported')
    entry = entries[0]
    required = {
        'id', 'works', 'kind', 'summary', 'why', 'occurred_at', 'recorded_at',
        'actor', 'source_refs', 'transition_id',
    }
    if (not isinstance(entry, dict) or not required <= set(entry)
            or not set(entry) <= required | {'related_entries'}
            or entry['kind'] != 'evidence_recorded'
            or entry['works'] != [changes[0]['target']]
            or entry['actor'] != request['actor_id']
            or entry['transition_id'] != request['transition_id']
            or request['preconditions'] != []):
        raise ValueError('saved request is not an exact technical evidence record')
    if 'related_entries' in entry:
        links = _related_ids(entry['related_entries'])
        if links != entry['related_entries'] or entry['id'] in links:
            raise ValueError('saved related entry IDs must be distinct and not self-referential')


def _readback(store_path, backend, request):
    result = CanonicalWorkMapCore(open_store(store_path, backend)).lookup_transition(request['transition_id'])
    receipt = result.get('receipt', {})
    # Core owns the final commit time; all caller-authored content must match.
    entry = dict(request['thread_entries'][0], recorded_at=receipt.get('recorded_at'))
    same = (result.get('status') == 'applied' and receipt.get('request_digest') == _digest(request)
            and result.get('thread_entries') == [entry])
    return {'status': 'confirmed' if same else 'changed_or_unavailable',
            'revision': result['observed_revision'], 'entry_id': entry['id']}


def record_evidence(store_path, *, backend, actor=None, work=None, summary=None,
                    sources=(), why=None, occurred_at=None, related_entries=(), resume=None):
    """Append evidence, or replay the exact saved request after a lost result.

No retry rebases an old decision onto a new revision. The small request file is
the same durable recovery mechanism used by the other routine entry points.
"""
    start = perf_counter()
    store_path = Path(store_path).resolve()
    binding = {'path': str(store_path), 'backend': backend}
    if resume is not None:
        if any(v is not None for v in (actor, work, summary, why, occurred_at)) or sources or related_entries:
            raise ValueError('resume uses the saved request, without replacement facts')
        request_path = Path(resume).resolve()
        bundle = json.loads(request_path.read_text(encoding='utf-8'))
        if (not isinstance(bundle, dict) or set(bundle) != {'store_binding', 'request'}
                or bundle['store_binding'] != binding):
            raise ValueError('saved evidence request belongs to a different Store or backend')
        request = bundle['request']
        _check_request(request)
    else:
        _text(actor, 'actor'); _text(work, 'Work'); _text(summary, 'result')
        if not isinstance(sources, (list, tuple)) or not sources:
            raise ValueError('at least one source reference is required')
        refs = list(dict.fromkeys(_text(ref, 'source') for ref in sources))
        if why is not None:
            _text(why, 'recording reason')
        if occurred_at is not None:
            _text(occurred_at, 'occurrence time')
        state = validate_snapshot(open_store(store_path, backend).read().snapshot)
        if state['metadata'].get('store_role') != 'canonical_current':
            raise ValueError('selected Store is not canonical current')
        target = work if work in state['works'] else state['aliases'].get(work)
        if target not in state['map']['members']:
            raise ValueError('Work is not current; inspect its history instead')
        links = _related_ids(related_entries)
        if links:
            records = {e['id']: e for e in state['thread_entries']}
            if any(i not in records or target not in records[i]['works'] for i in links):
                raise ValueError('related records must already exist in the same Work')
        tid = 'transition:' + str(uuid4())
        entry = {'id': 'thread:' + tid, 'works': [target], 'kind': 'evidence_recorded',
                 'summary': summary, 'why': why or '実際に得た結果と根拠を後から確認できるようにする',
                 'occurred_at': occurred_at, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                 'actor': actor, 'source_refs': refs, 'transition_id': tid}
        if links:
            entry['related_entries'] = links
        request = {'format_version': state['format_version'], 'project_id': state['project_id'],
                   'transition_id': tid, 'actor_id': actor, 'base_revision': state['revision'],
                   'intent': 'Record one observed result, without changing current Work or authority',
                   'source_refs': refs, 'preconditions': [],
                   'changes': [{'op': 'append_thread', 'target': target}], 'thread_entries': [entry]}
        _check_request(request)
        directory = (store_path if backend == 'text' else store_path.parent) / 'transition-requests'
        directory.mkdir(exist_ok=True)
        request_path = directory / (tid.removeprefix('transition:') + '.evidence.json')
        with request_path.open('x', encoding='utf-8') as stream:
            json.dump({'store_binding': binding, 'request': request}, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())

    report = {'request_path': str(request_path), 'transition_id': request['transition_id'],
              'work_key': request['changes'][0]['target']}
    try:
        report['result'] = _apply_request(store_path, request, backend=backend)
    except (ContextUnknown, TransitionDenied, TransitionConflict, UnsupportedTransition, TransitionError) as exc:
        report['result'] = {'result': 'rejected', 'category': type(exc).__name__, 'reason': str(exc)}
    except (OSError, StoreError, SnapshotInvalid):
        report['result'] = {'result': 'unknown', 'reason': 'Resume this saved request; do not create another record'}
    except (ValueError, UnicodeError) as exc:
        report['result'] = {'result': 'rejected', 'category': 'invalid_input', 'reason': str(exc)}
    if report['result'].get('result') == 'applied':
        try:
            report['readback'] = _readback(store_path, backend, request)
        except (OSError, StoreError, SnapshotInvalid, TransitionError):
            # Keep the known success receipt. A failed reread is not a rollback.
            report['readback'] = {'status': 'unavailable', 'reason': 'Resume the same saved request to verify'}
    report['entrypoint_seconds'] = perf_counter() - start
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--backend', choices=('text', 'sqlite'), required=True)
    parser.add_argument('--actor')
    parser.add_argument('--work')
    parser.add_argument('--result', dest='summary')
    parser.add_argument('--source', action='append', default=[])
    parser.add_argument('--why', help='Reason for keeping this result; not a new design decision')
    parser.add_argument('--occurred-at', help='Actual occurrence time if known; omitted means unknown')
    parser.add_argument('--related-entry', action='append', default=[],
                        help='existing record ID in this Work; repeat for explicit references, not automatic withdrawal')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    if not args.store.is_absolute():
        parser.error('Store path must be absolute')
    try:
        report = record_evidence(args.store, backend=args.backend, actor=args.actor, work=args.work,
                                 summary=args.summary, sources=args.source, why=args.why,
                                 occurred_at=args.occurred_at, related_entries=args.related_entry, resume=args.resume)
    except (OSError, StoreError, SnapshotInvalid) as exc:
        report = {'result': {'result': 'unknown', 'reason': str(exc)}}
    except (ValueError, TypeError, ContextUnknown, TransitionError) as exc:
        report = {'result': {'result': 'rejected', 'reason': str(exc)}}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if (report['result'].get('result') == 'applied'
                 and report.get('readback', {}).get('status') == 'confirmed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
