"""Local health reports and SQLite snapshots."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def report(db, directory):
    coverage = read_json(directory / 'coverage.json')
    boundary = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec='seconds')
    totals = db.execute("""SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),
                                 COALESCE(SUM(reserved_tokens),0) FROM packet_extraction_attempts WHERE at>=?""",
                         (boundary,)).fetchone()
    sources = {source: {'last_indexed': info.get('indexedAt') or coverage.get('completedAt'),
                        'incomplete': bool(info.get('syncIncomplete') or info.get('failed') or info.get('skippedLines'))}
               for source, info in coverage.get('sources', {}).items()}
    indexed = db.execute("""SELECT COUNT(DISTINCT session_id) FROM work w WHERE kind='summary' AND session_id IS NOT NULL
                         AND NOT EXISTS (SELECT 1 FROM packet_extractions e WHERE e.session_id=w.session_id)""").fetchone()[0]
    return {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'sources': sources,
            'last_collection': coverage.get('completedAt'), 'collector_exit': coverage.get('exit'),
            'packet_refresh': coverage.get('packetRefresh'), 'pending_summary_sessions': indexed,
            'model_last_24h': dict(zip(('attempts','measured_input_tokens','measured_output_tokens','reserved_tokens'), totals)),
            'sqlite': db.execute('PRAGMA quick_check').fetchone()[0]}


def write_report(db, directory):
    snapshot = report(db, directory)
    path = directory / 'health.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(snapshot, ensure_ascii=False) + '\n')
    temp.replace(path)
    return snapshot


def backup(db, directory, retain=7):
    path = directory / 'backups'
    path.mkdir(mode=0o700, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    dest = path / f'brain-{day}.sqlite'
    if not dest.exists():
        temp = dest.with_suffix('.tmp')
        with sqlite3.connect(temp) as snapshot:
            db.backup(snapshot)
            if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite backup did not pass integrity check')
        os.chmod(temp, 0o600)
        temp.replace(dest)
        for file in sorted(path.glob('brain-????-??-??.sqlite'), reverse=True)[retain:]:
            file.unlink()
    return dest
