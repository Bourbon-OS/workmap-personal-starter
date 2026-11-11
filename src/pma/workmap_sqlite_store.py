"""SQLite rollback Store for explicit, single-host Work Map connections.

Preserves the 0.4.2 snapshot/Core contract. Selecting this adapter is not a
canonical migration or an authority grant. No external operation is executed.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import chain, islice
from pathlib import Path
import sqlite3
import struct
import zlib

from pma.workmap_store import (
    CommittedVersion, CorruptStore, MissingStore, MissingRetainedSnapshot, StoreConflict, StoreError,
    _encode, _decode, _sha256,
)


RAW_APPLICATION_ID = 1095977033
A_APPLICATION_ID = 0x41534841
A_PHYSICAL_VERSION = 1
BOUNDED_PHYSICAL_VERSION = 2
A_MAGIC = b'ASHIBA-A\x01'
A_MAX_RAW_BYTES = 64 * 1024 * 1024
A_MAX_PAYLOAD_BYTES = 65 * 1024 * 1024


def _pack_a(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= A_MAX_RAW_BYTES:
        raise ValueError('snapshot exceeds the A uncompressed body limit')
    payload = A_MAGIC + struct.pack('>Q', len(raw)) + zlib.compress(raw, 6)
    if len(payload) > A_MAX_PAYLOAD_BYTES:
        raise ValueError('snapshot exceeds the A stored body limit')
    return payload


def _unpack_a(payload):
    if (not isinstance(payload, bytes) or len(payload) > A_MAX_PAYLOAD_BYTES
            or len(payload) <= len(A_MAGIC) + 8 or not payload.startswith(A_MAGIC)):
        raise CorruptStore('invalid A body header or stored length')
    length, = struct.unpack('>Q', payload[len(A_MAGIC):len(A_MAGIC) + 8])
    if not 0 < length <= A_MAX_RAW_BYTES:
        raise CorruptStore('A body declares an unsupported expanded length')
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(payload[len(A_MAGIC) + 8:], length + 1)
        if (len(raw) != length or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail):
            raise CorruptStore('A body is truncated, oversized or has trailing bytes')
    except zlib.error as exc:
        raise CorruptStore('A body cannot be expanded') from exc
    return raw


@dataclass(frozen=True)
class _PreparedCommit:
    """One immutable, handle-bound buffer; never authority or persisted state."""
    owner: object
    location: Path
    expected_token: str
    token: str
    raw: bytes
    payload: bytes
    project_id: str
    revision: str

    def commit(self, *, fault_at=None):
        return self.owner._commit_prepared(self, fault_at=fault_at)


class SQLiteStore:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError('absolute path required')

    @contextmanager
    def connection(self, create=False, *, read_only=False):
        # rw prevents an unavailable/mistyped Store silently becoming empty.
        if create and read_only:
            raise ValueError('cannot create through a read-only connection')
        mode = 'ro' if read_only else ('rwc' if create else 'rw')
        if not create and not self.path.exists():
            raise MissingStore(str(self.path))
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + '?mode=' + mode,
                                 uri=True, timeout=2, isolation_level=None)
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise CorruptStore('SQLite Store requires rollback DELETE journal')
            if read_only:
                db.execute('PRAGMA query_only=ON')
            else:
                db.execute('PRAGMA synchronous=FULL')
            if not create:
                self._profile(db)
            yield db
        except sqlite3.Error as exc:
            raise StoreError(f'SQLite operation failed: {type(exc).__name__}: {exc}') from exc
        finally:
            if db is not None:
                db.close()  # rolls back any uncommitted transaction

    @staticmethod
    def _profile(db):
        application_id = db.execute('PRAGMA application_id').fetchone()[0]
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if application_id == RAW_APPLICATION_ID and version in (0, BOUNDED_PHYSICAL_VERSION):
            return 'none'
        if application_id == A_APPLICATION_ID and version in (A_PHYSICAL_VERSION, BOUNDED_PHYSICAL_VERSION):
            return 'a'
        raise CorruptStore('unsupported Ashiba SQLite physical format')

    @staticmethod
    def _retention(db):
        SQLiteStore._profile(db)
        return 'current_and_previous' if db.execute('PRAGMA user_version').fetchone()[0] == BOUNDED_PHYSICAL_VERSION else 'all'

    def _trim_comparison_cache(self, db, token, previous):
        # Runs after the current-root CAS and in the SAME transaction. A slow
        # participant keeps its read bytes; it must reread if its cache expires.
        if self._retention(db) == 'current_and_previous':
            db.execute('DELETE FROM snapshots WHERE token NOT IN (?,?)', (token, previous))

    @staticmethod
    def version(row):
        if row is None:
            raise CorruptStore('missing current snapshot')
        token, raw = row
        if (not isinstance(token, str) or len(token) != 64
                or any(c not in '0123456789abcdef' for c in token)
                or not isinstance(raw, bytes)):
            raise CorruptStore('invalid snapshot row types')
        if _sha256(raw) != token:
            raise CorruptStore('snapshot hash differs')
        return CommittedVersion(token, _decode(raw, 'snapshot'))

    def _read_version(self, db, object_id=None, *, retained=False):
        compressed = self._profile(db) == 'a'
        source = ('snapshots s WHERE s.token=?' if retained else
                  'current c JOIN snapshots s ON s.token=c.token WHERE c.id=1')
        params = (object_id,) if retained else ()
        if compressed:
            # Both queries run in one read/write transaction. Reject oversized
            # BLOBs before Python's sqlite3 driver allocates the body bytes.
            shape = db.execute('SELECT s.token,typeof(s.body),length(s.body) FROM ' + source, params).fetchone()
            if shape is None:
                if retained and self._retention(db) == 'current_and_previous':
                    raise MissingRetainedSnapshot(object_id)
                return self.version(None)
            if shape[1] != 'blob' or not 0 < shape[2] <= A_MAX_PAYLOAD_BYTES:
                raise CorruptStore('invalid A row type or stored body limit')
        row = db.execute('SELECT s.token,s.body FROM ' + source, params).fetchone()
        if row is None and retained and self._retention(db) == 'current_and_previous':
            raise MissingRetainedSnapshot(object_id)
        if compressed and row is not None:
            row = row[0], _unpack_a(row[1])
        return self.version(row)

    def read(self, *, read_only=False):
        with self.connection(read_only=read_only) as db:
            db.execute('BEGIN')
            return self._read_version(db)

    def read_retained_snapshot(self, object_id):
        with self.connection() as db:
            db.execute('BEGIN')
            return self._read_version(db, object_id, retained=True).snapshot

    def retained_snapshot_tokens(self):
        """Inventory existing retained versions for explicit history audits."""
        with self.connection() as db:
            return [row[0] for row in db.execute('SELECT token FROM snapshots ORDER BY token')]

    def _retained_bytes(self, db, object_id, *, max_raw_bytes=None):
        """Verified original bytes, without reserializing an old JSON format."""
        profile = self._profile(db)
        shape = db.execute('SELECT typeof(body),length(body) FROM snapshots WHERE token=?', (object_id,)).fetchone()
        if shape is None:
            if self._retention(db) == 'current_and_previous':
                raise MissingRetainedSnapshot(object_id)
            raise CorruptStore('missing retained snapshot')
        limit = A_MAX_PAYLOAD_BYTES if profile == 'a' else max_raw_bytes
        if shape[0] != 'blob' or (limit is not None and not 0 < shape[1] <= limit):
            raise CorruptStore('retained body type or size is unsupported')
        body = db.execute('SELECT body FROM snapshots WHERE token=?', (object_id,)).fetchone()[0]
        raw = _unpack_a(body) if profile == 'a' else body
        self.version((object_id, raw))  # original hash + JSON object, not current schema
        if max_raw_bytes is not None and len(raw) > max_raw_bytes:
            raise CorruptStore('retained raw bytes exceed the conversion limit')
        return raw

    def read_retained_bytes(self, object_id):
        with self.connection(read_only=True) as db:
            db.execute('BEGIN')
            return self._retained_bytes(db, object_id)

    def iter_retained_bytes(self, expected_token, *, max_raw_bytes=None):
        """One consistent source view; stream bodies instead of loading all."""
        with self.connection(read_only=True) as db:
            db.execute('BEGIN')
            if self._read_version(db).token != expected_token:
                raise StoreConflict('source changed before retained snapshot read')
            for (token,) in db.execute('SELECT token FROM snapshots ORDER BY token'):
                yield token, self._retained_bytes(db, token, max_raw_bytes=max_raw_bytes)

    def _retain_versions(self, expected_token, records, *, expected_role):
        """Physical maintenance only: insert immutable history, never a head.

        The caller must authorize its reviewed conversion/cutover. Ordinary
        Work transitions do not expose this method. A batch holds one body at
        a time; current-root checks remain inside each finite transaction.
        """
        if expected_role not in ('recovery_candidate', 'canonical_current'):
            raise ValueError('unsupported retained import role')
        records = iter(records)
        count = 0
        while True:
            first = next(records, None)
            if first is None:
                return count
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self._read_version(db)
                if current.token != expected_token:
                    raise StoreConflict('retained import current root changed')
                if current.snapshot.get('metadata', {}).get('store_role') != expected_role:
                    raise StoreConflict('retained import role changed')
                compressed = self._profile(db) == 'a'
                batch = chain((first,), islice(records, 31))
                first = None
                for token, raw in batch:
                    if compressed and (not isinstance(raw, bytes) or not 0 < len(raw) <= A_MAX_RAW_BYTES):
                        raise ValueError('retained snapshot exceeds the A body limit')
                    project_id = self.version((token, raw)).snapshot.get('project_id')
                    if project_id != current.snapshot['project_id']:
                        raise ValueError('retained version belongs to another project')
                    present = db.execute('SELECT 1 FROM snapshots WHERE token=?', (token,)).fetchone()
                    if present:
                        if self._retained_bytes(db, token) != raw:
                            raise CorruptStore('existing retained version differs')
                    else:
                        if (self._retention(db) == 'current_and_previous'
                                and db.execute('SELECT count(*) FROM snapshots').fetchone()[0] >= 2):
                            raise StoreConflict('finite comparison cache cannot import an unbounded version catalog')
                        db.execute('INSERT INTO snapshots VALUES (?,?)', (token, _pack_a(raw) if compressed else raw))
                    count += 1
                db.execute('COMMIT')

    def initialize(self, snapshot, *, compression='none', retention='all'):
        # Explicit opt-in only; never convert an existing DB or change callers'
        # current noncompressed initialization/recovery defaults.
        if compression not in ('none', 'a'):
            raise ValueError('unsupported SQLite body compression')
        if retention not in ('all', 'current_and_previous'):
            raise ValueError('unsupported SQLite snapshot retention')
        if not isinstance(snapshot, dict) or not snapshot.get('project_id') or not snapshot.get('revision'):
            raise ValueError('snapshot identity required')
        raw = _encode(snapshot)  # reject unserializable input before creating a file
        token = _sha256(raw)
        body = _pack_a(raw) if compression == 'a' else raw
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open('xb'):
                pass
        except FileExistsError as exc:
            raise StoreConflict('SQLite target already exists; read/inspect it first. '
                                'If initialization was interrupted, preserve it and use a fresh target '
                                'with the verified input; never overwrite an uncertain Store.') from exc
        with self.connection(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('PRAGMA application_id=' + str(A_APPLICATION_ID if compression == 'a' else RAW_APPLICATION_ID))
            version = BOUNDED_PHYSICAL_VERSION if retention == 'current_and_previous' else (A_PHYSICAL_VERSION if compression == 'a' else 0)
            db.execute('PRAGMA user_version=' + str(version))
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (token TEXT PRIMARY KEY NOT NULL, body BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS current (id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL)')
            if db.execute('SELECT 1 FROM current').fetchone():
                raise StoreConflict('already initialized')
            db.execute('INSERT INTO snapshots VALUES (?,?)', (token, body))
            db.execute('INSERT INTO current VALUES (1,?)', (token,))
            db.execute('COMMIT')
        return CommittedVersion(token, _decode(raw, 'snapshot'))

    def prepare_commit(self, expected_token, candidate):
        """Prepare A before the Core's final source check, without a write lock.

        Raw Stores keep the existing path. No caller-supplied replacement token,
        mutable candidate, or compression format is accepted at commit time.
        """
        with self.connection(read_only=True) as db:
            if self._profile(db) != 'a':
                return None
        raw = _encode(candidate)
        payload = _pack_a(raw)
        frozen = _decode(raw, 'prepared snapshot')
        return _PreparedCommit(self, self.path.resolve(), expected_token, _sha256(raw), raw, payload,
                               frozen.get('project_id'), frozen.get('revision'))

    def _commit_prepared(self, prepared, *, fault_at=None):
        if (not isinstance(prepared, _PreparedCommit) or prepared.owner is not self
                or prepared.location != self.path.resolve()):
            raise ValueError('prepared snapshot belongs to a different Store handle')
        if fault_at not in (None, 'after_object', 'after_cache_trim', 'after_head'):
            raise ValueError('unknown fault')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._profile(db) != 'a':
                raise CorruptStore('prepared A Store format changed')
            current = self._read_version(db)
            if current.token != prepared.expected_token:
                raise StoreConflict('committed root changed')
            if (prepared.project_id != current.snapshot['project_id']
                    or not prepared.revision or prepared.revision == current.snapshot['revision']):
                raise ValueError('new revision in same project required')
            db.execute('INSERT INTO snapshots VALUES (?,?)', (prepared.token, prepared.payload))
            if fault_at == 'after_object':
                raise RuntimeError('injected before commit')
            changed = db.execute('UPDATE current SET token=? WHERE id=1 AND token=?',
                                 (prepared.token, prepared.expected_token))
            if changed.rowcount != 1:
                raise StoreConflict('committed root changed')
            self._trim_comparison_cache(db, prepared.token, prepared.expected_token)
            if fault_at == 'after_cache_trim':
                raise RuntimeError('injected after cache trim before commit')
            db.execute('COMMIT')
            if fault_at == 'after_head':
                raise RuntimeError('injected after commit')
        return CommittedVersion(prepared.token, _decode(prepared.raw, 'snapshot'))

    def commit_validated(self, expected_token, candidate, *, fault_at=None):
        if fault_at not in (None, 'after_object', 'after_cache_trim', 'after_head'):
            raise ValueError('unknown fault')
        prepared = self.prepare_commit(expected_token, candidate)
        if prepared is not None:
            return prepared.commit(fault_at=fault_at)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._profile(db) != 'none':
                raise CorruptStore('noncompressed Store format changed')
            current = self._read_version(db)
            if current.token != expected_token:
                raise StoreConflict('committed root changed')
            if (candidate.get('project_id') != current.snapshot['project_id']
                    or not candidate.get('revision') or candidate['revision'] == current.snapshot['revision']):
                raise ValueError('new revision in same project required')
            raw = _encode(candidate)
            token = _sha256(raw)
            db.execute('INSERT INTO snapshots VALUES (?,?)', (token, raw))
            if fault_at == 'after_object':
                raise RuntimeError('injected before commit')
            changed = db.execute('UPDATE current SET token=? WHERE id=1 AND token=?', (token, expected_token))
            if changed.rowcount != 1:
                raise StoreConflict('committed root changed')
            self._trim_comparison_cache(db, token, expected_token)
            if fault_at == 'after_cache_trim':
                raise RuntimeError('injected after cache trim before commit')
            db.execute('COMMIT')
            if fault_at == 'after_head':
                raise RuntimeError('injected after commit')
        return CommittedVersion(token, _decode(raw, 'snapshot'))

    def export(self):
        return self.read().snapshot

