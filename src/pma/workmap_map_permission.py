"""Explicit Map-wide Work registration using the existing Permission shape.

This is not inheritance of ordinary Parent permissions. An explicit operation,
project binding and unchanged single-root Map scope are all required.
"""
from pathlib import Path
from pma.workmap_contract import inspect_work, require_decision_context, current_subtree
from pma.workmap_core import TransitionDenied, TransitionError

MAP_ADD_OPERATION = 'add_work_in_map'
PROJECT_SCOPE_PREFIX = 'map_project_id:'
STORE_SCOPE_PREFIX = 'map_store_path:'


def _local_path(value):
    if value is None or str(value).startswith(('\\\\', '//')):
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    path = path.resolve()
    return None if str(path).startswith(('\\\\', '//')) else path


def _bound_path(value):
    # Never re-resolve a saved binding: a later junction change must not move it.
    if value is None or str(value).startswith(('\\\\', '//')):
        return None
    path = Path(value)
    return path if path.is_absolute() and '..' not in path.parts else None


def map_scope_root(snapshot):
    if snapshot['metadata'].get('store_role') != 'canonical_current':
        return None
    members = set(snapshot['map']['members'])
    children = {r['child'] for r in snapshot['map']['relations'] if r['kind'] == 'parent'}
    roots = members - children
    if len(roots) != 1:
        return None
    root = next(iter(roots))
    return root if set(current_subtree(snapshot, root)) == members else None


def _matches_scope(snapshot, permission, root, store_path):
    scopes = [c[len(PROJECT_SCOPE_PREFIX):] for c in permission.get('constraints', [])
              if isinstance(c, str) and c.startswith(PROJECT_SCOPE_PREFIX)]
    stores = [c[len(STORE_SCOPE_PREFIX):] for c in permission.get('constraints', [])
              if isinstance(c, str) and c.startswith(STORE_SCOPE_PREFIX)]
    return (root is not None and permission['targets'] == [root]
            and permission['operations'] == [MAP_ADD_OPERATION]
            and scopes == [snapshot['project_id']] and len(stores) == 1
            and _local_path(store_path) is not None
            and _bound_path(stores[0]) == _local_path(store_path))


def validate_map_add_grant(snapshot, permission, store_path):
    """Reject malformed/fuzzy scopes at the ordinary set_permission boundary."""
    if MAP_ADD_OPERATION in permission.get('operations', []):
        if not _matches_scope(snapshot, permission, map_scope_root(snapshot), store_path):
            raise TransitionError('Map-wide addition needs the unchanged single root, exact project and Store binding')


def map_add_permissions(snapshot, work_key, actor=None, *, store_path=None):
    if work_key not in snapshot['map']['members']:
        return []
    root = map_scope_root(snapshot)
    return [p for p in snapshot['map']['permissions'] if _matches_scope(snapshot, p, root, store_path)
            and (actor is None or p.get('grantee') == actor)]


def require_add_work_permission(snapshot, work_key, actor, *, store_path=None):
    direct = [p for p in snapshot['map']['permissions'] if work_key in p['targets']
              and 'add_work' in p['operations'] and p.get('grantee') == actor]
    wide = map_add_permissions(snapshot, work_key, actor, store_path=store_path)
    if any(p['effect'] != 'allow' for p in [*direct, *wide]):
        raise TransitionDenied('direct or Map-wide registration denial still applies')
    root = map_scope_root(snapshot)
    if root is not None:
        # An unknown root collection could conceal a relevant Map-wide denial.
        require_decision_context(inspect_work(snapshot, root), needs_pin=False,
                                 needs_parent_intent=False)
    if not direct and not wide:
        raise TransitionDenied('Work registration is not permitted for this actor and Map')
