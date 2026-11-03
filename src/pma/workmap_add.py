"""Build an ordinary add_work request from a chosen snapshot, without I/O."""
from datetime import datetime, timezone
from uuid import uuid4

from pma.workmap_contract import child_address_prefix


def build_add_request(current, *, actor, parent, name, intent, origin, refs,
                      reason=None, root=False, display_id=None, recorded_at=None):
    if not all(isinstance(v, str) and v.strip() for v in (actor, name, intent, origin)):
        raise ValueError('actor, Name, Intent and Origin are required')
    if not refs:
        raise ValueError('Origin needs an actual source reference')
    if current['metadata'].get('store_role') != 'canonical_current':
        raise ValueError('selected Store is not canonical current')
    if root:
        if parent is not None or not display_id:
            raise ValueError('root needs an explicit address, without Parent')
        parent_key = None
    else:
        parent_key = current['aliases'].get(parent, parent)
        if parent_key not in current['map']['members'] or display_id is not None:
            raise ValueError('Parent is not a current Work, or explicit address is root-only')
    refs = list(dict.fromkeys(refs))
    prefix = 'W' if root else child_address_prefix(current['display_ids'][parent_key])
    reserved = set(current['aliases']) | set(current['display_ids'].values())
    number = 1
    while prefix + str(number) in reserved:
        number += 1
    display = display_id if root else prefix + str(number)
    key, tid = str(uuid4()), 'transition:' + str(uuid4())
    request = {'format_version': current['format_version'], 'project_id': current['project_id'],
               'transition_id': tid, 'actor_id': actor, 'base_revision': current['revision'],
               'intent': 'Register the specified Work', 'source_refs': refs,
               'preconditions': [], 'changes': [{'op': 'add_work', 'target': key if root else parent_key,
                   'value': {'work': {'work_key': key, 'name': name, 'intent': intent,
                                      'origin': {'summary': origin, 'refs': refs}}, 'display_id': display}}],
               'thread_entries': []}
    if root:
        request['changes'][0]['value']['parent'] = None
    if reason:
        request['thread_entries'] = [{'id': 'rationale:' + tid, 'works': [key] if root else [key, parent_key],
            'kind': 'rationale_added', 'summary': name + ': additional rationale', 'why': reason,
            'occurred_at': None, 'recorded_at': recorded_at or datetime.now(timezone.utc).isoformat(),
            'actor': actor, 'source_refs': refs, 'transition_id': tid}]
    return request
