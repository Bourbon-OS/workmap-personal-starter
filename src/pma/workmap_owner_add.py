"""Exact Owner-directed registration; does not grant Permission or move Pins."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from pma.workmap_core import TransitionDenied, _digest
from pma.workmap_contract import inspect_work


def is_root_add(change: dict) -> bool:
    value = change.get('value')
    return change.get('op') == 'add_work' and isinstance(value, dict) and 'parent' in value and value['parent'] is None


@dataclass(frozen=True)
class ReviewedOwnerAdd:
    # Local operator trust input, not Owner authentication.
    request_digest: str
    source_refs: tuple[str, ...]
    evidence_repo: Path
    detail: dict


def review_owner_add(request: dict, repo: Path, snapshot: dict | None = None) -> ReviewedOwnerAdd:
    """Operator must first read and attribute the exact one-Work instruction."""
    try:
        changes, entries = request['changes'], request['thread_entries']
        if len(changes) != 1 or changes[0]['op'] != 'add_work' or not isinstance(entries, list) or len(entries) > 1:
            raise ValueError()
        if entries and entries[0].get('kind') == 'work_added':
            # Existing requests retain their exact provenance and digest.
            detail = json.loads(entries[0]['consequence'])
        else:
            # Authority is an input to this trusted local review, independent
            # of whether the caller has an additional reason for meaning Thread.
            if snapshot is None or snapshot['revision'] != request['base_revision']:
                raise ValueError()
            refs = [ref for ref in request['source_refs']
                    if ref.startswith('repo:docs/work-management/evidence/owner_') and ref.endswith('.txt')]
            bindings = []
            for ref in refs:
                path = (repo / ref[5:]).resolve()
                if path.parent != (repo / 'docs/work-management/evidence').resolve():
                    raise ValueError()
                bindings.append({'ref': ref, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            detail = {'parent_intent': None if is_root_add(changes[0]) else snapshot['works'][changes[0]['target']]['intent'],
                      'registration_only': True,
                      'owner_sources': bindings}
        if set(detail) != {'owner_sources', 'parent_intent', 'registration_only'} or detail['registration_only'] is not True:
            raise ValueError()
        sources = detail['owner_sources']
        if not isinstance(sources, list) or not sources:
            raise ValueError()
        refs = []
        for source in sources:
            if set(source) != {'ref', 'sha256'}:
                raise ValueError()
            ref = source['ref']
            if not ref.startswith('repo:docs/work-management/evidence/owner_') or not ref.endswith('.txt'):
                raise ValueError()
            path = (repo / ref[5:]).resolve()
            if path.parent != (repo / 'docs/work-management/evidence').resolve():
                raise ValueError()
            if hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
                raise ValueError()
            if ref not in request['source_refs']:
                raise ValueError()
            refs.append(ref)
        return ReviewedOwnerAdd(_digest(request), tuple(refs), repo, detail)
    except (KeyError, IndexError, TypeError, ValueError, OSError, AttributeError) as exc:
        raise TransitionDenied('exact reviewed Owner registration evidence is required') from exc


def verify_owner_add_sources(review: ReviewedOwnerAdd) -> None:
    """Recheck the bytes bound at review, immediately before commit."""
    try:
        for source in review.detail['owner_sources']:
            if hashlib.sha256((review.evidence_repo / source['ref'][5:]).read_bytes()).hexdigest() != source['sha256']:
                raise ValueError()
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise TransitionDenied('Owner registration source changed after review') from exc


def validate_owner_add(snapshot: dict, request: dict, review: ReviewedOwnerAdd, *, store_path=None) -> None:
    """Allow just this addition while leaving incomplete collections intact."""
    if not isinstance(review, ReviewedOwnerAdd) or review.request_digest != _digest(request):
        raise TransitionDenied('Owner registration review does not match request')
    change = request['changes'][0]
    detail = review.detail
    if (set(detail) != {'owner_sources', 'parent_intent', 'registration_only'}
            or detail['registration_only'] is not True
            or tuple(s['ref'] for s in detail['owner_sources']) != review.source_refs
            or not review.source_refs):
        raise TransitionDenied('registration-only scope is required')
    if is_root_add(change):
        work = change['value'].get('work', {})
        if detail['parent_intent'] is not None or change['target'] != work.get('work_key') or change['target'] in snapshot['display_ids']:
            raise TransitionDenied('root registration must bind a new identity without a fabricated Parent')
        children = {r['child'] for r in snapshot['map']['relations'] if r['kind'] == 'parent'}
        chain = set(snapshot['map']['members']) - children
    else:
        context = inspect_work(snapshot, change['target'])
        if (not detail['parent_intent'] or detail['parent_intent'] != snapshot['works'][change['target']]['intent']
                or any(p['intent_status'] != 'confirmed' for p in context['parent_chain'])):
            raise TransitionDenied('current Parent Intent must be known and match the reviewed basis')
        chain = {p['work_key'] for p in context['parent_chain']} | {change['target']}
    if any(chain.intersection(p['targets']) and p['grantee'] == request['actor_id']
           and 'add_work' in p['operations'] and p['effect'] != 'allow'
           for p in snapshot['map']['permissions']):
        raise TransitionDenied('known denial must be resolved before registration')
    if not is_root_add(change):
        from pma.workmap_map_permission import map_add_permissions
        if any(p['effect'] != 'allow' for p in map_add_permissions(
                snapshot, change['target'], request['actor_id'], store_path=store_path)):
            raise TransitionDenied('known Map-wide registration denial must be resolved')
    if any(chain.intersection(c['targets']) for c in snapshot['map']['action_rules']):
        raise TransitionDenied('known Parent Action Rules require separate assessment')
    work = change['value']['work']
    if set(work) != {'work_key', 'name', 'intent', 'origin'}:
        raise TransitionDenied('registration may only add Work identity and meaning')
