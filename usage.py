"""Local MCP usage counters and weekly project updates."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def token_counter(directory=None):
    try:
        import sys
        if directory:
            modules = directory / 'tokenizer' / 'modules'
            if modules.is_dir():
                sys.path.append(str(modules))
            cache = directory / 'tokenizer' / 'cache'
            if cache.is_dir() and not os.environ.get('TIKTOKEN_CACHE_DIR'):
                os.environ['TIKTOKEN_CACHE_DIR'] = str(cache)
        import tiktoken
        # An explicit local cache keeps this feature usable offline. The tokenizer loads it only if present.
        from hashlib import sha1
        cache = os.environ.get('TIKTOKEN_CACHE_DIR')
        reference = 'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'
        if not cache or not (Path(cache) / sha1(reference.encode()).hexdigest()).is_file():
            return None
        codec = tiktoken.get_encoding('o200k_base')
        return lambda value: len(codec.encode_ordinary(value))
    except (ImportError, OSError, ValueError):
        return None


def record(directory, source, name, response, encoded, error, count_tokens, origin='client'):
    row = {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'source': source or 'unknown', 'origin': origin, 'tool': name,
           'project_id': response.get('id') or response.get('project_id'),
           'result': 'error' if error else 'returned'}
    if count_tokens:
        row['tokens'] = count_tokens(encoded)
        row['encoding'] = 'o200k_base'
    row['utf8_bytes'] = len(encoded.encode())
    if error:
        row['hit'] = False
    elif 'matches' in response:
        row['hit'] = bool(response['matches'])
    elif 'projects' in response:
        row['hit'] = bool(response['projects'])
    elif name == 'log_work':
        row['hit'] = response.get('written', False)
    else:
        row['hit'] = not error and response.get('status') not in {'没命中', '暂无记录', '待整理', '未提取到文本'}
    fd = os.open(directory / 'usage.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as log:
        print(json.dumps(row, ensure_ascii=False, separators=(',', ':')), file=log)


def events(directory):
    path = directory / 'usage.jsonl'
    if not path.is_file():
        return
    with path.open() as log:
        for line in log:
            try:
                row = json.loads(line)
                instant = datetime.fromisoformat(row['at'])
            except (ValueError, KeyError, TypeError):
                continue
            if instant.tzinfo is not None and row.get('origin') == 'client':
                yield instant, row


def observed_usage(directory, day=None):
    local = ZoneInfo('Asia/Shanghai')
    day = day or datetime.now(local).date()
    start = day - timedelta(days=6)
    counts = defaultdict(set)
    completed = set()
    for instant, row in events(directory):
        local_day = instant.astimezone(local).date()
        if start <= local_day <= day and row['source'] != 'unknown':
            counts[row['source']].add(local_day)
            completed.add(local_day)
    return {'days_with_client_calls': len(completed), 'days_by_client': {source: len(days) for source, days in counts.items()},
            'first_day': str(start), 'last_day': str(day)}


def weekly(db, data, date=None):
    local = ZoneInfo('Asia/Shanghai')
    day = date or datetime.now(local).date()
    monday = day - timedelta(days=day.weekday())
    start = datetime.combine(monday - timedelta(days=7), datetime.min.time(), local)
    finish = start + timedelta(days=7)
    totals = defaultdict(lambda: {'calls':0, 'hits':0, 'measured':0, 'tokens':0})
    for instant, row in events(data):
        if not start <= instant < finish:
            continue
        item = totals[(row['source'], row['tool'])]
        item['calls'] += 1
        item['hits'] += bool(row.get('hit'))
        if row.get('tokens') is not None:
            item['measured'] += 1
            item['tokens'] += row['tokens']
    report = ['# 本周使用情况', '', f'{start.date()} 至 {finish.date() - timedelta(days=1)}', '',
              '| 客户端 | 工具 | 调用 | 命中 | 有 Token 计量的调用 | Token |',
              '|---|---|---:|---:|---:|---:|']
    for (source, tool), row in sorted(totals.items()):
        report.append(f"| {source} | {tool} | {row['calls']} | {row['hits']} | {row['measured']} | {row['tokens']} |")
    projects = ['# 本周各项目动态', '', f'{start.date()} 至 {finish.date() - timedelta(days=1)}', '']
    for row in db.execute("""SELECT p.name,w.source,w.occurred,w.data FROM work w JOIN projects p ON p.id=w.project_id
                           WHERE w.occurred>=? AND w.occurred<? ORDER BY p.name, w.occurred DESC""",
                          (start.astimezone(timezone.utc).isoformat(), finish.astimezone(timezone.utc).isoformat())):
        item = json.loads(row['data'])
        summary = ' '.join(item['summary'].split())[:260]
        projects.append(f"- **{row['name']}** · {row['source']} · {row['occurred'][:10]} · {summary}")
    directory = data / 'weekly' / str(start.date())
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for filename, lines in [('本周使用情况.md', report), ('本周各项目动态.md', projects)]:
        path = directory / filename
        temp = path.with_suffix('.tmp')
        temp.write_text('\n'.join(lines) + '\n')
        temp.replace(path)
    return directory
