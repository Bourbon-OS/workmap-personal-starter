"""Operation-bound local review of unknown impact, without confirming history.

As with close/Owner review, the operator assesses evidence and meaning. Hashes
bind that assessment; they cannot prove its truth or authenticate an Owner.
Only single supported operations are accepted. No review is inherited by a
batch, retry with new premises, or another operation.
"""
from dataclasses import dataclass
import hashlib
import json

from pma.workmap_contract import inspect_work, require_decision_context, current_subtree, ContextUnknown
from pma.workmap_core import TransitionConflict, TransitionDenied, _digest
from pma.workmap_close import repository_evidence


@dataclass(frozen=True)
class ReviewedDecisionContext:
    request_digest: str
    snapshot_digest: str
    revision: str
    target: str
    assessments: str
    bindings: tuple
    resolver: object
    related_targets: tuple = ()

    def permits(self, context, collection):
        return (context['revision'] == self.revision and context['work'] in (self.related_targets or (self.target,))
                and collection in json.loads(self.assessments))


def reparent_scope_keys(snapshot, change):
    keys = set(current_subtree(snapshot, change['target']))
    parents = {r['child']: r['parent'] for r in snapshot['map']['relations'] if r['kind'] == 'parent'}
    for start in (change['target'], change['new_parent']):
        if start is None:
            continue
        if start not in snapshot['map']['members']:
            raise TransitionConflict('reparent scope is not current')
        seen = set()
        while start is not None:
            if start in seen:
                raise TransitionConflict('Parent cycle')
            seen.add(start)
            keys.add(start)
            start = parents.get(start)
    return tuple(sorted(keys))


def reparent_review_records(snapshot, change):
    keys = set(reparent_scope_keys(snapshot, change))
    return {name: [r for r in snapshot['map'][name]
                  if (r['work_id'] in keys if name == 'pins' else keys.intersection(r['targets']))]
            for name in ('pins', 'permissions', 'conditions')}


def require_reparent_context(snapshot, request, review):
    change = request['changes'][0]
    keys = reparent_scope_keys(snapshot, change)
    records = reparent_review_records(snapshot, change)
    if records['conditions']:
        raise TransitionDenied('affected Condition needs disposition before reparent')
    if any(p['grantee'] == request['actor_id'] and 'reparent' in p['operations']
           and p['effect'] != 'allow' for p in records['permissions']):
        raise TransitionDenied('affected reparent denial still applies')
    for key in keys:
        context = inspect_work(snapshot, key)
        for name, status in ((n, context[k]) for n, k in (('pins', 'pin_status'), ('permissions', 'permission_status'), ('conditions', 'condition_status'))):
            if status != 'confirmed' and not (review is not None and review.permits(context, name)):
                raise ContextUnknown('affected reparent collection is unknown')


def review_decision_context(snapshot, request, assessments, *, resolver=repository_evidence):
    """Call only after reviewing positive sources, never absence of records.

Each collection assessment has mode, reason and refs. current_scope also
names the exact recorded set being assessed, including applicable ancestors
for authority/conditions. It establishes this operation's scope, not historic
completeness. An explicit grant's storage is distinct from its later exercise.
"""
    try:
        if len(request['changes']) != 1 or len(request['thread_entries']) != 1:
            raise ValueError('single operation only')
        change = request['changes'][0]
        op, target = change['op'], change['target']
        if (op not in ('update_work', 'set_permission', 'remove_permission', 'remove_pin', 'close', 'reparent', 'set_note')
                or (op == 'update_work' and set(change['value']) != {'origin'})
                or (op == 'set_permission' and (change['expected'] is not None
                    or change['value']['grantor_ref'] != 'principal:owner'
                    or change['value']['effect'] != 'allow'))):
            raise ValueError('unsupported review scope')
        if request['base_revision'] != snapshot['revision']:
            raise ValueError('stale review')
        context = inspect_work(snapshot, target)
        chain = {w['work_key'] for w in context['parent_chain']}
        if not isinstance(assessments, dict) or not assessments:
            raise ValueError('impact assessments required')
        related = reparent_scope_keys(snapshot, change) if op == 'reparent' else ()
        move_records = reparent_review_records(snapshot, change) if related else None
        if op in ('reparent', 'remove_permission', 'set_note') and set(assessments) != {'pins', 'permissions', 'conditions'}:
            raise ValueError('all affected collections must be assessed')
        refs = set()
        for name, item in assessments.items():
            if name not in ('pins', 'permissions', 'conditions'):
                raise ValueError('unknown collection')
            if not isinstance(item, dict) or not isinstance(item.get('reason'), str) or not item['reason'].strip():
                raise ValueError('operation-specific reason required')
            evidence = item.get('refs')
            if not isinstance(evidence, list) or not evidence or not set(evidence) <= set(request['source_refs']):
                raise ValueError('positive evidence must belong to this request')
            refs.update(evidence)
            mode = item.get('mode')
            if mode == 'operation_irrelevant':
                if set(item) != {'mode', 'reason', 'refs'}:
                    raise ValueError('invalid assessment')
                # Saving an additive Owner statement cannot authorize its use.
                # Only past participation is irrelevant to an Origin correction.
                if not (op in ('set_permission', 'update_work') and name == 'pins'):
                    raise ValueError('this collection can affect the operation')
            elif mode == 'current_scope':
                if set(item) != {'mode', 'reason', 'refs', 'records'}:
                    raise ValueError('exact assessed current scope required')
                records = [r for r in snapshot['map'][name]
                           if (r['work_id'] == target if name == 'pins' else chain.intersection(r['targets']))]
                if move_records is not None:
                    records = move_records[name]
                if item['records'] != records:
                    raise ValueError('assessed recorded scope differs')
            else:
                raise ValueError('unknown impact not resolved')
        bindings = tuple((ref, hashlib.sha256(resolver(ref)).hexdigest()) for ref in sorted(refs))
        return ReviewedDecisionContext(_digest(request), _digest(snapshot), snapshot['revision'], target,
                                       json.dumps(assessments, ensure_ascii=False, sort_keys=True), bindings, resolver, related)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise TransitionDenied('unknown impact needs an exact sourced operation review') from exc


def verify_context_sources(review):
    try:
        if not isinstance(review, ReviewedDecisionContext):
            raise ValueError()
        if any(hashlib.sha256(review.resolver(ref)).hexdigest() != digest for ref, digest in review.bindings):
            raise ValueError()
    except (ValueError, OSError, TypeError) as exc:
        raise TransitionConflict('operation context evidence changed') from exc


def require_operation_context(snapshot, request, review, **requirements):
    target = request['changes'][0]['target']
    context = inspect_work(snapshot, target)
    if review is not None:
        if not isinstance(review, ReviewedDecisionContext) or review.request_digest != _digest(request):
            raise TransitionDenied('operation context review does not match request')
        if review.snapshot_digest != _digest(snapshot):
            raise TransitionConflict('operation context premises changed')
        verify_context_sources(review)
        # Reviewed context does not waive any known applicable restriction.
        chain = {w['work_key'] for w in context['parent_chain']}
        change = request['changes'][0]
        ops = change['value']['operations'] if change['op'] == 'set_permission' else [change['op']]
        if change['op'] == 'set_note':
            ops.append('append_thread')
        actor = change['value']['grantee'] if change['op'] == 'set_permission' else request['actor_id']
        if any(chain.intersection(c['targets']) for c in snapshot['map']['conditions']):
            raise TransitionDenied('applicable Condition still needs disposition')
        if any(chain.intersection(p['targets']) and p['grantee'] == actor
               and set(ops).intersection(p['operations']) and p['effect'] != 'allow'
               for p in snapshot['map']['permissions']):
            raise TransitionDenied('applicable denial still blocks this operation')
    require_decision_context(context, operation_review=review, **requirements)


def retain_context_assessment(entry, review):
    """Technical proof on the existing record; never a new meaning entry."""
    try:
        audit = json.loads(entry.get('assumptions', '{}'))
    except (ValueError, TypeError):
        audit = None
    if not isinstance(audit, dict):
        audit = {'reported_assumptions': entry.get('assumptions')}
    audit['operation_context'] = {'request_digest': review.request_digest,
             'snapshot_digest': review.snapshot_digest, 'revision': review.revision,
             'target': review.target, 'assessments': json.loads(review.assessments),
             'evidence': [{'ref': r, 'sha256': h} for r, h in review.bindings]}
    if review.related_targets:
        audit['operation_context']['affected_work_keys'] = list(review.related_targets)
    entry['assumptions'] = json.dumps(audit, ensure_ascii=False, sort_keys=True)

