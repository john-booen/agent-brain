import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
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
        self.assertEqual([json.loads(line)['status'] for line in
                          (self.data / 'health.log').read_text().splitlines()], ['failed', 'failed', 'failed', 'ok'])

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
            index.executescript('''CREATE TABLE sessions(id,cwd,transcript_revision,display_name,session_name);
                                  CREATE TABLE messages(session_id,ordinal,role,timestamp,content,dialogue_text,is_system);''')
            index.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sid, str(self.root), '1', project, project))
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
        self.assertEqual(len(tools), 7)
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
            'deliverables': [{'path': str(report), 'task': 'Report', 'ordinal': 2, 'final': True, 'confirmation': 3},
                             {'path': str(self.root / 'invented.md'), 'task': 'Other', 'ordinal': 2}],
            'rules': [{'quote': 'Use the conservative range', 'scope': '对外版本', 'topic': 'figures', 'ordinal': 1},
                      {'quote': 'Delivered', 'scope': '项目', 'topic': 'false', 'ordinal': 2}],
            'todos': [],
        }]}
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

    def test_model_extracts_selected_answer_without_passing_question_options(self):
        sid = 'deepseek-harness:fictional-selection'
        self.archive_messages(sid, 'Atlas', [('user', json.dumps({
            'kind': 'answer_to_pending_question',
            'questions': [{'id': 'a', 'options': [{'label': 'picked answer'}, {'label': 'unselected option'}]}],
            'answers': [{'id': 'a', 'selected': ['picked answer']}],
        })), ('assistant', 'Delivered [report](research/fictional-report.md).')])
        with sqlite3.connect(self.data / 'agentsview/sessions.db') as index:
            index.row_factory = sqlite3.Row
            index.execute("UPDATE messages SET is_system=1 WHERE ordinal=0")
            evidence = brain.extraction.transcript(index, sid, brain.visible, brain.packet.paths_in, brain.json_text)
        self.assertEqual(evidence[0]['text'], 'User selected: picked answer')
        self.assertEqual(evidence[1]['files'], ['research/fictional-report.md'])

    def test_usage_log_contains_only_metadata(self):
        brain.usage.record(self.data, 'codex', 'get_project',
                           {'id': 'fictional'}, 'private card body', False, lambda _: 4)
        body = (self.data / 'usage.jsonl').read_text()
        assert 'private card body' not in body
        self.assertEqual(json.loads(body)['tokens'], 4)
        self.assertTrue(json.loads(body)['hit'])

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


if __name__ == '__main__':
    unittest.main()
