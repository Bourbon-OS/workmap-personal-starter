"""Add a Work from meaning fields through the existing canonical Core.

The saved file is an ordinary Transition, not a second Store. After uncertain
delivery resume that file; do not generate a new request to bypass uncertainty.
Owner attribution remains an explicit local-operator trust input.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

from pma.workmap_add import build_add_request
from pma.workmap_contract import validate_snapshot
from pma.workmap_contract import SnapshotInvalid
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from tools.apply_workmap_transition import apply_result
from tools.read_workmap_store import read_result

REPO = Path(__file__).resolve().parents[1]


def add_work(store_path, *, backend, actor=None, parent=None, name=None, intent=None,
             origin=None, sources=(), reason=None, reviewed_owner_add=False,
             owner_instruction=None, resume=None, root=False, display_id=None):
    started = datetime.now(timezone.utc).isoformat()
    t0 = perf_counter()
    store_path = Path(store_path).resolve()
    timings = {}
    if resume is None:
        if not all(isinstance(v, str) and v.strip() for v in (actor, name, intent, origin)):
            raise ValueError('actor, Name, Intent and Origin are required')
        if root:
            if parent is not None or not display_id or not reviewed_owner_add:
                raise ValueError('root needs an explicit address and reviewed Owner registration, without Parent')
        elif not parent or display_id is not None:
            raise ValueError('normal addition needs Parent; explicit address is root-only')
        if not sources and not owner_instruction:
            raise ValueError('Origin needs an actual source reference')
        if owner_instruction and not reviewed_owner_add:
            raise ValueError('Owner text requires explicit local attribution review')
        current = validate_snapshot(open_store(store_path, backend).read().snapshot)
        if current['metadata'].get('store_role') != 'canonical_current':
            raise ValueError('selected Store is not canonical current')
        parent_key = None if root else current['aliases'].get(parent, parent)
        if not root and parent_key not in current['map']['members']:
            raise ValueError('Parent is not a current Work')
        timings['map_read_seconds'] = perf_counter() - t0
        refs = list(dict.fromkeys(sources))
        if owner_instruction:
            raw = ('Operator-attributed Owner instruction for this registration:\n\n' + owner_instruction + '\n').encode('utf-8')
            evidence = REPO/'docs/work-management/evidence'/('owner_add_' + hashlib.sha256(raw).hexdigest() + '.txt')
            try:
                with evidence.open('xb') as f: f.write(raw)
            except FileExistsError:
                if evidence.read_bytes() != raw: raise ValueError('Owner evidence path contains different bytes')
            refs.append('repo:' + evidence.relative_to(REPO).as_posix())
        request = build_add_request(current, actor=actor, parent=parent_key, name=name,
            intent=intent, origin=origin, refs=refs, reason=reason, root=root,
            display_id=display_id, recorded_at=started)
        tid = request['transition_id']
        # Preserve the exact request before any Store mutation so ambiguous
        # responses can be checked/replayed with the same identity and base.
        directory = (store_path if backend == 'text' else store_path.parent)/'transition-requests'
        directory.mkdir(exist_ok=True)
        request_path = directory/(tid.removeprefix('transition:') + '.json')
        with request_path.open('x', encoding='utf-8') as f:
            json.dump(request, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        timings['request_preparation_seconds'] = perf_counter() - t0 - timings['map_read_seconds']
    else:
        if root or display_id is not None or any(v is not None for v in (actor, parent, name, intent, origin, reason, owner_instruction)) or sources:
            raise ValueError('resume accepts the saved request, not replacement meaning fields')
        request_path = Path(resume).resolve()
        request = json.loads(request_path.read_text(encoding='utf-8'))
        if len(request.get('changes', [])) != 1 or request['changes'][0].get('op') != 'add_work':
            raise ValueError('resume requires one saved add_work request')
    apply_started = perf_counter()
    result = apply_result(store_path, request_path, backend=backend, reviewed_owner_add=reviewed_owner_add)
    timings['apply_seconds'] = perf_counter() - apply_started
    report = {'result': result, 'request_path': str(request_path), 'started_at': started,
              'timings': timings}
    if result.get('result') == 'applied':
        read_started = perf_counter()
        try:
            snapshot = validate_snapshot(open_store(store_path, backend).read().snapshot)
        except (OSError, StoreError, SnapshotInvalid) as exc:
            report['readback'] = {'status': 'unknown', 'reason': str(exc)}
            report['completed_at'] = datetime.now(timezone.utc).isoformat()
            timings['entrypoint_seconds'] = perf_counter() - t0
            return report
        change = request['changes'][0]
        work, display = change['value']['work'], change['value']['display_id']
        key = work['work_key']
        root_request = 'parent' in change['value'] and change['value']['parent'] is None
        same_parent = (not any(r['kind'] == 'parent' and r['child'] == key for r in snapshot['map']['relations'])
                       if root_request else any(r['kind'] == 'parent' and r['child'] == key and r['parent'] == change['target'] for r in snapshot['map']['relations']))
        same = (snapshot['metadata'].get('store_role') == 'canonical_current'
                and key in snapshot['map']['members'] and snapshot['works'].get(key) == work
                and snapshot['display_ids'].get(key) == display
                and same_parent)
        view = read_result(store_path, display, thread=True, backend=backend)
        same = same and view.get('revision') == snapshot['revision'] and view.get('status') == 'confirmed'
        records = [e for e in snapshot['thread_entries'] if e.get('transition_id') == request['transition_id']]
        report['readback'] = {'status': 'confirmed' if same else 'changed_or_unavailable',
                              'revision': snapshot['revision'], 'work_key': key, 'display_id': snapshot['display_ids'].get(key),
                              'work': snapshot['works'].get(key), 'thread': view,
                              'technical_records': sum(e['kind'] == 'work_added' for e in records)}
        timings['readback_seconds'] = perf_counter() - read_started
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    timings['entrypoint_seconds'] = perf_counter() - t0
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store', type=Path, required=True)
    p.add_argument('--backend', choices=('text', 'sqlite'), required=True)
    for field in ('actor', 'parent', 'name', 'intent', 'origin', 'reason', 'owner-instruction'):
        p.add_argument('--' + field)
    p.add_argument('--source', action='append', default=[])
    p.add_argument('--reviewed-owner-add', action='store_true')
    p.add_argument('--resume', type=Path)
    p.add_argument('--root', action='store_true', help='explicit Parent-free Owner registration')
    p.add_argument('--display-id', help='explicit unused root address; only with --root')
    a = p.parse_args()
    if not a.store.is_absolute(): p.error('Store path must be absolute')
    try:
        report = add_work(a.store, backend=a.backend, actor=a.actor, parent=a.parent,
                          name=a.name, intent=a.intent, origin=a.origin, sources=a.source,
                          reason=a.reason, reviewed_owner_add=a.reviewed_owner_add,
                          owner_instruction=a.owner_instruction, resume=a.resume, root=a.root, display_id=a.display_id)
    except (OSError, ValueError, KeyError, TypeError, StoreError, SnapshotInvalid) as exc:
        p.exit(2, str(exc) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['result'].get('result') == 'applied' and report.get('readback', {}).get('status') == 'confirmed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
