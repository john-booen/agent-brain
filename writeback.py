"""Associate successful native MCP receipts with immutable work records, locally."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from time import monotonic


SUPPORTED = {'codex', 'cowork', 'kimi-work'}
REVIEW_PREFIXES = ('The following is the Codex agent history whose request action you are assessing.',
                   'The following is the Codex agent history added since your last approval assessment.')


def schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS work_session_links(
            work_id TEXT NOT NULL REFERENCES work(id), session_id TEXT NOT NULL,
            call_id TEXT NOT NULL, at TEXT NOT NULL, proof_sha256 TEXT NOT NULL,
            PRIMARY KEY(work_id,session_id,call_id));
        CREATE TABLE IF NOT EXISTS work_link_scans(
            session_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        CREATE VIEW IF NOT EXISTS work_with_sessions AS
            SELECT w.id,w.project_id,w.kind,w.occurred,w.source,
                   coalesce(nullif(w.session_id,''),l.session_id) AS session_id,w.data,
                   w.rowid AS work_rowid
            FROM work w LEFT JOIN (
                SELECT work_id,min(session_id) AS session_id FROM work_session_links
                GROUP BY work_id HAVING count(DISTINCT session_id)=1
            ) l ON l.work_id=w.id;
    ''')


def receipt(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict) or value.get('isError') or value.get('is_error'):
        return None
    if value.get('written') is True and isinstance(value.get('work_id'), str):
        return value
    if 'output' in value:
        return receipt(value['output'])
    content = value.get('content')
    if isinstance(content, str):
        return receipt(content)
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and content[0].get('type') == 'text':
        return receipt(content[0].get('text'))
    return None


def tool_name(name):
    return name in {'mcp__agent-brain__log_work', 'mcp__agent_brain__log_work',
                    'mcp__plugin-agent-brain_agent-brain__log_work'}


def native_receipts(path, source, sid, deadline):
    pending = {}
    with path.open() as transcript:
        for line in transcript:
            if monotonic() >= deadline:
                raise TimeoutError('writeback association budget')
            # Only structured native events count; quoted chat and approval copies do not.
            if source == 'codex' and ('McpToolCall' not in line or 'log_work' not in line):
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(row, dict):
                continue
            at, call_id, args, result = None, None, None, None
            if source == 'codex' and row.get('type') == 'event_msg':
                event = row.get('payload') or {}
                if not isinstance(event, dict):
                    continue
                item = event.get('item') or {}
                if not isinstance(item, dict):
                    continue
                if (event.get('type') == 'item_completed' and item.get('type') == 'McpToolCall'
                        and item.get('server') in {'agent_brain', 'agent-brain'}
                        and item.get('tool') == 'log_work' and item.get('status') == 'completed'
                        and event.get('thread_id') == sid.removeprefix('codex:')):
                    at, call_id, args, result = row.get('timestamp'), item.get('id'), item.get('arguments'), item.get('result')
            elif source == 'cowork' and (row.get('session_id') == sid.removeprefix('cowork:')
                    or row.get('session_id') is None and path.stem == sid.removeprefix('cowork:')):
                message = row.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if row.get('type') == 'assistant' and block.get('type') == 'tool_use' and tool_name(block.get('name')):
                        pending[block.get('id')] = block.get('input')
                    elif (row.get('type') == 'user' and block.get('type') == 'tool_result'
                          and block.get('tool_use_id') in pending and not block.get('is_error')):
                        at = row.get('timestamp') or row.get('_audit_timestamp')
                        call_id = block['tool_use_id']
                        args, result = pending.pop(call_id), block
                        found = receipt(result)
                        if found:
                            yield at, call_id, args, found, hashlib.sha256(line.encode()).hexdigest()
                continue
            elif source == 'kimi-work' and row.get('type') == 'context.append_loop_event':
                event = row.get('event') or {}
                if not isinstance(event, dict):
                    continue
                if event.get('type') == 'tool.call' and tool_name(event.get('name')):
                    pending[event.get('toolCallId')] = event.get('args')
                elif event.get('type') == 'tool.result' and event.get('toolCallId') in pending:
                    at, call_id = row.get('time'), event['toolCallId']
                    args, result = pending.pop(call_id), event.get('result')
            found = receipt(result)
            if found:
                yield at, call_id, args, found, hashlib.sha256(line.encode()).hexdigest()


def reconcile(db, directory, clean, resolve_project, seconds=8):
    started = monotonic()
    deadline = started + max(0, seconds)
    boundary = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    records = {row['id']: dict(row) for row in db.execute('''SELECT w.* FROM work w
        WHERE w.kind='log' AND w.session_id IS NULL AND w.occurred>=?''', (boundary,))}
    stats = {'scanned': 0, 'cached': 0, 'linked': 0, 'unsupported': 0, 'unreadable': 0, 'budget_exhausted': False}
    archive = directory / 'agentsview/sessions.db'
    if not records or not archive.is_file():
        return stats
    try:
        with closing(sqlite3.connect(archive.resolve().as_uri()+'?mode=ro', uri=True, timeout=3)) as index:
            index.row_factory = sqlite3.Row
            sessions = list(index.execute('''SELECT * FROM sessions WHERE deleted_at IS NULL
                AND (ended_at>=? OR started_at>=?) ORDER BY coalesce(ended_at,started_at) DESC''', (boundary,boundary)))
    except sqlite3.Error:
        return {**stats, 'status': 'archive_metadata_unavailable'}
    cached = dict(db.execute('SELECT session_id,fingerprint FROM work_link_scans'))
    pending_sources = {row['source'] for row in records.values()}
    root = (directory / 'sources').resolve()
    for session in sessions:
        if monotonic() >= deadline:
            stats['budget_exhausted'] = True
            break
        sid, source = session['id'], session['agent']
        if source not in pending_sources:
            continue
        if source == 'codex' and session['relationship_type'] == 'subagent' and str(session['first_message'] or '').lstrip().startswith(REVIEW_PREFIXES):
            continue
        if source not in SUPPORTED:
            stats['unsupported'] += 1
            continue
        path = Path(session['file_path'] or '').resolve()
        if not path.is_relative_to(root) or path.suffix != '.jsonl':
            stats['unsupported'] += 1
            continue
        try:
            before = path.stat()
            fingerprint = f'2:{before.st_size}:{before.st_mtime_ns}:{session["transcript_revision"]}'
            if cached.get(sid) == fingerprint:
                stats['cached'] += 1
                continue
            links = []
            for at, call_id, args, output, proof in native_receipts(path, source, sid, deadline):
                original = records.get(output['work_id'])
                if not original or original['source'] != source or not call_id or not isinstance(args, dict):
                    continue
                if clean(str(args.get('summary') or '').strip()) != json.loads(original['data'])['summary']:
                    continue
                if resolve_project(db, output.get('project_id')) != resolve_project(db, original['project_id']):
                    continue
                try:
                    actual = (datetime.fromtimestamp(at/1000,timezone.utc) if type(at) in (int,float)
                              else datetime.fromisoformat(at))
                    logged = datetime.fromisoformat(original['occurred'])
                    if actual.tzinfo is None or logged.tzinfo is None or abs((actual-logged).total_seconds()) > 300:
                        continue
                except (ValueError, TypeError):
                    continue
                links.append((original['id'], sid, str(call_id), actual.isoformat(), proof))
            after = path.stat()
            if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
                stats['unreadable'] += 1
                continue
            with db:
                prior = db.total_changes
                db.executemany('INSERT OR IGNORE INTO work_session_links VALUES(?,?,?,?,?)',links)
                stats['linked'] += db.total_changes-prior
                db.execute('INSERT OR REPLACE INTO work_link_scans VALUES(?,?)',(sid,fingerprint))
            stats['scanned'] += 1
        except TimeoutError:
            stats['budget_exhausted'] = True
            break
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            stats['unreadable'] += 1
    stats['seconds'] = round(monotonic()-started,3)
    stats['associated_logs'] = db.execute('SELECT count(*) FROM work WHERE kind="log" AND session_id IS NULL AND id IN '
        '(SELECT work_id FROM work_session_links GROUP BY work_id HAVING count(DISTINCT session_id)=1)').fetchone()[0]
    return stats
