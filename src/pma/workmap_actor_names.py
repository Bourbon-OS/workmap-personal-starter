"""Optional local chat titles for display only; never identity or authority."""
from datetime import datetime
import json
import os
from pathlib import Path
import re


ACTOR = re.compile(r'actor:codex(?:-task)?:([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z')


def local_task_index_path():
    """Only the local title index, not conversations, auth, or session bodies."""
    root = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    return root / 'session_index.jsonl'


def local_actor_names(path, actors):
    by_thread = {}
    for actor in actors:
        match = ACTOR.fullmatch(actor)
        if match:
            by_thread.setdefault(match[1].lower(), []).append(actor)
    if not by_thread or path is None or str(path).startswith(('\\\\', '//')):
        return {}
    latest = {}
    try:
        with Path(path).open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                        continue
                    key = row['id'].lower()
                    if key not in by_thread:
                        continue
                    name = row.get('thread_name')
                    if not isinstance(name, str) or not name.strip():
                        continue
                    stamp = datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00'))
                    if stamp.utcoffset() is None:
                        continue
                    name = name.strip()
                    old = latest.get(key)
                    if old is None or stamp > old[0]:
                        latest[key] = (stamp, name)
                    elif stamp == old[0] and name != old[1]:
                        # Conflicting equally recent names are not guessed.
                        latest[key] = (stamp, None)
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
    except (OSError, UnicodeError):
        return {}
    return {actor: name for key, (_, name) in latest.items() if name is not None
            for actor in by_thread[key]}
