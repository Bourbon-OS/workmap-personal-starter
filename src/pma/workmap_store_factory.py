"""Explicit provider choice; never detect, copy or switch a canonical Store."""
from pma.workmap_store import LocalTextStore


def open_store(path, backend='text'):
    if backend == 'text':
        return LocalTextStore(path)
    if backend == 'sqlite':
        from pma.workmap_sqlite_store import SQLiteStore
        return SQLiteStore(path)
    raise ValueError('unsupported Store backend')
