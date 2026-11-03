"""SQLite rollback Store for explicit, single-host Work Map connections.

Preserves the 0.4.2 snapshot/Core contract. Selecting this adapter is not a
canonical migration or an authority grant. No external operation is executed.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from pma.workmap_store import (
    CommittedVersion, CorruptStore, MissingStore, StoreConflict, StoreError,
    _encode, _decode, _sha256,
)


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
            if not create and db.execute('PRAGMA application_id').fetchone()[0] != 1095977033:
                raise CorruptStore('not an Ashiba SQLite Store')
            yield db
        except sqlite3.Error as exc:
            raise StoreError(f'SQLite operation failed: {type(exc).__name__}: {exc}') from exc
        finally:
            if db is not None:
                db.close()  # rolls back any uncommitted transaction

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

    def read(self, *, read_only=False):
        with self.connection(read_only=read_only) as db:
            return self.version(db.execute(
                'SELECT s.token,s.body FROM current c JOIN snapshots s ON s.token=c.token WHERE c.id=1'
            ).fetchone())

    def read_retained_snapshot(self, object_id):
        with self.connection() as db:
            return self.version(db.execute(
                'SELECT token,body FROM snapshots WHERE token=?', (object_id,)
            ).fetchone()).snapshot

    def retained_snapshot_tokens(self):
        """Inventory existing retained versions for explicit history audits."""
        with self.connection() as db:
            return [row[0] for row in db.execute('SELECT token FROM snapshots ORDER BY token')]

    def initialize(self, snapshot):
        if not isinstance(snapshot, dict) or not snapshot.get('project_id') or not snapshot.get('revision'):
            raise ValueError('snapshot identity required')
        raw = _encode(snapshot)  # reject unserializable input before creating a file
        token = _sha256(raw)
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
            db.execute('PRAGMA application_id=1095977033')
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (token TEXT PRIMARY KEY NOT NULL, body BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS current (id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL)')
            if db.execute('SELECT 1 FROM current').fetchone():
                raise StoreConflict('already initialized')
            db.execute('INSERT INTO snapshots VALUES (?,?)', (token, raw))
            db.execute('INSERT INTO current VALUES (1,?)', (token,))
            db.execute('COMMIT')
        return CommittedVersion(token, _decode(raw, 'snapshot'))

    def commit_validated(self, expected_token, candidate, *, fault_at=None):
        if fault_at not in (None, 'after_object', 'after_head'):
            raise ValueError('unknown fault')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.version(db.execute(
                'SELECT s.token,s.body FROM current c JOIN snapshots s ON s.token=c.token WHERE c.id=1'
            ).fetchone())
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
            db.execute('COMMIT')
            if fault_at == 'after_head':
                raise RuntimeError('injected after commit')
        return CommittedVersion(token, _decode(raw, 'snapshot'))

    def export(self):
        return self.read().snapshot

