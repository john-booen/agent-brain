"""Local health reports and SQLite snapshots."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

import usage


CODEX_ID = re.compile(r'([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\.jsonl(?:\.zst)?$')


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
                        'source_files_found': info.get('found', 0),
                        'files_updated': info.get('prepared', 0),
                        'file_failures': info.get('failed', 0),
                        'skipped_lines': info.get('skippedLines', 0),
                        'failure_reasons': info.get('failureReasons', {}),
                        'incomplete': any(info.get(key) for key in ('syncIncomplete', 'failed', 'skippedLines'))}
               for source, info in coverage.get('sources', {}).items()}
    archive = directory / 'agentsview' / 'sessions.db'
    if archive.is_file():
        with closing(sqlite3.connect(archive.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as index:
            rows = index.execute("""SELECT agent,COUNT(*),SUM(user_message_count>0),SUM(message_count=0),
                SUM(CASE WHEN parser_malformed_lines>0 OR is_truncated<>0 THEN 1 ELSE 0 END)
                FROM sessions WHERE deleted_at IS NULL GROUP BY agent""")
            for source, sessions, with_user, empty, incomplete in rows:
                status = sources.setdefault(source, {})
                status.update(indexed_sessions=sessions, sessions_with_user_messages=with_user,
                              sessions_without_messages=empty, parser_incomplete_sessions=incomplete,
                              incomplete=bool(status.get('incomplete') or incomplete))
            for source, files in transcript_coverage(index, directory).items():
                status = sources.setdefault(source, {})
                status.update(files)
                if files.get('transcripts_without_index'):
                    status['incomplete'] = True
    indexed = db.execute("""SELECT COUNT(DISTINCT session_id) FROM work w WHERE kind='summary' AND session_id IS NOT NULL
                         AND NOT EXISTS (SELECT 1 FROM packet_extractions e WHERE e.session_id=w.session_id)""").fetchone()[0]
    return {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'sources': sources,
            'last_collection': coverage.get('completedAt'), 'collector_exit': coverage.get('exit'),
            'packet_refresh': coverage.get('packetRefresh'), 'file_refresh': coverage.get('fileRefresh'),
            'pending_summary_sessions': indexed,
            'model_last_24h': dict(zip(('attempts','measured_input_tokens','measured_output_tokens','reserved_tokens'), totals)),
            'client_usage': usage.observed_usage(directory),
            'sqlite': db.execute('PRAGMA quick_check').fetchone()[0]}


def transcript_coverage(index, directory):
    """Match staged transcripts to indexed files, including alternate copies of a session."""
    manifest = read_json(directory / 'sources.json')
    if not manifest:
        return {}
    indexed = set()
    codex_ids = set()
    for agent, sid, file_path in index.execute(
            "SELECT agent,id,file_path FROM sessions WHERE deleted_at IS NULL"):
        if file_path:
            path = Path(file_path)
            indexed.add(str(path.relative_to(directory)) if path.is_relative_to(directory) else file_path)
        if agent == 'codex':
            codex_ids.add(sid.removeprefix('codex:'))
    by_source = {}
    kimi_auxiliary = set()
    for staged, item in manifest.items():
        key = Path(staged)
        if len(key.parts) < 4 or key.parts[0] != 'sources' or key.suffix not in {'.jsonl', '.zst'} or key.name == 'audit.jsonl':
            continue
        source = key.parts[1]
        original = item['source']
        if (source == 'kimi-work' and len(key.parts) >= 6 and key.name == 'wire.jsonl'
                and key.parts[4].startswith(('ctitle-', 'sklsum-', 'dvlt-'))):
            if Path(original).is_file():
                kimi_auxiliary.add(original)
            continue
        match = CODEX_ID.search(key.name) if source == 'codex' else None
        present = staged in indexed or original in indexed or bool(match and match[1] in codex_ids)
        originals = by_source.setdefault(source, {})
        originals[original] = originals.get(original, False) or present
    result = {}
    for source, found in by_source.items():
        gaps = [path for path, matched in found.items() if not matched]
        unresolved = sum(Path(path).is_file() for path in gaps)
        result[source] = {'transcripts_matched_to_index': len(found) - len(gaps),
                          'transcripts_without_index': unresolved,
                          'unmatched_originals_no_longer_on_disk': len(gaps) - unresolved}
    if kimi_auxiliary:
        result.setdefault('kimi-work', {})['excluded_auxiliary_logs'] = len(kimi_auxiliary)
    return result


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
