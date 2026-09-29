"""Export one committed Work Map revision or restore it to an isolated candidate.

Recovery never overwrites or switches the original canonical Store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from pma.workmap_contract import validate_snapshot
from pma.workmap_store_factory import open_store
from pma.workmap_thread_view import (BINDING, SelectionUnavailable, meaning_thread,
                                    selection_refs, validate_selection)


def _encode(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _thread_files(snapshot: dict, repo: Path) -> list[tuple[Path, bytes]]:
    """Only the latest bound projection is needed for this revision's meaning.

    Reuse the existing reference/hash contract, without adding backup metadata
    or following arbitrary Evidence/Artifact paths.
    """
    bindings = [selection_refs(e) for e in snapshot['thread_entries'] if selection_refs(e)]
    if not bindings:
        return []
    refs = bindings[-1]
    match = BINDING.fullmatch(refs[0]) if len(refs) == 1 else None
    if match is None:
        raise SelectionUnavailable('latest selection reference is invalid or ambiguous')
    relative = Path('docs/work-management/evidence') / match[1]
    path = (repo / relative).resolve()
    if not path.is_relative_to(repo.resolve()) or path.parent != (repo / relative.parent).resolve():
        raise SelectionUnavailable('selection escapes the Evidence root')
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != match[2]:
            raise SelectionUnavailable('selection bytes differ from the snapshot reference')
        validate_selection(snapshot, json.loads(raw))
    except (OSError, ValueError) as exc:
        raise SelectionUnavailable(str(exc)) from exc
    return [(relative, raw)]


def _copy_thread_files(files: list[tuple[Path, bytes]], root: Path) -> None:
    for relative, raw in files:
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('backup Evidence destination escapes its root')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('xb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError('existing Evidence differs; use a fresh backup/recovery directory')


def export_one(store_path: Path, output_path: Path, *, backend: str = 'text',
               evidence_repo: Path | None = None) -> dict:
    current = open_store(store_path, backend).read()
    snapshot = validate_snapshot(current.snapshot)
    if snapshot["metadata"].get("store_role") != "canonical_current":
        raise ValueError("source is not the declared canonical Store")
    if output_path.exists():
        raise FileExistsError(output_path)
    repo = evidence_repo or Path(__file__).resolve().parents[1]
    files = _thread_files(snapshot, repo)
    # Publish backup.json last; a crash before this leaves no completed backup.
    _copy_thread_files(files, output_path.parent)
    raw = _encode({"format": "ashiba-workmap-backup-1", "store_token": current.token, "snapshot": snapshot})
    with output_path.open("xb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    return {"revision": snapshot["revision"], "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            'required_thread_files': [p.as_posix() for p, _ in files],
            'backup_boundary': 'backup JSON plus required_thread_files relative to its directory'}


def recover_isolated(backup_path: Path, target_path: Path, expected_sha256: str, *, backend: str = 'text') -> dict:
    raw = backup_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError("backup SHA-256 differs from reviewed bytes")
    backup = json.loads(raw)
    if not isinstance(backup, dict) or backup.get("format") != "ashiba-workmap-backup-1":
        raise ValueError("backup envelope is invalid")
    snapshot = validate_snapshot(backup.get("snapshot"))
    if snapshot["metadata"].get("store_role") != "canonical_current":
        raise ValueError("backup is not from the declared canonical Store")
    try:
        files = _thread_files(snapshot, backup_path.parent)
        thread_error = None
    except SelectionUnavailable as exc:
        files, thread_error = [], str(exc)
    snapshot["metadata"]["store_role"] = "recovery_candidate"
    snapshot["metadata"]["recovery_source_sha256"] = digest
    target = open_store(target_path, backend)
    committed = target.initialize(snapshot)
    if validate_snapshot(target.read().snapshot) != snapshot or committed.snapshot != snapshot:
        raise RuntimeError("isolated recovery differs from backup")
    # A candidate remains isolated even if companion Evidence is missing or
    # cannot be copied. Never claim complete management recovery in that case.
    evidence_root = target_path.parent / (target_path.name + '.evidence')
    view = None
    if thread_error is None:
        try:
            _copy_thread_files(files, evidence_root)
            view = meaning_thread(snapshot, repo=evidence_root)
            if view['status'] == 'unknown':
                thread_error = view['reason']
        except (OSError, ValueError) as exc:
            thread_error = str(exc)
    return {"revision": snapshot["revision"], "project_id": snapshot["project_id"], "source_sha256": digest, "role": "recovery_candidate",
            "management_recovery": 'incomplete' if thread_error else 'verified',
            'map_recovery': 'verified', 'thread_recovery': 'unknown' if thread_error else 'verified',
            'thread_status': 'unknown' if thread_error else view['status'],
            'thread_unreviewed_count': None if thread_error else view['unreviewed_count'],
            'thread_reason': thread_error, 'evidence_repo': str(evidence_root),
            "artifact_recovery": "unknown",
            "artifact_reason": "backup retains references, not external artifact contents"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    exporting = sub.add_parser("export")
    exporting.add_argument("--store", type=Path, required=True)
    exporting.add_argument("--output", type=Path, required=True)
    exporting.add_argument('--evidence-repo', type=Path, help='root resolving the snapshot-bound Thread selection')
    recovering = sub.add_parser("recover")
    recovering.add_argument("--backup", type=Path, required=True)
    recovering.add_argument("--target", type=Path, required=True)
    recovering.add_argument("--expected-sha256", required=True)
    exporting.add_argument("--backend", choices=("text", "sqlite"), default="text")
    recovering.add_argument("--backend", choices=("text", "sqlite"), default="text")
    args = parser.parse_args()
    result = export_one(args.store, args.output, backend=args.backend, evidence_repo=args.evidence_repo) if args.command == "export" else recover_isolated(args.backup, args.target, args.expected_sha256, backend=args.backend)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
