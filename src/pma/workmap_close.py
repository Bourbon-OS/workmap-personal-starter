"""Bind the existing close judgment to a request and its reviewed evidence.

No Work field or new completion criterion is introduced. The local operator
assesses the meaning; hashes only bind the reviewed versions.
"""

import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, _digest, _refs

PROJECTION = 'close_basis'


@dataclass(frozen=True)
class ReviewedClose:
    request_digest: str


def repository_evidence(ref: str) -> bytes:
    relative = ref.removeprefix('repo:')
    parts = PurePosixPath(relative).parts
    root = Path(__file__).resolve().parents[2]
    if (not ref.startswith('repo:') or not parts or parts[0] not in ('docs', 'skills')
            or any(p in ('..', '.') or ':' in p or '\\' in p for p in parts)
            or '#' in relative):
        raise TransitionDenied('close evidence needs a supported repository reference')
    path = (root / relative).resolve()
    if not path.is_relative_to(root / parts[0]):
        raise TransitionDenied('close evidence escapes the repository boundary')
    return path.read_bytes()


def _entry(request):
    changes, entries = request.get('changes'), request.get('thread_entries')
    if (not isinstance(changes, list) or not changes or any(not isinstance(c, dict) for c in changes)
            or changes[-1].get('op') != 'close' or not isinstance(entries, list)
            or any(not isinstance(e, dict) for e in entries)):
        raise TransitionError('close review needs a request ending with close and its reasons')
    if any(not isinstance(c.get('target'), str) or not c['target'].strip() for c in changes):
        raise TransitionError('close request target is missing')
    closing = [c['target'] for c in changes if c.get('op') == 'close']
    if len(closing) != len(set(closing)):
        raise TransitionError('duplicate close target')
    selected = []
    for target in closing:
        matches = [e for e in entries if e.get('kind') == 'close' and e.get('works') == [target]]
        if len(matches) != 1:
            raise TransitionError('each closed Work needs its own close reason')
        entry = matches[0]
        if (entry.get('outcome') not in ('completed', 'stopped', 'replaced')
                or not isinstance(entry.get('why'), str) or not entry['why'].strip()
                or not isinstance(entry.get('residuals'), list)):
            raise TransitionError('close reason, outcome and residual disposition are required')
        evidence = entry.get('evidence_refs', [])
        if (not isinstance(evidence, list) or any(not isinstance(r, str) or not r.strip() for r in evidence)
                or (entry['outcome'] == 'completed' and not evidence)):
            raise TransitionError('completed requires Intent achievement evidence for this Work')
        selected.append(entry)
    for entry in entries:
        if not _refs(entry.get('source_refs')):
            raise TransitionError('close sources are unreadable')
    if not _refs(request.get('source_refs')):
        raise TransitionError('close sources are unreadable')
    return selected[-1]


def _versions(request, resolver):
    _entry(request)
    refs = list(request['source_refs'])
    for entry in request['thread_entries']:
        extra = entry.get('evidence_refs', [])
        if not isinstance(extra, list) or any(not isinstance(r, str) or not r.strip() for r in extra):
            raise TransitionError('evidence references are unreadable')
        refs.extend(entry['source_refs'] + extra)
    for relation in close_precondition(request)['expected'].get('retained_dependencies', []):
        refs.extend(relation['refs'])
    refs = sorted(set(refs))
    try:
        return [{'ref': ref, 'sha256': hashlib.sha256(resolver(ref)).hexdigest()} for ref in refs]
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        raise TransitionConflict('close evidence is unavailable; re-evaluate the close basis') from exc


def bind_close_basis(snapshot, request, *, resolver=repository_evidence, retained_dependency_ids=()):
    """Prepare, then inspect and review this exact request before applying it."""
    _entry(request)
    if request.get('preconditions'):
        close_precondition(request)
    if request['base_revision'] != snapshot['revision']:
        raise TransitionConflict('close basis must use the observed Store revision')
    result = copy.deepcopy(request)
    ids = list(retained_dependency_ids)
    if any(not isinstance(key, str) for key in ids) or len(ids) != len(set(ids)):
        raise TransitionError('retained Dependency selection is invalid')
    selected = [copy.deepcopy(r) for r in snapshot['map']['relations']
                if r['kind'] == 'dependency' and r['id'] in ids]
    if len(selected) != len(ids):
        raise TransitionConflict('retained Dependency does not exist')
    expected = {'snapshot_sha256': _digest(snapshot), 'evidence': []}
    if selected:
        expected['retained_dependencies'] = selected
    result['preconditions'] = [{'selector': request['changes'][-1]['target'], 'projection': PROJECTION,
        'expected': expected}]
    expected['evidence'] = _versions(result, resolver)
    return result


def review_close(request):
    """Operator confirmed Intent/outcome, residuals, applicable grants and sources.

    Calling this without that assessment is not a valid semantic review. This
    is neither an additional Owner approval nor authentication.
    """
    _entry(request)
    return ReviewedClose(_digest(request))


def close_precondition(request):
    pre = request.get('preconditions')
    if (not isinstance(pre, list) or len(pre) != 1 or not isinstance(pre[0], dict)
            or set(pre[0]) != {'selector', 'projection', 'expected'}
            or pre[0]['projection'] != PROJECTION
            or pre[0]['selector'] != request['changes'][-1]['target']
            or not isinstance(pre[0]['expected'], dict)
            or set(pre[0]['expected']) not in ({'snapshot_sha256', 'evidence'},
                {'snapshot_sha256', 'evidence', 'retained_dependencies'})):
        raise TransitionError('close needs a bound current basis')
    retained = pre[0]['expected'].get('retained_dependencies', [])
    if (not isinstance(retained, list) or any(not isinstance(r, dict)
            or r.get('kind') != 'dependency' or not _refs(r.get('refs'))
            or not all(isinstance(r.get(k), str) for k in ('id', 'consumer', 'provider')) for r in retained)):
        raise TransitionError('retained Dependency basis is unreadable')
    return pre[0]


def verify_retained_dependencies(snapshot, request):
    """Retention is explicit disposition, never a fulfillment judgment."""
    closing = {c['target'] for c in request['changes'] if c['op'] == 'close'}
    retained = close_precondition(request)['expected'].get('retained_dependencies', [])
    incoming = [r for r in snapshot['map']['relations'] if r['kind'] == 'dependency' and r['provider'] in closing]
    if retained != incoming:
        raise TransitionConflict('current Dependency needs explicit retention disposition')
    if any(r['consumer'] in closing for r in retained):
        raise TransitionConflict('retained Dependency consumer must remain current')
    for provider in {r['provider'] for r in retained}:
        entry = next(e for e in request['thread_entries'] if e.get('kind') == 'close' and e.get('works') == [provider])
        if not entry.get('residuals'):
            raise TransitionError('retained Dependency needs a residual disposition reason')


def verify_evidence(request, resolver):
    expected = close_precondition(request)['expected']
    if expected['evidence'] != _versions(request, resolver):
        raise TransitionConflict('close evidence changed; re-evaluate the close basis')


def verify_close(snapshot, request, review, resolver):
    if not isinstance(review, ReviewedClose) or review.request_digest != _digest(request):
        raise TransitionDenied('close basis needs local review of this exact request')
    expected = close_precondition(request)['expected']
    if expected['snapshot_sha256'] != _digest(snapshot):
        raise TransitionConflict('close context changed; re-evaluate Intent and residual disposition')
    verify_retained_dependencies(snapshot, request)
    verify_evidence(request, resolver)
