"""Select saved merges only while all source analyses still match the library."""
from __future__ import annotations


def current_recording(library, store, battle_id):
    views = {r['sha256']: r for r in library.list_replays(battle_id)}
    records = sorted(store.list_recordings(battle_id), key=lambda r: (r.get('created_at', ''), r['id']), reverse=True)
    for record in records:
        sources = record.get('sources') or []
        if {s['sha256'] for s in sources} != set(views):
            continue
        if all(views[s['sha256']]['analysis_revision'] == s['analysis_revision']
               and views[s['sha256']]['report_sha256'] == s['report_sha256'] for s in sources):
            return record
    return None
