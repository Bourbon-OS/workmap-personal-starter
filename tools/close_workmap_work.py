"""Close one reviewed Work through the existing canonical Core.

Meaning and authority remain with the local operator. Only request assembly,
version binding, persistence and readback are mechanical. Residual disposition,
granting authority, moving children and releasing other actors are not supported.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pma.workmap_canonical_core import CanonicalWorkMapCore
from pma.workmap_close import bind_close_basis, close_precondition, repository_evidence
from pma.workmap_contract import ContextUnknown, SnapshotInvalid, validate_snapshot
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, UnsupportedTransition, _digest
from pma.workmap_resume import close_history
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from tools.apply_workmap_transition import _apply_request


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} is required')
    return value


def _refs(values, name):
    if not isinstance(values, (list, tuple)):
        raise ValueError(f'{name} must be a list')
    return list(dict.fromkeys(_text(value, name) for value in values))


def _check_request(request):
    """Recovery is limited to this command, never a general mutation loader."""
    if not isinstance(request, dict) or set(request) != {
        'format_version', 'project_id', 'transition_id', 'actor_id', 'base_revision',
        'intent', 'source_refs', 'preconditions', 'changes', 'thread_entries',
    }:
        raise ValueError('saved close request has invalid fields')
    changes, entries = request['changes'], request['thread_entries']
    if (not isinstance(changes, list) or len(changes) not in (1, 2)
            or any(not isinstance(change, dict) for change in changes)
            or set(changes[-1]) != {'op', 'target'} or changes[-1]['op'] != 'close'
            or not isinstance(entries, list) or len(entries) != 1):
        raise ValueError('only one close, optionally releasing one own Pin, is supported')
    target, entry = changes[-1]['target'], entries[0]
    fields = {'id', 'works', 'kind', 'summary', 'why', 'occurred_at', 'recorded_at',
              'actor', 'source_refs', 'transition_id', 'outcome', 'residuals'}
    if (not isinstance(entry, dict) or not fields <= set(entry)
            or not set(entry) <= fields | {'evidence_refs', 'consequence', 'revisit', 'successor'}
            or entry['kind'] != 'close' or entry['works'] != [target]
            or entry['actor'] != request['actor_id']
            or entry['transition_id'] != request['transition_id']
            or entry['id'] != 'thread:' + request['transition_id']
            or entry['source_refs'] != request['source_refs']):
        raise ValueError('saved request is not this command\'s exact close')
    if len(changes) == 2:
        release = changes[0]
        pin = release.get('expected')
        if (set(release) != {'op', 'target', 'pin_id', 'expected'}
                or release['op'] != 'remove_pin' or release['target'] != target
                or not isinstance(pin, dict) or pin.get('id') != release['pin_id']
                or pin.get('actor_id') != request['actor_id'] or pin.get('work_id') != target):
            raise ValueError('recovery cannot release another Pin or change authority')
    close_precondition(request)
    if 'retained_dependencies' in request['preconditions'][0]['expected']:
        raise ValueError('Dependency retention needs the existing explicit disposition route')


def _readback(store_path, backend, request):
    store = open_store(store_path, backend)
    observed = CanonicalWorkMapCore(store).lookup_transition(request['transition_id'])
    state = validate_snapshot(store.read().snapshot)
    receipt = observed.get('receipt', {})
    expected = request['thread_entries'][0]
    records = observed.get('thread_entries', [])
    closes = [entry for entry in records if entry.get('id') == expected['id']]
    actual = closes[0] if len(closes) == 1 else None
    history = close_history(actual) if actual else None
    target = request['changes'][-1]['target']
    same = (observed.get('status') == 'applied'
            and receipt.get('request_digest') == _digest(request)
            and observed['observed_revision'] == state['revision']
            and actual is not None and history is not None
            and all(actual.get(key) == value for key, value in expected.items()
                    if key not in ('recorded_at', 'consequence'))
            and actual['recorded_at'] == receipt.get('recorded_at')
            and history['reported_consequence'] == expected.get('consequence')
            and history['work'] == target and history.get('identity', {}).get('work_key') == target
            and target not in state['map']['members']
            and state['retirements'].get(target) == expected['id']
            and len(records) == len(request['changes']))
    if len(request['changes']) == 2:
        release = request['changes'][0]
        released = [entry for entry in records if entry.get('id') == expected['id'] + ':release:' + release['pin_id']]
        same = (same and len(released) == 1 and released[0]['kind'] == 'pin_removed'
                and released[0]['actor'] == request['actor_id'] and released[0]['works'] == [target]
                and not any(pin['id'] == release['pin_id'] for pin in state['map']['pins']))
    return {'status': 'confirmed' if same else 'changed_or_unavailable',
            'revision': state['revision'], 'work_key': target,
            'close_record_ref': expected['id'],
            'parent_at_close': history['parent_at_close'] if history else None}


def close_work(store_path, *, backend, actor=None, work=None, outcome=None, reason=None,
               sources=(), evidence_refs=(), result=None, revisit=None, successors=(),
               residuals=(), release_own_pin=False, reviewed_close=False, resume=None):
    """Assemble a reviewed one-Work close; replay never rebases an old decision."""
    started = perf_counter()
    store_path = Path(store_path).resolve()
    binding = {'path': str(store_path), 'backend': backend}
    if resume is not None:
        if (any(value is not None for value in (actor, work, outcome, reason, result, revisit))
                or sources or evidence_refs or successors or residuals or release_own_pin):
            raise ValueError('resume uses the saved request, without replacement decisions')
        request_path = Path(resume).resolve()
        bundle = json.loads(request_path.read_text(encoding='utf-8'))
        if (not isinstance(bundle, dict) or set(bundle) != {'store_binding', 'request'}
                or bundle['store_binding'] != binding):
            raise ValueError('saved close belongs to a different Store or backend')
        request = bundle['request']
        _check_request(request)
    else:
        if not reviewed_close:
            raise TransitionDenied('review Intent, outcome, residuals, grants and sources before closing')
        _text(actor, 'actor'); _text(work, 'Work'); _text(reason, 'close reason')
        if outcome not in ('completed', 'stopped', 'replaced'):
            raise ValueError('choose completed, stopped or replaced explicitly')
        refs, evidence = _refs(sources, 'source'), _refs(evidence_refs, 'achievement evidence')
        if not refs:
            raise ValueError('close needs an actual source reference')
        if outcome == 'completed' and not evidence:
            raise ValueError('completed needs Intent achievement evidence')
        rest = _refs(residuals, 'residual disposition')
        following = _refs(successors, 'successor')
        if (outcome == 'replaced') != bool(following):
            raise ValueError('named successors are required for replaced, and only for replaced')
        for value, name in ((result, 'reported result'), (revisit, 'revisit condition')):
            if value is not None:
                _text(value, name)
        state = validate_snapshot(open_store(store_path, backend).read().snapshot)
        if state['metadata'].get('store_role') != 'canonical_current':
            raise ValueError('selected Store is not canonical current')
        target = state['aliases'].get(work, work)
        if target not in state['map']['members']:
            raise ValueError('Work is not current; inspect its history instead')
        successor_keys = [state['aliases'].get(key, key) for key in following]
        if any(key not in state['map']['members'] or key == target for key in successor_keys):
            raise ValueError('this command needs distinct current successor Works')
        tid = 'transition:' + str(uuid4())
        entry = {'id': 'thread:' + tid, 'works': [target], 'kind': 'close',
                 'summary': state['works'][target]['name'] + ': ' + outcome, 'why': reason,
                 'occurred_at': None, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                 'actor': actor, 'source_refs': refs, 'transition_id': tid,
                 'outcome': outcome, 'residuals': rest}
        if evidence:
            entry['evidence_refs'] = evidence
        if successor_keys:
            entry['successor'] = list(dict.fromkeys(successor_keys))
        if result is not None:
            entry['consequence'] = result
        if revisit is not None:
            entry['revisit'] = revisit
        changes = []
        if release_own_pin:
            own = [pin for pin in state['map']['pins'] if pin['work_id'] == target and pin['actor_id'] == actor]
            if len(own) != 1:
                raise ValueError('explicit own Pin release needs exactly one current own Pin')
            changes.append({'op': 'remove_pin', 'target': target, 'pin_id': own[0]['id'], 'expected': own[0]})
        changes.append({'op': 'close', 'target': target})
        request = {'format_version': state['format_version'], 'project_id': state['project_id'],
                   'transition_id': tid, 'actor_id': actor, 'base_revision': state['revision'],
                   'intent': 'Close the specified reviewed Work, without choosing residual disposition',
                   'source_refs': refs, 'preconditions': [], 'changes': changes, 'thread_entries': [entry]}
        request = bind_close_basis(state, request, resolver=repository_evidence)
        _check_request(request)
        directory = (store_path if backend == 'text' else store_path.parent) / 'transition-requests'
        directory.mkdir(exist_ok=True)
        request_path = directory / (tid.removeprefix('transition:') + '.close.json')
        with request_path.open('x', encoding='utf-8') as stream:
            json.dump({'store_binding': binding, 'request': request}, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())

    report = {'request_path': str(request_path), 'transition_id': request['transition_id'],
              'work_key': request['changes'][-1]['target']}
    try:
        report['result'] = _apply_request(store_path, request, backend=backend, reviewed_close=reviewed_close)
    except (ContextUnknown, TransitionDenied, TransitionConflict, UnsupportedTransition, TransitionError) as exc:
        report['result'] = {'result': 'rejected', 'category': type(exc).__name__, 'reason': str(exc)}
    except (OSError, StoreError, SnapshotInvalid):
        report['result'] = {'result': 'unknown', 'reason': 'Resume the saved close; do not create a new request'}
    except (ValueError, UnicodeError) as exc:
        report['result'] = {'result': 'rejected', 'category': 'invalid_input', 'reason': str(exc)}
    if report['result'].get('result') == 'applied':
        try:
            report['readback'] = _readback(store_path, backend, request)
        except (OSError, StoreError, SnapshotInvalid, TransitionError):
            report['readback'] = {'status': 'unavailable', 'reason': 'Resume the same saved close to verify'}
    report['entrypoint_seconds'] = perf_counter() - started
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--backend', choices=('text', 'sqlite'), required=True)
    for field in ('actor', 'work', 'outcome', 'reason', 'result', 'revisit'):
        parser.add_argument('--' + field)
    for field in ('source', 'evidence', 'successor', 'residual'):
        parser.add_argument('--' + field, action='append', default=[])
    parser.add_argument('--release-own-pin', action='store_true', help='Explicitly end only your own involvement in this Work')
    parser.add_argument('--reviewed-close', action='store_true', help='Operator reviewed this close meaning and scope; grants no authority')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    if not args.store.is_absolute():
        parser.error('Store path must be absolute')
    try:
        report = close_work(args.store, backend=args.backend, actor=args.actor, work=args.work,
                            outcome=args.outcome, reason=args.reason, sources=args.source,
                            evidence_refs=args.evidence, result=args.result, revisit=args.revisit,
                            successors=args.successor, residuals=args.residual,
                            release_own_pin=args.release_own_pin, reviewed_close=args.reviewed_close,
                            resume=args.resume)
    except (OSError, StoreError, SnapshotInvalid) as exc:
        report = {'result': {'result': 'unknown', 'reason': str(exc)}}
    except (ValueError, TypeError, ContextUnknown, TransitionError) as exc:
        report = {'result': {'result': 'rejected', 'reason': str(exc)}}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if (report['result'].get('result') == 'applied'
                 and report.get('readback', {}).get('status') == 'confirmed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
