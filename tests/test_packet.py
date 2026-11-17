import importlib.util
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from time import sleep
from types import SimpleNamespace
import unittest
from datetime import date, datetime, timedelta, timezone
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

    def test_duplicate_scan_accepts_handoffs_delivered_in_chat(self):
        sid = 'codex:fictional-atlas-handoff'
        handoff = 'Atlas has delivered the complete written handoff.'
        self.archive_messages(sid, 'Atlas Reports', [('user', 'Prepare a handoff.'), ('assistant', handoff)])
        report = self.root / 'Atlas report.md'
        report.write_text('Fictional report.')
        self.tool('log_work', project='Atlas Reports', source='codex', create_project=True, session_id=sid,
                  summary='Recorded two handoff outputs.',
                  deliverables=[{'task': 'Handoff', 'text': handoff, 'ordinal': 1},
                                {'task': 'Report', 'path': str(report)}])
        self.tool('log_work', project='Atlas Reports Poster', source='codex', create_project=True,
                  summary='Referenced the same report.',
                  deliverables=[{'task': 'Report', 'path': str(report)}])
        for project in ('Atlas Reports', 'Atlas Reports Poster'):
            self.tool('log_work', project=project, source='codex', summary='Reviewed the project.')
        match, = brain.packet.merge_candidates(self.db, active_only=True)
        self.assertEqual({match['target'], match['duplicate']}, {'Atlas Reports', 'Atlas Reports Poster'})
        self.assertEqual(match['shared_files'], 1)

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
    def test_collection_stages_complete_lines_from_an_active_codex_transcript(self):
        archive = self.root / 'mock-agentsview'
        archive.write_text('#!/bin/sh\nif [ "$2" = list ]; then echo \'{"agents":[]}\'; fi\n')
        archive.chmod(0o700)
        transcript = self.root / '.codex/sessions/rollout-fictional.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"event":"first"}\n')
        staged = self.data / 'sources/codex/0/rollout-fictional.jsonl'
        preload = self.root / 'append-during-collection.mjs'
        preload.write_text("""
import fs from 'node:fs/promises';
const path = process.env.TEST_ACTIVE_TRANSCRIPT;
const stat = fs.stat;
let calls = 0;
fs.stat = async (file, ...args) => {
  if (file === path && ++calls === 3) {
    await fs.appendFile(path, '{"event":"password=example"}\\n{"event":"');
  }
  return stat(file, ...args);
};
""")
        env = {**os.environ, 'HOME': str(self.root), 'AGENT_BRAIN_DATA': str(self.data), 'AGENT_BRAIN_SYNC_DAEMON': '0',
               'AGENTSVIEW_BIN': str(archive), 'AGENT_BRAIN_PACKET_PYTHON': '', 'TEST_ACTIVE_TRANSCRIPT': str(transcript)}
        cmd = [str(Path(brain.__file__).with_name('collect.mjs')), 'codex']
        result = subprocess.run(['node', '--import', str(preload), *cmd], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = json.loads(result.stdout)['sources']['codex']
        self.assertEqual((source['activeSnapshots'], source['failed'], source['skippedLines']), (1, 0, 0))
        self.assertEqual(staged.read_text().splitlines(), ['{"event":"first"}', '{"event":"password=[REDACTED]"}'])
        with transcript.open('a') as writer:
            writer.write('third"}\n')
        result = subprocess.run(['node', *cmd], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(staged.read_text().splitlines()[-1], '{"event":"third"}')

        # If the file is rewritten while the prefix is checked, keep the last good copy.
        before = staged.read_bytes()
        transcript.write_text('{"event":"replacement"}\n')
        preload.write_text("""
import fs from 'node:fs/promises';
const path = process.env.TEST_ACTIVE_TRANSCRIPT;
const stat = fs.stat;
let calls = 0;
fs.stat = async (file, ...args) => {
  if (file === path) {
    calls++;
    if (calls === 3) await fs.appendFile(path, '{"event":"new line"}\\n');
    if (calls === 4 || calls === 6) await fs.writeFile(path, '{"event":"truncated"}\\n');
    if (calls === 5) await fs.appendFile(path, '{"event":"regrown"}\\n');
  }
  return stat(file, ...args);
};
""")
        result = subprocess.run(['node', '--import', str(preload), *cmd], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = json.loads(result.stdout)['sources']['codex']
        self.assertEqual(source['failureReasons'], {'SOURCE_CHANGED': 1})
        self.assertEqual(staged.read_bytes(), before)

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
        self.assertEqual(self.tool('read_file', project='Atlas', path=v1.name, section='Valuation',
                                   max_chars=3500), result)
        with self.assertRaisesRegex(ValueError, 'Invalid max_chars'):
            self.tool('read_file', project='Atlas', path=v1.name, max_chars=0)
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

    def test_read_file_uses_archived_delivery_to_disambiguate_registered_names(self):
        files = [self.root / folder / 'report.md' for folder in ('first', 'second', 'unregistered')]
        for file in files:
            file.parent.mkdir()
            file.write_text(f'# Figures\n{file.parent.name}')
        sid = 'cowork:fictional-distinct-files'
        self.archive_messages(sid, 'Atlas', [
            ('assistant', 'Here is the report: [first](first/report.md).'),
            ('assistant', f'Here is the report: [second](computer://{files[1]}).'),
            ('assistant', f'Drafts: [first]({files[0]}) and [second]({files[1]}).'),
            ('assistant', f'Here is another report: [unregistered]({files[2]}).'),
            ('user', f'Working on [this report]({files[0]}).'),
            ('assistant', f'Examples: [first]({files[0]}) and [unregistered]({files[2]}).'),
        ])
        self.tool('log_work', project='Atlas', create_project=True, summary='Register two reports.', source='codex',
                  inputs=[{'path': str(file)} for file in files[:2]])
        args = {'project':'Atlas', 'path':'report.md', 'session_id':sid}
        for ordinal, expected in enumerate(('first', 'second')):
            self.assertIn(expected, self.tool('read_file', **args, ordinal=ordinal)['text'])
        for ordinal in (2,3,4,5,99):
            with self.assertRaisesRegex(ValueError, '引用'):
                self.tool('read_file', **args, ordinal=ordinal)
        with self.assertRaisesRegex(ValueError, '会话.*消息号'):
            self.tool('read_file', **args)
        self.assertIn('second', self.tool('read_file', **{**args, 'path':str(files[1])}, ordinal=2)['text'])

    def test_read_file_uses_verified_log_for_nested_relative_deliveries(self):
        path = self.root / 'nested' / 'output' / 'report.md'
        path.parent.mkdir(parents=True)
        path.write_text('Verified nested report.')
        sid = 'deepseek-harness:fictional-nested-output'
        self.archive_messages(sid, 'Fictional earlier work', [('assistant','Saved [Report](output/report.md)\n'),
                                                              ('assistant','Saved [Report](../output/report.md)\n')])
        self.tool('log_work',project='Earlier Work',create_project=True,source='deepseek-harness',
                  summary='Earlier project.',session_id=sid)
        self.tool('log_work',project='Atlas',create_project=True,source='codex',summary='Register the output.',session_id=sid,
                  deliverables=[{'path':str(path),'ordinal':0,'task':'Nested report'}])
        args = {'project':'Atlas','path':path.name,'session_id':sid}
        value = self.tool('read_file',**args,ordinal=0)
        self.assertIn('Verified nested report.',value['text'])
        with self.assertRaisesRegex(ValueError,'引用'):
            self.tool('read_file',**args,ordinal=1)

    def test_read_file_uses_the_original_user_attachment_reference(self):
        attachment = self.root / 'input' / 'notes.md'
        deliverable = self.root / 'output' / 'notes.md'
        for path in (attachment, deliverable):
            path.parent.mkdir()
            path.write_text(f'# Notes\nFrom {path.parent.name}.')
        sid = 'codex:fictional-original-attachment'
        self.archive_messages(sid, 'Atlas', [
            ('user', f'Use <attachment>{json.dumps({"path":str(attachment)})}</attachment> for Atlas.'),
            ('assistant', f'Delivered the revised notes: [{deliverable.name}]({deliverable}).'),
            ('user', f'Please continue working from [{deliverable.name}]({deliverable}).'),
        ])
        self.tool('log_work', project='Atlas', create_project=True, source='codex',
                  summary='Record the inputs and deliverable.', session_id=sid,
                  inputs=[{'path':str(attachment),'ordinal':0}],
                  deliverables=[{'path':str(deliverable),'ordinal':1,'task':'Revised notes'}])
        args = {'project':'Atlas','path':'notes.md','session_id':sid}
        self.assertIn('From input.', self.tool('read_file', **args, ordinal=0)['text'])
        self.assertIn('From output.', self.tool('read_file', **args, ordinal=1)['text'])
        with self.assertRaisesRegex(ValueError, '尚未按这条消息登记.*可直接读取附件'):
            self.tool('read_file', **args, ordinal=2)
        attachment.unlink()
        with self.assertRaisesRegex(ValueError, '本机不可用'):
            self.tool('read_file', **args, ordinal=0)

    def test_read_file_follows_a_registered_copy_from_the_original_delivery(self):
        original = self.root / 'old' / 'report.md'
        copy = self.root / 'copy' / 'report.md'
        other = self.root / 'other' / 'report.md'
        for file in (copy, other):
            file.parent.mkdir(parents=True)
            file.write_text(f'Report in {file.parent.name}.')
        sid = 'cowork:fictional-registered-copy'
        self.archive_messages(sid, 'Atlas', [
            ('assistant', f'Delivered [report]({original}).'),
            ('user', f'Mentioned a different [{other.name}]({other}).'),
        ])
        self.tool('log_work', project='Atlas', create_project=True, source='codex',
                  summary='Register the earlier result.', session_id=sid,
                  deliverables=[{'path':str(original),'task':'Report','ordinal':0}])
        self.tool('get_project', project='Atlas')
        brain.register_path_relocation(self.db, str(original), str(copy), 'Verified existing copy.')
        args = {'project':'Atlas','session_id':sid,'ordinal':0}
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        for path in (copy.name, str(original), str(copy)):
            result = self.tool('read_file', **args, path=path, expected_sha256=digest)
            self.assertTrue(result['registered_copy'])
            self.assertIn('Report in copy.', result['text'])
        with self.assertRaisesRegex(ValueError,'引用'):
            self.tool('read_file', **args, path=str(other))
        with self.assertRaisesRegex(ValueError,'指纹已变化'):
            self.tool('read_file', **args, path=copy.name, expected_sha256='a'*64)

    def test_read_file_checks_fingerprint_before_returning_a_changed_version(self):
        file = self.root/'report.md'
        file.write_text('# Report\n'+'First edition. '*300)
        self.tool('log_work',project='Atlas',create_project=True,source='codex',summary='Deliver the report.',
                  deliverables=[{'path':str(file),'task':'Report'}])
        args = {'project':'Atlas','path':file.name}
        original = self.tool('read_file',**args,max_chars=100)
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        self.assertEqual(original['sha256'],digest)
        self.assertEqual(self.tool('read_file',**args,offset=original['next_offset'],expected_sha256=digest.upper())['sha256'],digest)
        file.write_text('# Report\nSecond edition.')
        with patch.object(brain.file_text,'extract',side_effect=AssertionError('changed version was opened')):
            with self.assertRaisesRegex(ValueError,'指纹已变化'):
                self.tool('read_file',**args,expected_sha256=digest)
        with self.assertRaisesRegex(ValueError,'64 位'):
            self.tool('read_file',**args,expected_sha256='abc')
        self.assertIn('Second edition.',self.tool('read_file',**args)['text'])

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

    def test_confirmation_can_precede_a_separate_image_request(self):
        path = self.root / 'slides.pptx'
        path.write_bytes(b'Fictional handoff')
        sid = 'codex:fictional-approval-next-task'
        opening = '这版可以，很好。'
        approval = opening + '\n\n请另帮我做一张文章配图，我好给内容组投稿'
        self.archive_messages(sid, 'Atlas', [('assistant', f'已交付：{path}'), ('user', approval)])
        self.tool('log_work', project='Atlas', create_project=True, source='codex', session_id=sid,
                  summary='Record the approval and the separate request.',
                  deliverables=[{'path':str(path),'task':'Presentation','confirmed':True,'ordinal':1}])
        self.assertIn('用户确认', self.tool('get_project',project='Atlas',section='deliverables')['card'])

        delivery = {'session_id':sid,'ordinal':0,'role':'assistant','files':[str(path)]}
        source = {'session_id':sid,'ordinal':1,'role':'user','text':approval}
        claim = {'delivery_ref':'0','path':str(path)}
        self.assertEqual(brain.recall.confirmation_source(claim,source,{'0':delivery}),delivery)
        for followup in ('你再把这版的主办方改一下。', '你再给我生成一个横版的PPT。',
                         '你再给我生成一个头图，我好把这版替换掉。'):
            with self.subTest(followup=followup):
                self.assertFalse(brain.packet.confirms_file_delivery(opening + '\n\n' + followup,str(path)))
        self.assertFalse(brain.packet.confirms_file_delivery(approval,'poster.png'))

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

    def test_overwritten_delivery_does_not_inherit_previous_approval(self):
        path = self.root / 'checklist.md'
        path.write_text('# Checklist\nOwner is Amy.')
        sid = 'codex:fictional-overwrite'
        self.archive_messages(sid, 'Atlas', [('user', 'Draft the Atlas checklist.'),
                                            ('assistant', f'已交付：{path}'),
                                            ('user', '这份作为终版。')])
        with patch.object(brain, 'now', return_value='2026-01-02T00:00:00+00:00'):
            self.tool('log_work', project='Atlas', create_project=True, source='codex', work_id='approved',
                      session_id=sid, summary='Reviewed the checklist.',
                      deliverables=[{'path': str(path), 'task': 'Release process', 'confirmed': True, 'ordinal': 2}])
            before = self.tool('get_project', project='Atlas', section='deliverables')['card']
            self.assertIn('用户确认', before)
            path.write_text('# Checklist\nOwner is Sam.')
            self.tool('log_work', project='Atlas', source='kimi-work', work_id='overwritten',
                      summary='Updated the assigned owner in the same file.',
                      files=[{'path': str(path), 'status': '已交付'}])
        after = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn('实际使用待核', after)
        self.assertIn('Release process', after)
        self.assertNotIn('用户确认', after)
        self.assertIn('Owner is Sam.', self.tool('read_file',project='Atlas',path=path.name)['text'])
        with sqlite3.connect(self.data / 'agentsview/sessions.db') as index:
            for ordinal, role, text in ((3, 'assistant', f'已交付：{path}'), (4, 'user', '这份作为终版。')):
                index.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,0)',
                              (sid, ordinal, role, f'2026-01-03T00:00:0{ordinal}Z', text, text))
            index.execute("UPDATE sessions SET transcript_revision='2' WHERE id=?", (sid,))
        with patch.object(brain, 'now', return_value='2026-01-04T00:00:00+00:00'):
            self.tool('log_work', project='Atlas', source='codex', work_id='approved-again', session_id=sid,
                      summary='Reviewed the updated checklist.',
                      deliverables=[{'path': str(path), 'task': 'Release process', 'confirmed': True, 'ordinal': 4}])
        confirmed = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn('用户确认', confirmed)
        self.assertIn(sid + '#4', confirmed)

    def test_cached_approval_checks_the_latest_index_revision(self):
        path = self.root / 'checklist.md'
        path.write_text('# Checklist\nOwner is Amy.')
        sid = 'codex:fictional-cached-approval'
        self.archive_messages(sid, 'Atlas', [
            ('user', 'Draft the Atlas checklist.'),
            ('assistant', f'已交付：{path}'),
            ('user', '这份作为终版。'),
        ])
        self.tool('log_work', project='Atlas', create_project=True, source='codex',
                  session_id=sid, summary='Reviewed the checklist.',
                  deliverables=[{'path':str(path),'task':'Checklist','confirmed':True,'ordinal':2}])
        def delivery():
            return self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn('用户确认', delivery())
        with patch.object(brain.packet, 'build', side_effect=AssertionError('must reuse unchanged packet')):
            self.assertIn('用户确认', delivery())
        with sqlite3.connect(self.data / 'agentsview/sessions.db') as index:
            index.execute("UPDATE messages SET content='还要修改。', dialogue_text='还要修改。' WHERE ordinal=2")
            index.execute("UPDATE sessions SET transcript_revision='2' WHERE id=?", (sid,))
        self.assertIn('实际使用待核', delivery())

    def test_manual_save_keeps_historical_approval_separate_from_current_file(self):
        path = self.root / 'pitch.md'
        path.write_text('# Pitch\nOriginal pitch.')
        sid = 'codex:fictional-manual-save'
        self.archive_messages(sid, 'Atlas', [
            ('assistant', f'已交付：{path}'),
            ('user', '这份作为终版。'),
        ])
        self.tool('log_work', project='Atlas', create_project=True, source='codex',
                  session_id=sid, summary='Record the approved pitch.',
                  deliverables=[{'path': str(path), 'task': 'Pitch', 'confirmed': True, 'ordinal': 1}])
        args = {'project': 'Atlas', 'path': path.name}
        original = self.tool('read_file', **args)
        self.assertEqual(brain.packet.prepare_file(self.db, str(path), brain.clean)[0], 'built')

        path.write_text('# Pitch\nManually revised pitch.')
        self.assertEqual(brain.packet.prepare_file(self.db, str(path), brain.clean)[0], 'built')
        card = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn(f'{sid}#1', card)
        self.assertIn('用户确认记录 · 本机当前版本待核', card)
        current = self.tool('read_file', **args)
        self.assertIn('Manually revised pitch.', current['text'])
        self.assertNotEqual(current['sha256'], original['sha256'])
        with self.assertRaisesRegex(ValueError, '文件指纹已变化'):
            self.tool('read_file', **args, expected_sha256=original['sha256'])

    def test_a_user_merged_final_requires_evidence_of_the_user_version(self):
        draft = self.root / 'Agent 终版.md'
        merged = self.root / 'User merged.md'
        draft.write_text('# Agent delivery\nOriginal pitch.')
        merged.write_text('# User merged\nThe pitch I used.')
        sid = 'codex:fictional-user-merged'
        self.archive_messages(sid, 'Atlas', [
            ('user', 'Prepare the Atlas pitch.'),
            ('assistant', f'已交付：{draft}'),
        ])
        self.tool('log_work', project='Atlas', create_project=True, source='codex',
                  session_id=sid, summary='Delivered the agent version.',
                  deliverables=[{'task':'Pitch','path':str(draft),'ordinal':1,'final':True}])
        pending = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn(draft.name, pending)
        self.assertIn('实际使用待核', pending)
        self.assertNotIn(merged.name, pending)

        confirmation = f'我合并修改完了，这份作为终版：{merged}'
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            index.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,0)',
                          (sid, 2, 'user', '2026-01-03T00:00:02Z', confirmation, confirmation))
            index.execute("UPDATE sessions SET transcript_revision='2' WHERE id=?", (sid,))
        self.tool('log_work', project='Atlas', source='codex', session_id=sid,
                  summary='Recorded the user-confirmed merged file.',
                  deliverables=[{'task':'Pitch','path':str(merged),'ordinal':2,'confirmed':True}])
        approved = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn(merged.name, approved)
        self.assertIn('用户确认', approved)
        self.assertIn(sid + '#2', approved)
        self.assertIn('The pitch I used.', self.tool('read_file', project='Atlas', path=str(merged))['text'])

    def test_log_work_records_an_in_chat_delivery_with_its_assistant_excerpt(self):
        sid = 'kimi-work:fictional-chat-delivery'
        original = 'The checklist distinguishes scheduled and completed reviews.'
        later = 'The revised checklist distinguishes pending and completed reviews.'
        self.archive_messages(sid, 'Atlas', [
            ('user', 'Please draft the Atlas checklist.'),
            ('assistant', original),
            ('user', 'Keep it short and clear.'),
            ('assistant', later),
        ])
        def record(work_id, ordinal, text):
            return self.tool('log_work', project='Atlas', create_project=True, source='kimi-work',
                             summary='Record the checklist', work_id=work_id, session_id=sid,
                             deliverables=[{'text':text, 'task':'Checklist', 'ordinal':ordinal, 'final':True}])
        self.assertTrue(record('first', 1, original)['written'])
        self.assertFalse(record('first', 1, original)['written'])
        correct = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn(original, correct)
        self.assertIn(sid + '#1', correct)
        self.assertIn('实际使用待核', correct)
        record('wrong-role', 2, 'Keep it short and clear.')
        record('invented', 3, 'This excerpt did not appear.')
        history = self.tool('get_project', project='Atlas', section='history')['card']
        self.assertIn('原文待核', history)
        self.assertEqual(correct, self.tool('get_project', project='Atlas', section='deliverables')['card'])
        record('revision', 3, later)
        revised = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn(later, revised)
        self.assertIn(sid + '#3', revised)
        self.assertNotIn(original, revised)

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

    def test_writeback_reports_match_legacy_ids_and_cross_client_logs(self):
        raw = '00000000-1111-2222-3333-444444444444'
        linked = 'cowork:fictional-writeback'
        file = self.root / 'handoff.md'
        file.write_text('Fictional handoff.')
        for name, source, sid in (('codex-handoff', 'codex', 'codex:' + raw),
                                  ('cowork-handoff', 'cowork', linked)):
            self.tool('log_work', project='Atlas', create_project=True, work_id=name, source=source,
                      session_id=sid, summary='Deliver the handoff.',
                      files=[{'path': str(file), 'status': '已交付'}])
            self.db.execute("UPDATE work SET kind='summary' WHERE id=?", (name,))
            self.db.commit()
        self.tool('log_work', project='Atlas', source='codex', session_id=raw,
                  summary='Record the Codex handoff.', work_id='codex-writeback')
        self.tool('log_work', project='Atlas', source='codex', session_id=linked,
                  summary='Record the cross-client handoff.', work_id='cross-client-writeback')
        self.tool('log_work', project='Atlas', source='codex', session_id='codex:unrelated',
                  summary='Another conversation.', work_id='other-writeback')
        instant = datetime.now(timezone.utc)
        result = brain.usage.delivery_writeback(self.db, (instant - timedelta(days=1)).isoformat(),
                                               (instant + timedelta(days=1)).isoformat())
        self.assertEqual(result, {
            'codex': {'delivery_sessions': 1, 'with_writeback': 1},
            'cowork': {'delivery_sessions': 1, 'with_writeback': 1},
        })

    def test_health_writeback_uses_original_delivery_time_and_legacy_session_id(self):
        raw = '00000000-1111-2222-3333-444444444444'
        sid = 'codex:' + raw
        self.archive_messages(sid, 'Atlas', [('assistant', 'Delivered the checklist.')])
        instant = datetime.now(timezone.utc)
        fresh = (instant - timedelta(hours=1)).isoformat()
        old = (instant - timedelta(days=8)).isoformat()
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('agent', 'deleted_at', 'parser_malformed_lines', 'is_truncated'):
                index.execute('ALTER TABLE sessions ADD COLUMN ' + col)
            index.execute("UPDATE sessions SET agent='codex',parser_malformed_lines=0,is_truncated=0")
            index.execute('UPDATE messages SET timestamp=?', (fresh,))
            for other, stamp, role in (('cowork:old', old, 'assistant'), ('cowork:user', fresh, 'user')):
                index.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,0)',
                              (other, 0, role, stamp, 'A message.', 'A message.'))
        for other in (sid, 'cowork:old', 'cowork:user', 'cowork:missing'):
            self.db.execute('INSERT INTO recall_segments VALUES(?,0,0,?,?,?)',
                            (other, '1', fresh, json.dumps({'latest_time': fresh, 'delivery_anchors': [0]})))
        self.db.commit()
        self.tool('log_work', project='Atlas', create_project=True, source='codex',
                  session_id=raw, summary='Recorded the checklist.')
        with patch.object(brain.health, 'transcript_coverage', return_value={}), \
             patch.object(brain.health, 'recent_transcripts', return_value={}), \
             patch.object(brain.recall, 'pending_segments', return_value=[]):
            coverage = brain.health.report(self.db, self.data)['log_work_7d']
        self.assertEqual((coverage['delivery_sessions'], coverage['logged_sessions'], coverage['percent']),
                         (1, 1, 100.0))

    def test_health_reports_collection_failure_and_backs_up_sqlite(self):
        (self.data / 'coverage.json').write_text(json.dumps({'exit': 1, 'packetRefresh': {'status': 'failed'}}))
        self.tool('log_work', project='Atlas', create_project=True, summary='Initial record.', source='codex')
        self.assertIn('索引可能过期', self.tool('get_project', project='Atlas')['coverage'])
        report = brain.health.write_report(self.db, self.data)
        self.assertEqual(report['sqlite'], 'ok')
        self.assertEqual(report['collector_exit'], 1)
        with sqlite3.connect(brain.health.backup(self.db, self.data)) as snapshot:
            self.assertEqual(snapshot.execute('SELECT name FROM projects').fetchone(), ('Atlas',))

    def test_tag_queue_prioritizes_appended_messages_and_retries_after_local_budget_wait(self):
        sid = 'kimi-work:fictional-append'
        fresh = 'codex:fictional-new'
        historic = 'cowork:fictional-history'
        old = '2026-01-01T00:00:00+00:00'
        current = datetime.now(timezone.utc)
        appended = (current - timedelta(hours=23, minutes=55)).isoformat()
        newer = current.isoformat()
        self.archive_messages(sid, 'Atlas', [('user', 'Old tagged request.'), ('user', 'Unprocessed new request.')])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('agent', 'deleted_at', 'relationship_type', 'first_message'):
                index.execute(f'ALTER TABLE sessions ADD COLUMN {col} TEXT')
            index.execute("UPDATE sessions SET agent='kimi-work'")
            index.execute('UPDATE messages SET timestamp=CASE WHEN ordinal=0 THEN ? ELSE ? END', (old, appended))
            for name, agent, when in ((fresh, 'codex', newer), (historic, 'cowork', old)):
                index.execute('INSERT INTO sessions(id,agent,started_at) VALUES(?,?,?)', (name, agent, when))
                index.execute("INSERT INTO messages VALUES(?,0,'user',?,'Separate request.','Separate request.',0)",
                              (name, when))
            self.db.execute('INSERT INTO recall_segments VALUES(?,?,?,?,?,?)',
                            (sid, 0, 0, 'saved', old, json.dumps({'time': old, 'summary': 'Old tag.'})))
            queue = {row[0]:row[3:] for row in brain.recall.pending_segments(self.db, index)}
            self.assertEqual(queue[sid], (appended, appended, 'kimi-work'))
            self.assertEqual(queue[fresh], (newer, newer, 'codex'))
            self.assertEqual(queue[historic], (old, old, 'cowork'))
        args = (self.db, self.data, {'enabled': True}, 1, brain.visible,
                brain.packet.paths_in, brain.json_text, brain.model, brain.clean)
        with patch.object(brain.eco, 'call', side_effect=brain.eco.BudgetUnavailable('Wait for the ledger')) as call:
            result = brain.recall.tag_batch(*args)
            self.assertEqual(result['status'], 'BudgetUnavailable')
            payload = json.loads(call.call_args.args[2][1]['content'])
            self.assertEqual([row['ordinal'] for row in payload['messages']], [1])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recall_tag_attempts').fetchone()[0], 0)
        with patch.object(brain.eco, 'call', return_value={'summary': 'Current tag.', 'tags': [], 'anchors': [1]}):
            self.assertEqual(brain.recall.tag_batch(*args)['indexed'], 1)
        finish, raw = self.db.execute('SELECT finish,data FROM recall_segments WHERE session_id=?',(sid,)).fetchone()
        self.assertEqual(finish, 1)
        self.assertEqual(json.loads(raw)['time'], old)

    def test_tagged_delivery_requires_an_assistant_handoff_reference(self):
        sid = 'kimi-work:fictional-delivery-tag'
        report = self.root / 'report.md'
        report.write_text('FILE_BODY_STAYS_LOCAL')
        self.archive_messages(sid, 'Atlas', [('user', 'Please prepare the report.'),
            ('assistant', 'We can consider several ways to approach the report.'),
            ('assistant', f'Delivered the report at `{report}`.'),
            ('assistant', 'The revised message is ready to send to the participants in the final schedule.')])
        with sqlite3.connect(self.data/'agentsview'/'sessions.db') as index:
            for col in ('agent', 'deleted_at', 'relationship_type', 'first_message'):
                index.execute(f'ALTER TABLE sessions ADD COLUMN {col} TEXT')
            index.execute("UPDATE sessions SET agent='kimi-work'")
        args = (self.db, self.data, {'enabled': True}, 1, brain.visible,
                brain.packet.paths_in, brain.json_text, brain.model, brain.clean)
        for refs, expected in (([0, 1, 99], []), ([0, 2, 3, 99], [2, 3])):
            with self.subTest(refs=refs):
                reply = {'summary':'Fixture tag.', 'tags':['讨论', '交付'],
                         'anchors':[0], 'delivery_anchors':refs}
                with patch.object(brain.eco, 'call', return_value=reply) as call:
                    self.assertEqual(brain.recall.tag_batch(*args)['indexed'], 1)
                    self.assertNotIn('FILE_BODY_STAYS_LOCAL',brain.json_text(call.call_args.args[2]))
                saved = json.loads(self.db.execute('SELECT data FROM recall_segments WHERE session_id=?',(sid,)).fetchone()[0])
                self.assertEqual(saved['delivery_anchors'], expected)
                self.assertEqual('交付' in saved['tags'], bool(expected))
                self.assertIn('讨论', saved['tags'])
                self.db.execute('DELETE FROM recall_segments WHERE session_id=?',(sid,))

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

    def test_kimi_health_counts_native_user_and_assistant_dialogue(self):
        wire = self.root / 'wire.jsonl'
        first_time = 1_784_001_000_000

        def event(role, blocks, elapsed):
            return {'type':'agent.message.appended', 'time':first_time + elapsed,
                    'message': {'message': {'role': role, 'content': blocks}}}

        user = event('user', [{'type':'text','text':'Fixture question.'}], 0)
        assistant = event('assistant', [{'type':'think','text':'internal'},
                                         {'type':'text','text':'Fixture final response.'}], 1000)
        thought = event('assistant', [{'type':'think','text':'internal'}], 2000)
        tool_call = event('assistant', [], 3000)
        tool_result = event('tool', [{'type':'text','text':'Fixture tool response.'}], 4000)
        for messages, delay in (([user], 0), ([user, assistant, thought, tool_call, tool_result], 1000)):
            wire.write_text(''.join(json.dumps(message) + '\n' for message in messages))
            expected = datetime.fromtimestamp((first_time + delay) / 1000, timezone.utc)
            self.assertEqual(brain.health.latest_source_message(wire, wire), expected)

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
        validation_path = self.data/'recall_validation.jsonl'
        validation, = map(json.loads, validation_path.read_text().splitlines())
        self.assertEqual({key: validation[key] for key in ('sha256','checked','accepted','whitespace_matches','rejected')},
                         {'sha256':audit['sha256'], 'checked':3, 'accepted':2, 'whitespace_matches':0,
                          'rejected':{'reference':1}})
        self.assertNotIn('daily schedule', validation_path.read_text())
        self.assertEqual(validation_path.stat().st_mode & 0o777, 0o600)
        self.db.execute("INSERT INTO recall_segments VALUES(?,0,2,'revision','now',?)", (sid,
            json.dumps({'summary':'Atlas schedule handoff','tags':['Atlas'],'anchors':[2]})))
        self.db.commit()
        with patch.object(brain, 'model', side_effect=OSError('offline')):
            answer = self.tool('recall', project='Atlas', question='Where is the schedule?')
        self.assertEqual((answer['level'],answer['route']), (3,'local'))
        self.assertIn(2,[row['ordinal'] for row in answer['evidence']])

    def test_local_recall_recovers_inline_handoff_quotes_after_eco_filtering(self):
        sid = 'codex:fictional-handoff-quote'
        self.archive_messages(sid, 'Atlas', [
            ('user', 'Please fix the monthly voice recap.'),
            ('assistant', '已修复月报口播。缺日期时先说「按已有记录」，再报完成数。'
                          '<tool_result>PRIVATE_TOOL</tool_result>\n\n## 正文\nPRIVATE_FILE_BODY'),
        ])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('deleted_at','first_message','relationship_type'):
                index.execute('ALTER TABLE sessions ADD COLUMN ' + col + ' TEXT')
        self.tool('log_work', project='Atlas', create_project=True, source='codex', session_id=sid,
                  summary='Fixed the monthly voice recap.')
        args = {'project': 'Atlas', 'question': '月报口播修了什么？'}
        with patch.object(brain.eco, 'call', side_effect=brain.eco.RouteUnavailable) as route:
            local = self.tool('recall', **args, local_only=True)
            route.assert_not_called()
            fallback = self.tool('recall', **args)
        self.assertNotIn('按已有记录', str(route.call_args.args[2]))
        for answer in (local, fallback):
            self.assertEqual(answer['route'], 'local')
            handoff, = [row for row in answer['evidence'] if row['ordinal'] == 1]
            self.assertIn('「按已有记录」', handoff['text'])
            self.assertNotIn('PRIVATE_', handoff['text'])

    def test_recall_allows_whitespace_differences_and_checks_original_quote(self):
        evidence = {'1:1': {'role':'user','session_id':'codex:test','ordinal':1,
                            'text':'采用会议口径\n\t4,000 万。'}}
        claim = {'ref':'1:1','kind':'rule','text':'采用会议口径。'}
        diagnostics = {}
        checked = brain.recall.checked_claims({'claims':[
            {**claim,'quote':'采用会议口径 4,000 万。'},
            {**claim,'quote':'采用会议口径 5,000 万。'},
            {**claim,'quote':'采用会议口径 4,000 万!'},
            {**claim,'quote':'采用会议口径 4,000 万。','kind':'deliverable'},
        ]},evidence,diagnostics)
        self.assertEqual([row['quote'] for row in checked], ['采用会议口径\n\t4,000 万。'])
        self.assertEqual(diagnostics,{'checked':4,'accepted':1,'whitespace_matches':1,
                                     'rejected':{'quote':2,'role':1}})
        self.assertFalse(brain.recall.checked_claims({'claims':[{**claim,'quote':'采用 会议口径'}]},evidence))

    def test_recall_checks_one_explicit_yuan_amount_without_requiring_matching_units(self):
        source = {'role':'user','session_id':'codex:fictional-amount','ordinal':2,
                  'text':'收入采用会议口径。2026 年约为４，０００ 万元，之后另核。'}
        claim = {'ref':'1:2','quote':'采用会议口径。','kind':'rule'}
        diagnostics = {}
        replies = {'claims':[{**claim,'text':text} for text in
                             ('2026 年收入为 5,000 万元。','2026 年约 0.4 亿元。','写作40,000,000元。',
                              '采用会议口径。')]}
        found = brain.recall.checked_claims(replies, {'1:2':source}, diagnostics)
        self.assertEqual([item['text'] for item in found], [item['text'] for item in replies['claims'][1:]])
        self.assertEqual(diagnostics['rejected'], {'amount_mismatch':1})
        multiple = {**source,'text':source['text']+'原来另有 1.2 亿元的预测。'}
        self.assertTrue(brain.recall.checked_claims({'claims':[replies['claims'][0]]}, {'1:2':multiple}))
        shorthand = {**source,'text':'采用会议口径。2026 年约 4,000 万、2027 年收入过亿。'}
        self.assertFalse(brain.recall.checked_claims({'claims':[replies['claims'][0]]}, {'1:2':shorthand}))
        self.assertEqual(brain.recall.yuan_amounts('粉丝总数 4,000 万。'), set())
        no_amount = {**source,'text':'采用会议口径。优先检查 8 个元素。'}
        self.assertTrue(brain.recall.checked_claims({'claims':[replies['claims'][0]]}, {'1:2':no_amount}))

    def test_recall_resolves_a_local_uri_or_unique_name_from_the_cited_message(self):
        path = '/fictional/client outputs/Agenda.docx'
        source = {'role':'assistant', 'session_id':'cowork:fictional', 'ordinal':2,
                  'text':'The seven-page agenda is ready.', 'files':[path]}
        claim = {'ref':'1:2', 'kind':'deliverable', 'text':source['text'], 'quote':source['text']}
        candidates = [path, 'computer://' + path, 'Agenda.docx', '/unregistered/Agenda.docx',
                      'computer://host/fictional/client outputs/Agenda.docx']
        diagnostics = {}
        verified = brain.recall.checked_claims({'claims':[{**claim, 'path':p} for p in candidates[:4]]},
                                               {'1:2':source}, diagnostics)
        self.assertEqual([row['file'] for row in verified], ['Agenda.docx'] * 3)
        self.assertEqual(diagnostics['rejected'], {'path':1})
        self.assertFalse(brain.recall.checked_claims({'claims':[{**claim, 'path':candidates[4]}]}, {'1:2':source}))
        ambiguous = {**source, 'files':[path, '/another/Agenda.docx']}
        self.assertFalse(brain.recall.checked_claims({'claims':[{**claim, 'path':'Agenda.docx'}]}, {'1:2':ambiguous}))

    def test_recall_retains_claims_and_uncertainty_before_pruning_quotes(self):
        answer = {'level':1, 'route':'Eco', 'cached':True, 'uncertain':'最终日期等对方确认。', 'evidence':[
            {'kind':'deliverable','text':'已完成报告。','session_id':'cowork:fictional','ordinal':n,
             'file':'报告.md','quote':'这是一段较长的原始交付说明。'*8}
            for n in range(4)]}
        compact = brain.recall.bounded(answer, brain.json_text, None)
        self.assertEqual(compact['verified'], 'citations')
        self.assertEqual([item['ordinal'] for item in compact['evidence']], list(range(4)))
        self.assertEqual(compact['uncertain'], answer['uncertain'])
        self.assertTrue(all('quote' in item for item in answer['evidence']))
        self.assertLessEqual(brain.response_size(compact), 800)
        long_source = 'cowork:' + 'a' * 80
        long_answer = {**answer,'evidence':[{**item,'text':'可核结论内容。'*17,'session_id':long_source}
                                            for item in answer['evidence']]}
        limited = brain.recall.bounded(long_answer, brain.json_text, None)
        self.assertGreater(limited['omitted_claims'], 0)
        self.assertEqual(len(limited['evidence'])+limited['omitted_claims'], 4)
        self.assertEqual(limited['uncertain'], answer['uncertain'])
        self.assertEqual({limited['sessions'][item['session']] for item in limited['evidence']}, {long_source})
        self.assertLessEqual(brain.response_size(limited), 800)

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

    def test_recall_checks_hidden_revisions_before_returning_cached_approval(self):
        sid = 'codex:fictional-hidden-version'
        file = '/tmp/atlas.md'
        approval = '这份 atlas.md 作为终版。'
        self.archive_messages(sid,'Atlas',[
            ('assistant',f'First version [{file}]({file}).'),
            ('assistant','I checked the schedule.'),
            ('user',approval),
        ])
        work = self.tool('log_work',project='Atlas',create_project=True,source='codex',summary='Deliver the report.',session_id=sid)
        index_file = self.data/'projects'/work['project_id']/'recall.md'
        evidence = {'1:0':{'role':'assistant','text':'First version','session_id':sid,'ordinal':0,'files':[file]},
                    '1:2':{'role':'user','text':approval,'session_id':sid,'ordinal':2}}
        reply = {'claims':[{'ref':'1:2','kind':'confirmed','quote':approval,'text':'Approved.','path':file,
                            'delivery_ref':'1:0'}]}
        args = {'project':'Atlas','question':'Which version was approved?'}
        with patch.object(brain.recall,'evidence_package',return_value=({},evidence,[])), \
             patch.object(brain.eco,'call',return_value=reply) as eco_call:
            initial = self.tool('recall',**args)
            self.assertEqual(initial['evidence'][0]['delivery_ordinal'],0)
            self.assertTrue(self.tool('recall',**args)['cached'])
            self.assertIn(approval,index_file.read_text())
            self.assertNotIn('Approved.',index_file.read_text())
            with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
                newer = f'Replaced the report at [{file}]({file}).'
                index.execute('UPDATE messages SET content=?,dialogue_text=? WHERE session_id=? AND ordinal=1',(newer,newer,sid))
                index.execute("UPDATE sessions SET transcript_revision='2' WHERE id=?",(sid,))
            result = self.tool('recall',**args)
            self.assertEqual(result['route'],'local')
            self.assertEqual(eco_call.call_count,2)
            self.assertNotIn(approval,index_file.read_text())
        validation = [json.loads(line) for line in (self.data/'recall_validation.jsonl').read_text().splitlines()]
        self.assertEqual(validation[-1]['rejected'],{'confirmation':1})

    def test_recall_index_keeps_quoted_evidence_and_marks_legacy_paraphrases(self):
        pid = self.tool('log_work', project='Atlas', create_project=True, source='codex',
                        summary='Review project rules.')['project_id']
        base = {'kind':'answer','session_id':'codex:fictional-citation','ordinal':2}
        for fingerprint, evidence in (
                ('legacy', {**base,'text':'Old model paraphrase.'}),
                ('new', {**base,'text':'Wrong amount 5,000.', 'quote':'Use 4,000.'})):
            self.db.execute('INSERT INTO recall_answers VALUES(?,?,?,?)',
                            (fingerprint,pid,'2026-01-01',json.dumps({'evidence':[evidence]})))
        brain.recall.write_index(self.db,self.data,pid,'Atlas')
        body = (self.data/'projects'/pid/'recall.md').read_text()
        excerpts, prior = body.split('## 待复核的历史转述')
        self.assertIn('Use 4,000.',excerpts)
        self.assertIn('Old model paraphrase.',prior)
        self.assertNotIn('Wrong amount',body)

    def test_local_excerpt_preserves_a_citation_cut_by_the_length_limit(self):
        lead = 'The collector runs every five minutes. See '
        citation = 'codex:12345678-1111-2222-3333-444444444444（第 16 条消息）'
        text = lead + citation + '. Extra context follows. ' * 6
        clipped = brain.recall.local_text_excerpt(text, 100)
        self.assertIn('The collector runs every five minutes.', clipped)
        self.assertIn('codex:12345678-1111-2222-3333-444444444444（第 16 条', clipped)
        self.assertLessEqual(len(clipped), len(brain.extraction.text_excerpt(text, 100)))
        self.assertEqual(brain.recall.local_text_excerpt(text, len(text)), text)
        other = lead + 'ordinary details. ' * 15
        self.assertEqual(brain.recall.local_text_excerpt(other, 100), brain.extraction.text_excerpt(other, 100))

    def test_local_recall_reads_registered_task_identifiers_from_original_messages(self):
        sid = 'codex:fictional-task-identifier'
        good = self.root / 'old' / 'README.md'
        other = self.root / 'elsewhere' / 'README.md'
        user = '我先建一个微信2并采用轻量模型。'
        self.archive_messages(sid, 'Atlas', [
            ('assistant', f'The CV acceptance entry is [here]({good}).'),
            ('assistant', f'The CV6 entry is [here]({other}).'),
            ('user', user),
        ])
        archive = self.data/'agentsview/sessions.db'
        def collect(question, items):
            return brain.recall.named_task_evidence(archive,question,brain.visible,brain.packet.paths_in,brain.json_text,items)
        entries = [{'path':str(good), 'ref':{'session_id':sid,'ordinal':0}},
                   {'path':str(other), 'ref':{'session_id':sid,'ordinal':1}},
                   {'text':user, 'ref':{'session_id':sid,'ordinal':2}},
                   {'text':'这句话未出现在历史里。', 'ref':{'session_id':sid,'ordinal':2}}]
        self.assertEqual(list(collect('CV 从哪个入口继续？',entries)),[(sid,0)])
        self.assertEqual(list(collect('为什么要建微信2？',entries)),[(sid,2)])
        self.assertEqual(collect('为什么要建微信3？',entries),{})

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

    def test_recall_uses_legacy_codex_work_log_ids_without_rewriting_them(self):
        bare = '12345678-1111-2222-3333-444444444444'
        canonical = 'codex:' + bare
        args = dict(project='Atlas', create_project=True, source='codex', session_id=bare,
                    work_id='atlas-collector', summary='Saved collector settings.',
                    conclusions=[{'text': 'Atlas collector uses a five minute interval.', 'ordinal': 3}])
        result = self.tool('log_work', **args)
        sessions, seeds = brain.recall.project_seeds(self.db, result['project_id'], True)
        self.assertEqual(sessions, {canonical})
        self.assertTrue(seeds[canonical][3][1])
        evidence = {'other': {'session_id': 'codex:other', 'ordinal': 1},
                    'anchor': {'session_id': canonical, 'ordinal': 3}}
        ordered = brain.recall.prioritize_logged_evidence(
            self.db, result['project_id'], 'What interval does the collector use?', evidence)
        self.assertEqual(list(ordered), ['anchor', 'other'])
        self.assertEqual(self.db.execute('SELECT session_id FROM work').fetchone()[0], bare)
        self.assertFalse(self.tool('log_work', **args)['written'])

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
            self.assertEqual([item['ordinal'] for item in value['evidence']],[0,2])
            self.assertEqual(value['next_offset'],2)
            self.assertEqual(set(value['evidence'][1]['files']),{notes.name,evidence.name})
            self.assertNotIn('FILE_BODY_STAYS_LOCAL',brain.json_text(value))
            self.assertNotIn(str(report),brain.json_text(route.call_args.args[2]))
            focused = self.tool('recall',project='Atlas',question='Atlas 具体哪份报告?',local_only=True)
            self.assertEqual([item['ordinal'] for item in focused['evidence']],[1])
            rest = self.tool('recall',project='Atlas',question='Atlas 接着做到哪了？',local_only=True,offset=2)
            self.assertEqual([item['ordinal'] for item in rest['evidence']],[1])
            self.assertEqual(route.call_count,1)

    def test_named_progress_uses_the_latest_logged_conclusion_with_the_delivery(self):
        legacy_id = '12345678-1111-4222-8333-444444444444'
        sid = 'codex:' + legacy_id
        report = self.root / 'report.md'
        report.write_text('FILE_BODY_STAYS_LOCAL')
        self.archive_messages(sid, 'Atlas', [('assistant', f'Delivered `{report}`.'),
            ('assistant', 'The current decision is to keep the phase closed.')])
        self.tool('log_work', project='Atlas', create_project=True, summary='Delivered the report.',
                  source='codex', session_id=sid, deliverables=[{'path':str(report),'task':'Report','ordinal':0}])
        self.tool('log_work', project='Atlas', summary='Recorded the current decision.', source='codex',
                  session_id=legacy_id, conclusions=[{'text':'Unconfirmed statement.','ordinal':999},
                                                     {'text':'The phase stays closed.','ordinal':1}])
        evidence = brain.recall.project_progress_evidence(
            self.db,self.data/'agentsview'/'sessions.db',brain.resolve_project_id(self.db,'Atlas'),
            'Atlas 做到哪了？',brain.visible,brain.packet.paths_in,brain.json_text,
            lambda pid: brain.packet.load(self.db,self.data,pid,brain.visible,brain.clean,lazy=True)[0])
        self.assertEqual(list(evidence),[(sid,1),(sid,0)])
        self.assertIn('phase closed',evidence[sid,1]['text'])

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

    def test_recall_keeps_nearby_handoff_from_explicit_client(self):
        evidence = {ref:{'session_id':sid} for ref,sid in [('a','cowork:meta'),('b','kimi-work:target'),
                    ('c','kimi-work:other'),('d','kimi-work:target'),('e','codex:context')]}
        named = brain.recall.named_client
        prioritize = brain.recall.prioritize_client_evidence
        self.assertEqual(list(prioritize(named('Kimi Work 的可编辑文件在哪？'),evidence)),['b','d','c','a','e'])
        self.assertEqual(list(prioritize(named('上次在 Kimi Work 里完成的？'),evidence,{'kimi-work:target'})),
                         ['b','d','a','c','e'])
        self.assertEqual(list(prioritize(named('上次在 Kimi Work 里完成的？'),evidence,{'cowork:meta'})),list(evidence))
        for question in ('Kimi Work 和 Codex 的结果有什么区别？','哪里提到 Kimi Work？','不要在 Kimi Work 里找。'):
            self.assertIsNone(named(question))

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
                          'current_sha256':timeline['current']['sha256'],
                          'current_matches_reported_hash':False})
        claim = {'ref':old_ref,'text':'The initial report had two pages.','kind':'deliverable',
                 'quote':'had 2 页','path':str(version)}
        verified, = brain.recall.checked_claims({'claims':[claim]},evidence)
        self.assertEqual(verified['later_version'], {'session_id':sid,'ordinal':3,'reported_pages':[2,1],
                         'reported_format_quotes':['`PAGE` 域','No footer'],'current_matches_reported_hash':False,
                         'current_sha256':timeline['current']['sha256']})
        self.assertNotIn(str(version),str(verified))
        other = f'{self.root}/notice.docx'
        unrelated = {**evidence[old_ref],'files':[str(version),other]}
        checked, = brain.recall.checked_claims({'claims':[{**claim,'path':other}]},{old_ref:unrelated})
        self.assertNotIn('later_version',checked)

    def test_recall_search_uses_agentsview_trigrams_for_full_words(self):
        with sqlite3.connect(':memory:') as db:
            db.executescript("""CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id, ordinal, role,
                              is_system, dialogue_text, content);
                CREATE VIRTUAL TABLE message_trigram_fts USING fts5(content,tokenize='trigram',detail='none');
                INSERT INTO messages VALUES(1,'codex:one',10,'assistant',0,'Atlas-notes.md 修订稿 归档','');
                INSERT INTO messages VALUES(2,'codex:two',20,'assistant',0,'Atlas plan','');
                INSERT INTO messages VALUES(3,'codex:three',30,'assistant',0,'ATLAS-NOTES.MD 终稿','');
                INSERT INTO messages VALUES(4,'codex:four',40,'assistant',0,'atl tla las as- s-n -no not ote tes es. s.m .md','');
                INSERT INTO messages VALUES(5,'codex:five',50,'user',0,NULL,'归档');
                INSERT INTO messages VALUES(6,'codex:six',60,'assistant',0,'ÄBC','');
                INSERT INTO messages VALUES(7,'codex:seven',70,'assistant',1,'Atlas-notes.md 归档','');
                INSERT INTO message_trigram_fts(rowid,content) SELECT id,coalesce(dialogue_text,content) FROM messages;""")
            self.assertEqual([(sid,ordinal) for sid,ordinal,_ in brain.recall.text_matches(db,{'atlas-notes.md','修订稿'})],
                             [('codex:one',10),('codex:three',30)])
            mixed = {'atlas-notes.md','归档','äbc'}
            indexed = sorted(brain.recall.text_matches(db,mixed))
            self.assertEqual([sid for sid,_,_ in indexed],['codex:five','codex:one','codex:three'])
            db.execute('DROP TABLE message_trigram_fts')
            self.assertEqual(indexed,sorted(brain.recall.text_matches(db,mixed)))

    def test_file_timelines_keep_same_named_deliveries_in_separate_directories(self):
        sid = 'codex:fictional-documents'
        primary, unrelated, alternate = [self.root / folder / name for folder, name in (
            ('launch', 'report.docx'), ('research', 'report.docx'), ('launch', 'report_v2.docx'))]
        for file in (primary, unrelated, alternate):
            file.parent.mkdir(exist_ok=True)
            file.write_text(file.parent.name + file.name)
        self.archive_messages(sid, 'Atlas', [
            ('assistant', f'Delivered [{primary.name}]({primary}). 2 页. SHA256: {"a"*64}'),
            ('assistant', f'Replaced [{primary.name}]({primary}). 1 页. SHA256: {"b"*64}'),
            ('assistant', f'Delivered [{alternate.name}]({alternate}). 3 页.'),
            ('assistant', f'Delivered [{unrelated.name}]({unrelated}). 9 页.'),
            ('assistant', f'Compare [{primary.name}]({primary}) and [{unrelated.name}]({unrelated}). 5 页.'),
            ('assistant', f'Compare [{primary.name}]({primary}) and [{alternate.name}]({alternate}). 7 页.'),
        ])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            index.row_factory = sqlite3.Row
            rows = brain.extraction.transcript(index,sid,brain.visible,brain.packet.paths_in,brain.json_text,
                                               outbound=True,focused=True)
            evidence = {(sid, row['ordinal']): row for row in rows}
            order = {key: (1, 0, 0, 0, key[1]) for key in evidence}
            found = brain.recall.file_timeline(index,evidence,order,{sid:('1',str(self.root))},brain.visible,
                                               brain.packet.paths_in,brain.json_text)
        timelines = {Path(path).parent.name: records for _,records,_,path in found}
        self.assertEqual(set(timelines), {'launch','research'})
        self.assertEqual(set(timelines['launch']), {(sid,n) for n in (0,1,2,4)})
        self.assertEqual(set(timelines['research']), {(sid,n) for n in (3,4)})
        self.assertNotIn('reported_pages',timelines['launch'][sid,4])
        self.assertNotIn('reported_pages',timelines['research'][sid,4])

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

    def test_local_recall_preserves_handoff_details_after_a_long_file_link(self):
        path = self.root / ('nested-' * 32) / 'Orders (EN).xlsx'
        details = '处理方式：订单数字、表格结构、345 个公式全部原样保留。'
        text = (f'翻译完成。\n\n[查看英文版表格](computer://{path})\n\n{details}\n\n'
                f'[本机出处](<{path}:12>) [参考](https://example.invalid)')
        source = {'session_id': 'cowork:fictional-orders', 'ordinal': 53, 'role': 'assistant',
                  'text': text, 'files': [str(path)]}
        result = brain.recall.local_evidence({'1:53': source}, brain.json_text, None)
        handoff, = result['evidence']
        self.assertEqual(handoff['ordinal'], 53)
        self.assertEqual(handoff['files'], [path.name])
        self.assertIn(details, handoff['text'])
        self.assertIn('https://example.invalid', handoff['text'])
        self.assertNotIn(str(path), handoff['text'])
        self.assertLessEqual(len(brain.json_text(result).encode()), 800)

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
        choice = json.dumps({'kind':'answer_to_pending_question',
                             'answers':[{'id':'plan','selected':['批准换 8 个']}]})
        self.archive_messages(sid,'General',[('user','Adjust the Bot model allocation.'),
                                             ('user',choice),
                                             ('assistant','回读核对通过，模型共调整 4 个。')])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('deleted_at','first_message','relationship_type'):
                index.execute('ALTER TABLE sessions ADD COLUMN '+col+' TEXT')
            index.execute('UPDATE messages SET is_system=1 WHERE ordinal=1')
            index.execute('UPDATE messages SET ordinal=26 WHERE ordinal=2')
            index.row_factory=sqlite3.Row
            project=self.tool('log_work',project='Atlas',create_project=True,source='codex',summary='Setup.')['project_id']
            _,evidence,_=brain.recall.evidence_package(self.db,index,project,'Bot 模型实际改了多少？',
                                                       brain.visible,brain.packet.paths_in,brain.json_text,use_tags=False)
        rows=list(evidence.values())
        self.assertEqual({(row['session_id'],row['ordinal']) for row in rows},{(sid,0),(sid,1),(sid,26)})
        result_index=next(i for i,row in enumerate(rows) if row['ordinal']==26 and '4 个' in row['text'])
        self.assertEqual(rows[result_index+1]['ordinal'],1)
        self.assertEqual(rows[result_index+1]['text'],'User selected: 批准换 8 个')

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

    def test_search_falls_back_to_index_when_archive_api_is_unavailable(self):
        sid = 'kimi-work:fictional-recent-search'
        self.archive_messages(sid, 'Atlas', [('user','Check the final result'),
                                             ('assistant','The glass compass is ready.'),
                                             ('assistant','A glass compass was used by a tool.')])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            index.execute('ALTER TABLE sessions ADD COLUMN deleted_at TEXT')
            index.execute('ALTER TABLE sessions ADD COLUMN agent TEXT')
            index.execute("UPDATE sessions SET agent='kimi-work'")
            index.execute('UPDATE messages SET is_system=1 WHERE ordinal=2')
        with patch.object(brain,'archive',side_effect=RuntimeError('Loopback unavailable')):
            result = self.tool('search', query='glass compass')
            self.assertEqual((result['status'],result['route']),('ok','local'))
            self.assertEqual([(x['session_id'],x['source'],x['ordinal']) for x in result['matches']],[(sid,'kimi-work',1)])
            self.assertEqual(self.tool('search',query='used by a tool')['status'],'没命中')
            with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
                index.execute("UPDATE sessions SET deleted_at='2026-01-01T00:00:00Z'")
            self.assertEqual(self.tool('search',query='glass compass')['status'],'没命中')

    def test_shared_search_and_history_skip_only_internal_reviews(self):
        review = 'codex:fictional-review'
        normal = 'codex:fictional-project-subtask'
        term = 'The fictional copper compass is ready.'
        prefix = brain.CODEX_REVIEW_PREFIXES[0]
        self.archive_messages(review, 'Approval record', [('user', prefix), ('assistant', term)])
        with sqlite3.connect(self.data/'agentsview'/'sessions.db') as index:
            for col in ('agent', 'relationship_type', 'first_message', 'deleted_at', 'file_path', 'ended_at'):
                index.execute(f'ALTER TABLE sessions ADD COLUMN {col} TEXT')
            index.execute("UPDATE sessions SET agent='codex',relationship_type='subagent',first_message=?", (prefix,))
            index.execute("INSERT INTO sessions(id,agent,relationship_type,first_message) VALUES(?,'codex','subagent','Project check')", (normal,))
            index.execute("INSERT INTO messages VALUES(?,0,'assistant','2026-01-01T00:00:00Z',?,?,0)", (normal, term, term))

        def search_candidates(query, limit):
            yield review, 'codex', {'role':'assistant', 'ordinal':1, 'content':term}
            yield normal, 'codex', {'role':'assistant', 'ordinal':0, 'content':term}

        with patch.object(brain, 'search_matches', side_effect=search_candidates):
            self.assertEqual([hit['session_id'] for hit in self.tool('search', query='copper compass')['matches']], [normal])
        with patch.object(brain, 'archive', side_effect=RuntimeError('Loopback unavailable')):
            self.assertEqual([hit['session_id'] for hit in self.tool('search', query='copper compass')['matches']], [normal])
            with self.assertRaisesRegex(ValueError, '内部审核'):
                self.tool('get_session', session_id=review, ordinal=1)
        info = {'agent':'codex', 'relationship_type':'subagent', 'first_message':prefix}
        with patch.object(brain, 'archive', return_value={'session':info}) as route:
            with self.assertRaisesRegex(ValueError, '内部审核'):
                self.tool('get_session', session_id=review, ordinal=1)
            route.assert_called_once_with('session','get',review)

    def test_get_session_falls_back_and_paginates_long_original_text(self):
        sid = 'kimi-work:fictional-local-excerpt'
        original = '保留第一版口径。' * 475
        self.archive_messages(sid, 'Atlas', [('user','Use the original version.'),('assistant',original),
                                             ('assistant','Internal output.'),('user','Next request.')])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('agent','file_path','ended_at','deleted_at'):
                index.execute(f'ALTER TABLE sessions ADD COLUMN {col} TEXT')
            index.execute('UPDATE messages SET ordinal=ordinal*3, is_system=ordinal=2')
        with patch.object(brain,'archive',side_effect=RuntimeError('Query service unavailable')):
            page = self.tool('get_session',session_id=sid,around=3,limit=2)
            retrieved = []
            for _ in range(20):
                self.assertEqual(page['route'],'local')
                self.assertLessEqual(brain.response_size(page),brain.QUERY_BYTES)
                retrieved.extend(row['text'] for row in page['messages'] if row['ordinal']==3)
                self.assertFalse(any(row['ordinal']==6 for row in page['messages']))
                if 'next_text_offset' not in page:
                    break
                self.assertEqual(page['next_from'],3)
                page = self.tool('get_session',session_id=sid,start=3,text_offset=page['next_text_offset'],limit=2)
            self.assertEqual(''.join(retrieved),original)
            self.assertEqual(self.tool('get_session',session_id=sid,start=6,limit=1)['messages'][0]['ordinal'],9)

    def test_get_session_replays_the_user_choice_on_both_archive_paths(self):
        sid = 'deepseek-harness:fictional-user-choice'
        choice = json.dumps({
            'kind': 'answer_to_pending_question',
            'answers': [{'id': 'q', 'selected': ['Keep figures']}],
            'questions': [{'id': 'q', 'question': 'Internal choice prompt',
                           'options': [{'label': 'Keep figures', 'description': 'Use meeting numbers. password=example'},
                                       {'label': 'Discard figures', 'description': 'Unselected text'}]}],
        })
        dialogue = [('assistant', 'Source figures.'), ('user', choice),
                    ('user', '{"kind":"internal", "text":"Hidden event"}')]
        self.archive_messages(sid, 'Atlas', dialogue)
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('agent','file_path','ended_at','deleted_at'):
                index.execute(f'ALTER TABLE sessions ADD COLUMN {col} TEXT')
            index.execute('UPDATE messages SET is_system=ordinal>0')
        rows = [{'ordinal': n, 'role': role, 'content': value, 'is_system': n>0}
                for n, (role,value) in enumerate(dialogue)]
        with patch.object(brain, 'archive', side_effect=[{'session': {}}, {'messages': rows}]):
            online = self.tool('get_session', session_id=sid, around=1, limit=3)
        with patch.object(brain, 'archive', side_effect=RuntimeError('Query service unavailable')):
            local = self.tool('get_session', session_id=sid, around=1, limit=3)
        expected = [(0, 'Source figures.'),
                    (1, 'User selected: Keep figures · Use meeting numbers. password=[REDACTED]')]
        for result in (online, local):
            self.assertEqual([(row['ordinal'],row['text']) for row in result['messages']], expected)

    def test_get_session_exact_ordinal_returns_only_the_requested_message(self):
        sid = 'kimi-work:fictional-exact'
        original = '保留原要求。' * 260
        rows = [('user','Use the original wording.'), ('assistant',original), ('user','Future direction.')]
        self.archive_messages(sid, 'Atlas', rows)
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('agent','file_path','ended_at','deleted_at'):
                index.execute(f'ALTER TABLE sessions ADD COLUMN {col} TEXT')
            index.execute('UPDATE messages SET ordinal=ordinal*2')
        returned = [{'ordinal':n*2,'role':role,'dialogue_text':line} for n,(role,line) in enumerate(rows)]
        with patch.object(brain,'archive',side_effect=[{'session': {}}, {'messages': returned}]), \
             patch.object(brain.usage,'token_counter',return_value=len):
            exact = self.tool('get_session',session_id=sid,ordinal=2)
            self.assertEqual([row['ordinal'] for row in exact['messages']],[2])
            self.assertLessEqual(len(brain.json_text(exact)),brain.EXACT_MESSAGE_TOKENS)
            self.assertNotIn('path',exact)
        with patch.object(brain,'archive',side_effect=RuntimeError('Query service unavailable')), \
             patch.object(brain.usage,'token_counter',return_value=None):
            parts,offset = [],0
            while True:
                result = self.tool('get_session',session_id=sid,ordinal=2,text_offset=offset)
                self.assertEqual([row['ordinal'] for row in result['messages']],[2])
                self.assertLessEqual(brain.response_size(result),brain.QUERY_BYTES)
                parts.append(result['messages'][0]['text'])
                if 'next_text_offset' not in result:
                    self.assertNotIn('next_from',result)
                    break
                self.assertGreater(result['next_text_offset'],offset)
                offset = result['next_text_offset']
            self.assertEqual(''.join(parts),original)
            missing = self.tool('get_session',session_id=sid,ordinal=3)
            self.assertEqual((missing['status'],missing['messages']),('没命中',[]))
            mixed = self.tool('get_session',session_id=sid,ordinal=2,around=1)
            self.assertEqual([row['ordinal'] for row in mixed['messages']],[2])
            self.assertIn('around=2',mixed['hint'])
            self.assertLessEqual(brain.response_size(mixed),brain.QUERY_BYTES)
            resumed = mixed['next_text_offset']
            next_page = self.tool('get_session',session_id=sid,ordinal=2,around=1,text_offset=resumed)
            self.assertEqual([row['ordinal'] for row in next_page['messages']],[2])
            self.assertTrue(original[resumed:].startswith(next_page['messages'][0]['text']))
            context = self.tool('get_session',session_id=sid,around=2,limit=3)
            self.assertEqual(context['messages'][0]['ordinal'],0)

    def test_get_session_keyword_finds_literal_text_deep_in_one_message(self):
        sid = 'codex:fictional-keyword'
        original = ('Long background. ' * 800).join('Setting [Interval] = ' + str(n) for n in range(5))
        self.archive_messages(sid, 'Atlas', [('assistant',original),('user','A different message: OtherSetting.')])
        with sqlite3.connect(self.data/'agentsview/sessions.db') as index:
            for col in ('agent','file_path','ended_at','deleted_at'):
                index.execute(f'ALTER TABLE sessions ADD COLUMN {col} TEXT')
        with patch.object(brain,'archive',side_effect=RuntimeError('Query service unavailable')), \
             patch.object(brain.usage,'token_counter',return_value=None):
            results,offset = [],0
            while True:
                page = self.tool('get_session',session_id=sid,ordinal=0,contains='[interval]',text_offset=offset)
                self.assertLessEqual(brain.response_size(page),brain.QUERY_BYTES)
                self.assertTrue(page['messages'])
                results.extend(page['messages'])
                if 'next_text_offset' not in page:
                    break
                self.assertGreater(page['next_text_offset'],offset)
                offset = page['next_text_offset']
            self.assertEqual(len(results),5)
            self.assertTrue(all(row['ordinal']==0 and '[Interval]' in row['text'] for row in results))
            self.assertGreater(results[-1]['match_offset'],40_000)
            self.assertIn('[Interval] = 4',results[-1]['text'])
            missing = self.tool('get_session',session_id=sid,ordinal=0,contains='OtherSetting')
            self.assertEqual((missing['status'],missing['messages']),('没命中',[]))
            with self.assertRaisesRegex(ValueError,'exact message ordinal'):
                self.tool('get_session',session_id=sid,contains='Interval')

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

    def write_presentation(self, path, pages):
        from xml.sax.saxutils import escape
        relationships = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        with ZipFile(path, 'w') as deck:
            slides = ''.join(f'<p:sldId r:id="slide{n}"/>' for n in range(1, len(pages) + 1))
            deck.writestr('ppt/presentation.xml',
                          f'<p:presentation xmlns:p="urn:p" xmlns:r="{relationships}">'
                          f'<p:sldIdLst>{slides}</p:sldIdLst></p:presentation>')
            links = ''.join(f'<Relationship Id="slide{n}" Type="test/slide" Target="slides/slide{n}.xml"/>'
                            for n in range(1, len(pages) + 1))
            deck.writestr('ppt/_rels/presentation.xml.rels', f'<Relationships>{links}</Relationships>')
            for n, text in enumerate(pages, 1):
                deck.writestr(f'ppt/slides/slide{n}.xml',
                              '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree><p:sp><p:txBody>'
                              f'<a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p>'
                              '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>')

    def refresh_files(self):
        return brain.build_packets(self.db, SimpleNamespace(project=None, days=60, min_records=0,
                                   limit=0, files=True, force=False, file_budget=5))

    def test_discovered_merged_presentation_can_be_read_from_its_project(self):
        first, second = self.root/'agent-a.pptx', self.root/'agent-b.pptx'
        page_a, page_b = ('Fictional Atlas product details. ' * 6, 'Fictional Atlas market analysis. ' * 6)
        self.write_presentation(first, [page_a])
        self.write_presentation(second, [page_b])
        for path, source in ((first, 'codex'), (second, 'cowork')):
            self.tool('log_work', project='Atlas', create_project=True, source=source,
                      summary='Deliver a pitch draft.', deliverables=[{'task':path.stem, 'path':str(path)}])
        folder = self.root/'user-output'
        folder.mkdir()
        merged = folder/'My merged pitch.pptx'
        self.write_presentation(merged, [page_a, page_b, 'Manually rewritten closing.'])
        outside = self.root/'Outside pitch.pptx'
        self.write_presentation(outside, [page_a, page_b])
        (folder/'linked.pptx').symlink_to(outside)
        (folder/'~$locked.pptx').write_text('Temporary office data.')
        self.assertEqual(self.refresh_files()['file_discovery']['directories'], 0)
        env = {**os.environ, 'AGENT_BRAIN_DATA': str(self.data)}
        subprocess.run([__import__('sys').executable, str(Path(brain.__file__)), 'watch-files', str(folder)],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        self.assertEqual(self.refresh_files()['file_discovery']['waiting'], 1)
        self.assertNotIn(merged.name, self.tool('get_project', project='Atlas')['card'])
        self.assertEqual(self.refresh_files()['file_discovery']['matched'], 1)
        card = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn(merged.name, card)
        self.assertIn('本机工作版 · 实际使用待核', card)
        self.assertNotIn('linked.pptx', card)
        original = self.tool('read_file', project='Atlas', path=merged.name, page=3)
        self.assertIn('Manually rewritten closing.', original['text'])
        self.assertIn('Manually rewritten closing.', self.tool('read_file', path=str(merged), page=3)['text'])

        self.write_presentation(merged, [page_a, page_b, 'A later local revision.'])
        self.assertEqual(self.refresh_files()['file_discovery']['waiting'], 1)
        self.assertEqual(self.refresh_files()['file_discovery']['matched'], 1)
        with self.assertRaisesRegex(ValueError, '指纹已变化'):
            self.tool('read_file', project='Atlas', path=merged.name, expected_sha256=original['sha256'])
        self.assertIn('A later local revision.', self.tool('read_file', project='Atlas', path=merged.name, page=3)['text'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM local_file_versions WHERE path=?',
                                        (str(merged),)).fetchone()[0], 2)
        (self.data/'config.json').write_text('{"file_discovery":{"directories":[]}}')
        self.assertNotIn(merged.name, self.tool('get_project', project='Atlas')['card'])
        (self.data/'config.json').write_text(json.dumps({'file_discovery':{'directories':[str(folder)]}}))
        self.tool('log_work', project='Atlas', source='codex', summary='Register the local handoff.',
                  deliverables=[{'path':str(merged), 'task':'Manual pitch'}])
        registered = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn('Manual pitch', registered)
        self.assertIn(merged.name, registered)
        self.assertNotIn('本机工作版', registered)

    def test_discovery_keeps_shared_templates_and_cross_project_merges_ambiguous(self):
        common = ['Shared template and standard boilerplate. ' * 5, 'Shared terms and generic context. ' * 5]
        atlas = ['Atlas distinctive product and technology. ' * 5, 'Atlas market and operating figures. ' * 5]
        orion = ['Orion distinctive product and technology. ' * 5, 'Orion market and operating figures. ' * 5]
        for name, pages in (('Atlas', atlas), ('Orion', orion)):
            path = self.root/f'{name}.pptx'
            self.write_presentation(path, common + pages)
            self.tool('log_work', project=name, create_project=True, source='codex', summary='Deliver the draft.',
                      deliverables=[{'task':'Pitch', 'path':str(path)}])
        folder = self.root/'deliveries'
        folder.mkdir()
        for name, pages in (('Template', common), ('Mixed', atlas + orion), ('Atlas work', atlas + common)):
            self.write_presentation(folder/f'{name}.pptx', pages)
        (self.data/'config.json').write_text(json.dumps({'file_discovery':{'directories':[str(folder)]}}))
        self.refresh_files()
        result = self.refresh_files()['file_discovery']
        self.assertEqual((result['matched'], result['ambiguous']), (1, 2))
        atlas_card = self.tool('get_project', project='Atlas', section='deliverables')['card']
        self.assertIn('Atlas work.pptx', atlas_card)
        self.assertNotIn('Template.pptx', atlas_card)
        self.assertNotIn('Mixed.pptx', atlas_card)
        self.assertNotIn('Atlas work.pptx', self.tool('get_project', project='Orion')['card'])
        with self.assertRaisesRegex(ValueError, '只可读取'):
            self.tool('read_file', project='Atlas', path=str(folder/'Mixed.pptx'))

    def test_missing_discovery_directory_is_visible_in_coverage(self):
        (self.data/'config.json').write_text(json.dumps({
            'file_discovery': {'directories': [str(self.root/'missing')]}}))
        result = self.refresh_files()['file_discovery']
        self.assertEqual(result['unreadable'], 1)
        self.assertIn('工作稿自动发现', brain.packet_coverage()['coverage'])

    def test_read_file_reads_powerpoint_in_presentation_order(self):
        file = self.root/'deck.pptx'
        relationships = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        with ZipFile(file,'w') as deck:
            deck.writestr('ppt/presentation.xml',f'<p:presentation xmlns:p="urn:p" xmlns:r="{relationships}">'
                          '<p:sldIdLst><p:sldId r:id="second"/><p:sldId r:id="first"/></p:sldIdLst></p:presentation>')
            deck.writestr('ppt/_rels/presentation.xml.rels',
                          '<Relationships><Relationship Id="first" Type="test/slide" Target="slides/slide1.xml"/>'
                          '<Relationship Id="second" Type="test/slide" Target="/ppt/slides/slide2.xml"/>'
                          '<Relationship Id="external" Type="test/slide" TargetMode="External" Target="https://example.invalid"/>'
                          '</Relationships>')
            for number, word in ((1,'Plan'),(2,'Intro')):
                deck.writestr(f'ppt/slides/slide{number}.xml',
                    f'<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree><p:sp><p:txBody><a:p>'
                    f'<a:r><a:t>{word}</a:t></a:r><a:br/><a:r><a:t>Text</a:t></a:r>'
                    '</a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
        self.tool('log_work',project='Atlas',create_project=True,source='codex',summary='Deliver the deck.',
                  deliverables=[{'path':str(file),'task':'Deck'}])
        args = {'project':'Atlas','path':file.name}
        self.assertEqual(self.tool('read_file',**args,page=1)['text'],'第 1 页\nIntro\nText')
        self.assertEqual(self.tool('read_file',**args,page=2)['text'],'第 2 页\nPlan\nText')
        self.assertIn('第 2 页',self.tool('read_file',**args,section='Plan')['text'])
        with self.assertRaisesRegex(ValueError,'超出'):
            self.tool('read_file',**args,page=3)

    def test_read_file_reads_registered_json_by_delivery_reference(self):
        result = self.root / 'measurements.json'
        result.write_text('{"scope":"test data","runs":[{"value":1.234567890123456789e-25}]}')
        unregistered = self.root / 'extra.json'
        unregistered.write_text('{"scope":"Unregistered result"}')
        settings = self.root / 'config.json'
        settings.write_text('{"scope":"Client configuration"}')
        sid = 'codex:fictional-structured-result'
        self.archive_messages(sid, 'Atlas', [
            ('assistant', f'Delivered [results]({result}) and [extra]({unregistered}). Related [settings]({settings}).'),
        ])
        self.tool('log_work', project='Atlas', create_project=True, source='codex',
                  summary='Deliver the result', session_id=sid,
                  deliverables=[{'path':str(result),'task':'Measurements','ordinal':0}],
                  inputs=[{'path':str(settings),'ordinal':0}])
        args = {'project':'Atlas','path':result.name,'session_id':sid,'ordinal':0}
        data = self.tool('read_file', **args, section='runs')
        self.assertIn('1.234567890123456789e-25', data['text'])
        self.assertNotIn('test data', data['text'])
        self.assertEqual(data['sha256'], hashlib.sha256(result.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError, '原交付'):
            self.tool('read_file', project='Atlas', path=result.name)
        with self.assertRaisesRegex(ValueError, '登记'):
            self.tool('read_file', **{**args,'path':unregistered.name})
        with self.assertRaisesRegex(ValueError, '客户端配置'):
            self.tool('read_file', **{**args,'path':settings.name})
        with self.assertRaisesRegex(ValueError, '顶层字段'):
            self.tool('read_file', **args, section='value')
        result.write_text('{"runs":[{"accessToken":["test-secret"]}],"scope":"test data"}')
        with self.assertRaisesRegex(ValueError, '结构化凭据'):
            self.tool('read_file', **args, section='scope')
        result.write_text('{"runs":[NaN]}')
        with self.assertRaisesRegex(ValueError, '非标准数字'):
            self.tool('read_file', **args)

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
