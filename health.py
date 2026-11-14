"""Local health reports and SQLite snapshots."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

import usage
import eco
import recall


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
    queue = {'pending': 0, 'oldest': None}
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
            for source, item in recent_transcripts(index, directory, boundary).items():
                sources.setdefault(source, {}).update(item)
            if index.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
                for _, _, _, first in recall.pending_segments(db, index):
                    queue['pending'] += 1
                    if first and (not queue['oldest'] or first < queue['oldest']):
                        queue['oldest'] = first
    indexed = db.execute("""SELECT COUNT(DISTINCT session_id) FROM work w WHERE kind='summary' AND session_id IS NOT NULL
                         AND NOT EXISTS (SELECT 1 FROM packet_extractions e WHERE e.session_id=w.session_id)""").fetchone()[0]
    settings = read_json(directory / 'config.json').get('eco', {})
    try:
        budget = eco.snapshot(db, settings)
    except eco.BudgetUnavailable as exc:
        budget = {'status': str(exc)}
    alerts = [f'{source} 有近期日志落后超过 1 小时' for source, info in sources.items() if info.get('lagged_over_60m')]
    if queue['oldest'] and queue['oldest'] < boundary:
        alerts.append('最旧未标记段落超过 24 小时')
    for window, name in (('24h', '24 小时'), ('7d', '7 天')):
        if budget.get(window, {}).get('warning'):
            alerts.append(f'Eco {name}积分用量含预留已达预算的 80%')
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    delivery_sessions = {sid for (sid,) in db.execute("""SELECT DISTINCT session_id FROM recall_segments
        WHERE json_extract(data, '$.latest_time')>=? AND json_array_length(data, '$.delivery_anchors')>0""", (week,))}
    logged = {sid for (sid,) in db.execute("""SELECT DISTINCT session_id FROM work
        WHERE kind='log' AND occurred>=? AND session_id IS NOT NULL""", (week,))}
    log_coverage = {'sample': '近七天已标记且含交付证据的会话', 'delivery_sessions': len(delivery_sessions),
                    'logged_sessions': len(delivery_sessions & logged),
                    'percent': round(100 * len(delivery_sessions & logged) / len(delivery_sessions), 1)
                    if delivery_sessions else None}
    return {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'sources': sources,
            'last_collection': coverage.get('completedAt'), 'collector_exit': coverage.get('exit'),
            'packet_refresh': coverage.get('packetRefresh'), 'file_refresh': coverage.get('fileRefresh'),
            'pending_summary_sessions': indexed,
            'model_last_24h': dict(zip(('attempts','measured_input_tokens','measured_output_tokens','reserved_tokens'), totals)),
            'eco_credits': budget,
            'log_work_7d': log_coverage,
            'tag_queue': queue, 'alerts': alerts,
            'client_usage': usage.observed_usage(directory),
            'sqlite': db.execute('PRAGMA quick_check').fetchone()[0]}


def recent_transcripts(index, directory, boundary):
    manifest = read_json(directory / 'sources.json')
    if not manifest:
        return {}
    fields = {'observed_recent_files': 0, 'new_indexed_24h': 0, 'appended_indexed_24h': 0,
              'recent_unmatched': 0, 'source_modified_over_15m_after_latest_index': 0, 'lagged_over_60m': 0,
              'message_gap_sampled': 0, 'message_gap_over_15m': 0, 'latest_message_gap_seconds': None}
    result = {}
    seen = set()
    now = datetime.now(timezone.utc)
    cutoff = datetime.fromisoformat(boundary)
    columns = {v[1] for v in index.execute("PRAGMA table_info(sessions)")}
    if not {'started_at','ended_at','local_modified_at'} <= columns:
        return {}
    for sid, agent, file, started, ended, updated in index.execute("""SELECT id,agent,file_path,started_at,ended_at,local_modified_at
            FROM sessions WHERE deleted_at IS NULL AND (ended_at>=? OR local_modified_at>=?)""", (boundary,boundary)):
        info = result.setdefault(agent, fields.copy())
        latest_indexed = index.execute("""SELECT timestamp FROM messages WHERE session_id=?
            AND is_system=0 AND role IN ('user','assistant') AND timestamp IS NOT NULL
            ORDER BY ordinal DESC LIMIT 1""", (sid,)).fetchone()
        message_time = latest_indexed[0] if latest_indexed else None
        if started and started >= boundary:
            info['new_indexed_24h'] += 1
        elif message_time and message_time >= boundary:
            info['appended_indexed_24h'] += 1
        path = Path(file) if file else None
        if not path or not path.is_relative_to(directory):
            continue
        item = manifest.get(str(path.relative_to(directory)))
        if not item:
            continue
        original = Path(item['source'])
        seen.add(original)
        try:
            modified = datetime.fromtimestamp(original.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                continue
            info['observed_recent_files'] += 1
            if updated:
                delay = (modified-datetime.fromisoformat(updated)).total_seconds()
                if delay > 900:
                    info['source_modified_over_15m_after_latest_index'] += 1
                if delay > 3600:
                    info['lagged_over_60m'] += 1
            if message_time:
                original_last = latest_source_message(original, Path(file))
                if original_last:
                    gap = max(0, int((original_last-datetime.fromisoformat(message_time)).total_seconds()))
                    info['latest_message_gap_seconds'] = max(gap, info['latest_message_gap_seconds'] or 0)
                    info['message_gap_sampled'] += 1
                    if gap > 900:
                        info['message_gap_over_15m'] += 1
                    if gap > 3600:
                        info['lagged_over_60m'] += 1
        except (OSError, ValueError):
            continue
    for staged, item in manifest.items():
        key = Path(staged)
        if len(key.parts)<5 or key.parts[0]!='sources' or key.name=='audit.jsonl' or key.suffix not in {'.jsonl','.zst'}:
            continue
        if key.parts[1]=='kimi-work' and key.parts[4].startswith(('ctitle-','sklsum-','dvlt-')):
            continue
        original = Path(item['source'])
        if original in seen:
            continue
        seen.add(original)
        try:
            modified = datetime.fromtimestamp(original.stat().st_mtime,timezone.utc)
        except OSError:
            continue
        if modified >= cutoff:
            info=result.setdefault(key.parts[1],fields.copy())
            info['recent_unmatched'] += 1
            if now-modified > timedelta(hours=1):
                info['lagged_over_60m'] += 1
    return result


def latest_source_message(original, staged):
    # Compressed sources are already expanded in their staged copies when available.
    candidates = (original, staged) if original.suffix == '.jsonl' else (staged,)
    for file in candidates:
        if file.suffix != '.jsonl':
            continue
        with file.open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - 256 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            for line in reversed(stream.read().split(b'\n')):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get('type') or event.get('kind')
                payload = event.get('payload') or {}
                message = event.get('message')
                indexed_dialogue = isinstance(message, dict) and message.get('role') in {'user', 'assistant'}
                if (kind in {'user', 'assistant'} and event.get('toolUseResult') is not None
                        or kind == 'context.append_message' and not indexed_dialogue):
                    continue
                if not (kind in {'user','assistant','turn.prompt','context.append_message',
                                 'user/message','assistant/message'}
                        or kind == 'response_item' and isinstance(payload,dict) and payload.get('type') == 'message'
                        and payload.get('role') in {'user','assistant'}):
                    continue
                value = event.get('timestamp') or event.get('time')
                if type(value) is int and 1_000_000_000_000 <= value < 10_000_000_000_000:
                    return datetime.fromtimestamp(value / 1000, timezone.utc)
                if isinstance(value, str):
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo:
                        return parsed
    return None


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


def update_todo(path, alerts):
    marker = '<!-- agent-brain-health -->'
    current = path.read_text() if path.is_file() else ''
    rest = current.split(marker + '\n', 2)[-1] if current.startswith(marker + '\n') else current
    section = marker + '\n' + ''.join(f'- **{text}**\n' for text in alerts) + marker + '\n' if alerts else ''
    if current != section + rest:
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
            handle.write(section + rest)
            temporary = Path(handle.name)
        temporary.replace(path)


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
