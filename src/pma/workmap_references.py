"""Read-side availability of public repository references; never authority."""

from pathlib import Path, PurePosixPath


def inspect_references(refs: list[str], repo: Path) -> list[dict]:
    results = []
    root = repo.resolve()
    for ref in dict.fromkeys(refs):
        result = {'ref': ref, 'availability': 'unknown', 'version_status': 'unknown'}
        relative = ref.removeprefix('repo:').split('#', 1)[0]
        parts = PurePosixPath(relative).parts
        if (not ref.startswith('repo:') or not parts or parts[0] not in ('docs', 'skills')
                or any(p in ('.', '..') or ':' in p or '\\' in p for p in parts)):
            result['reason'] = 'reference needs its environment resolver'
        else:
            try:
                path = (root / relative).resolve()
                if not path.is_relative_to(root / parts[0]):
                    result['reason'] = 'reference escapes the public repository directory'
                elif path.is_file():
                    result['availability'] = 'available'
                    result['reason'] = 'file exists; contents, revision and applicability are not verified'
                else:
                    result['reason'] = 'referenced file is missing or is not a file'
            except (OSError, ValueError, RuntimeError):
                result['reason'] = 'reference availability cannot be read'
        results.append(result)
    return results


def current_reference_status(current: dict, repo: Path) -> list[dict]:
    refs = [ref for note in current.get('notes', []) for ref in note.get('refs', [])]
    relations = current.get('dependencies', current.get('relations', []))
    refs.extend(ref for item in relations if item.get('kind') == 'dependency' for ref in item.get('refs', []))
    refs.extend(ref for item in relations for ref in (item.get('provider_close', {}).get('evidence_refs') or []))
    refs.extend(ref for item in current.get('recorded_evidence', []) for ref in item.get('source_refs', []))
    refs.extend(ref for item in current.get('actor_helpers', []) for ref in item['note']['refs'])
    if isinstance(current.get('map'), dict):
        refs.extend(r['ref'] for r in current_reference_status(current['map'], repo))
    if isinstance(current.get('thread'), dict):
        refs.extend(ref for item in current['thread'].get('thread_entries', []) for ref in item.get('source_refs', []))
    return inspect_references(refs, repo)
