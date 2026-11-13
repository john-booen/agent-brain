import importlib.util
import json
import sqlite3
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

    def test_confirmed_version_scoped_requirements_and_read_file(self):
        v1 = self.root / 'old.md'
        v2 = self.root / 'new.md'
        v1.write_text('# Valuation\n' + 'Adjusted revenue × multiple.\n' * 180)
        v2.write_text('# Valuation\nDifferent assumptions.')
        initial = self.tool('log_work', project='Atlas', summary='Prior reviewed version', source='codex', work_id='old',
                            inputs=[{'text': 'User source material'}],
                            deliverables=[{'path': str(v1), 'task': 'Valuation', 'confirmed': True}],
                            rules=[{'text': 'Use internal figures.', 'topic': 'scope', 'scope': '项目'}])
        with patch.object(brain, 'now', return_value='2030-01-01T00:00:00+00:00'):
            self.tool('log_work', project='Atlas', summary='New proposal', source='kimi-work', work_id='new',
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
        with self.assertRaisesRegex(ValueError, '登记'):
            self.tool('read_file', project='Atlas', path=str(v2))
        v1.unlink()
        v1.symlink_to(v2)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.tool('read_file', project='Atlas', path=str(v1))

    def test_archive_extraction_keeps_two_deliverables_and_user_evidence(self):
        sid = 'kimi-work:fictional-packet'
        doc = self.root / 'Atlas description.md'
        first = self.root / 'Atlas meeting brief-v1.md'
        final = self.root / 'Atlas meeting brief-终版.md'
        other = self.root / 'Atlas outline.md'
        for f in (doc, first, final, other):
            f.write_text('# Outline\nAn independently created project test document.')
        archive = self.data / 'agentsview'
        archive.mkdir()
        with sqlite3.connect(archive / 'sessions.db') as index:
            index.executescript('''CREATE TABLE sessions(id,cwd,transcript_revision,display_name,session_name);
                                  CREATE TABLE messages(session_id,ordinal,role,timestamp,content,dialogue_text,is_system);''')
            index.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sid, str(self.root), '1', 'Atlas', 'Atlas'))
            messages = [
                ('user', f'<attachment>{json.dumps({"path": str(doc)})}</attachment>\n请按原始材料整理。不要添加公司名字。'),
                ('assistant', f'已交付 [初稿]({first})'),
                ('assistant', f'已交付 [终版]({final})'),
                ('user', '可以'),
                ('assistant', f'已交付 [现场提纲]({other})'),
            ]
            for n, (role, value) in enumerate(messages):
                index.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,0)', (sid, n, role, f'2026-01-01T00:00:0{n}Z', value, value))
        record = self.tool('log_work', project='Atlas', summary='Completed the project', source='kimi-work', session_id=sid)
        payload, _ = brain.packet.load(self.db, self.data, record['project_id'], brain.visible, brain.clean)
        self.assertEqual({x['path'] for x in payload['deliverables']}, {str(final), str(other)})
        self.assertTrue(next(x for x in payload['deliverables'] if x['path'] == str(final))['pinned'])
        self.assertIn(str(doc), {x['path'] for x in payload['inputs']})
        self.assertIn('不要添加公司名字', {x['text'] for x in payload['rules']})
        self.assertIn(str(first), {x.get('path') for x in payload['history']})
        with patch.object(brain.packet, 'build', side_effect=AssertionError('must use cached packet')):
            self.assertIn('终版', self.tool('get_project', project='Atlas')['card'])

    def test_project_redirect_and_record_move_preserve_original_ids(self):
        a = self.tool('log_work', project='Atlas', summary='Initial progress', source='codex', work_id='one')
        b = self.tool('log_work', project='Atlas materials', summary='Generated materials', source='codex', work_id='two')
        c = self.tool('log_work', project='Separate event', summary='Event plan', source='codex', work_id='three')
        brain.merge_projects(self.db, 'Atlas', ['Atlas materials'], 'Same project and materials')
        self.assertEqual(self.tool('get_project', project=b['project_id'])['id'], a['project_id'])
        self.assertEqual(len(brain.project_list(self.db)), 2)
        retry = self.tool('log_work', project='Atlas materials', summary='Generated materials', source='codex', work_id='two')
        self.assertFalse(retry['written'])
        brain.move_project_record(self.db, 'two', 'Separate event', 'Matched the event handoff')
        self.assertEqual(self.db.execute('SELECT project_id FROM work WHERE id=?', ('two',)).fetchone()[0], c['project_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM work').fetchone()[0], 3)

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
