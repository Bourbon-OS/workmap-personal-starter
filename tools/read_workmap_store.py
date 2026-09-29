"""Read a committed Work Map for an AI or Steward, without a human portal."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from pma.workmap_contract import SnapshotInvalid, inspect_work, validate_snapshot
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from pma.workmap_resume import inspect_retired_work
from pma.workmap_thread_view import meaning_thread, project_context
from pma.workmap_references import current_reference_status


def id_title_tree(snapshot: dict) -> str:
    """Render current members only, using one validated Store revision."""
    display = snapshot["display_ids"]
    parents = {item["child"]: item["parent"] for item in snapshot["map"]["relations"] if item["kind"] == "parent"}
    children: dict[str | None, list[str]] = {}
    for key in snapshot["map"]["members"]:
        children.setdefault(parents.get(key), []).append(key)
    order = lambda key: tuple(int(part) for part in display[key][1:].split("."))
    for group in children.values():
        group.sort(key=order)
    lines: list[str] = []

    def visit(key: str, prefix: str, last: bool, root: bool = False) -> None:
        marker = "" if root else ("└─ " if last else "├─ ")
        lines.append(f"{prefix}{marker}{display[key]}  {snapshot['works'][key]['name']}")
        descendants = children.get(key, [])
        next_prefix = prefix + ("" if root else ("   " if last else "│  "))
        for index, child in enumerate(descendants):
            visit(child, next_prefix, index == len(descendants) - 1)

    for root in children.get(None, []):
        visit(root, "", True, True)
    return "\n".join(lines)


def read_result(directory: Path, display_id: str | None = None, *, tree: bool = False,
                history: bool = False, thread: bool = False, expected_project_id: str | None = None,
                evidence_repo: Path | None = None, backend: str = 'text', entry_id: str | None = None) -> dict:
    if entry_id is not None and not history:
        raise ValueError('entry lookup requires explicit history mode')
    try:
        committed = open_store(directory, backend).read()
        snapshot = validate_snapshot(committed.snapshot)
        if snapshot["metadata"].get("store_role") != "canonical_current":
            return {"status": "unknown", "reason": "selected Store is not declared canonical current"}
        if expected_project_id is not None and snapshot["project_id"] != expected_project_id:
            return {"status": "unknown", "reason": "selected Store belongs to another project"}
        if tree:
            return {"status": "confirmed", "store_token": committed.token, "revision": snapshot["revision"], "tree": id_title_tree(snapshot)}
        repo = evidence_repo or Path(__file__).resolve().parents[1]
        work_key = snapshot["aliases"].get(display_id) if display_id is not None else None
        if display_id is not None and work_key is None:
            return {"status": "unknown", "reason": "display ID is not in the committed alias index", "revision": snapshot["revision"]}
        if history:
            records = [e for e in snapshot["thread_entries"] if work_key is None or work_key in e["works"]]
            if entry_id is not None:
                records = [e for e in records if e['id'] == entry_id]
                if len(records) != 1:
                    return {'status': 'unknown', 'revision': snapshot['revision'],
                            'reason': 'raw entry is absent from the requested history scope'}
            tids = {e.get("transition_id") for e in records}
            return {"status": "confirmed", "revision": snapshot["revision"], "raw_records": records,
                    "receipts": {k: v for k, v in snapshot["receipts"].items() if (work_key is None and entry_id is None) or k in tids},
                    "receipt_scope": "all" if work_key is None and entry_id is None else "linked records only; use unscoped history for all receipts"}
        if thread or display_id is None:
            selection = meaning_thread(snapshot, work_key, repo=repo)
            result = {"status": selection["status"], "revision": snapshot["revision"], "thread": selection}
            if display_id is None and not thread:
                current_map = copy.deepcopy(snapshot['map'])
                members = set(current_map['members'])
                # A historical restart note is not a current instruction. Keep
                # raw storage and references to past providers unchanged.
                current_map['notes'] = [n for n in current_map['notes'] if members.intersection(n['targets'])]
                result.update(project_id=snapshot["project_id"], works={k: snapshot["works"][k] for k in snapshot["map"]["members"]},
                              map=current_map, display_ids={k: snapshot["display_ids"][k] for k in snapshot["map"]["members"]},
                              completeness=snapshot["metadata"].get("completeness"))
                result['reference_status'] = current_reference_status(current_map, repo)
            return result
        context = inspect_work(snapshot, work_key) if work_key in snapshot["map"]["members"] else inspect_retired_work(snapshot, work_key)
        context = project_context(context, snapshot, repo)
        context['reference_status'] = current_reference_status(context, repo)
        return {"status": "confirmed", "store_token": committed.token, "context": context}
    except (OSError, StoreError, SnapshotInvalid) as exc:
        return {"status": "unknown", "reason": f"committed Work Map is unreadable: {type(exc).__name__}: {exc}"}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Read one committed Work Map revision")
    parser.add_argument("--store", type=Path, required=True, help="absolute directory of the canonical Store")
    parser.add_argument("--backend", choices=("text", "sqlite"), default="text")
    parser.add_argument("--work", help="address-shaped display ID for a bounded context")
    parser.add_argument("--tree", action="store_true", help="show current ID and title tree from this revision")
    views = parser.add_mutually_exclusive_group()
    views.add_argument("--thread", action="store_true", help="show reviewed meaning only, with unknown/pending selection visible")
    views.add_argument("--history", action="store_true", help="explicit raw technical/provenance history and receipts")
    parser.add_argument('--entry', help='resolve one related_entries/selection_record raw ID; requires --history')
    parser.add_argument('--evidence-repo', type=Path, help='root resolving the snapshot-bound Thread selection')
    parser.add_argument("--expected-project", default="project:ashiba-steward-development")
    args = parser.parse_args()
    if not args.store.is_absolute():
        parser.error("--store must be an absolute path")
    if args.entry and not args.history:
        parser.error('--entry requires --history')
    if args.tree and (args.work or args.thread or args.history or args.entry):
        parser.error("--tree cannot be combined with --work, --thread or --history")
    result = read_result(args.store, args.work, tree=args.tree, thread=args.thread, history=args.history, expected_project_id=args.expected_project, backend=args.backend, entry_id=args.entry, evidence_repo=args.evidence_repo)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "confirmed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
