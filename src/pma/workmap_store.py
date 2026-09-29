"""Local structured-text Work Map root for the constrained initial Store.

This is the physical store only. It does not authorize or validate Work changes;
the Core must validate a transition before calling ``commit_validated``. It is
limited to cooperating single-host writers: a direct rewrite of head.json to
a valid older head is undetectable without a separately trusted witness.
"""

from __future__ import annotations

import copy
import hashlib
import json
import msvcrt
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class StoreError(Exception):
    """The committed store cannot be used as requested."""


class MissingStore(StoreError):
    """There is no committed root at this location."""


class StoreConflict(StoreError):
    """The caller's expected committed root is no longer current."""


class CorruptStore(StoreError):
    """A committed root or its referenced object is missing or inconsistent."""


@dataclass(frozen=True)
class CommittedVersion:
    token: str
    snapshot: dict


def _encode(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def snapshot_object_id(snapshot: dict) -> str:
    """Content reference to an immutable object, not proof it was committed."""
    return _sha256(_encode(snapshot))


def _decode(raw: bytes, what: str) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptStore(f"invalid {what}") from exc
    if not isinstance(value, dict):
        raise CorruptStore(f"invalid {what}")
    return value


class LocalTextStore:
    """Local, single-host store; writers must cooperate on one lock file.

    The head is the only published root. Hash-named snapshot objects are
    immutable and retained. The caller chooses an absolute path; this class
    neither changes Codex sandbox permissions nor protects against an actor
    that can deliberately rewrite both the head and its objects.
    """

    def __init__(self, directory: Path | str):
        requested = Path(directory)
        if not requested.is_absolute():
            raise ValueError("store directory must be absolute")
        self.directory = requested.resolve()
        self._head = self.directory / "head.json"
        self._objects = self.directory / "objects"
        self._lock_file = self.directory / "writer.lock"

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock_file.open("xb") as new_lock:
                new_lock.write(b"0")
                new_lock.flush()
                os.fsync(new_lock.fileno())
        except FileExistsError:
            pass
        with self._lock_file.open("r+b") as lock:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def _read_committed(self) -> CommittedVersion:
        try:
            # A Windows head replacement may briefly deny a concurrent open.
            # Retry that open only, then validate one complete published root.
            # Permanent denial still propagates; never use cached or temp data.
            for attempt in range(10):
                try:
                    head_raw = self._head.read_bytes()
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.025)
        except FileNotFoundError as exc:
            raise MissingStore(str(self._head)) from exc
        head = _decode(head_raw, "head")
        if head.get("format_version") != "p1-trial-1":
            raise CorruptStore("unsupported head format")
        previous = head.get("previous_head_sha256")
        if previous is not None and (
            not isinstance(previous, str)
            or len(previous) != 64
            or any(ch not in "0123456789abcdef" for ch in previous)
        ):
            raise CorruptStore("invalid previous head token")
        object_hash = head.get("object_sha256")
        if not isinstance(object_hash, str) or len(object_hash) != 64 or any(ch not in "0123456789abcdef" for ch in object_hash):
            raise CorruptStore("invalid object hash in head")
        try:
            object_raw = (self._objects / f"{object_hash}.json").read_bytes()
        except FileNotFoundError as exc:
            raise CorruptStore("committed object is missing") from exc
        if _sha256(object_raw) != object_hash:
            raise CorruptStore("committed object hash differs")
        snapshot = _decode(object_raw, "snapshot")
        if snapshot.get("project_id") != head.get("project_id") or snapshot.get("revision") != head.get("revision"):
            raise CorruptStore("head and snapshot identity differ")
        if not isinstance(head.get("revision"), str) or not head["revision"]:
            raise CorruptStore("invalid committed revision")
        return CommittedVersion(token=_sha256(head_raw), snapshot=snapshot)

    def read(self) -> CommittedVersion:
        """Read exactly one published head and verify its immutable object."""
        result = self._read_committed()
        return CommittedVersion(result.token, copy.deepcopy(result.snapshot))

    def read_retained_snapshot(self, object_id: str) -> dict:
        """Read one hash-addressed object without treating it as current.

        Prepared, unpublished objects can exist. Core must separately establish
        that this snapshot belongs to retained committed history before reuse.
        """
        if not isinstance(object_id, str) or len(object_id) != 64 or any(c not in "0123456789abcdef" for c in object_id):
            raise ValueError("invalid retained snapshot reference")
        try:
            raw = (self._objects / f"{object_id}.json").read_bytes()
        except FileNotFoundError as exc:
            raise CorruptStore("referenced prior snapshot is unavailable") from exc
        if _sha256(raw) != object_id:
            raise CorruptStore("retained snapshot hash differs")
        return _decode(raw, "retained snapshot")

    def initialize(self, initial_snapshot: dict) -> CommittedVersion:
        """Create a fresh store; never overwrite a committed root."""
        if not isinstance(initial_snapshot, dict) or not initial_snapshot.get("project_id") or not initial_snapshot.get("revision"):
            raise ValueError("initial snapshot needs project_id and revision")
        with self._writer_lock():
            if self._head.exists():
                raise StoreConflict("store is already initialized")
            self._publish(initial_snapshot, previous_token=None)
            return self.read()

    def commit_validated(self, expected_token: str, candidate: dict, *, fault_at: str | None = None) -> CommittedVersion:
        """Publish a Core-validated full candidate against one exact head token.

        ``fault_at`` is a test-driver hook: ``after_object`` or ``after_head``.
        A caller must never treat this low-level method as Work authorization.
        """
        if fault_at not in (None, "after_object", "after_head"):
            raise ValueError("unknown test fault")
        with self._writer_lock():
            current = self._read_committed()
            if current.token != expected_token:
                raise StoreConflict("committed root changed")
            if not isinstance(candidate, dict) or candidate.get("project_id") != current.snapshot.get("project_id"):
                raise ValueError("candidate project differs")
            if not isinstance(candidate.get("revision"), str) or not candidate["revision"] or candidate["revision"] == current.snapshot["revision"]:
                raise ValueError("candidate needs a new revision")
            self._publish(candidate, previous_token=current.token, fault_at=fault_at)
            return self.read()

    def _publish(self, snapshot: dict, previous_token: str | None, fault_at: str | None = None) -> None:
        self._objects.mkdir(parents=True, exist_ok=True)
        object_raw = _encode(snapshot)
        object_hash = _sha256(object_raw)
        object_path = self._objects / f"{object_hash}.json"
        if object_path.exists():
            if object_path.read_bytes() != object_raw:
                raise CorruptStore("existing hash-named object differs")
        else:
            object_temp = self._objects / f".{uuid.uuid4().hex}.new"
            try:
                with object_temp.open("xb") as output:
                    output.write(object_raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.rename(object_temp, object_path)
            finally:
                object_temp.unlink(missing_ok=True)
        if fault_at == "after_object":
            raise RuntimeError("injected after_object fault")
        head = {
            "format_version": "p1-trial-1",
            "project_id": snapshot["project_id"],
            "revision": snapshot["revision"],
            "object_sha256": object_hash,
            "previous_head_sha256": previous_token,
        }
        head_temp = self.directory / f".head-{uuid.uuid4().hex}.new"
        try:
            with head_temp.open("xb") as output:
                output.write(_encode(head))
                output.flush()
                os.fsync(output.fileno())
            # A concurrent Windows reader may briefly prevent replacing the
            # head even after its read handle closes. Retry only that transient
            # sharing failure; all attempts still target the same prepared root.
            for attempt in range(10):
                try:
                    os.replace(head_temp, self._head)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.025)
        finally:
            head_temp.unlink(missing_ok=True)
        if fault_at == "after_head":
            raise RuntimeError("injected after_head fault")

    def export(self) -> dict:
        """Return a copy of one verified committed snapshot."""
        return self.read().snapshot
