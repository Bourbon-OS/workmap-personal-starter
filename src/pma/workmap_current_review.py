"""Read-only impact assessment for a registered current-Map review.

No grants, retries of writes, history inventory or AI calls. The caller must
reconsider relevant subject changes; acknowledging a snapshot grants nothing.
"""
from __future__ import annotations

import json

from pma.workmap_contract import ContextUnknown, validate_snapshot
from pma.workmap_core import TransitionConflict, TransitionDenied
from pma.workmap_store import snapshot_object_id


# Existing Core history kinds and the current collections they may change.
# Unrecognised history cannot prove independence. This is not a new schema.
EFFECTS = {
    'delegation_accepted': {'pins', 'permissions'},
    'delegation_finished': {'pins', 'permissions'},
    'owner_scope_accepted': {'pins', 'permissions'},
    'owner_scope_renewed': {'pins', 'permissions'},
    'pin_added': {'pins'}, 'pin_changed': {'pins'}, 'pin_removed': {'pins'},
    'pin_active_moved': {'pins', 'notes'},
    'permission_changed': {'permissions', 'metadata'},
    'action_rule_changed': {'action_rules'},
    'action_rule_transferred': {'action_rules'},
    'note_changed': {'notes'},
    'work_renamed': {'works'},
    'correction': {'works'},
    'intent_confirmation': {'works'}, 'intent_correction': {'works'}, 'intent_refinement': {'works'},
    'dependency_added': {'relations'}, 'dependency_fulfilled': {'relations'},
    'dependency_disposition_authorized': set(),
    'work_added': {'works', 'members', 'relations', 'aliases', 'display_ids', 'metadata'},
    'reparent': {'relations', 'aliases', 'display_ids', 'display_id_history'},
    'close': {'works', 'members', 'relations', 'retirements'},
    'reopen': {'works', 'members', 'relations'},
    'handoff': {'pins', 'permissions'},
    'evidence_recorded': set(), 'rationale_added': set(),
}
AUTHORITY_HISTORY = {
    'permission_changed', 'handoff', 'intent_confirmation', 'intent_correction',
    'intent_refinement', 'reparent', 'close', 'reopen',
}


def _effects(entry):
    if entry['kind'] == 'evidence_recorded' and entry.get('consequence'):
        try:
            detail = json.loads(entry['consequence'])
        except (TypeError, ValueError) as exc:
            raise ContextUnknown('technical result history is unreadable') from exc
        if isinstance(detail, dict) and set(detail) == {'old_note', 'new_note'}:
            from pma.workmap_notes import note_history
            note_history(entry)
            return {'notes'}
    return EFFECTS[entry['kind']]


def _keys(field, value, state):
    if value is None:
        return set()
    if field == 'pins':
        return {value['work_id']}
    if field in {'permissions', 'action_rules', 'notes'}:
        return set(value['targets'])
    if field == 'relations':
        return ({value['child'], value['parent']} if value['kind'] == 'parent'
                else {value['consumer'], value['provider']})
    if field == 'aliases':
        return {value}
    if field in {'works', 'members', 'display_ids', 'retirements'}:
        return set()
    return set(state['map']['members'])


def _differences(base, current):
    """Shallow changed-record index; omit values and unchanged history bodies."""
    result = []
    for field in ('pins', 'permissions', 'action_rules', 'notes', 'relations'):
        old = {v['id']: v for v in base['map'][field]}
        new = {v['id']: v for v in current['map'][field]}
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                works = _keys(field, old.get(key), base) | _keys(field, new.get(key), current)
                result.append({'field': field, 'id': key, 'works': sorted(works)})
    for field in ('works', 'aliases', 'display_ids', 'retirements'):
        old, new = base[field], current[field]
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                works = (_keys(field, old.get(key), base) | _keys(field, new.get(key), current)
                         if field == 'aliases' else {key})
                result.append({'field': field, 'id': key, 'works': sorted(works)})
    for key in sorted(set(base['map']['members']) ^ set(current['map']['members'])):
        result.append({'field': 'members', 'id': key, 'works': [key]})
    for field in ('metadata', 'display_id_history'):
        if base[field] != current[field]:
            result.append({'field': field, 'id': field,
                           'works': sorted(set(base['map']['members']) | set(current['map']['members']))})
    allowed = {'revision', 'recorded_at', 'thread_entries', 'receipts', 'map', 'works',
               'aliases', 'display_ids', 'retirements', 'metadata', 'display_id_history'}
    if any(base[f] != current[f] for f in set(base) - allowed):
        raise ContextUnknown('unassessed global review context changed')
    if set(base['map']) != set(current['map']) or set(base['map']) != {'members', 'pins', 'permissions', 'action_rules', 'notes', 'relations'}:
        raise ContextUnknown('unassessed Map collection changed')
    return result


def _history(base, current):
    if (any(current['receipts'].get(t) != r for t, r in base['receipts'].items())
            or current['thread_entries'][:len(base['thread_entries'])] != base['thread_entries']):
        raise TransitionConflict('original committed review history is not retained')
    suffix = current['thread_entries'][len(base['thread_entries']):]
    new_receipts = set(current['receipts']) - set(base['receipts'])
    tids = {e.get('transition_id') for e in suffix}
    if new_receipts != tids or any(t in base['receipts'] for t in tids):
        # Includes freshness-only/unsupported updates without sufficient history.
        raise ContextUnknown('intervening updates lack assessable committed history')
    for entry in suffix:
        if entry['kind'] not in EFFECTS:
            raise ContextUnknown('intervening history kind is not assessed: ' + entry['kind'])
        if current['receipts'][entry['transition_id']]['result'] != 'applied':
            raise ContextUnknown('intervening history is not an applied transition')
    return suffix


def guard_review_history(suffix, related, actors, request):
    """A subject reread cannot revive an ended acceptance or changed authority."""
    for entry in suffix:
        if related.intersection(entry['works']) and entry['kind'] in AUTHORITY_HISTORY:
            raise TransitionDenied('related authority/purpose history changed; renewed scope is required')
        if (entry['kind'] in {'pin_added', 'pin_changed', 'pin_removed', 'pin_active_moved'}
                and entry['actor'] in actors):
            raise TransitionDenied('accepted participant Pin history changed')
        if entry['kind'] == 'delegation_finished':
            try:
                if json.loads(entry['consequence'])['registration'] == request['transition_id']:
                    raise TransitionDenied('review registration was ended; it cannot be revived')
            except (ValueError, KeyError, TypeError) as exc:
                raise ContextUnknown('delegation disposition history is unreadable') from exc


def assess_current_review(base, current, request, *, scope_context):
    """Return ready/needs_reconsideration; invalid authority raises instead."""
    related, actors = scope_context(base, request, allow_action_rules=True)
    now_related, now_actors = scope_context(current, request, allow_action_rules=True)
    all_related = related | now_related
    if actors != now_actors:
        raise TransitionDenied('accepted participants changed')
    value, target = request['changes'][0]['value'], request['changes'][0]['target']
    for state in (base, current):
        for actor in actors:
            active = [p for p in state['map']['pins'] if p['actor_id'] == actor and p['mode'] == 'active']
            if len(active) != 1 or active[0]['work_id'] != target:
                raise TransitionDenied('accepted participant is no longer solely active on this Work')
    # Original purpose and its authority chain cannot be widened by an ack.
    from pma.workmap_contract import inspect_work
    old_chain = [(w['work_key'], w['intent']) for w in inspect_work(base, target)['parent_chain']]
    new_chain = [(w['work_key'], w['intent']) for w in inspect_work(current, target)['parent_chain']]
    if old_chain != new_chain:
        raise TransitionDenied('accepted purpose or Parent changed; renewed scope is required')
    suffix = _history(base, current)
    changes = _differences(base, current)
    for delta in changes:
        if not any(delta['field'] in _effects(e) and set(delta['works']).intersection(e['works']) for e in suffix):
            raise ContextUnknown('changed data is not covered by committed operation history: ' + delta['field'])
    guard_review_history(suffix, all_related, actors, request)
    relevant = [d for d in changes if all_related.intersection(d['works'])]
    # Direct child/sibling membership and labels are part of the reader's brief.
    relatives = {v['work_key'] for s in (base, current) for name in ('children', 'siblings')
                 for v in inspect_work(s, target)[name]}
    relevant += [d for d in changes if d['field'] in {'works', 'display_ids', 'members', 'relations'}
                 and relatives.intersection(d['works']) and d not in relevant]
    history = [{'id': e['id'], 'kind': e['kind'], 'works': e['works']}
               for e in suffix if all_related.intersection(e['works'])]
    # Even restored subject bytes need re-evaluation; do not ignore semantic ABA.
    needs = bool(relevant or history or related != now_related)
    return {'status': 'needs_reconsideration' if needs else 'ready',
            'impact': 'related' if needs else 'unrelated', 'changes': relevant,
            'history': history, 'review_token': snapshot_object_id(current)}


def acknowledge_current_subject(store, token, current, assessment, request, *, scope_context):
    """Explicit cooperating reviewer input after reread, not automated approval."""
    if token is None:
        return assessment
    if (not isinstance(token, str) or len(token) != 64
            or any(c not in '0123456789abcdef' for c in token)):
        raise TransitionDenied('reconsidered token must be a reviewed current snapshot hash')
    reviewed = (current if token == snapshot_object_id(current)
                else validate_snapshot(store.read_retained_snapshot(token)))
    if (snapshot_object_id(reviewed) != token or reviewed['project_id'] != current['project_id']
            or reviewed['metadata'].get('store_role') != 'canonical_current'
            or reviewed['receipts'].get(request['transition_id']) != current['receipts'].get(request['transition_id'])):
        raise TransitionConflict('reconsidered subject does not retain the original registration')
    if reviewed == current:
        return {**assessment, 'status': 'ready', 'impact': 'reconsidered', 'comparison_token': token}
    # A prior explicit reconsideration survives subsequent unrelated updates.
    # A new relevant update is returned to the reviewer, not auto-acknowledged.
    delta = assess_current_review(reviewed, current, request, scope_context=scope_context)
    return {**delta, 'impact': 'reconsidered' if delta['status'] == 'ready' else delta['impact'],
            'comparison_token': token}
