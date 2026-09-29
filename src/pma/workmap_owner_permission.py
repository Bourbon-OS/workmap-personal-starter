"""Local attribution review for the existing set_permission operation.

No close exemption: the resulting ordinary Permission is used by normal Core.
The caller must assess the actual Owner instruction and every grant constraint.
"""
import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from pma.workmap_contract import inspect_work, require_decision_context, confirm_new_work_collections
from pma.workmap_core import TransitionDenied, _digest


@dataclass(frozen=True)
class ReviewedOwnerPermission:
    request_digest: str
    repo: Path
    bindings: tuple


def _read(repo, ref):
    if not isinstance(ref, str) or not ref.startswith('repo:docs/work-management/'):
        raise ValueError('unsupported permission evidence')
    path = (repo / ref[5:]).resolve()
    if not path.is_relative_to((repo / 'docs/work-management').resolve()):
        raise ValueError('evidence escapes repository')
    return path.read_bytes()


def review_owner_permission(request, repo):
    """Explicit trusted operator input, not authentication or AI text approval."""
    try:
        changes, entries = request['changes'], request['thread_entries']
        if len(changes) != 1 or changes[0]['op'] != 'set_permission' or len(entries) != 1:
            raise ValueError()
        grant = changes[0]['value']
        if (grant['grantor_ref'] != 'principal:owner' or grant['effect'] != 'allow'
                or grant['targets'] != [changes[0]['target']] or changes[0]['expected'] is not None):
            raise ValueError('only a fresh explicit grant is reviewed here')
        owner_refs = [r for r in grant['source_refs']
                      if r.startswith('repo:docs/work-management/evidence/owner_') and r.endswith('.txt')]
        if not owner_refs or not set(grant['source_refs']) <= set(request['source_refs']):
            raise ValueError('actual Owner sources required')
        repo = Path(repo).resolve()
        bindings = tuple((r, hashlib.sha256(_read(repo, r)).hexdigest()) for r in request['source_refs'])
        return ReviewedOwnerPermission(_digest(request), repo, bindings)
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        raise TransitionDenied('exact Owner Permission and readable sources must be reviewed') from exc


def verify_permission_sources(review):
    try:
        for ref, digest in review.bindings:
            if hashlib.sha256(_read(review.repo, ref)).hexdigest() != digest:
                raise ValueError()
    except (ValueError, OSError) as exc:
        raise TransitionDenied('Permission source changed after review') from exc


def _recover_untouched_birth(snapshot, request, repo):
    """Repair the old add_work omission only from a committed exact request.

    This is not evidence that an imported Work has no Pins or Conditions.
    Any later participation/change/close requires its own assessment instead.
    """
    entry = request['thread_entries'][0]
    detail = json.loads(entry.get('consequence', '{}'))
    ref = detail.get('birth_request_ref')
    if not ref:
        return
    if ref not in request['source_refs']:
        raise ValueError('birth evidence is not bound')
    birth = json.loads(_read(repo, ref))
    change = birth['changes'][0]
    target = request['changes'][0]['target']
    receipt = snapshot['receipts'].get(birth['transition_id'], {})
    records = [e for e in snapshot['thread_entries'] if target in e['works']]
    if (len(birth['changes']) != 1 or change['op'] != 'add_work'
            or birth['project_id'] != snapshot['project_id']
            or receipt.get('request_digest') != _digest(birth) or receipt.get('result') != 'applied'
            or change['value']['work'] != snapshot['works'][target]
            or change['value']['work']['work_key'] != target
            or change['value']['display_id'] != snapshot['display_ids'][target]
            or target in snapshot['retirements']
            or not records or any(e.get('transition_id') != birth['transition_id'] for e in records)
            or not any(e['kind'] == 'work_added' and e['works'] == [target, change['target']] for e in records)):
        raise ValueError('not an untouched, proven Core birth')
    m = snapshot['map']
    relations = [r for r in m['relations'] if target in r.values()]
    if (len(relations) != 1 or relations[0]['kind'] != 'parent'
            or relations[0]['child'] != target or relations[0]['parent'] != change['target']
            or any(p['work_id'] == target for p in m['pins'])
            or any(target in x['targets'] for n in ('permissions', 'conditions', 'notes') for x in m[n])):
        raise ValueError('birth context has changed')
    confirm_new_work_collections(snapshot, target)


def validate_owner_permission(snapshot, request, review, operation_review=None):
    if not isinstance(review, ReviewedOwnerPermission) or review.request_digest != _digest(request):
        raise TransitionDenied('Permission review does not match this request')
    verify_permission_sources(review)
    before = copy.deepcopy(snapshot['metadata'].get('completeness'))
    try:
        _recover_untouched_birth(snapshot, request, review.repo)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise TransitionDenied('birth confirmation evidence is insufficient') from exc
    change = request['changes'][0]
    context = inspect_work(snapshot, change['target'])
    from pma.workmap_decision_context import require_operation_context
    require_operation_context(snapshot, request, operation_review, needs_pin=False)
    grant = change['value']
    chain = {w['work_key'] for w in context['parent_chain']}
    if any(chain.intersection(p['targets']) and p['grantee'] == grant['grantee']
           and set(p['operations']).intersection(grant['operations']) and p['effect'] != 'allow'
           for p in snapshot['map']['permissions']):
        raise TransitionDenied('resolve the existing denial before granting')
    if any(chain.intersection(c['targets']) for c in snapshot['map']['conditions']):
        raise TransitionDenied('applicable Conditions need assessment before granting')
    # Preserve proof/version bindings in the existing technical permission record.
    entry = request['thread_entries'][0]
    detail = {'owner_sources': [{'ref': r, 'sha256': h} for r, h in review.bindings],
              'old_permission': change['expected'], 'new_permission': grant,
              'reported_consequence': entry.get('consequence')}
    if before != snapshot['metadata'].get('completeness'):
        detail.update(old_completeness=before, new_completeness=copy.deepcopy(snapshot['metadata']['completeness']))
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)
