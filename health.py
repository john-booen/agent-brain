"""Local health reports and SQLite snapshots."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
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
            'collector_observation': collector_observation(directory),
            'sqlite': db.execute('PRAGMA quick_check').fetchone()[0]}


def collector_observation(directory, now=None):
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=48)
    last_before = None
    selected, failures = [], 0
    try:
        log = (directory / 'health.log').open()
    except FileNotFoundError:
        return {'passed': False, 'rounds': 0, 'observed_hours': 0, 'failures': 0}
    with log:
        for line in log:
            try:
                event = json.loads(line)
                at = datetime.fromisoformat(event.get('at', ''))
            except (ValueError, TypeError, AttributeError):
                continue
            if at.tzinfo is None:
                continue
            kind = event.get('event')
            if at > now or kind not in {'packet_refresh', 'collector', 'source_collection'}:
                continue
            if at < start and kind == 'packet_refresh':
                last_before = max(last_before or at, at)
            elif at >= start:
                if event.get('status') in {'failed', 'partial'}:
                    failures += 1
                if kind == 'packet_refresh':
                    selected.append(at)
    times = [*([last_before] if last_before else [start]), *sorted(selected), now]
    max_gap = max((b - a).total_seconds() for a, b in zip(times, times[1:]))
    observed = min(48, (now - (last_before or selected[0])).total_seconds() / 3600) if selected else 0
    covers_start = last_before is not None or bool(selected and min(selected) == start)
    return {'passed': bool(covers_start and selected and max_gap <= 720),
            'rounds': len(selected), 'observed_hours': round(observed, 2),
            'max_gap_seconds': round(max_gap), 'failures': failures}


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
        with closing(sqlite3.connect(temp)) as snapshot:
            db.backup(snapshot)
            if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite backup did not pass integrity check')
        os.chmod(temp, 0o600)
        temp.replace(dest)
        for file in sorted(path.glob('brain-????-??-??.sqlite'), reverse=True)[retain:]:
            file.unlink()
    return dest
