"""Small read hints and display comparisons; never a cache of canonical state."""
from pma.workmap_sqlite_store import SQLiteStore
from pma.workmap_store import StoreError
from pma.workmap_thread_view import BINDING, selection_refs


def selection_paths(snapshot, repo):
    refs = next((refs for entry in reversed(snapshot['thread_entries'])
                 if (refs := selection_refs(entry))), [])
    folder = (repo / 'docs/work-management/evidence').resolve()
    paths = []
    for ref in refs:
        if snapshot.get('format_version') == '0.4.3':
            from pma.workmap_transfer import embedded_evidence
            if embedded_evidence(snapshot, ref) is not None:
                continue
        match = BINDING.fullmatch(ref)
        if match is None:
            continue  # Normal meaning validation reports this invalid binding.
        path = (folder / match[1]).resolve()
        if path.parent != folder:
            continue
        paths.append(path)
    return tuple(paths)


def file_marks(paths):
    marks = []
    for path in paths:
        try:
            stat = path.stat()
            marks.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except OSError as exc:
            marks.append(('unavailable', type(exc).__name__))
    return tuple(marks)


def read_marker(source, paths=()):
    """Read one existing head value, without the snapshot BLOB or JSON parsing."""
    with SQLiteStore(source.path).connection(read_only=True) as db:
        row = db.execute('SELECT token FROM current WHERE id=1').fetchone()
    token = row[0] if row else None
    if (not isinstance(token, str) or len(token) != 64
            or any(c not in '0123456789abcdef' for c in token)):
        raise StoreError('現在の保存版を確認できません。')
    return token, file_marks(paths), file_marks((source.path,))


def map_inputs(snapshot, *, people=False):
    members = snapshot['map']['members']
    labels = {k: (snapshot['display_ids'][k], snapshot['works'][k]['name'],
                  None if people else snapshot['works'][k]['intent']) for k in members}
    return (members, labels, snapshot['map']['pins'],
            snapshot['metadata'].get('completeness'),
            None if people else snapshot['map']['relations'])


def thread_inputs(data, labels):
    view = data['thread']
    related = {key for entry in view['thread_entries'] for key in entry['related_entries']}
    provenance = {r['id']: (r.get('actor'), r.get('occurred_at'), r.get('recorded_at'))
                  for r in data['snapshot']['thread_entries'] if r['id'] in related}
    return view['status'], view.get('reason'), view['thread_entries'], labels, provenance


def pane_state(pane):
    return ({k: (s.expanded, s.meta.expanded if s.meta else None)
             for k, s in pane.sections.items()}, pane.canvas.yview()[0])


def restore_folds(pane, state):
    folds, _ = state
    for key, (expanded, meta) in folds.items():
        section = pane.sections.get(key)
        if section is not None:
            section.expanded = expanded; section.show()
            if meta is not None and section.meta is not None:
                section.meta.expanded = meta; section.meta.show()
