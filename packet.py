"""Build project packets from local evidence, one project at a time."""
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import file_text
import notes


VERSION = 24
EXTENSIONS = r'(?:docx|pdf|xlsx|md|markdown|txt|csv|tsv|pptx|doc|xls|zip|html|png|jpe?g)'
PATH = re.compile(r'(?<![\w/])(/(?:Users|Volumes|private|tmp|var)/[^\n\r<>\[\]（）。，；、`"|]*?\.' + EXTENSIONS + r')(?=$|[^\w.])', re.I)
NAME = re.compile(r'([^/\s\\()（）《》<>\[\]，。；：、]+\.' + EXTENSIONS + r')(?=$|[^\w.])', re.I)
ATTACHMENT = re.compile(r'<attachment>(.*?)</attachment>', re.S | re.I)
CLAUSE = re.compile(r'(?m)[^。！？；\n]+')
NON_USER = re.compile(r'^(?:\s*<system(?:>|-reminder)|\s*\[(?:Message|Reply) from |\s*\[Tool result\]|\s*\[Tool output\]|The following is the Codex agent history)', re.I)
INSTRUCTIONS = re.compile(r'只(?:做|给|保留|改|用|写)|(?:一律|统一|对外|内部|数据|数字|口径|交付|必须|不要|不得|不能|不用|别|请按|以.{1,18}为准|改成|不做|脱敏|保守|以后|后续|今后|严格|保持)')
APPROVAL = re.compile(r'(?:这(?:一)?[份个版]|就用这版).{0,22}(?:就是|作为|定为|当作)\s*(?:终版|定稿|正式版|最终版)|^按.{1,22}(?:定稿|定版)[。！!\s]*$|^(?:可以|好的|好|确认|通过|定稿|就这样|没问题|用这版)[。！!，,\s]*$')
NEGATIVE = re.compile(r'不要|不对|不行|还没|草稿|不满意|未通过|不是终版|未确认|别|重做')
REVISION_REQUEST = re.compile(r'改成|改为|修改|调整|补充|重写|再改|再做|出一版|生成|重新|优化|完善')
IRRELEVANT = re.compile(r'未(?:直接)?涉及|未提供具体|仅作为(?:上下文|背景)|仅提及|从未执行|无具体|不属于本项目')
DELIVERY_SIGNAL = re.compile(r'交付|下载|已生成|已完成|已保存|写入|定稿|最终|终版|保存至|文件在|输出|\.docx\]|\.pdf\]|\.xlsx\]|\.md\]', re.I)
TEXT_DELIVERY = re.compile(r'(?:现场|口头|沟通|可直接照着说).{0,16}(?:对话稿|口头稿|逐字稿|话术)|可直接(?:复制|粘贴|发送)')
PREVIEW_NAME = re.compile(r'预览|对比图|三款对比|候选|备份|contact.sheet|(?:^|[-_])(?:draft|preview|backup|qa|render)(?:$|[-_])', re.I)


def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS project_packets(
      project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
      signature TEXT NOT NULL, data TEXT NOT NULL, built TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS packet_file_summaries(
      fingerprint TEXT PRIMARY KEY, summary TEXT NOT NULL, method TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS packet_file_paths(
      path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, digest TEXT NOT NULL REFERENCES packet_file_summaries(fingerprint)
    );
    CREATE TABLE IF NOT EXISTS packet_file_failures(
      path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS project_redirects(
      old_id TEXT PRIMARY KEY REFERENCES projects(id), project_id TEXT NOT NULL REFERENCES projects(id),
      reason TEXT NOT NULL, merged TEXT NOT NULL, CHECK(old_id<>project_id)
    );
    CREATE TABLE IF NOT EXISTS project_record_moves(
      work_id TEXT PRIMARY KEY, from_project TEXT NOT NULL REFERENCES projects(id),
      to_project TEXT NOT NULL REFERENCES projects(id), reason TEXT NOT NULL, moved TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS project_evidence_corrections(
      session_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      from_project TEXT NOT NULL REFERENCES projects(id), to_project TEXT NOT NULL REFERENCES projects(id),
      reason TEXT NOT NULL, PRIMARY KEY(session_id, ordinal, from_project)
    );
    CREATE TABLE IF NOT EXISTS packet_path_relocations(
      old_path TEXT PRIMARY KEY, path TEXT NOT NULL, evidence TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS work_session_project ON work(session_id, project_id);
    """)


def paths_in(text):
    paths = []
    for match in ATTACHMENT.finditer(text):
        try:
            attachment = json.loads(match[1])
        except json.JSONDecodeError:
            continue
        if isinstance(attachment, dict) and isinstance(attachment.get('path'), str):
            paths.append(attachment['path'])
    paths.extend(match[1].strip() for match in PATH.finditer(text))
    return list(dict.fromkeys(paths))


def task_key(path):
    name = Path(path).stem.casefold()
    name = re.sub(r'(?<!\d)(?:19|20)\d{2}[-_.]?\d{2}[-_.]?\d{2}(?!\d)', '', name)
    name = re.sub(r'(?i)(?:[ _.-]+v(?:er)?[ _.-]*\d+(?:\.\d+)*|[-_（( ]*(?:终版|最终版|定稿|最终稿|修订稿|修订版|优化稿|更新版|交付版|草稿|word版|可编辑版|备份|完整版|终稿|初稿)[）) ]*)+$', '', name)
    name = re.sub(r'(?:[_ -](?:可编辑|原版文字微调|版式增强|排版优化|文字微调|微调|长版|短版|预览))+$', '', name)
    return re.sub(r'[^\w\u3400-\u9fff]+|_', '', name)


def group_by_task(item, key, confirmed_version):
    path = item.get('path')
    if item.get('explicit_task') or confirmed_version:
        return key
    if Path(path).suffix.lower() in {'.png', '.jpg', '.jpeg', '.pptx'}:
        return f'visual:{key}'
    return f"{Path(path).parent}:{key}"


def work_rows(db, project_id):
    return list(db.execute("""
        SELECT w.*, (SELECT COUNT(DISTINCT w2.project_id) FROM work w2
          LEFT JOIN project_record_exclusions e2 ON e2.work_id=w2.id
          WHERE w2.session_id=w.session_id AND e2.work_id IS NULL) AS project_count
        FROM work w LEFT JOIN project_record_exclusions e ON e.work_id=w.id
        WHERE w.project_id=? AND e.work_id IS NULL
        ORDER BY w.occurred, w.id
    """, (project_id,)))


def source_versions(index, sessions):
    if index is None:
        return {}
    versions = {}
    for sid in sessions:
        row = index.execute('SELECT transcript_revision, cwd, display_name, session_name FROM sessions WHERE id=?', (sid,)).fetchone()
        if row:
            versions[sid] = tuple(row)
    return versions


def citation(row, ordinal=None, time=None):
    return {'source': row['source'], 'session_id': row['session_id'] or row['id'],
            'ordinal': ordinal, 'time': time or row['occurred']}


def available_path(path, cwd=None):
    if not path or len(path) > 1500 or path.startswith(('https:', 'http:')):
        return None
    candidate = Path(path)
    if '..' in candidate.parts:
        return None
    if not candidate.is_absolute() and cwd and Path(cwd).is_absolute() and '..' not in candidate.parts:
        candidate = Path(cwd) / candidate
    if candidate.is_absolute():
        return str(candidate)
    return None


def project_terms(name):
    terms = re.findall(r'[\u3400-\u9fff]{2,}|[a-zA-Z][a-zA-Z0-9-]{2,}', name.casefold())
    for term in terms[:]:
        if len(term) > 4 and re.fullmatch(r'[\u3400-\u9fff]+', term):
            terms.append(term[:4])
        if len(term) >= 6 and '银行' in term:
            terms.append(term.split('银行')[0])
    return {term for term in terms if term not in {'项目', '方案', '研究', '活动', '工作', '进展', '问题', '整理', '共享', 'word', 'pdf'}}


def matches_project(value, terms, loose=True):
    value = value.casefold()
    return any(term in value or (loose and len(term) == 4 and term[:3] in value) for term in terms)


def same_task_name(name):
    value = re.sub(r'（[^）]+）|\([^)]*\)', '', name).casefold()
    return re.sub(r'[\s_—–-]+', '', value)


def merge_candidates(db, active_only=False):
    """Require a matching name and independent evidence of work on the same task."""
    projects = list(db.execute('SELECT p.id,p.name FROM projects p LEFT JOIN project_redirects r ON p.id=r.old_id WHERE r.old_id IS NULL'))
    if active_only:
        active = {row['id'] for row in active_projects(db)}
        projects = [p for p in projects if p['id'] in active]
    evidence = {}
    for project in projects:
        rows = work_rows(db, project['id'])
        sessions, files = set(), set()
        for row in rows:
            data = json.loads(row['data'])
            if IRRELEVANT.search(data['summary']) and not data.get('files'):
                continue
            if row['session_id']:
                sessions.add(row['session_id'])
            files.update(item['path'] for item in data.get('files', []) + data.get('deliverables', [])
                         if Path(item['path']).is_absolute())
        evidence[project['id']] = (sessions, files, len(rows))
    pairs = []
    for i, left in enumerate(projects):
        a = same_task_name(left['name'])
        if len(a) < 4:
            continue
        for right in projects[i + 1:]:
            b = same_task_name(right['name'])
            prefix = (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 5
            if a != b and not prefix and (not active_only or SequenceMatcher(None, a, b).ratio() < 0.76):
                continue
            sa, fa, na = evidence[left['id']]
            sb, fb, nb = evidence[right['id']]
            shared_paths = len(fa & fb)
            shared_sessions = len(sa & sb)
            if not shared_paths and not (a == b and shared_sessions):
                continue
            target, duplicate = (left, right) if na >= nb else (right, left)
            pairs.append({'target': target['name'], 'duplicate': duplicate['name'],
                          'shared_files': shared_paths, 'shared_sessions': shared_sessions})
    return pairs


def messages_for(index, sid, rows, focused, terms):
    if index is None:
        return []
    if focused:
        return index.execute("""SELECT role,ordinal,timestamp,content,dialogue_text,is_system FROM messages
                                WHERE session_id=? AND role IN ('user','assistant') ORDER BY ordinal""", (sid,))
    anchors = {item for row, data in rows for item in [data.get('ordinal'), *(f.get('ordinal') for f in data.get('files', []))]
               if type(item) is int}
    needles = [term for term in sorted(terms) if len(term) >= 3][:8]
    if needles:
        conditions = ' OR '.join('instr(COALESCE(dialogue_text,content),?)>0' for _ in needles)
        anchors.update(row[0] for row in index.execute(
            f"SELECT ordinal FROM messages WHERE session_id=? AND role='user' AND ({conditions}) LIMIT 80", [sid, *needles]))
    intervals = []
    for start, end in sorted((max(0, n - 28), n + 28) for n in anchors):
        if intervals and start <= intervals[-1][1] + 1:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
        else:
            intervals.append((start, end))
    # Separate range lookups keep the SQL expression size bounded on long conversations.
    return (message for start, end in intervals for message in index.execute(
        """SELECT role,ordinal,timestamp,content,dialogue_text,is_system FROM messages
           WHERE session_id=? AND role IN ('user','assistant') AND ordinal BETWEEN ? AND ? ORDER BY ordinal""",
        (sid, start, end)))


def scope_for(text):
    if '对外' in text or '外发' in text:
        return '对外版本'
    if re.search(r'今后|以后|整个项目|这个项目.*(?:一律|全部)', text):
        return '项目'
    return '本任务'


def rule_topic(text):
    for name, pattern in (
        ('文件形式', r'只做.{0,12}(?:Word|PPT)|不做.{0,8}(?:PPT|Word)|(?:Word|PPT).{0,8}(?:就行|不用|不要)'),
        ('脱敏', r'脱敏|匿名|隐藏.{0,8}(?:客户|供应链)'),
        ('收入口径', r'(?:收入|营收).{0,30}(?:按|统一|口径|万|亿)'),
        ('估值口径', r'估值.{0,30}(?:按|统一|口径|倍|万|亿)'),
        ('保守口径', r'保守|往低|往少'),
    ):
        if re.search(pattern, text, re.I):
            return name
    return ' '.join(text.casefold().split())


def normalize_rule(item):
    return {**item, 'topic': item.get('topic') or rule_topic(item['text'])}


def confirms_delivery(text):
    text = text.strip()
    return bool(APPROVAL.search(text) and not (NEGATIVE.search(text) or REVISION_REQUEST.search(text)))


def recorded_confirmation(index, session_id, ordinal, path, visible):
    if index is None or not session_id or type(ordinal) is not int or ordinal < 0:
        return False
    message = index.execute("SELECT * FROM messages WHERE session_id=? AND ordinal=? AND role='user'",
                            (session_id, ordinal)).fetchone()
    if not message:
        return False
    text = visible(dict(message))
    if not confirms_delivery(text):
        return False
    if Path(path).name in text:
        return True
    previous = index.execute("SELECT * FROM messages WHERE session_id=? AND ordinal>=? AND ordinal<? AND role='assistant' "
                             'ORDER BY ordinal DESC LIMIT 1', (session_id, ordinal - 4, ordinal)).fetchone()
    return bool(previous and Path(path).name in visible(dict(previous)))


def quoted_rules(text):
    for part in CLAUSE.finditer(text):
        value = re.sub(r'^\s*(?:(?:[-•]|\d{1,3}[.)、])\s*)?', '', part[0]).strip(' \t，')
        if (8 <= len(value) <= 165 and not value.endswith(('吗', '呢')) and INSTRUCTIONS.search(value)
                and not re.search(r'能不能|能否|是否|可不可以|怎么办|该如何|为什么|要不要|你觉得', value)):
            if re.search(r'必须|不要|不得|不能|只做|只给|不用|不做|脱敏|以.{1,18}为准|保守|往低|一律|统一|请按|改成|以后|严格|保持', value):
                topic = rule_topic(value)
                if topic != ' '.join(value.casefold().split()) or re.search(r'^(?:请|你|我们|必须|不要|不得|只|以后|后续|统一|一律|以|按)|我要求|我的要求', value):
                    yield {'text': value, 'scope': scope_for(value), 'topic': topic}


def chosen_rules(text):
    if not text.lstrip().startswith('{'):
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict) or data.get('kind') != 'answer_to_pending_question':
        return []
    questions = {q['id']: q for q in data.get('questions', []) if isinstance(q, dict) and 'id' in q}
    result = []
    for answer in data.get('answers', []):
        if not isinstance(answer, dict):
            continue
        q = questions.get(answer.get('id'), {})
        for selected in answer.get('selected', []):
            if not isinstance(selected, str):
                continue
            option = next((item for item in q.get('options', []) if isinstance(item, dict) and item.get('label') == selected), {})
            content = str(q.get('question', '')).rstrip('？?') + ' ' + selected + ' ' + str(option.get('description', ''))
            result.append({'text': content.strip()[:200], 'topic': rule_topic(content), 'scope': '本任务', 'chosen': True})
    return result


def entry(path, ref, **fields):
    return {'path': path, 'ref': ref, **fields}


def finish_entries(inputs, deliveries, rules, approvals):
    input_paths = {}
    for item in sorted(inputs, key=lambda item: item['ref']['time']):
        key = item.get('path') or hashlib.sha256((item.get('text') or '').encode()).hexdigest()
        input_paths.setdefault(key, item)
    grouped = defaultdict(dict)
    by_stem = defaultdict(list)
    for item in deliveries:
        if item.get('path'):
            by_stem[task_key(item['path'])].append(item)
    for item in deliveries:
        path = item.get('path')
        if not item.get('task'):
            item['task'] = Path(path).stem
        key = task_key(item['task']) or task_key(path or '')
        # Distinct task labels are authoritative, while the same basename in different folders
        # is only compared inside a directory or when a version has an explicit task label.
        confirmed_version = any(other.get('pinned') or other.get('final') for other in by_stem[key])
        key = group_by_task(item, key, confirmed_version)
        versions = grouped[key]
        path = path or f"{item['ref']['session_id']}#{item['ref']['ordinal']}"
        rank = (item.get('pinned', False), item['ref']['time'], item.get('recorded', False))
        current = versions.get(path)
        if not current or rank >= (current.get('pinned', False), current['ref']['time'], current.get('recorded', False)):
            versions[path] = {**item, 'final': item.get('final', False) or bool(current and current.get('final'))}
    # Fold duplicate paths explicitly registered under a task into that task.
    registered = {path: key for key, versions in grouped.items() for path, item in versions.items() if item.get('explicit_task')}
    for key, versions in list(grouped.items()):
        if any(item.get('explicit_task') for item in versions.values()):
            continue
        targets = {registered[path] for path in versions.keys() & registered.keys()}
        if len(targets) == 1:
            target = grouped[next(iter(targets))]
            for path, item in versions.items():
                target.setdefault(path, item)
            del grouped[key]
    approved = {}
    for path, ref in sorted(approvals, key=lambda pair: pair[1]['time']):
        approved[path] = ref
    winners, history = [], []
    for versions in grouped.values():
        options = []
        for path, item in versions.items():
            if path in approved and item.get('status') != '已失效':
                item = {**item, 'pinned': True, 'approval': approved[path], 'status': '用户确认'}
            if item['status'] == '已失效':
                history.append({'kind': '交付', **item})
            elif item['status'] == '草稿':
                history.append({'kind': '草稿', **item})
            else:
                options.append(item)
        if options:
            options.sort(key=lambda item: (item.get('pinned', False),
                                           (item.get('approval') or item['ref'])['time'] if item.get('pinned') else '',
                                           item.get('final', False), item['ref']['time'], item.get('reused', False)), reverse=True)
            winners.append(options[0])
            history.extend({'kind': '旧版本', **item} for item in options[1:])
    winners.sort(key=lambda item: (item.get('pinned', False), item.get('final', False), item['ref']['time']), reverse=True)
    decisions = {}
    for rule in sorted(rules, key=lambda item: item['ref']['time'], reverse=True):
        item = normalize_rule(rule)
        key = (item['topic'], item['scope'], item.get('task') or '')
        if key not in decisions:
            decisions[key] = item
        elif item['text'] != decisions[key]['text']:
            history.append({'kind': '旧要求', **item})
    current = sorted(decisions.values(), key=lambda item: (item.get('recorded', False), item.get('chosen', False),
                     item['topic'] in {'文件形式','脱敏','收入口径','估值口径','保守口径'}, item['ref']['time']), reverse=True)
    history.extend({'kind': '其他要求', **item} for item in current[10:])
    return {'inputs': list(input_paths.values()), 'deliverables': winners,
            'rules': current[:10], 'history': history}


def add_extracted(project, row, versions, inputs, deliveries, rules, todos, outcomes):
    cwd = versions[row['session_id']][1]
    message_time = project.get('time')
    outcomes.append({'text': project['summary'], 'ref': citation(row, project['ordinal'], message_time), 'modeled': True})
    for item in project['inputs']:
        ref = citation(row, item['ordinal'], item.get('time'))
        path = available_path(item.get('path'), cwd) or item.get('path')
        inputs.append(entry(path, ref, text=item.get('text'), modeled=True))
    for item in project['deliverables']:
        ref = citation(row, item['ordinal'], item.get('time'))
        path = available_path(item.get('path'), cwd) or item.get('path')
        confirmation = item.get('confirmation')
        deliveries.append(entry(path, ref, task=item['task'], text=item.get('text'), explicit_task=True,
                                final=item['final'], pinned=confirmation is not None, modeled=True,
                                description=item.get('description'),
                                approval=citation(row, confirmation, item.get('confirmation_time')) if confirmation is not None else None,
                                status='用户确认' if confirmation is not None else '已交付' if item['final'] else '草稿'))
    rules.extend({**item, 'ref': citation(row, item['ordinal'], item.get('time')), 'modeled': True} for item in project['rules'])
    todos.extend({**item, 'ref': citation(row, item['ordinal'], item.get('time')), 'modeled': True} for item in project['todos'])


def build(rows, versions, index, visible, clean, names, project_terms_by_session, extracted=None, project_id=None, extra_rows=None):
    inputs, deliveries, rules, approvals = [], [], [], []
    sessions = defaultdict(list)
    delivery_by_name = defaultdict(set)
    first_seen = {}
    terms = set().union(*(project_terms(name) for name in names))
    folders = set()
    user_attached = set()
    todos = []
    outcomes = []
    modeled_sessions = set()
    extracted = extracted or {}
    for row in [*rows, *(extra_rows or [])]:
        data = json.loads(row['data'])
        sid = row['session_id']
        cwd = versions.get(sid, ('', None))[1]
        if sid in extracted:
            if sid not in modeled_sessions:
                for project in extracted[sid]:
                    if project['id'] == project_id:
                        add_extracted(project, row, versions, inputs, deliveries, rules, todos, outcomes)
                modeled_sessions.add(sid)
            if row['kind'] != 'log':
                continue
        if sid in versions and not (IRRELEVANT.search(data['summary']) and not data.get('files')):
            sessions[sid].append((row, data))
        for item in data.get('inputs', []):
            ref = citation(row, item.get('ordinal'), item.get('evidence_time'))
            inputs.append(entry(available_path(item.get('path'), cwd), ref, text=item.get('text'), recorded=True))
        for item in data.get('rules', []):
            ref = citation(row, item.get('ordinal'), item.get('evidence_time'))
            rules.append({**item, 'ref': ref, 'recorded': True})
        for item in data.get('files', []) + data.get('deliverables', []):
            ref = citation(row, item.get('ordinal'), item.get('evidence_time'))
            path = available_path(item['path'], cwd) or item['path']
            pinned = item['status'] == '用户确认' and (
                row['kind'] == 'summary' or item.get('confirmed') is True
            ) and recorded_confirmation(index, sid, item.get('ordinal'), path, visible)
            inferred_status = '已交付' if item['status'] == '用户确认' and not pinned else item['status']
            if row['kind'] == 'summary' and inferred_status == '已交付' and PREVIEW_NAME.search(Path(path).stem):
                inferred_status = '草稿'
            if Path(path).is_absolute() and matches_project(str(Path(path).parent), terms):
                folders.add(str(Path(path).parent))
            if item['status'] != '用户确认' or row['kind'] == 'log':
                first_seen[path] = min(first_seen.get(path, ref['time']), ref['time'])
            deliveries.append(entry(path, ref, task=item.get('task'), status=inferred_status,
                                    explicit_task=bool(item.get('task')), final=item.get('final', False),
                                    pinned=pinned,
                                    recorded=row['kind'] == 'log', legacy=True))
            delivery_by_name[Path(path).name].add(path)

    raw_inputs, outputs, reused = set(), set(), set()
    for sid, project_rows in sessions.items():
        if sid in extracted:
            continue
        row = project_rows[0][0]
        _, cwd, title, name = versions[sid]
        focused = (matches_project((title or '') + ' ' + (name or ''), terms)
                   or (sid.startswith('kimi-work:') and len(project_rows) >= 2)
                   or (row['project_count'] == 1 and not sid.startswith('deepseek-harness:')))
        task_anchor = False
        task_ordinal = -100
        user_has_project = False
        previous_bot_delivery = []
        previous_bot_ordinal = -10
        previous_delivery = []
        previous_ordinal = -10
        names = {k for k in delivery_by_name if k}
        for message in messages_for(index, sid, project_rows, focused, terms):
            raw = message['content'] or ''
            text = visible(dict(message))
            ordinal = message['ordinal']
            ref = citation(row, ordinal, message['timestamp'])
            source_text = text or raw
            structured = chosen_rules(source_text) if message['role'] == 'user' else []
            if structured and (focused or ordinal - task_ordinal <= 65 or any(abs(ordinal - (data.get('ordinal') or -100)) <= 20 for _, data in project_rows)):
                rules.extend({**item, 'text': clean(item['text']), 'ref': ref} for item in structured)
                continue
            if sid.startswith('deepseek-harness:') and source_text.lstrip().startswith('[Reply from '):
                text = clean(source_text)
                rels = re.findall(r'`([^`\n]+\.' + EXTENSIONS + r')`', text, re.I)
                local = [path for value in paths_in(text) + rels if (path := available_path(value, cwd))]
                relevant = [path for path in local if matches_project(path, terms) or str(Path(path).parent) in folders]
                if relevant and DELIVERY_SIGNAL.search(text[:700]):
                    previous_bot_delivery = relevant
                    previous_bot_ordinal = ordinal
                continue
            if not text or NON_USER.match(text) or NON_USER.match(raw):
                continue
            path_matches = paths_in(raw if message['role'] == 'user' else text)
            path_matches += [match[1].strip() for match in re.finditer(r'\]\(([^()\r\n]+\.' + EXTENSIONS + r')\)', text, re.I)
                             if not match[1].startswith(('/', 'https:', 'http:'))]
            if not path_matches and '.' in text:
                for match in NAME.finditer(text):
                    if match[1] in names and len(delivery_by_name[match[1]]) == 1:
                        path_matches.extend(delivery_by_name[match[1]])
            path_matches = list(dict.fromkeys(path for value in path_matches if (path := available_path(value, cwd))))
            if message['role'] == 'assistant':
                prefix = re.sub(r'\s+', '', text[:450])
                if user_has_project and (focused or task_anchor or matches_project(text, terms)) and len(text) >= 200 and TEXT_DELIVERY.search(prefix) and not path_matches:
                    tag = TEXT_DELIVERY.search(prefix)[0]
                    deliveries.append(entry(None, ref, task=tag, text=clean(' '.join(text.split()))[:200],
                                            status='已交付', explicit_task=True))
                if not DELIVERY_SIGNAL.search(prefix) and not path_matches:
                    continue
                if not path_matches and ordinal - previous_bot_ordinal <= 5 and re.search(r'^(?:终版|定稿|最终版)|已交付|交付如下', prefix):
                    path_matches = previous_bot_delivery
                previous_delivery = []
                previous_ordinal = ordinal
                multiple_choices = bool(re.search(r'三款|三版|三种|供.*选|任选|选一|选定后', prefix)) and len(path_matches) >= 2
                for path in path_matches:
                    if not (focused or matches_project(path, terms) or str(Path(path).parent) in folders or path in first_seen):
                        continue
                    if path in raw_inputs and path not in outputs and path not in first_seen:
                        continue
                    if path in first_seen or DELIVERY_SIGNAL.search(prefix) or re.search(r'\]\([^)]*' + re.escape(Path(path).name) + r'\)', text):
                        outputs.add(path)
                        previous_delivery.append(path)
                        draft = multiple_choices or re.search(r'草稿|初稿', Path(path).stem) or PREVIEW_NAME.search(Path(path).stem)
                        final = bool(re.search(r'终版|定稿|终稿|最终版', Path(path).stem) or re.search(r'^(?:终版|定稿|最终版)', prefix))
                        deliveries.append(entry(path, ref, status='草稿' if draft and not final else '已交付', final=final))
                continue
            user_has_project = True
            task_anchor = matches_project(text, terms)
            competing_project = any(matches_project(Path(path).name, other, loose=False) for path in path_matches
                                    for other in project_terms_by_session.get(sid, []) if other)
            if not focused and competing_project and not any(matches_project(Path(path).name, terms, loose=False) for path in path_matches):
                task_ordinal = -100
                user_has_project = False
                continue
            if task_anchor:
                task_ordinal = ordinal
            if (ordinal - previous_ordinal <= 4 or path_matches) and confirms_delivery(text):
                chosen = [p for p in path_matches if p in outputs or p in first_seen]
                if not chosen and len(previous_delivery) == 1 and ordinal - previous_ordinal <= 4:
                    chosen = previous_delivery
                approvals.extend((path, ref) for path in chosen)
            for path in path_matches:
                if not (focused or matches_project(Path(path).name, terms) or path in first_seen):
                    continue
                if path in outputs or (path in first_seen and first_seen[path] < ref['time']):
                    reused.add(path)
                else:
                    raw_inputs.add(path)
                    inputs.append(entry(path, ref))
                    if ATTACHMENT.search(raw) and path in paths_in(raw):
                        user_attached.add(path)
            without_markup = re.sub(r'<(?:attachment|environment_context|system)>.*?</(?:attachment|environment_context|system)>', '', text, flags=re.S)
            if (focused or task_anchor) and len(without_markup) >= 450 and re.search(r'以下是|原文|会议纪要|会议转写|材料正文|粘贴|完整内容', without_markup[:300]):
                inputs.append(entry(None, ref, text=clean(' '.join(without_markup.split()))[:200]))
            if focused or task_anchor or ordinal - task_ordinal <= 22:
                rules.extend({**item, 'ref': ref} for item in quoted_rules(without_markup))

    for item in deliveries:
        path = item.get('path')
        if path in reused:
            item['reused'] = True
    # A document first provided by the user stays an input unless explicitly delivered as a task output.
    deliveries = [item for item in deliveries if item.get('explicit_task') or item.get('pinned')
                  or item.get('path') in outputs or item.get('path') not in raw_inputs]
    input_refs = {(source['path'], source['ref']['session_id'], source['ref']['ordinal']) for source in inputs}
    deliveries = [item for item in deliveries if item.get('explicit_task') or item.get('pinned')
                  or (item.get('path'), item['ref']['session_id'], item['ref']['ordinal']) not in input_refs]
    deliveries = [item for item in deliveries if not item.get('path') or
                  (Path(item['path']).suffix and (item.get('explicit_task') or item.get('final')
                   or item['path'] not in user_attached or item.get('status') != '用户确认'
                   or '终版' in Path(item['path']).stem))]
    result = finish_entries(inputs, deliveries, rules, approvals)
    result['extraction'] = '混合' if modeled_sessions and set(versions) - modeled_sessions else '模型核对' if modeled_sessions else '规则推断'
    result['model_todos'] = todos
    result['model_outcomes'] = outcomes
    result['model_sessions'] = sorted(modeled_sessions)
    return result


def load(db, data_dir, project_id, visible, clean, force=False, lazy=False):
    rows = work_rows(db, project_id)
    session_ids = {row['session_id'] for row in rows if row['session_id']}
    aliases = {row[0] for row in db.execute('SELECT old_id FROM project_redirects WHERE project_id=?', (project_id,))}
    lookup_ids = sorted({project_id, *aliases})
    placeholders = ','.join('?' for _ in lookup_ids)
    extracted_rows = db.execute(f"""SELECT e.session_id,e.revision,e.extracted_at FROM packet_extractions e
        WHERE EXISTS (SELECT 1 FROM work w WHERE w.session_id=e.session_id AND w.project_id IN ({placeholders}))
              OR e.session_id IN (SELECT a.session_id FROM packet_extraction_assignments a
                                  WHERE a.project_id IN ({placeholders})) ORDER BY e.session_id""",
        (*lookup_ids, *lookup_ids)).fetchall()
    previous = db.execute('SELECT data, signature FROM project_packets WHERE project_id=?', (project_id,)).fetchone()
    relocations = {row[0]: row[1] for row in db.execute('SELECT old_path,path FROM packet_path_relocations ORDER BY old_path')}
    notes_revision = db.execute('SELECT MAX(scanned) FROM vault_notes').fetchone()[0]
    work_signature = hashlib.sha256(json.dumps([VERSION, [tuple(row) for row in rows], relocations,
                                                [tuple(row) for row in extracted_rows], notes_revision], ensure_ascii=False).encode()).hexdigest()
    if lazy and previous and not force:
        if previous['signature'].startswith(work_signature + ':'):
            return json.loads(previous['data']), False
    path = data_dir / 'agentsview' / 'sessions.db'
    index = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) if path.is_file() else None
    try:
        if index:
            index.row_factory = sqlite3.Row
            index.execute('BEGIN')
        sessions = session_ids | {row['session_id'] for row in extracted_rows}
        versions = source_versions(index, sessions)
        names = [row[0] for row in db.execute('SELECT name FROM projects WHERE id=? UNION SELECT alias FROM project_aliases WHERE project_id=?',
                                             (project_id, project_id))]
        signature = work_signature + ':' + hashlib.sha256(json.dumps([versions, names], sort_keys=True,
                                                                     ensure_ascii=False).encode()).hexdigest()
        if previous and previous['signature'] == signature and not force:
            return json.loads(previous['data']), False
        alternatives = defaultdict(list)
        own_terms = set().union(*(project_terms(name) for name in names))
        for sid in sessions:
            for row in db.execute('SELECT DISTINCT p.name FROM work w JOIN projects p ON p.id=w.project_id '
                                  'WHERE w.session_id=? AND w.project_id<>?', (sid, project_id)):
                terms = project_terms(row[0]) - own_terms
                if terms:
                    alternatives[sid].append(terms)
        extracted = {}
        for item in extracted_rows:
            sid = item['session_id']
            if sid not in versions:
                continue
            raw = db.execute('SELECT data FROM packet_extractions WHERE session_id=? AND revision=?',
                             (sid, item['revision'])).fetchone()
            if not raw:
                continue
            value = json.loads(raw[0])
            if value.get('archive_revision') == versions[sid][0]:
                for project in value['projects']:
                    resolved = project['id']
                    while resolved and (redirect := db.execute('SELECT project_id FROM project_redirects WHERE old_id=?',
                                                               (resolved,)).fetchone()):
                        resolved = redirect[0]
                    project['id'] = resolved
                extracted[sid] = value['projects']
        indexed_at = {row['session_id']: row['extracted_at'] for row in extracted_rows}
        new_rows = [{'session_id': sid, 'source': sid.split(':', 1)[0], 'occurred': indexed_at[sid], 'kind': 'model',
                     'data': '{}'} for sid in sorted(extracted.keys() - session_ids)]
        result = build(rows, versions, index, visible, clean, names, alternatives, extracted, project_id, new_rows)
        for cwd in sorted({metadata[1] for metadata in versions.values() if metadata[1]}):
            for note in notes.project_inputs(db, cwd):
                result['inputs'].append(entry(note['path'], {'source': 'obsidian', 'session_id': 'obsidian',
                    'ordinal': None, 'time': note['scanned']}, kind='笔记', description=note['title']))
        for key in ('inputs', 'deliverables', 'history'):
            paths = set()
            items = []
            for item in result[key]:
                old = item.get('path')
                if old in relocations:
                    item = {**item, 'path': relocations[old], 'original_path': old}
                if item.get('path') and item['path'] in paths:
                    continue
                if item.get('path'):
                    paths.add(item['path'])
                items.append(item)
            result[key] = items
    finally:
        if index:
            index.close()
    with db:
        values = (signature, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat())
        if previous:
            updated = db.execute('UPDATE project_packets SET signature=?,data=?,built=? WHERE project_id=? AND signature=?',
                                 (*values, project_id, previous['signature'])).rowcount
        else:
            updated = db.execute('INSERT OR IGNORE INTO project_packets VALUES(?,?,?,?)', (project_id, *values)).rowcount
    return result, bool(updated)


def authorized_paths(db, value):
    return [row[0] for row in db.execute("""
        SELECT p.project_id FROM project_packets p
        WHERE EXISTS (SELECT 1 FROM json_each(p.data,'$.inputs') i WHERE json_extract(i.value,'$.path')=?)
           OR EXISTS (SELECT 1 FROM json_each(p.data,'$.deliverables') d WHERE json_extract(d.value,'$.path')=?)
        """, (value, value))]


def summary_status(db, value):
    try:
        path = Path(value)
        if path.suffix.lower() not in file_text.SUPPORTED:
            return '格式待支持'
        st = path.stat()
    except OSError:
        return '本机文件未找到'
    fingerprint = f'{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}'
    state = db.execute('SELECT status FROM packet_file_failures WHERE path=? AND fingerprint=?', (value, fingerprint)).fetchone()
    return {'no_text': '未提取到正文', 'unreadable': '文件提取失败'}.get(state['status'] if state else '', '摘要待生成')


def prepare_file(db, value, clean, force=False):
    try:
        path = file_text.document_path(value)
        before = path.stat()
        fingerprint = f'{before.st_dev}:{before.st_ino}:{before.st_size}:{before.st_mtime_ns}:{before.st_ctime_ns}'
    except FileNotFoundError:
        return 'missing', None, None
    except PermissionError:
        return 'unreadable', None, None
    except OSError:
        return 'unavailable', None, None
    except ValueError:
        return 'unsupported', None, None
    row = db.execute('SELECT fingerprint, digest FROM packet_file_paths WHERE path=?', (value,)).fetchone()
    failure = db.execute('SELECT status FROM packet_file_failures WHERE path=? AND fingerprint=?', (value, fingerprint)).fetchone()
    if failure and not force:
        return failure['status'], None, None
    if row and row['fingerprint'] == fingerprint and not force:
        summary = db.execute('SELECT summary, method FROM packet_file_summaries WHERE fingerprint=?', (row['digest'],)).fetchone()
        if summary:
            return 'cached', summary['summary'], summary['method']
    try:
        data = file_text.read_bytes(path)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return 'changed', None, None
        digest = hashlib.sha256(path.suffix.lower().encode() + b'\0' + data).hexdigest()
        summary = db.execute('SELECT summary, method FROM packet_file_summaries WHERE fingerprint=?', (digest,)).fetchone()
        if summary and not force:
            status, text, method = 'cached', summary['summary'], summary['method']
        else:
            parts = file_text.extract(path, data, preview=True)
            text = clean(file_text.summary_from_parts(parts))
            if not text:
                with db:
                    db.execute('INSERT OR REPLACE INTO packet_file_failures VALUES(?,?,?)', (value, fingerprint, 'no_text'))
                return 'no_text', None, None
            method = 'extractive'
            status = 'built'
    except (OSError, ValueError):
        with db:
            db.execute('INSERT OR REPLACE INTO packet_file_failures VALUES(?,?,?)', (value, fingerprint, 'unreadable'))
        return 'unreadable', None, None
    with db:
        db.execute('INSERT INTO packet_file_summaries VALUES(?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET '
                   'summary=excluded.summary,method=excluded.method WHERE excluded.method=\'model\' OR packet_file_summaries.method<>\'model\'',
                   (digest, text, method))
        db.execute('INSERT INTO packet_file_paths VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET '
                   'fingerprint=excluded.fingerprint,digest=excluded.digest', (value, fingerprint, digest))
        db.execute('DELETE FROM packet_file_failures WHERE path=?', (value,))
    return status, text, method


def file_summary(db, value):
    row = db.execute('''SELECT p.fingerprint,s.summary,s.method FROM packet_file_paths p
                        JOIN packet_file_summaries s ON s.fingerprint=p.digest WHERE p.path=?''', (value,)).fetchone()
    if not row:
        return None
    try:
        st = Path(value).stat()
    except OSError:
        return None
    if row['fingerprint'] != f'{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}':
        return None
    return row['summary']


def active_projects(db, days=60, min_records=2):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ''
    return db.execute('''SELECT p.id,COUNT(w.id) AS records,MAX(w.occurred) AS updated FROM projects p
                         JOIN work w ON w.project_id=p.id
                         LEFT JOIN project_redirects r ON r.old_id=p.id WHERE r.old_id IS NULL GROUP BY p.id
                         HAVING COUNT(w.id)>=? AND MAX(w.occurred)>=? ORDER BY updated DESC,p.id''', (min_records, cutoff))
