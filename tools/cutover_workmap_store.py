"""Explicit local Store conversion under recorded, path-bound authorization.

Keep the existing text-to-SQLite route and add SQLite raw/A conversion. Preserve
evidence, verify all retained bytes, then use the reviewed two-step cutover.
Never delete a Store or change a normal reader/writer locator automatically.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

from pma.workmap_contract import validate_snapshot, inspect_work, require_decision_context, rule_requires_disposition
from pma.workmap_core import _digest, TransitionDenied
from pma.workmap_owner_scope import check_scope, STORE_OPERATIONS, STORE_CONSTRAINTS
from pma.workmap_store import LocalTextStore, StoreConflict, _decode, _sha256, _encode
from pma.workmap_sqlite_store import SQLiteStore, A_MAX_RAW_BYTES
from pma.workmap_store_cutover import (prepare_cutover, execute_cutover, cancel_cutover,
                                     _validated_plan, _cancellation_snapshots)
from pma.workmap_owner_permission import _read as _read_permission_source
from pma.workmap_thread_view import meaning_thread
from tools.backup_workmap_store import export_one, recover_isolated, _thread_files, _copy_thread_files


def authorize(store, work, actor, scope_thread, operation):
    snapshot=validate_snapshot(store.read().snapshot)
    scope=check_scope(snapshot,work,actor,scope_thread)
    grant=scope['permission']
    if (grant['operations'],grant['constraints']) != (STORE_OPERATIONS,STORE_CONSTRAINTS) or operation not in grant['operations']:
        raise TransitionDenied('recorded local Store-cutover Permission required')
    return snapshot, grant['source_refs']


def local_paths(local_root, *paths):
    root=Path(local_root).resolve()
    if os.name != 'nt' or str(root).startswith('\\\\') or any(p.casefold()=='onedrive' or p.casefold().startswith('onedrive - ') for p in root.parts):
        raise ValueError('reviewed nonsynced Windows local root required')
    for path in paths:
        p=Path(path)
        if not p.is_absolute() or not p.resolve().is_relative_to(root) or p.resolve()==root:
            raise ValueError('all migration paths must stay within the reviewed local root')
    # A path test is not a sync-service detector. The operator must confirm
    # that this root is outside every configured synchronization service.


def prepare(source_path, target_path, backup_dir, local_root, work, actor, scope_thread):
    local_paths(local_root,source_path,target_path,backup_dir)
    source_path,target_path,backup_dir=map(lambda p:Path(p).resolve(),(source_path,target_path,backup_dir))
    if target_path.is_relative_to(source_path) or backup_dir.is_relative_to(source_path):
        raise ValueError('candidate and backup must be outside the source Store')
    source=LocalTextStore(source_path);old=source.read()
    state,refs=authorize(source,work,actor,scope_thread,'migrate_store')
    if state!=old.snapshot or state['metadata'].get('store_role')!='canonical_current':
        raise StoreConflict('current source changed')
    if target_path.exists():
        raise StoreConflict('candidate already exists; inspect/reuse the existing plan, never migrate twice')
    backup_dir.mkdir()  # exclusive; never overwrite an earlier preservation
    exported=export_one(source_path,backup_dir/'snapshot.json')
    retained=backup_dir/'retained-objects';retained.mkdir()
    records=[]
    for path in sorted((source_path/'objects').glob('*.json')):
        raw=path.read_bytes()
        if _sha256(raw)!=path.stem or _decode(raw,'retained snapshot').get('project_id')!=state['project_id']:
            raise ValueError('retained object hash/project mismatch; preserve and investigate')
        shutil.copyfile(path,retained/path.name)
        records.append({'name':path.name,'sha256':path.stem,'bytes':len(raw)})
    head=(source_path/'head.json').read_bytes()
    if _sha256(head)!=old.token or source.read()!=old:
        raise StoreConflict('source changed during preservation; candidate not published')
    (backup_dir/'source-head.json').write_bytes(head)  # not a live head.json
    recover_isolated(backup_dir/'snapshot.json',target_path,exported['sha256'],backend='sqlite')
    target=SQLiteStore(target_path)
    with target.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current=target.version(db.execute('SELECT s.token,s.body FROM current c JOIN snapshots s ON s.token=c.token WHERE c.id=1').fetchone())
        if current.snapshot['metadata'].get('store_role')!='recovery_candidate':
            raise StoreConflict('target is no longer a candidate')
        for item in records:
            raw=(retained/item['name']).read_bytes()
            if _sha256(raw)!=item['sha256']:
                raise ValueError('preserved object changed')
            db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?)',(item['sha256'],raw))
            if db.execute('SELECT body FROM snapshots WHERE token=?',(item['sha256'],)).fetchone()[0]!=raw:
                raise ValueError('candidate retained object differs')
        db.execute('COMMIT')
    if source.read()!=old:
        raise StoreConflict('source changed before plan; reread and reassess')
    plan=prepare_cutover(source,target,reason_refs=refs)
    report={'source':str(source_path),'target':str(target_path),'backup':str(backup_dir),
            'local_root':str(Path(local_root).resolve()),'work':work,'actor':actor,'scope_thread':scope_thread,
            'source_revision':state['revision'],'export':exported,'retained_objects':records,
            'plan_digest':_digest(plan),'plan':plan}
    path=backup_dir/'cutover-plan.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {'plan_path':str(path),'plan_digest':report['plan_digest'],'retained_objects':len(records),'source_revision':state['revision'],'candidate_role':'recovery_candidate'}


def apply_plan(path, expected_digest, *, cancel=False):
    report=json.loads(Path(path).read_text(encoding='utf-8'))
    local_paths(report['local_root'],report['source'],report['target'],report['backup'])
    if report['plan_digest']!=expected_digest or _digest(report['plan'])!=expected_digest:
        raise ValueError('plan is not the reviewed version')
    source,target=LocalTextStore(report['source']),SQLiteStore(report['target'])
    authorize(source,report['work'],report['actor'],report['scope_thread'],'recover_store' if cancel else 'cutover_store')
    fn=cancel_cutover if cancel else execute_cutover
    return fn(source,target,report['plan'],reviewed_digest=expected_digest)


def sqlite_conversion_constraints(source, target, local_root, compression):
    """Exact path/codec constraints in the existing Permission list."""
    if compression not in ('a', 'none'):
        raise ValueError('explicit target compression required')
    location = lambda p: os.path.normcase(str(Path(p).resolve()))
    return ['source_path:' + location(source), 'target_path:' + location(target),
            'local_root:' + location(local_root), 'target_compression:' + compression]


def _sqlite_authority(store, work, actor, permission_id, constraints, operations, repo):
    """Use current ordinary Permission and its existing Owner-source record.

    A reviewed-operation flag is operator judgment, not authentication. This
    neither grants authority nor reuses the former text-to-SQLite profile.
    """
    state = validate_snapshot(store.read(read_only=True).snapshot)
    key = state['aliases'].get(work, work)
    if key not in state['map']['members']:
        raise TransitionDenied('conversion Work must be current')
    context = inspect_work(state, key)
    require_decision_context(context)
    if not any(p['actor_id'] == actor and p['mode'] == 'active' for p in context['pins']):
        raise TransitionDenied('conversion Work must be actively accepted by this actor')
    chain = {p['work_key'] for p in context['parent_chain']}
    if any(chain.intersection(r['targets']) and rule_requires_disposition(r, operations)
           for r in state['map']['action_rules']):
        raise TransitionDenied('applicable Action Rule needs disposition')
    if any(chain.intersection(p['targets']) and p['grantee'] == actor
           and set(operations).intersection(p['operations']) and p['effect'] != 'allow'
           for p in state['map']['permissions']):
        raise TransitionDenied('current conversion Permission is denied or unknown')
    grants = [p for p in state['map']['permissions'] if p['id'] == permission_id]
    if len(grants) != 1:
        raise TransitionDenied('exact conversion Permission is missing')
    grant = grants[0]
    grant_constraints = grant.get('constraints')
    if (grant['targets'] != [key] or grant['grantee'] != actor or grant['effect'] != 'allow'
            or grant['grantor_ref'] != 'principal:owner' or not set(operations) <= set(grant['operations'])
            or not isinstance(grant_constraints, list) or any(not isinstance(c, str) for c in grant_constraints)
            or len(grant_constraints) != len(constraints) or set(grant_constraints) != set(constraints)):
        raise TransitionDenied('conversion Permission does not cover these paths and operations')
    proofs = []
    for entry in state['thread_entries']:
        if entry['kind'] != 'permission_changed':
            continue
        try:
            detail = json.loads(entry.get('consequence', '{}'))
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict):
            continue
        receipt = state['receipts'].get(entry.get('transition_id'), {})
        if (detail.get('new_permission') == grant and entry['works'] == [key]
                and receipt.get('result') == 'applied'
                and receipt.get('transition_id') == entry.get('transition_id')
                and detail.get('owner_sources')):
            proofs.append(detail['owner_sources'])
    if not proofs:
        raise TransitionDenied('recorded Owner Permission source proof is unavailable')
    bindings = proofs[-1]
    if (not isinstance(bindings, list) or not bindings
            or any(not isinstance(b, dict) or set(b) != {'ref', 'sha256'}
                   or not isinstance(b['ref'], str) or not isinstance(b['sha256'], str) for b in bindings)):
        raise TransitionDenied('recorded Owner Permission source proof is unreadable')
    refs = [b.get('ref') for b in bindings]
    if (not set(grant['source_refs']) <= set(refs)
            or not any(isinstance(r, str) and r.startswith('repo:docs/work-management/evidence/owner_')
                       and r.endswith('.txt') for r in grant['source_refs'])):
        raise TransitionDenied('conversion requires a recorded explicit Owner source')
    try:
        for binding in bindings:
            if hashlib.sha256(_read_permission_source(repo, binding['ref'])).hexdigest() != binding['sha256']:
                raise ValueError('Owner source bytes changed')
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise TransitionDenied('conversion Permission source cannot be verified') from exc
    return state, grant, bindings


def _sqlite_profile(store):
    with store.connection(read_only=True) as db:
        return store._profile(db)


def _write_plan_once(path, report):
    with path.open('x', encoding='utf-8', newline='\n') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write('\n'); output.flush(); os.fsync(output.fileno())


def _conversion_meaning(snapshot, repo):
    view = meaning_thread(snapshot, repo=repo, reference_fields=False, include_sources=True)
    if view['status'] == 'unknown':
        raise StoreConflict('meaning Evidence is unavailable; preserve and reassess')
    # Physical cutover changes observed revision. With no authored selection,
    # source_revision is just that observation too; selected provenance stays.
    view.pop('observed_revision', None)
    if view['selection_source'] is None:
        view.pop('source_revision', None)
    return view


def prepare_sqlite(source_path, target_path, backup_dir, local_root, work, actor,
                   permission_id, *, compression, evidence_repo, reviewed=False):
    """Prepare only; keep the source current and the new target isolated."""
    if not reviewed:
        raise TransitionDenied('review authorization, local root and quiesced writers first')
    source_path, target_path, backup_dir, local_root, repo = map(lambda p: Path(p).resolve(),
        (source_path, target_path, backup_dir, local_root, evidence_repo))
    local_paths(local_root, source_path, target_path, backup_dir)
    if (source_path == target_path or source_path.is_relative_to(backup_dir)
            or target_path.is_relative_to(backup_dir) or not local_root.is_dir()):
        raise ValueError('distinct source, target and new preservation directory required')
    source = SQLiteStore(source_path)
    constraints = sqlite_conversion_constraints(source_path, target_path, local_root, compression)
    state, grant, bindings = _sqlite_authority(source, work, actor, permission_id, constraints,
                                             ['backup_store', 'migrate_store'], repo)
    old = source.read(read_only=True)
    if old.snapshot != state or state['metadata'].get('store_role') != 'canonical_current':
        raise StoreConflict('source is not the reviewed current root')
    profile = _sqlite_profile(source)
    if profile == compression:
        raise ValueError('source already uses the requested physical format')
    if target_path.exists() or backup_dir.exists():
        raise StoreConflict('preserve existing files; use a fresh target and preservation directory')
    catalog_tokens = source.retained_snapshot_tokens()
    backup_dir.mkdir()
    exported = export_one(source_path, backup_dir / 'snapshot.json', backend='sqlite', evidence_repo=repo)
    if source.read(read_only=True) != old:
        raise StoreConflict('source changed while preserving current Evidence')
    candidate = copy.deepcopy(state)
    candidate['metadata'].update(store_role='recovery_candidate', recovery_source_sha256=exported['sha256'])
    target = SQLiteStore(target_path)
    created = target.initialize(candidate, compression=compression)
    _copy_thread_files(_thread_files(state, repo), target_path.parent / (target_path.name + '.evidence'))
    catalog = []
    def originals():
        for token, raw in source.iter_retained_bytes(old.token,
                max_raw_bytes=A_MAX_RAW_BYTES if compression == 'a' else None):
            catalog.append({'token': token, 'bytes': len(raw)})
            yield token, raw
    target._retain_versions(created.token, originals(), expected_role='recovery_candidate')
    if [r['token'] for r in catalog] != catalog_tokens or source.read(read_only=True) != old:
        raise StoreConflict('source catalog changed during conversion')
    for row in catalog:
        raw = source.read_retained_bytes(row['token'])
        if len(raw) != row['bytes'] or target.read_retained_bytes(row['token']) != raw:
            raise StoreConflict('converted retained bytes differ from the source')
    if set(target.retained_snapshot_tokens()) != set(catalog_tokens) | {created.token}:
        raise StoreConflict('candidate contains unexpected retained versions')
    if _conversion_meaning(state, repo) != _conversion_meaning(target.read().snapshot, repo):
        raise StoreConflict('candidate meaning differs from the source')
    # Recheck both authority sources and the exact source root after the full
    # copy. No valid plan is published for a partial or changed preparation.
    current, current_grant, current_bindings = _sqlite_authority(source, work, actor, permission_id,
        constraints, ['backup_store', 'migrate_store'], repo)
    if current != state or current_grant != grant or current_bindings != bindings:
        raise StoreConflict('conversion authority changed during preparation')
    plan = prepare_cutover(source, target, reason_refs=grant['source_refs'])
    report = {'format': 'ashiba-sqlite-body-cutover-1', 'source': str(source_path), 'target': str(target_path),
        'backup': str(backup_dir), 'local_root': str(local_root), 'evidence_repo': str(repo),
        'work': work, 'actor': actor, 'permission_id': permission_id,
        'permission': grant, 'owner_sources': bindings, 'source_compression': profile,
        'target_compression': compression, 'catalog': catalog,
        'export': exported, 'cutover_digest': _digest(plan), 'plan': plan}
    report['plan_digest'] = _digest(report)
    path = backup_dir / 'sqlite-cutover-plan.json'
    _write_plan_once(path, report)
    return {'plan_path': str(path), 'plan_digest': report['plan_digest'],
            'retained_versions': len(catalog), 'role': 'recovery_candidate'}


def _audit_sqlite_phase(source, target, report, *, cancel):
    """Verify all reviewed original bytes and only the known physical roots."""
    plan, digest = report['plan'], report['cutover_digest']
    frozen, active = _validated_plan(source, target, plan, digest)
    rejected, restored = _cancellation_snapshots(plan, digest)
    token = lambda s: _sha256(_encode(s))
    ft, at, jt, rt = map(token, (frozen, active, rejected, restored))
    old, new = source.read(), target.read()
    source_roots = {plan['source_token']: plan['source_snapshot'], ft: frozen}
    target_roots = {plan['target_token']: plan['candidate_snapshot'], at: active}
    if cancel:
        source_roots[rt], target_roots[jt] = restored, rejected
    if source_roots.get(old.token) != old.snapshot or target_roots.get(new.token) != new.snapshot:
        raise StoreConflict('cutover roots advanced or differ; reread and reassess')
    original = {r['token'] for r in report['catalog']}
    source_tokens, target_tokens = set(source.retained_snapshot_tokens()), set(target.retained_snapshot_tokens())
    target_required = original | {plan['target_token']}
    if new.token in (at, jt): target_required.add(at)
    if new.token == jt: target_required.add(jt)
    if not target_required <= target_tokens or not target_tokens <= target_required | {ft}:
        raise StoreConflict('candidate retained catalog differs from the reviewed conversion')
    source_required = original | ({ft} if old.token in (ft, rt) else set()) | ({rt} if old.token == rt else set())
    source_allowed = source_required | (target_tokens if old.token == rt else set())
    if not source_required <= source_tokens or not source_tokens <= source_allowed:
        raise StoreConflict('source retained catalog differs from the reviewed conversion')
    for row in report['catalog']:
        raw = source.read_retained_bytes(row['token'])
        if len(raw) != row['bytes'] or target.read_retained_bytes(row['token']) != raw:
            raise StoreConflict('retained original bytes changed since preparation')
    # Additional cancellation/activation roots must also carry their exact
    # reviewed bytes, not just a token present in a damaged table.
    known = {ft: frozen, at: active, jt: rejected, rt: restored,
             plan['target_token']: plan['candidate_snapshot']}
    for store, tokens in ((source, source_tokens), (target, target_tokens)):
        for key in tokens - original:
            if key not in known or store.read_retained_snapshot(key) != known[key]:
                raise StoreConflict('physical retained root differs from the plan')
    return old, new


def apply_sqlite_plan(path, expected_digest, *, cancel=False, reviewed=False, fault_at=None):
    if not reviewed:
        raise TransitionDenied('review the same plan, authority and quiesced writers first')
    valid_faults = (None, 'after_target_stopped', 'after_history') if cancel else (None, 'after_source', 'after_target', 'after_history')
    if fault_at not in valid_faults:
        raise ValueError('unsupported SQLite cutover fault')
    report = json.loads(Path(path).read_text(encoding='utf-8'))
    body = {k: v for k, v in report.items() if k != 'plan_digest'}
    if (report.get('format') != 'ashiba-sqlite-body-cutover-1'
            or report.get('plan_digest') != expected_digest or _digest(body) != expected_digest
            or _digest(report['plan']) != report['cutover_digest']):
        raise ValueError('SQLite conversion plan differs from reviewed bytes')
    local_paths(report['local_root'], report['source'], report['target'], report['backup'])
    source, target = SQLiteStore(Path(report['source'])), SQLiteStore(Path(report['target']))
    if (_sqlite_profile(source), _sqlite_profile(target)) != (report['source_compression'], report['target_compression']):
        raise StoreConflict('reviewed physical formats changed')
    repo = Path(report['evidence_repo'])
    constraints = sqlite_conversion_constraints(report['source'], report['target'], report['local_root'], report['target_compression'])
    _, grant, bindings = _sqlite_authority(source, report['work'], report['actor'], report['permission_id'], constraints,
        ['recover_store', 'migrate_store'] if cancel else ['cutover_store', 'migrate_store'], repo)
    if grant != report['permission'] or bindings != report['owner_sources']:
        raise StoreConflict('reviewed authority changed')
    _audit_sqlite_phase(source, target, report, cancel=cancel)
    if _conversion_meaning(source.read().snapshot, repo) != _conversion_meaning(target.read().snapshot, repo):
        raise StoreConflict('current meaning differs before cutover')
    # Catalog verification may be long. Do not leave the final authority check
    # before that work, or silently accept changed source files afterwards.
    _, grant, bindings = _sqlite_authority(source, report['work'], report['actor'], report['permission_id'], constraints,
        ['recover_store', 'migrate_store'] if cancel else ['cutover_store', 'migrate_store'], repo)
    if grant != report['permission'] or bindings != report['owner_sources']:
        raise StoreConflict('reviewed authority changed after catalog verification')
    if cancel:
        result = cancel_cutover(source, target, report['plan'], reviewed_digest=report['cutover_digest'],
            fault_at=fault_at if fault_at == 'after_target_stopped' else None)
        if result['result'] != 'not_started':
            # Preserve even the provisional activation/cancellation versions in
            # the returned current Store. Never discard newer business writes.
            old, candidate = source.read(), target.read()
            source._retain_versions(old.token, target.iter_retained_bytes(candidate.token), expected_role='canonical_current')
    else:
        result = execute_cutover(source, target, report['plan'], reviewed_digest=report['cutover_digest'],
            fault_at=fault_at if fault_at in ('after_source', 'after_target') else None)
        old, current = source.read(), target.read()
        target._retain_versions(current.token, [(old.token, source.read_retained_bytes(old.token))], expected_role='canonical_current')
    if fault_at == 'after_history':
        raise OSError('simulated response loss after retained history preservation')
    _audit_sqlite_phase(source, target, report, cancel=cancel)
    if _conversion_meaning(source.read().snapshot, repo) != _conversion_meaning(target.read().snapshot, repo):
        raise StoreConflict('meaning differs after cutover; keep writers quiesced')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='phase',required=True)
    q=sub.add_parser('prepare')
    for name in ('source','target','backup','local-root','work','actor','scope-thread'):q.add_argument('--'+name,required=True)
    for phase in ('activate','cancel'):
        q=sub.add_parser(phase);q.add_argument('--plan',type=Path,required=True);q.add_argument('--reviewed-plan-digest',required=True)
    q=sub.add_parser('prepare-sqlite')
    for name in ('source','target','backup','local-root','work','actor','permission-id','evidence-repo'):
        q.add_argument('--'+name,required=True)
    q.add_argument('--compression',choices=('a','none'),required=True)
    q.add_argument('--reviewed-operation',action='store_true')
    for phase in ('activate-sqlite','cancel-sqlite'):
        q=sub.add_parser(phase)
        q.add_argument('--plan',type=Path,required=True)
        q.add_argument('--reviewed-plan-digest',required=True)
        q.add_argument('--reviewed-operation',action='store_true')
    a=p.parse_args()
    if a.phase=='prepare-sqlite':
        result=prepare_sqlite(a.source,a.target,a.backup,a.local_root,a.work,a.actor,a.permission_id,
            compression=a.compression,evidence_repo=a.evidence_repo,reviewed=a.reviewed_operation)
    elif a.phase in ('activate-sqlite','cancel-sqlite'):
        result=apply_sqlite_plan(a.plan,a.reviewed_plan_digest,cancel=a.phase=='cancel-sqlite',reviewed=a.reviewed_operation)
    else:
        result=prepare(a.source,a.target,a.backup,a.local_root,a.work,a.actor,a.scope_thread) if a.phase=='prepare' else apply_plan(a.plan,a.reviewed_plan_digest,cancel=a.phase=='cancel')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
