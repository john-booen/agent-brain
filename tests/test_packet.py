import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from time import sleep
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

spec = importlib.util.spec_from_file_location('brain', Path(__file__).resolve().parents[1] / 'brain.py')
brain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brain)


class PacketTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'data'
        ctx = patch.object(brain, 'DATA', self.data)
        ctx.start()
        self.addCleanup(ctx.stop)
        self.db = brain.connect()
        self.addCleanup(self.db.close)

    def tool(self, name, **args):
        return brain.call_tool(self.db, name, args)

    def test_optional_rule_topic_in_new_and_legacy_records(self):
        file = self.root / 'source.md'
        file.write_text('# Figures\nSource figures.')
        args = dict(project='Atlas', create_project=True, summary='Review figures.', source='codex', work_id='legacy',
                    inputs=[{'path': str(file)}], rules=[{'text': '对外口径保守'}])
        record = self.tool('log_work', **args)
        stored = json.loads(self.db.execute('SELECT data FROM work WHERE id=?', ('legacy',)).fetchone()[0])
        self.assertEqual(stored['rules'][0].pop('topic'), '保守口径')
        with self.db:
            self.db.execute('UPDATE work SET data=? WHERE id=?', (json.dumps(stored), 'legacy'))
        for section in (None, 'rules'):
            card = self.tool('get_project', project=record['project_id'], **({'section': section} if section else {}))
            self.assertIn('对外口径保守', card['card'])
            self.assertLessEqual(brain.response_size(card), brain.QUERY_BYTES)
        self.assertIn('Source figures', self.tool('read_file', project='Atlas', path=file.name, section='Figures')['text'])
        self.assertFalse(self.tool('log_work', **args)['written'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required to exercise the collector')
    def test_collection_reports_refresh_failure_and_recovery(self):
        archive = self.root / 'mock-agentsview'
        archive.write_text('#!/bin/sh\nif [ "$2" = list ]; then echo \'{"agents":[]}\'; fi\n')
        archive.chmod(0o700)
        builder = self.root / 'mock-python'
        builder.write_text('#!/bin/sh\nexit 1\n')
        builder.chmod(0o700)
        todo = self.root / 'USER_TODO.md'
        todo.write_text('# Pending work\n')
        self.tool('log_work', project='Atlas', create_project=True, summary='Initial record.', source='codex')
        env = {**os.environ, 'HOME': str(self.root), 'AGENT_BRAIN_DATA': str(self.data), 'AGENT_BRAIN_SYNC_DAEMON': '0',
               'AGENTSVIEW_BIN': str(archive), 'AGENT_BRAIN_PACKET_PYTHON': str(builder), 'AGENT_BRAIN_USER_TODO': str(todo)}
        cmd = ['node', str(Path(brain.__file__).with_name('collect.mjs')), 'codex']
        for fail_count in range(1, 4):
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads((self.data / 'coverage.json').read_text())
            self.assertEqual(report['exit'], 0)
            self.assertEqual(report['packetRefresh']['consecutiveFailures'], fail_count)
            self.assertIn('可能过期', self.tool('get_project', project='Atlas')['coverage'])
            self.assertIn('可能过期', self.tool('list_projects')['coverage'])
            self.assertEqual('连续失败' in todo.read_text(), fail_count >= 3)
        builder.write_text('#!/bin/sh\nexit 0\n')
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(todo.read_text(), '# Pending work\n')
        self.assertNotIn('coverage', self.tool('get_project', project='Atlas'))
        events = [json.loads(line) for line in (self.data / 'health.log').read_text().splitlines()]
        self.assertEqual([event['status'] for event in events if event['event'] == 'packet_refresh'],
                         ['failed', 'failed', 'failed', 'ok'])

        source_dir = self.root / '.codex' / 'sessions'
        source_dir.mkdir(parents=True)
        transcript = source_dir / 'rollout-fictional.jsonl'
        transcript.write_text('invalid JSON\n')
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        collected = json.loads((self.data / 'coverage.json').read_text())
        self.assertEqual(collected['sources']['codex']['skippedLines'], 1)
        self.assertEqual(self.tool('get_project', project='Atlas')['coverage'], '部分来源采集不完整')
        self.assertEqual(self.tool('list_projects')['coverage'], '部分来源采集不完整')
        events = [json.loads(line) for line in (self.data / 'health.log').read_text().splitlines()]
        partial, = [event for event in events if event['event'] == 'source_collection']
        self.assertEqual(partial['skippedLines'], 1)
        transcript.write_text('{"fictional":true}\n')
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('coverage', self.tool('get_project', project='Atlas'))

        builder.write_text('#!/bin/sh\ncase "$*" in *--files*) exit 7;; esac\n')
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads((self.data / 'coverage.json').read_text())
        self.assertEqual(report['packetRefresh']['status'], 'ok')
        self.assertEqual(report['fileRefresh']['status'], 'failed')
        self.assertIn('文件摘要可能过期', self.tool('get_project', project='Atlas')['coverage'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required to exercise the collector')
    def test_collection_stages_cowork_short_folder_and_older_cli_transcript(self):
        archive = self.root / 'mock-agentsview'
        archive.write_text('#!/bin/sh\nif [ "$2" = list ]; then echo \'{"agents":[]}\'; fi\n')
        archive.chmod(0o700)
        home = self.root / 'Library/Application Support/Claude-3p/local-agent-mode-sessions/org/workspace'
        local_id = '12345678-1234-4123-a123-123456789012'
        current_id = '22345678-1234-4123-a123-123456789012'
        previous_id = '32345678-1234-4123-a123-123456789012'
        short = home / local_id[:8] / '.claude/projects/encoded-output'
        children = short / current_id / 'subagents/nested'
        children.mkdir(parents=True)
        meta = home / f'local_{local_id}.json'
        original = {'cliSessionId': current_id, 'title': 'Atlas', 'systemPrompt': 'Private fixture'}
        meta.write_text(json.dumps(original))
        for path in (short / f'{current_id}.jsonl', short / f'{previous_id}.jsonl', children / 'agent-fixture.jsonl'):
            path.write_text('{"type":"user"}\n')
        env = {**os.environ, 'HOME': str(self.root), 'AGENT_BRAIN_DATA': str(self.data),
               'AGENT_BRAIN_SYNC_DAEMON': '0', 'AGENTSVIEW_BIN': str(archive), 'AGENT_BRAIN_PACKET_PYTHON': ''}
        command = ['node', str(Path(brain.__file__).with_name('collect.mjs')), 'cowork']
        subprocess.run(command, env=env, check=True, capture_output=True)
        staged = self.data / 'sources/cowork/1/org/workspace'
        current = staged / f'local_{local_id}' / '.claude/projects/encoded-output'
        self.assertTrue((current / f'{current_id}.jsonl').is_file())
        self.assertTrue((current / current_id / 'subagents/nested/agent-fixture.jsonl').is_file())
        older = staged / f'additional-transcripts/{local_id}/local_{previous_id}'
        metadata = json.loads(older.with_suffix('.json').read_text())
        self.assertEqual(metadata, {'cliSessionId': previous_id, 'title': 'Atlas'})
        self.assertTrue((older / '.claude/projects/encoded-output' / f'{previous_id}.jsonl').is_file())
        self.assertEqual(json.loads(meta.read_text()), original)

    def test_confirmed_version_scoped_requirements_and_read_file(self):
        v1 = self.root / 'old.md'
        v2 = self.root / 'new.md'
        v1.write_text('# Valuation\n' + 'Adjusted revenue × multiple.\n' * 180)
        v2.write_text('# Valuation\nDifferent assumptions.')
        sid = 'codex:fictional-confirmation'
        self.archive_messages(sid, 'Atlas', [('user', f'这份 {v1.name} 作为终版')])
        initial = self.tool('log_work', project='Atlas', create_project=True, summary='Prior reviewed version', source='codex', work_id='old', session_id=sid,
                            inputs=[{'text': 'User source material'}],
                            deliverables=[{'path': str(v1), 'task': 'Valuation', 'confirmed': True, 'ordinal': 0}],
                            rules=[{'text': 'Use internal figures.', 'topic': 'scope', 'scope': '项目'}])
        with patch.object(brain, 'now', return_value='2030-01-01T00:00:00+00:00'):
            self.tool('log_work', project='Atlas', create_project=True, summary='New proposal', source='kimi-work', work_id='new',
                      deliverables=[{'path': str(v2), 'task': 'Valuation', 'final': True}],
                      rules=[{'text': 'Use public figures.', 'topic': 'scope', 'scope': '对外版本'}])
        card = self.tool('get_project', project='Atlas')
        self.assertIn('old.md', card['card'])
        self.assertNotIn('new.md', card['card'])
        self.assertIn('new.md', self.tool('get_project', project='Atlas', section='history')['card'])
        requirements = self.tool('get_project', project='Atlas', section='rules')['card']
        self.assertIn('internal figures', requirements)
        self.assertIn('public figures', requirements)
        result = self.tool('read_file', project=initial['project_id'], path=str(v1), section='Valuation')
        self.assertIn('Adjusted revenue', result['text'])
        self.assertLessEqual(brain.response_size(result), brain.QUERY_BYTES)
        self.assertGreater(result['next_offset'], 0)
        self.assertEqual(self.tool('read_file', project='Atlas', path=v1.name, section='Valuation'), result)
        with self.assertRaisesRegex(ValueError, '登记'):
            self.tool('read_file', project='Atlas', path=str(v2))
        v1.unlink()
        v1.symlink_to(v2)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.tool('read_file', project='Atlas', path=str(v1))

    def test_file_name_must_identify_one_registered_document(self):
        paths = [self.root / version / 'source.md' for version in ('first', 'second')]
        for path in paths:
            path.parent.mkdir()
            path.write_text(f'# Figures\n{path.parent.name}')
        self.tool('log_work', project='Atlas', create_project=True, summary='Registered two separate sources', source='codex',
                  inputs=[{'path': str(path)} for path in paths])
        with self.assertRaisesRegex(ValueError, '多份登记文件'):
            self.tool('read_file', project='Atlas', path='source.md', section='Figures')
        self.assertIn('first', self.tool('read_file', project='Atlas', path=str(paths[0]), section='Figures')['text'])

    def archive_messages(self, sid, project, messages):
        archive = self.data / 'agentsview'
        archive.mkdir()
        with sqlite3.connect(archive / 'sessions.db') as index:
            index.executescript('''CREATE TABLE sessions(id,cwd,transcript_revision,display_name,session_name,
                                                        message_count,user_message_count,started_at);
                                  CREATE TABLE messages(session_id,ordinal,role,timestamp,content,dialogue_text,is_system);''')
            index.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',
                          (sid,str(self.root),'1',project,project,len(messages),sum(role=='user' for role,_ in messages),
                           '2026-01-01T00:00:00Z'))
            for n, (role, value) in enumerate(messages):
                index.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,0)', (sid, n, role, f'2026-01-01T00:00:0{n}Z', value, value))

    def test_archive_extraction_keeps_two_deliverables_and_user_evidence(self):
        sid = 'kimi-work:fictional-packet'
        doc = self.root / 'Atlas description.md'
        first = self.root / 'Atlas meeting brief-草稿.md'
        final = self.root / 'Atlas meeting brief-终版.md'
        other = self.root / 'Atlas outline.md'
        for f in (doc, first, final, other):
            f.write_text('# Outline\nAn independently created project test document.')
        self.archive_messages(sid, 'Atlas', [
            ('user', f'<attachment>{json.dumps({"path": str(doc)})}</attachment>\n请按原始材料整理。不要添加公司名字。'),
            ('assistant', f'已交付 [初稿]({first})'),
            ('user', '好的，用这版改成正式版'),
            ('assistant', f'已交付 [终版]({final})'),
            ('user', '可以'),
            ('assistant', f'已交付 [现场提纲]({other})'),
        ])
        record = self.tool('log_work', project='Atlas', create_project=True, summary='Completed the project', source='kimi-work', session_id=sid)
        payload, _ = brain.packet.load(self.db, self.data, record['project_id'], brain.visible, brain.clean)
        self.assertEqual({x['path'] for x in payload['deliverables']}, {str(final), str(other)})
        self.assertTrue(next(x for x in payload['deliverables'] if x['path'] == str(final))['pinned'])
        self.assertIn(str(doc), {x['path'] for x in payload['inputs']})
        self.assertIn('不要添加公司名字', {x['text'] for x in payload['rules']})
        saved_draft = next(x for x in payload['history'] if x.get('path') == str(first))
        self.assertEqual(saved_draft['status'], '草稿')
        with patch.object(brain.packet, 'build', side_effect=AssertionError('must use cached packet')):
            self.assertIn('终版', self.tool('get_project', project='Atlas')['card'])

    def test_project_redirect_and_record_move_preserve_original_ids(self):
        a = self.tool('log_work', project='Atlas', create_project=True, summary='Initial progress', source='codex', work_id='one')
        b = self.tool('log_work', project='Atlas materials', create_project=True, summary='Generated materials', source='codex', work_id='two')
        c = self.tool('log_work', project='Separate event', create_project=True, summary='Event plan', source='codex', work_id='three')
        brain.merge_projects(self.db, 'Atlas', ['Atlas materials'], 'Same project and materials')
        self.assertEqual(self.tool('get_project', project=b['project_id'])['id'], a['project_id'])
        self.assertEqual(len(brain.project_list(self.db)), 2)
        retry = self.tool('log_work', project='Atlas materials', create_project=True, summary='Generated materials', source='codex', work_id='two')
        self.assertFalse(retry['written'])
        brain.move_project_record(self.db, 'two', 'Separate event', 'Matched the event handoff')
        self.assertEqual(self.db.execute('SELECT project_id FROM work WHERE id=?', ('two',)).fetchone()[0], c['project_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM work').fetchone()[0], 3)

    def test_confirmation_without_user_evidence_stays_unconfirmed(self):
        for name, fields in [('Status only', {'status': '用户确认'}), ('No message', {'confirmed': True})]:
            file = self.root / f'{name}.md'
            file.write_text('# Figures\nSource figures.')
            self.tool('log_work', project=name, create_project=True, summary='Record without confirmation evidence.', source='codex',
                      deliverables=[{'path': str(file), 'task': 'Figures', **fields}])
            self.assertNotIn('用户确认', self.tool('get_project', project=name, section='deliverables')['card'])

    def test_same_second_cross_client_handoff_uses_insertion_order(self):
        old = self.root / 'original.md'
        new = self.root / 'revision.md'
        old.write_text('# Checklist\nOwner is Amy.')
        new.write_text('# Checklist\nOwner is Sam.')
        with patch.object(brain, 'now', return_value='2026-01-01T00:00:00+00:00'):
            for source, work_id, path, owner in (('codex','a-original',old,'Amy'),('kimi-work','z-revision',new,'Sam')):
                self.tool('log_work', project='Atlas', create_project=True, source=source, work_id=work_id,
                          summary=f'{owner} owns the checklist.',
                          deliverables=[{'path':str(path),'task':'Checklist','final':True}],
                          rules=[{'text':f'Ask {owner} for sign-off.','topic':'reviewer'}])
        self.assertIn(new.name, self.tool('get_project',project='Atlas',section='deliverables')['card'])
        self.assertIn('Owner is Sam.', self.tool('read_file',project='Atlas',path=new.name)['text'])
        self.assertIn(old.name, self.tool('get_project',project='Atlas',section='history')['card'])
        self.assertIn('Ask Sam', self.tool('get_project',project='Atlas',section='rules')['card'])

    def test_work_log_citations_use_source_and_mark_unverified_items(self):
        sid = 'cowork:fictional-source-date'
        report = self.root / 'export' / 'report.md'
        other = self.root / 'export' / 'other.md'
        sheet = self.root / 'Order report (EN).xlsx'
        self.archive_messages(sid, 'Atlas', [('user','Use the original date.'),
            ('assistant',f'Ready at `export/report.md`. [See spreadsheet](computer://{sheet})'),
            ('assistant','[Skill]')])
        log = self.tool('log_work', project='Atlas', create_project=True, summary='Register a prior delivery.',
                        source='codex', session_id=sid, deliverables=[
                            {'path':str(report),'task':'Report','ordinal':1},
                            {'path':str(sheet),'task':'Sheet','ordinal':1},
                            {'path':str(other),'task':'Other','ordinal':1}], rules=[
                            {'topic':'date','text':'Use the original date.','ordinal':0},
                            {'topic':'format','text':'Use a table.','ordinal':1}], conclusions=[
                            {'topic':'arrived','text':'The file arrived.','ordinal':1},
                            {'topic':'unseen','text':'Missing evidence.','ordinal':99},
                            {'topic':'skill','text':'A skill notice.','ordinal':2}])
        for section, snippets in {
            'deliverables': (f'cowork · {sid}#1 · 2026-01-01T00:00:01Z','Other ·','原文待核'),
            'rules': (f'cowork · {sid}#0 · 2026-01-01T00:00:00Z','Use a table.','原文待核'),
            'conclusions': (f'cowork · {sid}#1 · 2026-01-01T00:00:01Z','Missing evidence.','原文待核'),
        }.items():
            card = self.tool('get_project', project=log['project_id'], section=section)['card']
            for snippet in snippets:
                self.assertIn(snippet, card)
            self.assertNotIn(f'{sid}#99', card)
            self.assertNotIn(f'{sid}#2', card)
            if section == 'deliverables':
                self.assertTrue(any(sheet.name in line and f'{sid}#1' in line for line in card.splitlines()))

    def test_pending_project_citation_is_rechecked_after_import(self):
        sid = 'codex:fictional-delayed-import'
        report = self.root / 'report.md'
        report.write_text('A delivered report.')
        self.tool('log_work', project='Atlas', create_project=True, summary='Delivery waiting for import.',
                  source='codex', session_id=sid, deliverables=[{'path':str(report),'task':'Report','ordinal':0}])
        card = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn('原文待核', card)
        self.assertNotIn(f'{sid}#0', card)
        self.assertIn('A delivered report.', self.tool('read_file', project='Atlas', path=report.name)['text'])
        self.archive_messages(sid, 'Atlas', [('assistant', f'Ready at [{report.name}]({report}).')])
        card = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn(f'{sid}#0', card)
        self.assertNotIn('原文待核', card)

    def test_antigravity_can_log_without_becoming_an_archive_source(self):
        result = self.tool('log_work', project='Atlas', create_project=True, summary='Fictional handoff.', source='antigravity')
        self.assertTrue(result['written'])
        self.assertIn('antigravity', self.tool('get_project', project='Atlas')['card'])
        self.assertNotIn('antigravity', brain.SOURCES)

    def test_bound_client_normalizes_and_records_source_mismatches(self):
        with patch.dict(os.environ, {'AGENT_BRAIN_SOURCE': 'cowork'}):
            args = dict(project='Atlas', create_project=True, summary='Fictional handoff.', work_id='handoff')
            self.assertTrue(self.tool('log_work', source='codex', **args)['written'])
            self.assertFalse(self.tool('log_work', **args)['written'])
        self.assertEqual(self.db.execute('SELECT source FROM work').fetchone()[0], 'cowork')
        event = json.loads((self.data / 'health.log').read_text())
        self.assertEqual((event['event'], event['source'], event['requested_source']),
                         ('log_source_mismatch', 'cowork', 'codex'))

    def test_similar_names_require_a_choice_without_writing(self):
        args = dict(summary='Fictional handoff.', source='codex')
        first = self.tool('log_work', project='Fictional Atlas', create_project=True, **args)
        result = self.tool('log_work', project='Fictional Atlas 项目', **args)
        self.assertEqual(result['status'], 'choose_project')
        self.assertIn(first['project_id'], {item['id'] for item in result['projects']})
        self.assertEqual(self.tool('log_work', project='Unrelated', **args)['status'], 'new_project')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM work').fetchone()[0], 1)
        self.assertFalse(self.tool('log_work', project=first['project_id'], **args)['created_project'])

    def test_advertised_mcp_tools_fit_the_schema_budget(self):
        tools = brain.mcp_tools()
        self.assertEqual(len(tools), 8)
        self.assertLessEqual(len(brain.json_text({'tools': tools})), 3000)
        log = next(tool for tool in tools if tool['name'] == 'log_work')
        self.assertLessEqual(len(brain.json_text(log)), 1000)
        self.assertEqual(log['inputSchema']['required'], ['project', 'summary'])

    def test_model_extraction_uses_evidence_and_reuses_unchanged_session(self):
        sid = 'codex:fictional-model'
        brief = self.root / 'Atlas brief.md'
        report = self.root / 'Atlas report.md'
        self.archive_messages(sid, 'Atlas', [
            ('user', f'Atlas source: {brief}\n<file_contents>PRIVATE_FILE_BODY</file_contents>'),
            ('user', 'Use the conservative range for external versions.'),
            ('assistant', f'Delivered [{report.name}]({report}).'),
            ('user', 'I accept the Atlas report as final.'),
        ])
        project = self.tool('log_work', project='Atlas', create_project=True, summary='Atlas handoff.',
                            source='codex', session_id=sid)['project_id']
        answer = {'projects': [{
            'id': project, 'ordinal': 2, 'summary': 'Atlas report delivered.',
            'inputs': [{'path': str(brief), 'ordinal': 0}],
            'deliverables': [{'path': str(report), 'task': 'Report', 'ordinal': 2, 'final': True,
                              'confirmation': 3, 'confirmation_quote': 'I accept the Atlas report as final.'},
                             {'path': str(self.root / 'invented.md'), 'task': 'Other', 'ordinal': 2}],
            'rules': [{'quote': 'Use the conservative range', 'scope': '对外版本', 'topic': 'figures', 'ordinal': 1},
                      {'quote': 'Delivered', 'scope': '项目', 'topic': 'false', 'ordinal': 2}],
            'todos': [],
        }]}
        answer['projects'][0]['deliverables'][1]['quote'] = 'Delivered'
        with patch.object(brain, 'model', return_value=answer) as model:
            stats = brain.extraction.process(self.db, self.data, [sid], brain.visible, brain.packet.paths_in,
                                             brain.json_text, brain.clean, model, 20000)
            self.assertEqual((stats['processed'], stats['rejected']), (1, 2))
            self.assertNotIn('PRIVATE_FILE_BODY', model.call_args.args[0][1]['content'])
            stats = brain.extraction.process(self.db, self.data, [sid], brain.visible, brain.packet.paths_in,
                                             brain.json_text, brain.clean, model, 20000)
            self.assertEqual(stats['cached'], 1)
            self.assertEqual(model.call_count, 1)
        card = self.tool('get_project', project='Atlas')
        self.assertEqual(card['evidence'], '模型核对')
        self.assertIn('用户确认', self.tool('get_project', project='Atlas', section='deliverables')['card'])
        self.assertIn('Use the conservative range', self.tool('get_project', project='Atlas', section='rules')['card'])

        model_count = self.db.execute('SELECT COUNT(*) FROM packet_extractions').fetchone()[0]
        with patch.object(brain, 'model', return_value={'projects': [
            {'id': project, 'ordinal': 99, 'summary': 'Unverifiable evidence.'},
        ]}) as invalid_model:
            stats = brain.extraction.process(
                self.db, self.data, [sid], brain.visible, brain.packet.paths_in, brain.json_text, brain.clean,
                invalid_model, 40000, force=True,
            )
        self.assertEqual((stats['processed'], stats['failed'], stats['rejected']), (0, 1, 1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM packet_extractions').fetchone()[0], model_count)
        self.assertIn('用户确认', self.tool('get_project', project='Atlas', section='deliverables')['card'])

    def test_model_extracts_selected_answer_without_passing_question_options(self):
        sid = 'deepseek-harness:fictional-selection'
        self.archive_messages(sid, 'Atlas', [('user', json.dumps({
            'kind': 'answer_to_pending_question',
            'questions': [{'id': 'a', 'question':'Private prompt', 'options': [
                {'label': 'picked answer','description':'Use the small room. <document_body>PRIVATE_BODY</document_body>'},
                {'label': 'unselected option','description':'PRIVATE_UNSELECTED'}]},
                {'id':'b','options':[{'label':'picked answer','description':'PRIVATE_OTHER_QUESTION'}]}],
            'answers': [{'id': 'a', 'selected': ['picked answer']}],
        })), ('assistant', 'Delivered [report](research/fictional-report.md).')])
        with sqlite3.connect(self.data / 'agentsview/sessions.db') as index:
            index.row_factory = sqlite3.Row
            index.execute("UPDATE messages SET is_system=1 WHERE ordinal=0")
            evidence = brain.extraction.transcript(index, sid, brain.visible, brain.packet.paths_in, brain.json_text)
            handoff = brain.extraction.transcript(index,sid,brain.visible,brain.packet.paths_in,brain.json_text,
                                                  focused=True,outbound=True)
        self.assertEqual(evidence[0]['text'], 'User selected: picked answer')
        self.assertEqual(evidence[1]['files'], ['research/fictional-report.md'])
        self.assertEqual(handoff[0]['text'], 'User selected: picked answer · Use the small room.')
        self.assertNotIn('PRIVATE',brain.json_text(handoff))
        self.assertNotIn('Private prompt',brain.json_text(handoff))

    def test_model_extraction_retains_inline_and_line_anchored_file_refs(self):
        sid = 'codex:fictional-refs'
        self.archive_messages(sid, 'Atlas', [('user', '<attachment>{"path":"/tmp/fixture.md","body":"private-source /tmp/private-body.md"}</attachment>'), ('assistant', """
Delivered `research/report.md` and [summary](/tmp/atlas.md:42).
<tool_result>`research/private-tool.md`</tool_result>
<oai-mem-citation>[memory](/tmp/private-memory.md)</oai-mem-citation>
""")])
        with sqlite3.connect(self.data / 'agentsview/sessions.db') as index:
            index.row_factory = sqlite3.Row
            attachment, message = brain.extraction.transcript(index, sid, brain.visible, brain.packet.paths_in, brain.json_text)
        self.assertEqual(attachment['files'], ['/tmp/fixture.md'])
        self.assertNotIn('private-', brain.json_text(attachment))
        self.assertEqual(set(message['files']), {'research/report.md', '/tmp/atlas.md'})
        self.assertNotIn('private-', message['text'])

    def test_model_transcript_excludes_client_control_messages(self):
        session = 'codex:fictional-controls'
        self.archive_messages(session, 'Atlas', [
            ('user', '<codex_internal_context>internal</codex_internal_context>'),
            ('user', '<external_codex_apps_open_page>{}</external_codex_apps_open_page>'),
            ('user', 'Use a plain-text daily schedule.'),
        ])
        with sqlite3.connect(self.data / 'agentsview/sessions.db') as index:
            index.row_factory = sqlite3.Row
            messages = brain.extraction.transcript(index, session, brain.visible, brain.packet.paths_in, brain.json_text)
        self.assertEqual([message['ordinal'] for message in messages], [2])

    def test_usage_log_contains_only_metadata(self):
        brain.usage.record(self.data, 'codex', 'get_project',
                           {'id': 'fictional'}, 'private card body', False, lambda _: 4)
        body = (self.data / 'usage.jsonl').read_text()
        assert 'private card body' not in body
        self.assertEqual(json.loads(body)['tokens'], 4)
        self.assertTrue(json.loads(body)['hit'])

    def test_recall_telemetry_counts_evidence_without_storing_it(self):
        write = brain.usage.record
        write(self.data, 'codex', 'recall', {'level': 4, 'navigation': 'private navigation'}, '{}', False, None)
        write(self.data, 'codex', 'recall', {'level': 3, 'evidence': [{'text': 'private evidence'}]}, '{}', False, None)
        records = [json.loads(line) for line in (self.data / 'usage.jsonl').read_text().splitlines()]
        self.assertEqual([(row['hit'], row['recall_level']) for row in records], [(False, 4), (True, 3)])
        self.assertNotIn('private', str(records))

    def test_weekly_report_excludes_test_calls_and_reports_missing_measurement(self):
        self.tool('log_work', project='Atlas', create_project=True, source='codex', summary='Initial record.')
        records = [
            {'at':'2026-11-10T00:00:00+00:00', 'origin':'test', 'source':'codex',
             'tool':'get_project','hit':True,'tokens':99},
            {'at':'2026-11-10T00:01:00+00:00', 'origin':'client', 'source':'codex',
             'tool':'get_project','hit':True},
        ]
        (self.data / 'usage.jsonl').write_text('\n'.join(map(json.dumps, records)))
        dest = brain.usage.weekly(self.db, self.data, date(2026, 11, 16))
        self.assertIn('| codex | get_project | 1 | 1 | 0 | 0 |', (dest / '本周使用情况.md').read_text())
        self.assertTrue((dest / '本周各项目动态.md').is_file())
        self.assertEqual(brain.usage.observed_usage(self.data, date(2026, 11, 16))['days_with_client_calls'], 1)

    def test_health_reports_collection_failure_and_backs_up_sqlite(self):
        (self.data / 'coverage.json').write_text(json.dumps({'exit': 1, 'packetRefresh': {'status': 'failed'}}))
        self.tool('log_work', project='Atlas', create_project=True, summary='Initial record.', source='codex')
        self.assertIn('索引可能过期', self.tool('get_project', project='Atlas')['coverage'])
        report = brain.health.write_report(self.db, self.data)
        self.assertEqual(report['sqlite'], 'ok')
        self.assertEqual(report['collector_exit'], 1)
        with sqlite3.connect(brain.health.backup(self.db, self.data)) as snapshot:
            self.assertEqual(snapshot.execute('SELECT name FROM projects').fetchone(), ('Atlas',))

    def test_health_reports_source_files_separately_from_indexed_sessions(self):
        (self.data / 'coverage.json').write_text(json.dumps({'sources': {
            'codex': {'found': 8, 'prepared': 1, 'failed': 1, 'failureReasons': {'SOURCE_CHANGED': 1}},
            'kimi-work': {'found': 2},
        }}))
        (self.data / 'agentsview').mkdir()
        original = self.root / 'pending.jsonl'
        original.write_text('{}\n')
        (self.data / 'sources.json').write_text(json.dumps({
            'sources/codex/0/rollout-12345678-1234-4123-a123-123456789012.jsonl.zst': {'source': str(original)},
            'sources/kimi-work/0/wire.jsonl': {'source': str(original)},
            'sources/kimi-work/0/workspace/ctitle-example/agents/main/wire.jsonl': {'source': str(original)},
            'sources/cowork/0/audit.jsonl': {'source': str(original)},
        }))
        with sqlite3.connect(self.data / 'agentsview' / 'sessions.db') as index:
            index.executescript("""
                CREATE TABLE sessions (agent TEXT, deleted_at TEXT, user_message_count INT,
                  message_count INT, parser_malformed_lines INT, is_truncated INT, id TEXT, file_path TEXT);
                INSERT INTO sessions VALUES ('codex',NULL,1,2,0,0,'codex:12345678-1234-4123-a123-123456789012',NULL),
                  ('codex',NULL,0,0,0,0,'codex:empty',NULL), ('codex','archived',1,2,1,1,'codex:deleted',NULL),
                  ('kimi-work',NULL,1,1,1,0,'kimi-work:partial',NULL);
            """)
        sources = brain.health.report(self.db, self.data)['sources']
        self.assertEqual(sources['codex']['source_files_found'], 8)
        self.assertEqual(sources['codex']['indexed_sessions'], 2)
        self.assertEqual(sources['codex']['sessions_without_messages'], 1)
        self.assertEqual(sources['codex']['sessions_with_user_messages'], 1)
        self.assertEqual(sources['codex']['failure_reasons'], {'SOURCE_CHANGED': 1})
        self.assertEqual(sources['codex']['files_waiting_for_retry'], 1)
        self.assertFalse(sources['codex']['incomplete'])
        self.assertEqual(sources['codex']['transcripts_matched_to_index'], 1)
        self.assertEqual(sources['codex']['transcripts_without_index'], 0)
        self.assertTrue(sources['kimi-work']['incomplete'])
        self.assertEqual(sources['kimi-work']['parser_incomplete_sessions'], 1)
        self.assertEqual(sources['kimi-work']['transcripts_without_index'], 1)
        self.assertEqual(sources['kimi-work']['excluded_auxiliary_logs'], 1)
        self.assertNotIn('cowork', sources)

    def test_pending_collection_is_distinct_from_actual_missing_content(self):
        timestamp = brain.now()
        coverage = self.data / 'coverage.json'
        report = {'completedAt':timestamp,'sources':{'codex':{
            'found':10,'failed':1,'failureReasons':{'SOURCE_CHANGED':1}}}}
        coverage.write_text(json.dumps(report))
        self.assertEqual(brain.source_status()['codex']['status'],'有会话等下轮补读')
        self.assertEqual(brain.packet_coverage()['coverage'],'有会话等下轮补读')
        health_path = self.data / 'health.json'
        health_path.write_text(json.dumps({'last_collection':timestamp,'sources':{'codex':{'message_gap_over_15m':1}}}))
        self.assertEqual(brain.packet_coverage()['coverage'],'部分来源采集不完整')
        health_path.write_text(json.dumps({'last_collection':'earlier','sources':{'codex':{'message_gap_over_15m':1}}}))
        self.assertEqual(brain.packet_coverage()['coverage'],'有会话等下轮补读')
        report['sources']['codex']['failed'] = 2
        coverage.write_text(json.dumps(report))
        self.assertEqual(brain.source_status()['codex']['status'],'内容不全')
        self.assertEqual(brain.packet_coverage()['coverage'],'部分来源采集不完整')

    def test_rate_limit_leaves_a_visible_status_and_stops_until_next_window(self):
        sid = 'codex:fictional-quota'
        self.archive_messages(sid, 'Atlas', [('user', 'Please continue the fictional project.')])
        self.tool('log_work', project='Atlas', create_project=True, source='codex', summary='Initial record.')
        with patch.object(brain, 'model', side_effect=brain.ModelQuotaError('120')) as model:
            for _ in range(2):
                status = brain.extraction.process(self.db, self.data, [sid], brain.visible, brain.packet.paths_in,
                                                  brain.json_text, brain.clean, model, 20000)
                self.assertTrue(status['rate_limited'])
            self.assertEqual(model.call_count, 1)
        self.assertIn('模型提取', self.tool('get_project', project='Atlas')['coverage'])
        self.assertIn('模型提取', self.tool('list_projects')['coverage'])

    def test_recall_charges_once_checks_citations_and_falls_back(self):
        sid = 'codex:fixture-recall'
        self.archive_messages(sid, 'Atlas', [
            ('user', 'Use a plain-text daily schedule. <file_contents>PRIVATE_BODY</file_contents>'),
            ('assistant', 'I am working on it.'),
            ('assistant', 'Delivered [Atlas report](/tmp/atlas.md).\n\n# ORIGINAL_FILE_BODY'),
            ('assistant', '已落盘 [Atlas notes](/tmp/notes.md)，有 12 条要点、15 句口径；“PRIVATE_QUOTE”'),
            ('user', '[Reply from imported agent] PRIVATE_AGENT_REPLY'),
            ('user', 'Gather the test evidence. [tool:read]'),
            ('assistant', 'The first version was saved in the workspace. <tool_output>PRIVATE_TOOL</tool_output>'),
            ('user', '<appshot app="Example">PRIVATE_APP_SNAPSHOT report report</appshot>\nPlease verify the report.'),
        ])
        settings = {'enabled': True, 'url': 'http://127.0.0.1:3425/v1', 'name': brain.eco.MODEL}
        (self.data / 'config.json').write_text(json.dumps({'eco': settings}))
        with sqlite3.connect(self.data / 'agentsview/sessions.db') as index:
            for col in ('deleted_at', 'first_message', 'relationship_type'):
                index.execute('ALTER TABLE sessions ADD COLUMN ' + col + ' TEXT')
        self.tool('log_work', project='Atlas', create_project=True, summary='Prepared the schedule.', source='codex', session_id=sid)

        def gateway(messages, **kwargs):
            self.assertNotIn('PRIVATE_BODY', str(messages))
            self.assertNotIn('ORIGINAL_FILE_BODY', str(messages))
            self.assertNotIn('PRIVATE_QUOTE', str(messages))
            self.assertNotIn('PRIVATE_AGENT_REPLY', str(messages))
            self.assertNotIn('PRIVATE_TOOL', str(messages))
            self.assertNotIn('PRIVATE_APP_SNAPSHOT', str(messages))
            self.assertIn('Please verify the report.', str(messages))
            self.assertIn('15 句口径', str(messages))
            self.assertEqual(kwargs['settings']['max_tokens'], 16000)
            self.assertEqual(kwargs['settings']['extra_body']['reasoning_effort'], 'low')
            kwargs['on_usage']({'prompt_tokens': 1000, 'completion_tokens': 500})
            return {'claims': [
                {'ref': '1:2', 'quote': 'Delivered', 'kind': 'deliverable', 'text': 'Atlas report',
                 'path': '/tmp/atlas.md'},
                {'ref': '1:0', 'quote': 'plain-text daily schedule', 'kind': 'rule', 'text': 'Use a plain-text daily schedule.'},
                {'ref': '1:999', 'quote': 'invented', 'kind': 'answer', 'text': 'Unverified.'},
            ]}
        with patch.object(brain, 'model', side_effect=gateway) as model:
            answer = self.tool('recall', project='Atlas', question='Which report is done?')
            self.assertEqual((answer['level'], len(answer['evidence'])), (1, 2))
            self.assertEqual([claim['quote'] for claim in answer['evidence']], ['Delivered','plain-text daily schedule'])
            self.assertTrue(self.tool('recall', project='Atlas', question='Which report is done?')['cached'])
            self.assertEqual(model.call_count, 1)
            local = self.tool('recall', project='Atlas', question='Which report is done?',local_only=True)
            self.assertEqual(local['route'],'local')
            self.assertIn(2,[row['ordinal'] for row in local['evidence']])
            self.assertTrue(all(local['sessions'][row['session']]==sid for row in local['evidence']))
            self.assertEqual(model.call_count,1)
        self.assertEqual(brain.eco.snapshot(self.db, settings)['24h']['used_or_reserved'], 9)
        audit = json.loads((self.data/'recall_evidence.jsonl').read_text().splitlines()[0])
        self.assertEqual(set(audit), {'at','sha256','bytes','messages'})
        self.assertIn([sid,2], audit['messages'])
        self.db.execute("INSERT INTO recall_segments VALUES(?,0,2,'revision','now',?)", (sid,
            json.dumps({'summary':'Atlas schedule handoff','tags':['Atlas'],'anchors':[2]})))
        self.db.commit()
        with patch.object(brain, 'model', side_effect=OSError('offline')):
            answer = self.tool('recall', project='Atlas', question='Where is the schedule?')
        self.assertEqual((answer['level'],answer['route']), (3,'local'))
        self.assertIn(2,[row['ordinal'] for row in answer['evidence']])

    def test_recall_confirmation_needs_user_approval_and_the_same_delivery(self):
        sid = 'codex:fictional-approval'
        report = '/tmp/atlas.md'
        def source(ordinal, role, text, **extra):
            return {'ordinal':ordinal,'role':role,'text':text,'session_id':sid,**extra}

        approval = '确认。'
        evidence = {
            '1:4':source(4,'assistant','Delivered the Atlas report.',files=[report]),
            '1:5':source(5,'user',approval),
            '1:7':source(7,'user','就用这版作为终版，再补充价格。'),
            '1:8':source(8,'assistant','Updated the Atlas report.',files=[report]),
            '1:9':source(9,'user','这份 atlas.md 作为终版。'),
            '1:10':{**source(10,'assistant','Delivered a separate report.',files=[report]),'session_id':'kimi-work:elsewhere'},
        }
        base = {'ref':'1:5','quote':approval,'kind':'confirmed','text':'Approved the Atlas report.',
                'path':report,'delivery_ref':'1:4'}
        verify = brain.recall.checked_claims
        result = verify({'claims':[base]},evidence)
        self.assertEqual([(x['ordinal'],x['delivery_ordinal'],x['file'],x['quote']) for x in result],
                         [(5,4,'atlas.md',approval)])
        for overrides in ({'delivery_ref':None}, {'delivery_ref':'1:10'}, {'path':'/tmp/other.md'},
                          {'ref':'1:7','quote':'就用这版作为终版'},
                          {'ref':'1:9','quote':'这份 atlas.md 作为终版'}):
            self.assertEqual(verify({'claims':[{**base,**overrides}]},evidence),[])
        later = verify({'claims':[{**base,'ref':'1:9','quote':'这份 atlas.md 作为终版','delivery_ref':'1:8'}]},evidence)
        self.assertEqual(later[0]['delivery_ordinal'],8)
        self.assertEqual(verify([],evidence),[])

    def test_recall_cites_inline_draft_separately_from_the_earlier_file(self):
        sid = 'kimi-work:fictional-handoff'
        evidence = {'1:1':{'session_id':sid,'ordinal':1,'role':'assistant','text':'Delivered the Word worksheet.',
                            'files':['/tmp/worksheet.docx']},
                    '1:3':{'session_id':sid,'ordinal':3,'role':'assistant','text':'Here is the spoken draft.',
                            'inline_sections':4}}
        file = {'ref':'1:1','kind':'deliverable','quote':'Delivered the Word worksheet',
                'text':'Worksheet for the meeting','path':'/tmp/worksheet.docx'}
        draft = {'ref':'1:3','kind':'deliverable','quote':'Here is the spoken draft','text':'Final spoken draft'}
        checked = brain.recall.checked_claims({'claims':[file,draft,{**draft,'path':file['path']}]},evidence)
        self.assertEqual([(item['ordinal'],item.get('file'),item.get('inline_sections')) for item in checked],
                         [(1,'worksheet.docx',None),(3,None,4)])

    def test_local_recall_pairs_a_user_request_with_its_answer(self):
        sid = 'codex:fictional-local-recall'
        evidence = {
            str(ordinal): {'session_id':sid,'ordinal':ordinal,'role':role,'text':value}
            for ordinal,role,value in [(9,'assistant','The schedule is attached.'),
                                      (15,'assistant','Unrelated recap.'),
                                      (8,'user','Use a plain-text schedule.'),
                                      (10,'user','Keep the existing dates.'),
                                      (11,'assistant','The existing dates are preserved.')]
        }
        def measure(value):
            return 355 * len(json.loads(value)['evidence'])

        page = brain.recall.local_evidence(evidence,brain.json_text,measure)
        self.assertEqual([row['ordinal'] for row in page['evidence']][:2],[9,8])
        following = brain.recall.local_evidence(evidence,brain.json_text,measure,page['next_offset'])
        self.assertEqual([row['ordinal'] for row in following['evidence']],[15,10])
        last = brain.recall.local_evidence(evidence,brain.json_text,measure,following['next_offset'])
        self.assertEqual([row['ordinal'] for row in last['evidence']],[11])
        self.assertNotIn('next_offset',last)

    def test_local_recall_promotes_only_matching_registered_source_messages(self):
        sid = 'codex:fictional-acceptance'
        self.archive_messages(sid, 'Atlas', [('user','Atlas acceptance?'),
            ('assistant','Keep the original checklist.'), ('assistant','A different schedule.'),
            ('assistant','Acceptance remains open.')])
        log = self.tool('log_work', project='Atlas', create_project=True, summary='Reviewed the test history.',
                        source='codex', session_id=sid, conclusions=[
                            {'topic':'duplicate test', 'text':'Stop duplicate tests.', 'ordinal':1},
                            {'topic':'weekly schedule', 'text':'Move the weekly schedule.', 'ordinal':2},
                            {'topic':'duplicate test', 'text':'Duplicate test still pending.', 'ordinal':3}])
        evidence = {
            str(ordinal): {'session_id':sid, 'ordinal':ordinal, 'role':'assistant', 'text':value}
            for ordinal,value in [(2,'A different schedule.'), (1,'Keep the original checklist.')]
        }
        prioritize = brain.recall.prioritize_logged_evidence
        def measure(value):
            return 500 * len(json.loads(value)['evidence'])

        ranked = prioritize(self.db, log['project_id'], 'Atlas duplicate test acceptance?', evidence)
        page = brain.recall.local_evidence(ranked, brain.json_text, measure)
        self.assertEqual([(row['ordinal'], row['text']) for row in page['evidence']],
                         [(1, 'Keep the original checklist.')])
        following = brain.recall.local_evidence(ranked, brain.json_text, measure, page['next_offset'])
        self.assertEqual([row['ordinal'] for row in following['evidence']], [2])
        self.assertEqual(list(evidence), ['2', '1'])
        self.assertEqual(list(prioritize(self.db, log['project_id'], 'Atlas packing instructions?', evidence)),
                         list(evidence))

    def test_progress_fallback_frontloads_report_and_newer_handoff(self):
        sid = 'codex:fictional-recall-progress'
        report, notes, evidence = (self.root / name for name in ('report.md','notes.md','evidence.md'))
        for path in (report,notes,evidence):
            path.write_text('FILE_BODY_STAYS_LOCAL')
        self.archive_messages(sid, 'Atlas', [('assistant',f'Delivered `{report}`.'),
            ('assistant','An older discussion of the progress question.'),
            ('assistant',f'Follow-up review at `{notes}` and `{evidence}`.')])
        log = self.tool('log_work', project='Atlas', create_project=True, summary='Two real handoffs.',
                        source='codex', session_id=sid, deliverables=[
                            {'path':str(report),'task':'Report','ordinal':0,'final':True},
                            {'path':str(notes),'task':'Review','ordinal':2},
                            {'path':str(evidence),'task':'Evidence','ordinal':2}])
        row = {'ref':'1:1','session_id':sid,'ordinal':1,'role':'assistant','text':'Unrelated progress chatter.'}
        package = ({'messages':[{k:v for k,v in row.items() if k!='session_id'}]}, {'1:1':row}, [])
        with patch.object(brain.recall,'evidence_package',return_value=package), \
             patch.object(brain.eco,'call',side_effect=ConnectionRefusedError) as route:
            value = self.tool('recall',question='Atlas 接着做到哪了？')
            self.assertEqual((value['level'],value['route']),(3,'local'))
            self.assertTrue(value['reason'])
            self.assertEqual([item['ordinal'] for item in value['evidence'][:2]],[0,2])
            self.assertEqual(set(value['evidence'][1]['files']),{notes.name,evidence.name})
            self.assertNotIn('FILE_BODY_STAYS_LOCAL',brain.json_text(value))
            self.assertNotIn(str(report),brain.json_text(route.call_args.args[2]))
            focused = self.tool('recall',project='Atlas',question='Atlas 具体哪份报告?',local_only=True)
            self.assertEqual([item['ordinal'] for item in focused['evidence']],[1])
            self.assertEqual(route.call_count,1)

    def test_implicit_local_project_matches_complete_names_without_guessing(self):
        alpha,_ = brain.ensure_project(self.db,'Atlas','fictional')
        expanded,_ = brain.ensure_project(self.db,'Atlas Docs','fictional')
        beta,_ = brain.ensure_project(self.db,'Borealis','fictional')
        brain.ensure_project(self.db,'Codex','fictional')
        self.db.execute('INSERT INTO project_aliases VALUES (?,?)',('北极星计划',alpha))
        lookup = brain.recall.named_project
        self.assertEqual(lookup(self.db,'Atlas Docs 进度如何？'),expanded)
        self.assertEqual(lookup(self.db,'北极星计划接着做到哪了'),alpha)
        self.assertEqual(lookup(self.db,'Borealis 的 Codex 记录在哪里'),beta)
        for question in ('Atlas和Borealis 进度如何？','Atlas2怎么做','Codex里之前怎么算的？','那件事做到哪了？'):
            self.assertIsNone(lookup(self.db,question))

    def test_unnamed_project_keeps_nearby_handoff_from_explicit_client(self):
        evidence = {ref:{'session_id':sid} for ref,sid in [('a','cowork:meta'),('b','kimi-work:target'),
                    ('c','kimi-work:other'),('d','kimi-work:target'),('e','codex:context')]}
        sort = brain.recall.prioritize_named_client
        self.assertEqual(list(sort('Kimi Work 的可编辑文件在哪？',evidence)),['b','d','c','a','e'])
        self.assertEqual(list(sort('Kimi Work 和 Codex 的结果有什么区别？',evidence)),list(evidence))
        self.assertEqual(list(sort('哪里提到 Kimi Work？',evidence)),list(evidence))

    def test_recall_uses_old_choices_and_keeps_file_version_evidence(self):
        sid = 'codex:fictional-history'
        version = self.root/'agenda.docx'
        version.write_text('PRIVATE_CURRENT_FILE')
        historical_hash = 'a' * 64
        selected = json.dumps({'kind':'answer_to_pending_question',
                               'questions':[{'id':'q','question':'Which venue policy should Atlas use?',
                                             'options':[{'label':'Go with the small room','description':'Two teams only'}]}],
                               'answers':[{'id':'q','selected':['Go with the small room']}]})
        dialogue = [('user',selected),('user','Atlas agenda version?'),
                    ('assistant',f'Agenda in `{version}` had 2 页. Uses `PAGE` 域. SHA256: {historical_hash}'),
                    ('assistant',f'The agenda in `{version}` was replaced with 1 页. No footer. SHA256: {historical_hash}'),
                    ('assistant',f'Compared `{version}` with `{self.root}/notice.docx`. The files had no footer.')]
        self.archive_messages(sid,'Atlas',dialogue + [('user','Unrelated test turn')]*150)
        self.tool('log_work',project='Atlas',create_project=True,summary='Agenda history.',source='codex',session_id=sid)
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('deleted_at','first_message','relationship_type'):
                index.execute('ALTER TABLE sessions ADD COLUMN '+col+' TEXT')
            index.execute('UPDATE messages SET is_system=1 WHERE ordinal=0')
            index.row_factory=sqlite3.Row
            project=brain.resolve_project_id(self.db,'Atlas')
            def package(question):
                return brain.recall.evidence_package(self.db,index,project,question,brain.visible,
                    brain.packet.paths_in,brain.json_text,use_tags=False)
            result,_,_=package('What venue policy did Atlas choose?')
            self.assertTrue(any(row['text']=='User selected: Go with the small room · Two teams only'
                                for row in result['messages']))
            result,evidence,_=package('What was the Atlas agenda version?')
        self.assertNotIn('PRIVATE_CURRENT_FILE',str(result))
        timeline=next(item for item in result['file_timelines'] if item['current'])
        self.assertEqual([row.get('reported_pages') for row in timeline['history'][:2]],[2,1])
        self.assertEqual([row.get('reported_format_quote') for row in timeline['history']],['`PAGE` 域','No footer',None])
        self.assertFalse(timeline['current']['matches_latest_reported_sha256'])
        old_ref=timeline['history'][0]['ref']
        local=brain.recall.local_evidence({old_ref:evidence[old_ref]},brain.json_text,None)
        self.assertEqual(local['evidence'][0]['later_version'],
                         {'session':'1','ordinal':3,'reported_pages':[2,1],'reported_format_quotes':['`PAGE` 域','No footer'],
                          'current_matches_reported_hash':False})

    def test_recall_search_uses_agentsview_trigrams_for_full_words(self):
        with sqlite3.connect(':memory:') as db:
            db.executescript("""CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id, ordinal, role,
                              is_system, dialogue_text, content);
                CREATE VIRTUAL TABLE message_trigram_fts USING fts5(content,tokenize='trigram',detail='none');
                INSERT INTO messages VALUES(1,'codex:one',10,'assistant',0,'Atlas-notes.md 修订稿','');
                INSERT INTO messages VALUES(2,'codex:two',20,'assistant',0,'Atlas plan','');
                INSERT INTO message_trigram_fts(rowid,content) SELECT id,dialogue_text FROM messages;""")
            self.assertEqual([(sid,ordinal) for sid,ordinal,_ in brain.recall.text_matches(db,{'atlas-notes.md','修订稿'})],
                             [('codex:one',10)])

    def test_recall_can_identify_inline_delivery_without_exporting_its_body(self):
        sid = 'codex:fictional-inline'
        self.archive_messages(sid, 'Atlas', [
            ('user','Prepare a conversation script.'),
            ('assistant','Here is the conversation script.\n\n## Opening\nPRIVATE_DIALOGUE\n## Closing\nMORE_DIALOGUE'),
            ('assistant','Saved the document [Atlas](/tmp/atlas.md).\n## Document\nPRIVATE_DOCUMENT'),
        ])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            index.row_factory=sqlite3.Row
            rows=brain.extraction.transcript(index,sid,brain.visible,brain.packet.paths_in,brain.json_text,
                                            outbound=True,focused=True)
        self.assertEqual(rows[1]['inline_sections'],2)
        self.assertNotIn('inline_sections',rows[2])
        self.assertNotIn('PRIVATE_',str(rows))

    def test_recall_recovers_a_short_task_delivery_and_its_request(self):
        sid = 'cowork:fictional-short-task'
        self.archive_messages(sid, 'Atlas', [
            ('user','Atlas order workbook: translate the labels.'),
            *[('assistant','') for _ in range(27)],
            ('assistant','Translated the workbook. [Download](/tmp/orders.xlsx)'),
        ])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('deleted_at','first_message','relationship_type'):
                index.execute('ALTER TABLE sessions ADD COLUMN '+col+' TEXT')
            index.row_factory=sqlite3.Row
            project=self.tool('log_work',project='Atlas',create_project=True,source='codex',summary='Workbook project.')['project_id']
            _,evidence,_=brain.recall.evidence_package(self.db,index,project,'Which order workbook?',brain.visible,
                                                       brain.packet.paths_in,brain.json_text,use_tags=False)
        self.assertEqual({row['ordinal'] for row in evidence.values()},{0,28})
        self.assertIn('/tmp/orders.xlsx', next(row['files'] for row in evidence.values() if row['role']=='assistant'))

    def test_recall_keeps_early_user_requirements_in_a_long_session(self):
        sid = 'codex:fictional-long-task'
        self.archive_messages(sid,'Atlas',[
            ('user','The rehearsal uses a silent speaker. Keep the network off.'),
            *(turn for n in range(150) for turn in [('user',f'Continue the setup, step {n}.'),
                                                     ('assistant','The silent speaker rehearsal is progressing.')]),
        ])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('deleted_at','first_message','relationship_type'):
                index.execute('ALTER TABLE sessions ADD COLUMN '+col+' TEXT')
            index.row_factory=sqlite3.Row
            project=self.tool('log_work',project='Atlas',create_project=True,source='codex',
                              summary='Prepared the rehearsal.',session_id=sid)['project_id']
            _,evidence,_=brain.recall.evidence_package(self.db,index,project,'Atlas silent speaker rehearsal',
                                                       brain.visible,brain.packet.paths_in,brain.json_text,use_tags=False)
        self.assertIn(0,[row['ordinal'] for row in list(evidence.values())[:5]])

    def test_recall_considers_recent_unassigned_tasks_with_a_concrete_result(self):
        sid = 'deepseek-harness:fictional-unassigned-task'
        self.archive_messages(sid,'General',[('user','Adjust the Bot model allocation.'),
                                             ('assistant','回读核对通过，模型共调整 4 个。')])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('deleted_at','first_message','relationship_type'):
                index.execute('ALTER TABLE sessions ADD COLUMN '+col+' TEXT')
            index.row_factory=sqlite3.Row
            project=self.tool('log_work',project='Atlas',create_project=True,source='codex',summary='Setup.')['project_id']
            _,evidence,_=brain.recall.evidence_package(self.db,index,project,'Bot 模型实际改了多少？',
                                                       brain.visible,brain.packet.paths_in,brain.json_text,use_tags=False)
        self.assertEqual({(row['session_id'],row['ordinal']) for row in evidence.values()},{(sid,0),(sid,1)})

    def test_recall_file_history_does_not_displace_the_best_message(self):
        sid='codex:fictional-repeated-file'
        version=self.root/'status.md'
        dialogue=[('user','What changed in monthly narration?'),
                  ('assistant',f'Monthly narration now reports completion counts. See `{version}`.')]
        dialogue.extend(('assistant',f'Workspace update {i}: `{version}`. Details: ' + 'x'*180)
                        for i in range(90))
        self.archive_messages(sid,'Atlas',dialogue)
        self.tool('log_work',project='Atlas',create_project=True,summary='Monthly narration.',source='codex',session_id=sid)
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('deleted_at','first_message','relationship_type'):
                index.execute('ALTER TABLE sessions ADD COLUMN '+col+' TEXT')
            index.row_factory=sqlite3.Row
            project=brain.resolve_project_id(self.db,'Atlas')
            with patch.object(brain.recall,'EVIDENCE_BYTES',4_000), patch.object(brain.recall,'TIMELINE_BYTES',1_000):
                payload,evidence,_=brain.recall.evidence_package(self.db,index,project,'What changed in monthly narration?',
                    brain.visible,brain.packet.paths_in,brain.json_text,use_tags=False)
        self.assertIn(1,[row['ordinal'] for row in list(evidence.values())[:3]])
        self.assertLessEqual(len(brain.json_text(payload).encode()),4_000)

    def test_eco_test_budget_override_retains_the_shared_meter(self):
        settings = {'daily_credits': 2, 'weekly_credits': 5}
        with self.assertRaises(brain.eco.BudgetUnavailable):
            brain.eco.reserve(self.db,settings,'ordinary',3)
        brain.eco.reserve(self.db,settings,'evaluation',3,enforce_budget=False)
        self.assertEqual(brain.eco.snapshot(self.db,settings)['24h']['used_or_reserved'],3)
        self.assertEqual(brain.eco.limits({'daily_credits':6000,'weekly_credits':35000}),(6000,35000))
        with self.assertRaises(brain.eco.BudgetUnavailable):
            brain.eco.reserve(self.db,settings,'ordinary',1)

    def test_busy_database_reports_error_and_accepts_retry_after_unlock(self):
        args = dict(project='Atlas', create_project=True, source='codex', summary='Fictional handoff.', work_id='lock')
        with sqlite3.connect(self.data / 'brain.sqlite') as writer:
            writer.execute('BEGIN IMMEDIATE')
            self.db.execute('PRAGMA busy_timeout=10')
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                self.tool('log_work', **args)
            writer.rollback()
        self.assertTrue(self.tool('log_work', **args)['written'])
        self.assertFalse(self.tool('log_work', **args)['written'])

    def test_obsidian_scan_is_read_only_and_registers_exact_project_folder(self):
        vault = self.root / 'vault'
        (vault / '.obsidian').mkdir(parents=True)
        notebook = vault / 'handbook.md'
        notebook.write_text('# Atlas status\nFictional notes for the project.')
        before = notebook.read_bytes()
        sid = 'codex:fictional-vault'
        self.archive_messages(sid, 'Atlas', [('user', 'Review the Atlas packet.')])
        with sqlite3.connect(self.data / 'agentsview/sessions.db') as index:
            index.execute('UPDATE sessions SET cwd=?', (str(vault),))
        self.tool('log_work', project='Atlas', create_project=True, source='codex', summary='Initial record.', session_id=sid)
        self.assertEqual(brain.notes.scan(self.db, vault, brain.clean)['scanned'], 1)
        self.assertEqual(notebook.read_bytes(), before)
        self.assertIn('handbook.md', self.tool('get_project', project='Atlas', section='inputs')['card'])
        with patch.object(brain, 'archive', return_value={}):
            self.assertIn('handbook.md', self.tool('search', query='Atlas status')['matches'][0]['path'])

    def test_document_extraction_and_fingerprint_cache(self):
        word = self.root / 'sample.docx'
        with ZipFile(word, 'w') as out:
            out.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Valuation</w:t></w:r></w:p>'
                         '<w:p><w:r><w:t>Revenue times the multiple.</w:t></w:r></w:p></w:body></w:document>')
        self.assertIn('Revenue times', brain.file_text.extract(word, word.read_bytes(), section='Valuation')[0][1])
        value = str(word)
        result = brain.packet.prepare_file(self.db, value, brain.clean)
        self.assertEqual(result[0], 'built')
        self.assertIn('Valuation', brain.packet.file_summary(self.db, value))
        self.assertEqual(brain.packet.prepare_file(self.db, value, brain.clean)[0], 'cached')
        word.write_text('changed')
        self.assertIsNone(brain.packet.file_summary(self.db, value))

    def test_file_summary_uses_the_outline_and_explicit_takeaway(self):
        path = self.root / 'rehearsal.md'
        path.write_text('Introduction text that should not appear as a takeaway.\n'
                        '# Rehearsal\nA day-by-day schedule.\n'
                        '# Summary\nThe coordinator owns rollback for Friday. Review the smoke check.')
        built, summary, method = brain.packet.prepare_file(self.db, str(path), brain.clean)
        self.assertEqual((built, method), ('built', brain.packet.FILE_SUMMARY_METHOD))
        self.assertIn('The coordinator owns rollback for Friday.', summary)
        self.assertIn('Rehearsal', summary)
        self.assertNotIn('Introduction', summary)
        self.db.execute("UPDATE packet_file_summaries SET method='extractive'")
        self.assertIsNone(brain.packet.file_summary(self.db, str(path)))
        self.assertEqual(brain.packet.prepare_file(self.db, str(path), brain.clean)[0], 'built')

    def test_background_summary_defers_a_cloud_placeholder(self):
        path = self.root / 'cloud-placeholder.md'
        path.write_text('# Fictional placeholder')
        with patch.object(brain.file_text, 'local_file_available', return_value=False), \
                patch.object(brain.file_text, 'read_bytes') as read:
            self.assertEqual(brain.packet.prepare_file(self.db, str(path), brain.clean)[0], 'unavailable')
            self.assertEqual(brain.packet.summary_status(self.db, str(path)), '文件尚未下载到本机')
            read.assert_not_called()

        with patch.object(brain.file_text, 'read_bytes', side_effect=lambda _: sleep(5)):
            self.assertEqual(brain.packet.prepare_file(self.db, str(path), brain.clean, open_timeout=0.02)[0], 'unavailable')
        self.assertEqual(brain.packet.prepare_file(self.db, str(path), brain.clean)[0], 'built')


if __name__ == '__main__':
    unittest.main()
