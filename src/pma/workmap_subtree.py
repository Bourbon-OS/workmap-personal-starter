"""A bounded view of one canonical Map, never a second current Store."""

import copy

from pma.workmap_contract import _collection_status, current_subtree, inspect_work
from pma.workmap_core import _digest
from pma.workmap_notes import recorded_evidence
from pma.workmap_references import inspect_references
from pma.workmap_resume import closed_identity
from pma.workmap_thread_view import meaning_thread


def subtree_view(snapshot, root_key, *, repo):
    keys = current_subtree(snapshot, root_key)
    selected = set(keys)
    members = set(snapshot['map']['members'])
    root = inspect_work(snapshot, root_key)
    ancestors = root['parent_chain'][:-1]
    relations = [copy.deepcopy(r) for r in snapshot['map']['relations']
                 if selected.intersection(r.get(n) for n in
                    (('child', 'parent') if r['kind'] == 'parent' else ('consumer', 'provider')))]
    external = {r[n] for r in relations for n in
                (('child', 'parent') if r['kind'] == 'parent' else ('consumer', 'provider'))
                if r[n] not in selected}
    context_keys = selected | external | {p['work_key'] for p in ancestors}

    def identity(key):
        value = snapshot['works'][key] if key in members else closed_identity(snapshot, key)
        status = 'current' if key in members else 'closed_history'
        if key not in members and snapshot['format_version'] == '0.4.3':
            from pma.workmap_transfer import provider_history, moved_out
            past = provider_history(snapshot, key)
            if past and (moved_out(snapshot, key) or value is None):
                value, status = past['provider_identity'], past['provider_status']
        return {'work_key': key, 'display_id': snapshot['display_ids'][key],
                'status': status,
                'identity': copy.deepcopy(value),
                'intent_status': 'confirmed' if value and value.get('intent') is not None else 'unknown'}

    collections = {
        key: {name: {'status': _collection_status(snapshot, name, key),
                     'recorded': copy.deepcopy([r for r in snapshot['map'][name]
                         if (r['work_id'] == key if name == 'pins' else key in r['targets'])])}
              for name in ('pins', 'permissions', 'action_rules')}
        for key in sorted(context_keys & members)
    }
    meaning, results, pending, historical = {}, {}, set(), {}
    statuses = []
    for key in keys:
        thread = meaning_thread(snapshot, key, repo=repo)
        statuses.append(thread['status'])
        pending.update(thread.get('unreviewed_record_ids', []))
        for e in thread['thread_entries']:
            meaning[e['id']] = {k: copy.deepcopy(e[k]) for k in
                                ('id', 'works', 'summary', 'related_entries', 'source_refs') if k in e}
            for k in e['works']:
                if k not in context_keys:
                    historical.setdefault(k, set()).add(e['id'])
        for e in recorded_evidence(snapshot, key):
            results[e['id']] = {k: copy.deepcopy(e[k]) for k in
                                ('id', 'works', 'summary', 'source_refs') if k in e}
    linked = {i for e in meaning.values() for i in e.get('related_entries', [e['id']])}
    raw = [e for e in snapshot['thread_entries']
           if context_keys.intersection(e['works']) or e['id'] in linked]
    refs = set()

    def collect(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ('refs', 'source_refs', 'evidence_refs') and isinstance(v, list):
                    refs.update(v)
                else:
                    collect(v)
        elif isinstance(value, list):
            for v in value:
                collect(v)

    view = {'root': root_key, 'works': [identity(k) for k in keys],
            'ancestors': copy.deepcopy(ancestors),
            'external_works': [identity(k) for k in sorted(external)],
            'historical_work_references': [{'work_key': k, 'entry_ids': sorted(ids),
                                           'retrieve': '--work ' + k + ' --thread'}
                                          for k, ids in sorted(historical.items())],
            'relations': relations, 'collections': collections,
            'external_dependency_history': [h for key in keys for h in inspect_work(snapshot, key)['external_dependency_history']],
            'thread': {'status': 'unknown' if 'unknown' in statuses else
                       'needs_review' if 'needs_review' in statuses else 'confirmed',
                       'entries': list(meaning.values()), 'unreviewed_record_ids': sorted(pending)},
            'evidence': list(results.values()),
            'history_bindings': [{'id': e['id'], 'sha256': _digest(e)} for e in raw],
            'return_to_whole': '--tree',
            'boundary': 'Current descendants and explicit links only; not all past district history. '
                        'Outside Work remains outside this view. Indexes do not prove authority, '
                        'current result applicability, or reference contents/versions.'}
    collect(view)
    view['references'] = inspect_references(sorted(refs), repo)
    return view


def subtree_digest(view, project_id):
    """One transient read projection: never authority for a write/reapplication."""
    return _digest({'project_id': project_id, 'view': view})
