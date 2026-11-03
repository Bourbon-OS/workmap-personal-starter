"""Explicit read/import boundary for the former 0.4.1 terminology.

Never mutate stored history or accept an old request as a current write.
Only this boundary translates the former collection and operation identifiers.
"""
from __future__ import annotations

import copy

CURRENT_FORMAT = '0.4.2'
LEGACY_FORMAT = '0.4.1'
LEGACY_OPERATIONS = {
    'set_condition': 'set_action_rule',
    'remove_condition': 'remove_action_rule',
    'transfer_condition': 'transfer_action_rule',
}


def current_shape(source):
    """Return an explicit validation/import copy; preserve raw records intact."""
    from pma.workmap_contract import SnapshotInvalid
    if not isinstance(source, dict) or source.get('format_version') != LEGACY_FORMAT:
        raise SnapshotInvalid('an explicit 0.4.1 source is required')
    m = source.get('map')
    if not isinstance(m, dict) or 'conditions' not in m or 'action_rules' in m:
        raise SnapshotInvalid('legacy collection is missing or mixed with Action Rules')
    result = copy.deepcopy(source)
    result['format_version'] = CURRENT_FORMAT
    result['map']['action_rules'] = result['map'].pop('conditions')
    complete = result.get('metadata', {}).get('completeness')
    if isinstance(complete, dict):
        if 'action_rules' in complete:
            raise SnapshotInvalid('legacy completeness mixes current terminology')
        if 'conditions' in complete:
            complete['action_rules'] = complete.pop('conditions')
    for grant in result['map'].get('permissions', []):
        if isinstance(grant, dict) and isinstance(grant.get('operations'), list):
            grant['operations'] = [LEGACY_OPERATIONS.get(op, op) for op in grant['operations']]
    return result


def validate_history_snapshot(source):
    """Validate a retained version explicitly and return its current-shape copy."""
    from pma.workmap_contract import validate_snapshot
    candidate = current_shape(source) if source.get('format_version') == LEGACY_FORMAT else copy.deepcopy(source)
    return validate_snapshot(candidate)
