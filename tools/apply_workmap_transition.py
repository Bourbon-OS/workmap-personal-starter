"""Apply one reviewed Work Map transition to an existing local canonical Store.

The local OS permission boundary identifies who may invoke this command. The
actor_id inside the request is a claimed work context, not authentication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from pma.workmap_close import review_close
from pma.workmap_decision_context import review_decision_context

from pma.workmap_canonical_core import CanonicalWorkMapCore, ReviewedOwnerHandoff, ReviewedOwnerIntent, ReviewedDependencyDisposition
from pma.workmap_contract import ContextUnknown, SnapshotInvalid, validate_snapshot
from pma.workmap_core import TransitionConflict, TransitionDenied, TransitionError, UnsupportedTransition, _digest
from pma.workmap_store import StoreError
from pma.workmap_store_factory import open_store
from pma.workmap_review_gate import review_delegation
from pma.workmap_owner_scope import ReviewedOwnerScope
from pma.workmap_pin_move import ReviewedPinMove
from pma.workmap_owner_add import review_owner_add
from pma.workmap_owner_permission import review_owner_permission
from pma.workmap_thread_view import validate_publication, SelectionUnavailable


def review_owner_handoff(request: dict) -> ReviewedOwnerHandoff:
    """Check bytes after the local operator explicitly reviews attribution/scope.

    Hash equality establishes the reviewed document version, not Owner identity.
    The explicit CLI flag is a trust input from the existing local OS boundary.
    """
    changes = request.get("changes") if isinstance(request, dict) else None
    if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict) or changes[0].get("op") != "handoff":
        raise TransitionDenied("local handoff review requires one handoff request")
    value = changes[0].get("value")
    sources = value.get("owner_sources") if isinstance(value, dict) else None
    if not isinstance(sources, list) or not sources:
        raise TransitionDenied("Owner source documents are missing")
    repo = Path(__file__).resolve().parents[1]
    refs = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"ref", "sha256"}:
            raise TransitionDenied("Owner source document binding is invalid")
        ref = source["ref"]
        prefix = "repo:docs/work-management/evidence/owner_"
        if not isinstance(ref, str) or not ref.startswith(prefix) or not ref.endswith(".txt"):
            raise TransitionDenied("Owner source is outside the handoff evidence boundary")
        path = (repo / ref[len("repo:"):]).resolve()
        if path.parent != (repo / "docs/work-management/evidence").resolve():
            raise TransitionDenied("Owner source path is outside the evidence directory")
        try:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TransitionDenied("Owner source document is unreadable") from exc
        if actual_hash != source["sha256"]:
            raise TransitionDenied("Owner source document differs from reviewed bytes")
        refs.append(ref)
    return ReviewedOwnerHandoff(_digest(request), tuple(refs))


def review_owner_intent(request: dict, repo: Path | None = None) -> ReviewedOwnerIntent:
    """Bind bytes after review of Owner attribution, same Work, and Action Rules.

    The operator must assess meaning; a kind label or matching hash cannot do
    that. This uses the existing cooperating local OS boundary, not new auth.
    """
    changes = request.get("changes") if isinstance(request, dict) else None
    if (not isinstance(changes, list) or len(changes) != 1
            or not isinstance(changes[0], dict) or changes[0].get("op") != "update_work"):
        raise TransitionDenied("local Intent review requires one Intent update")
    value = changes[0].get("value")
    detail = value.get("intent_change") if isinstance(value, dict) else None
    if not isinstance(detail, dict) or detail.get("kind") not in ("confirmation", "correction", "refinement"):
        raise TransitionDenied("only same-Work Intent confirmation, correction, or refinement may be reviewed; material_change needs a new Work")
    sources = detail.get("owner_sources")
    if not isinstance(sources, list) or not sources:
        raise TransitionDenied("Owner Intent evidence is missing")
    repo = (repo or Path(__file__).resolve().parents[1]).resolve()
    refs = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"ref", "sha256"}:
            raise TransitionDenied("Owner Intent evidence binding is invalid")
        ref = source["ref"]
        if (not isinstance(ref, str)
                or not ref.startswith("repo:docs/work-management/evidence/owner_")
                or not ref.endswith(".txt")):
            raise TransitionDenied("Owner Intent source is outside the evidence boundary")
        path = (repo / ref[len("repo:"):]).resolve()
        if path.parent != (repo / "docs/work-management/evidence").resolve():
            raise TransitionDenied("Owner Intent source path escapes the evidence directory")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TransitionDenied("Owner Intent source is unreadable") from exc
        if actual != source["sha256"]:
            raise TransitionDenied("Owner Intent source differs from reviewed bytes")
        refs.append(ref)
    return ReviewedOwnerIntent(_digest(request), tuple(refs))


def review_dependency_disposition(request: dict, repo: Path | None = None) -> ReviewedDependencyDisposition:
    """Bind bytes after local review of exact Owner scope or actual fulfillment.

    The operator must assess attribution and the results against the specific
    requirement. Matching files alone are not evidence of semantic fulfillment.
    """
    changes = request.get("changes") if isinstance(request, dict) else None
    if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict):
        raise TransitionDenied("one dependency disposition operation must be reviewed")
    change = changes[0]
    owner = change.get("op") == "authorize_dependency_disposition"
    if change.get("op") not in ("authorize_dependency_disposition", "remove_relation"):
        raise TransitionDenied("dependency review requires authorization or fulfillment")
    value = change.get("value")
    sources = value.get("owner_sources" if owner else "evidence") if isinstance(value, dict) else None
    if not isinstance(sources, list) or not sources:
        raise TransitionDenied("dependency authority or fulfillment evidence is missing")
    repo = (repo or Path(__file__).resolve().parents[1]).resolve()
    docs = repo / "docs/work-management"
    refs = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"ref", "sha256"}:
            raise TransitionDenied("dependency evidence binding is invalid")
        ref = source["ref"]
        if (not isinstance(ref, str) or not ref.startswith("repo:docs/work-management/")
                or not ref.endswith((".txt",) if owner else (".md", ".json"))
                or (owner and not ref.startswith("repo:docs/work-management/evidence/owner_"))):
            raise TransitionDenied("dependency evidence is outside the allowed document boundary")
        path = (repo / ref[len("repo:"):]).resolve()
        allowed = [docs / "evidence"] if owner else [docs, docs / "evidence"]
        if path.parent not in [directory.resolve() for directory in allowed]:
            raise TransitionDenied("dependency evidence path escapes the allowed directories")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TransitionDenied("dependency evidence is unreadable") from exc
        if actual != source["sha256"]:
            raise TransitionDenied("dependency evidence differs from reviewed bytes")
        refs.append(ref)
    return ReviewedDependencyDisposition(_digest(request), tuple(refs))



def review_owner_scope(request: dict) -> ReviewedOwnerScope:
    """Operator reviewed the current Owner instruction and actual actor acceptance.

    Limited repository inspection and isolated tests only; not authentication.
    """
    changes = request.get("changes") if isinstance(request, dict) else None
    if (not isinstance(changes, list) or len(changes) != 1
            or not isinstance(changes[0], dict) or changes[0].get("op") not in ("accept_owner_scope", "renew_owner_scope")):
        raise TransitionDenied("Owner scope review requires one acceptance record")
    value = changes[0].get("value")
    sources = value.get("owner_sources") if isinstance(value, dict) else None
    if not isinstance(sources, list) or not sources:
        raise TransitionDenied("Owner scope source is missing")
    repo = Path(__file__).resolve().parents[1]
    refs = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"ref", "sha256"}:
            raise TransitionDenied("Owner source binding is invalid")
        ref = source["ref"]
        if not isinstance(ref, str) or not ref.startswith("repo:docs/work-management/evidence/owner_") or not ref.endswith(".txt"):
            raise TransitionDenied("Owner scope source is outside the evidence boundary")
        path = (repo / ref[len("repo:"):]).resolve()
        if path.parent != (repo / "docs/work-management/evidence").resolve():
            raise TransitionDenied("Owner scope source escapes its boundary")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TransitionDenied("Owner scope source is unreadable") from exc
        if digest != source["sha256"]:
            raise TransitionDenied("Owner scope source differs from reviewed bytes")
        refs.append(ref)
    return ReviewedOwnerScope(_digest(request), tuple(refs))


def review_pin_move(request: dict) -> ReviewedPinMove:
    """Bind the local operator's semantic review to this request, without a grant."""
    changes = request.get("changes") if isinstance(request, dict) else None
    from pma.workmap_pin_move import is_held_reactivation
    if (not isinstance(changes, list) or len(changes) != 1
            or not isinstance(changes[0], dict)
            or not (changes[0].get("op") == "move_active_pin" or is_held_reactivation(request))):
        raise TransitionDenied("Pin move review requires one own-actor move")
    return ReviewedPinMove(_digest(request))


def _apply_request(store_path: Path, request: dict, *, reviewed_owner_handoff: bool = False,
                   reviewed_owner_intent: bool = False, reviewed_delegation: bool = False,
                   reviewed_dependency_disposition: bool = False, reviewed_owner_scope: bool = False, reviewed_pin_move: bool = False, reviewed_close: bool = False, reviewed_owner_add: bool = False, reviewed_owner_permission: bool = False, backend: str = 'text', context_assessments: dict | None = None, evidence_repo: Path | None = None) -> dict:
    if sum((reviewed_owner_handoff, reviewed_owner_intent, reviewed_delegation, reviewed_dependency_disposition, reviewed_owner_scope, reviewed_pin_move, reviewed_close, reviewed_owner_add, reviewed_owner_permission)) > 1:
        raise TransitionDenied("review exactly one semantic operation")
    if context_assessments is not None and any((reviewed_owner_handoff, reviewed_delegation,
            reviewed_dependency_disposition, reviewed_owner_scope,
            reviewed_close, reviewed_owner_add)):
        raise TransitionDenied("combined context review is limited to Owner Permission, Intent revision or own Pin move")
    # The selected Map's documents also resolve an explicitly reviewed Intent.
    # This selects evidence bytes, never Owner identity or a write destination.
    # Core still checks every operation's permission.
    selection_repo = Path(evidence_repo) if evidence_repo is not None else Path(__file__).resolve().parents[1]
    if not selection_repo.is_absolute():
        raise ValueError('selection Evidence root must be an absolute path')
    store = open_store(store_path, backend)
    changes = request.get('changes') if isinstance(request, dict) else None
    if (isinstance(changes, list) and len(changes) == 1
            and isinstance(changes[0], dict)
            and changes[0].get('op') == 'adopt_action_rules'):
        # The explicit Core migration validates the former source before
        # publishing a current-format snapshot. Ordinary requests below keep
        # the strict current-format validation and all existing review inputs.
        if context_assessments is not None:
            raise ValueError('format adoption does not accept unknown-context overrides')
        return CanonicalWorkMapCore(store).apply(request)
    current = validate_snapshot(store.read().snapshot)
    if current["metadata"].get("store_role") != "canonical_current":
        raise ValueError("target is not the declared canonical Store")
    try:
        validate_publication(current, request, selection_repo)
    except SelectionUnavailable as exc:
        raise TransitionError(str(exc)) from exc
    review = review_owner_handoff(request) if reviewed_owner_handoff else None
    intent_review = review_owner_intent(request, selection_repo) if reviewed_owner_intent else None
    delegation_review = review_delegation(request, Path(__file__).resolve().parents[1]) if reviewed_delegation else None
    dependency_review = review_dependency_disposition(request) if reviewed_dependency_disposition else None
    scope_review = review_owner_scope(request) if reviewed_owner_scope else None
    move_review = review_pin_move(request) if reviewed_pin_move else None
    close_review = review_close(request) if reviewed_close else None
    # Replays return the committed result via Core's digest check, even if the
    # original base revision or evidence is no longer current.
    add_review = review_owner_add(request, Path(__file__).resolve().parents[1], current) if reviewed_owner_add and request.get('transition_id') not in current['receipts'] else None
    permission_review = review_owner_permission(request, Path(__file__).resolve().parents[1]) if reviewed_owner_permission and request.get("transition_id") not in current["receipts"] else None
    context_review = review_decision_context(current, request, context_assessments) if context_assessments is not None and request.get('transition_id') not in current['receipts'] else None
    return CanonicalWorkMapCore(store, reviewed_decision_context=context_review, reviewed_owner_permission=permission_review, reviewed_owner_add=add_review, reviewed_close=close_review, reviewed_pin_move=move_review, reviewed_owner_scope=scope_review, reviewed_owner_handoff=review,
                               reviewed_owner_intent=intent_review,
                               reviewed_delegation=delegation_review,
                               reviewed_dependency_disposition=dependency_review).apply(request)


def apply_file(store_path: Path, transition_path: Path, *, reviewed_owner_handoff: bool = False,
               reviewed_owner_intent: bool = False, reviewed_delegation: bool = False,
               reviewed_dependency_disposition: bool = False, reviewed_owner_scope: bool = False, reviewed_pin_move: bool = False, reviewed_close: bool = False, reviewed_owner_add: bool = False, reviewed_owner_permission: bool = False, backend: str = 'text', context_assessments: dict | None = None, evidence_repo: Path | None = None) -> dict:
    request = json.loads(transition_path.read_text(encoding="utf-8"))
    return _apply_request(store_path, request, reviewed_owner_handoff=reviewed_owner_handoff,
                          reviewed_owner_intent=reviewed_owner_intent, reviewed_delegation=reviewed_delegation,
                          reviewed_dependency_disposition=reviewed_dependency_disposition, reviewed_owner_scope=reviewed_owner_scope, reviewed_pin_move=reviewed_pin_move, reviewed_close=reviewed_close, reviewed_owner_add=reviewed_owner_add, reviewed_owner_permission=reviewed_owner_permission, backend=backend, context_assessments=context_assessments, evidence_repo=evidence_repo)


def apply_result(store_path: Path, transition_path: Path, *, reviewed_owner_handoff: bool = False,
                 reviewed_owner_intent: bool = False, reviewed_delegation: bool = False,
                 reviewed_dependency_disposition: bool = False, reviewed_owner_scope: bool = False, reviewed_pin_move: bool = False, reviewed_close: bool = False, reviewed_owner_add: bool = False, reviewed_owner_permission: bool = False, backend: str = 'text', context_assessments: dict | None = None, evidence_repo: Path | None = None) -> dict:
    """Return a success receipt or an ephemeral structured failure.

    No failure receipt or Thread is written. I/O ambiguity is unknown, never a
    claim that the request could not have committed. Success shape is unchanged.
    """
    request = None
    try:
        request = json.loads(transition_path.read_text(encoding="utf-8"))
        return _apply_request(store_path, request, reviewed_owner_handoff=reviewed_owner_handoff,
                              reviewed_owner_intent=reviewed_owner_intent, reviewed_delegation=reviewed_delegation,
                              reviewed_dependency_disposition=reviewed_dependency_disposition, reviewed_owner_scope=reviewed_owner_scope, reviewed_pin_move=reviewed_pin_move, reviewed_close=reviewed_close, reviewed_owner_add=reviewed_owner_add, reviewed_owner_permission=reviewed_owner_permission, backend=backend, context_assessments=context_assessments, evidence_repo=evidence_repo)
    except ContextUnknown as exc:
        result, category, reason = "rejected", "context_unknown", str(exc)
    except TransitionDenied as exc:
        result, category, reason = "rejected", "denied", str(exc)
    except TransitionConflict as exc:
        result, category, reason = "rejected", "conflict", str(exc)
    except UnsupportedTransition as exc:
        result, category, reason = "rejected", "unsupported", str(exc)
    except TransitionError as exc:
        result, category, reason = "rejected", "invalid", str(exc)
    except SnapshotInvalid:
        result, category, reason = "unknown", "snapshot_unreadable", "current snapshot is unreadable"
    except (StoreError, OSError):
        result, category, reason = "unknown", "io_unknown", "Store or input I/O failed; check the transition ID before retrying"
    except (ValueError, UnicodeError):
        result, category, reason = "rejected", "invalid_input", "input or Store role is invalid"
    identity = request.get("transition_id") if isinstance(request, dict) else None
    if not isinstance(identity, str) or not identity.strip():
        identity = None
    return {"result": result, "category": category, "reason": reason, "transition_id": identity}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--transition", type=Path, required=True)
    parser.add_argument("--backend", choices=("text", "sqlite"), default="text")
    parser.add_argument('--evidence-repo', type=Path,
                        help='Absolute root resolving Thread selection and explicitly reviewed Intent evidence; grants no authority')
    parser.add_argument("--context-assessments", type=Path,
                        help="Exact operation-bound scope/unknown assessments; required for declared note working context, optional with Owner Permission or Intent review")
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument("--reviewed-owner-handoff", action="store_true",
                        help="Local operator has reviewed Owner attribution and exact handoff scope; not Owner authentication")
    review_group.add_argument("--reviewed-owner-intent", action="store_true",
                        help="Local operator reviewed Owner attribution, purpose continuity, exact Intent and Action Rules; not semantic proof or Owner authentication")
    review_group.add_argument("--reviewed-delegation", action="store_true",
                        help="Operator reviewed exact Owner scope and actual runtime acceptance/completion; dispatch alone is insufficient; not authentication")
    review_group.add_argument("--reviewed-dependency-disposition", action="store_true",
                        help="Operator reviewed exact Owner removal scope or fulfillment against this dependency; hashes are not authentication or semantic proof")
    review_group.add_argument("--reviewed-owner-scope", action="store_true", help="Operator reviewed exact fresh Owner scope and actual actor acceptance; unknown collections remain unknown")
    review_group.add_argument("--reviewed-pin-move", action="store_true", help="Operator checked own acceptance, current grant constraints, Action Rules, dependencies and shared WHERE; grants no permission")
    review_group.add_argument("--reviewed-close", action="store_true", help="Operator reviewed this bound close basis: Intent/outcome, residuals, grants and evidence; no new Owner approval")
    review_group.add_argument("--reviewed-owner-add", action="store_true", help="Operator reviewed exact Owner instruction for one Work registration only; adds no Permission or Pin")
    review_group.add_argument("--reviewed-owner-permission", action="store_true", help="Operator reviewed the actual Owner instruction for this exact existing Permission; not authentication")
    args = parser.parse_args()
    try:
        assessments = (json.loads(args.context_assessments.read_text(encoding='utf-8'))
                       if args.context_assessments is not None else None)
    except (OSError, ValueError, UnicodeError):
        print(json.dumps({'result': 'rejected', 'category': 'invalid_input',
                          'reason': 'context assessments are unreadable'}, ensure_ascii=False))
        sys.exit(2)
    result = apply_result(args.store, args.transition, reviewed_owner_handoff=args.reviewed_owner_handoff,
                          reviewed_owner_intent=args.reviewed_owner_intent, reviewed_delegation=args.reviewed_delegation,
                          reviewed_dependency_disposition=args.reviewed_dependency_disposition, reviewed_owner_scope=args.reviewed_owner_scope, reviewed_pin_move=args.reviewed_pin_move, reviewed_close=args.reviewed_close, reviewed_owner_add=args.reviewed_owner_add, reviewed_owner_permission=args.reviewed_owner_permission, backend=args.backend, context_assessments=assessments, evidence_repo=args.evidence_repo)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("result") != "applied":
        sys.exit(2)


if __name__ == "__main__":
    main()
