import importlib.util
import io
import os
import tempfile
import unittest
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("brain", root / "brain.py")
brain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brain)


class BrainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = patch.object(brain, "DATA", Path(self.temp.name))
        self.config.start()
        self.addCleanup(self.config.stop)
        self.db = brain.connect()
        self.addCleanup(self.db.close)

    def test_visible_dialogue_and_redaction(self):
        self.assertEqual(brain.visible({"role": "tool", "content": "data"}), "")
        self.assertEqual(brain.visible({"role": "user", "is_system": True, "content": "injected"}), "")
        self.assertEqual(brain.visible({"role": "assistant", "content": "[Thinking]\nprivate\n[/Thinking]\nDone."}), "Done.")
        self.assertEqual(brain.visible({"role": "assistant", "content": "hidden", "dialogue_text": ""}), "")
        result = brain.visible({"role": "user", "content": '<meta time="1" />\n密码 = "fake with spaces"'})
        self.assertEqual(result, "密码 = [REDACTED]")
        notice = 'Skill tool loaded instructions for this request. Follow them.\n<skill-loaded name="example">Hidden</skill-loaded>'
        self.assertEqual(brain.visible({"role": "user", "content": notice}), "")

    def test_retry_and_current_conclusion(self):
        first = brain.log_work(self.db, "Fictional Atlas", "First draft", "codex", work_id="first", create_project=True,
                               conclusions=[{"topic": "date", "text": "Friday"}])
        same = brain.log_work(self.db, "Fictional Atlas", "First draft", "codex", work_id="first", create_project=True,
                              conclusions=[{"topic": "date", "text": "Friday"}])
        self.assertFalse(same["written"])
        self.assertEqual(first["project_id"], same["project_id"])
        with patch.object(brain, "now", return_value="2030-01-01T00:00:00+00:00"):
            brain.log_work(self.db, "Fictional Atlas", "New plan", "cowork", work_id="second", create_project=True,
                           conclusions=[{"topic": "date", "text": "Monday"}])
        card = brain.call_tool(self.db, "get_project", {"project": "Fictional Atlas"})["card"]
        self.assertIn("Monday", card)
        self.assertNotIn("Friday", card)
        history = brain.call_tool(self.db, "get_project", {"project": "Fictional Atlas", "section": "history"})["card"]
        self.assertIn("Friday", history)
        self.assertIn("已更新", history)
        self.assertTrue((brain.DATA / "_index.md").is_file())

    def test_lookup_and_short_card_share_budget_and_history_pages(self):
        work = brain.log_work(self.db, "Fictional Atlas", "完成了可交付材料" * 100, "codex", create_project=True,
                              conclusions=[{"text": "有效结论" * 250}],
                              files=[{"path": "/tmp/final.docx", "status": "已交付"}],
                              todos=[{"text": "补证据"}])
        self.db.execute("INSERT INTO project_aliases VALUES(?,?)", ("Atlas delivery", work["project_id"]))
        lookup = brain.call_tool(self.db, "find_project", {"query": "Atlas delivery"})
        self.assertEqual(lookup["projects"][0]["id"], work["project_id"])
        brief = brain.call_tool(self.db, "get_project", {"project": "Atlas delivery"})
        self.assertIn("final.docx", brief["card"])
        self.assertIn("补证据", brief["card"])
        self.assertLessEqual(brain.response_size(lookup) + brain.response_size(brief), brain.QUERY_BYTES)
        args = {"project": work["project_id"], "section": "conclusions"}
        page = brain.call_tool(self.db, "get_project", args)
        self.assertIsNotNone(page["next_offset"])
        second = brain.call_tool(self.db, "get_project", {**args, "offset": page["next_offset"]})
        self.assertTrue(second["card"])
        self.assertLessEqual(brain.response_size(page), brain.QUERY_BYTES)
        self.assertLessEqual(brain.response_size(second), brain.QUERY_BYTES)

    def test_short_card_keeps_a_complete_status_sentence_and_its_source(self):
        work = brain.log_work(
            self.db, "Fictional Atlas", "首轮已交付；" + "较长的说明" * 80 + "。",
            "codex", create_project=True,
        )
        card = brain.get_project(self.db, work["project_id"])["card"]
        self.assertRegex(card.splitlines()[0], r"^现状 首轮已交付； — codex \d{4}-\d{2}-\d{2}$")

        work = brain.log_work(
            self.db, "Fictional Beacon", "较长的说明" * 80,
            "codex", conclusions=[{"text": "接续入口已核验。"}], create_project=True,
        )
        card = brain.get_project(self.db, work["project_id"])["card"]
        self.assertIn("接续入口已核验。 — codex", card.splitlines()[0])

    def test_transcript_excerpts_resume_within_an_oversize_message(self):
        original = '\\"\n多字' * 600
        session = {"session_id": "cowork:test", "path": None, "next_from": None, "status": "ok",
                   "messages": [{"ordinal": 17, "text": original}]}
        with patch.object(brain, "read_session", side_effect=lambda *a, **kw: {**session}):
            args = {"session_id": "cowork:test", "around": 17}
            first = brain.call_tool(self.db, "get_session", args)
            offset = first["next_text_offset"]
            second = brain.call_tool(self.db, "get_session", {"session_id": "cowork:test", "start": 17, "text_offset": offset})
        self.assertEqual(first["next_from"], 17)
        self.assertEqual(first["messages"][0]["text"], original[:offset])
        self.assertTrue(original[offset:].startswith(second["messages"][0]["text"]))
        for page in (first, second):
            self.assertLessEqual(brain.response_size(page), brain.QUERY_BYTES)

    def test_model_uses_evidence_and_caches_revision(self):
        session = {"messages": [{"ordinal": 1, "text": "Delivered /tmp/fictional.md", "time": "2030-01-01T00:00:00Z"}],
                   "revision": "1", "time": None, "source": "kimi-work"}
        model = {"projects": [{"name": "Fictional Atlas", "reason": "Evidence", "summary": "Delivered draft", "ordinal": 1,
                               "files": [{"path": "/tmp/fictional.md", "status": "已交付", "ordinal": 1},
                                         {"path": "/tmp/invented.md", "status": "已交付", "ordinal": 1}]}]}
        with patch.object(brain, "read_session", return_value=session), patch.object(brain, "model", return_value=model) as call:
            metadata = {"agent": "kimi-work"}
            a = brain.summarize_session(self.db, "kimi-work:fictional", metadata=metadata)
            b = brain.summarize_session(self.db, "kimi-work:fictional", metadata=metadata)
        self.assertEqual(a["records"], 1)
        self.assertEqual(b["status"], "unchanged")
        self.assertEqual(call.call_count, 1)
        card = brain.call_tool(self.db, "get_project", {"project": "Fictional Atlas"})["card"]
        self.assertIn("fictional.md", card)
        self.assertNotIn("invented.md", card)
        self.assertIn("kimi-work 2030-01-01", card)
        detailed = brain.get_project(self.db, "Fictional Atlas", section="deliverables")["card"]
        self.assertIn("kimi-work:fictional#1", detailed)

    def test_model_reports_usage_and_rejects_a_malformed_response(self):
        response = io.BytesIO(b'{"usage":{"prompt_tokens":20,"completion_tokens":3},"choices":[]}')
        measured = {}
        settings = {"url": "http://127.0.0.1:1234/v1", "name": "fictional-model"}
        with patch.object(brain, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "did not contain a choice"):
                brain.model([], settings=settings, on_usage=measured.update)
        self.assertEqual(measured, {"prompt_tokens": 20, "completion_tokens": 3})

        stream = [b'data: {"usage":{"prompt_tokens":10},"choices":[1]}']
        with self.assertRaisesRegex(ValueError, "choice array"):
            brain.stream_choice(stream, measured.update)
        self.assertEqual(measured["prompt_tokens"], 10)

    def test_truncated_model_stream_still_records_late_usage(self):
        measured = {}
        stream = [
            b'data: {"error":{"message":"Completion output limit reached"}}',
            b'data: {"choices":[{"finish_reason":"stop"}]}',
            b'data: {"usage":{"prompt_tokens":12,"completion_tokens":8},"choices":[]}',
            b'data: [DONE]',
        ]
        self.assertEqual(brain.stream_choice(stream, measured.update)['finish_reason'], 'length')
        self.assertEqual(measured, {"prompt_tokens": 12, "completion_tokens": 8})

    def test_reconcile_two_names_for_the_same_project(self):
        payload = {"reason": "Both messages discuss the same release."}
        results = [(0, "Fictional Atlas", payload), (1, "Atlas release checklist", payload)]
        with patch.object(brain, "model", side_effect=[{"groups": [{"name": "Fictional Atlas", "indices": [0]}]},
                                                       {"groups": [{"name": "Fictional Atlas", "indices": [0, 1]}]}]):
            reconciled = brain.reconcile_projects(results, [], [])
        self.assertEqual(reconciled, [(0, "Fictional Atlas", payload), (1, "Fictional Atlas", payload)])

    def test_approval_history_stays_out_of_model_input(self):
        metadata = {"agent": "codex", "relationship_type": "subagent",
                    "first_message": brain.CODEX_REVIEW_PREFIXES[0] + " Fictional history"}
        with patch.object(brain, "read_session") as read, patch.object(brain, "model") as model:
            result = brain.summarize_session(self.db, "codex:fictional-review", metadata=metadata)
        self.assertEqual(result["status"], "internal_review")
        read.assert_not_called()
        model.assert_not_called()
        self.assertFalse(brain.is_codex_review({**metadata, "relationship_type": ""}))

    def test_quota_stops_backfill_without_losing_prior_checkpoints(self):
        session = {"messages": [{"ordinal": 1, "text": "a" * 4200}, {"ordinal": 2, "text": "b" * 4200}],
                   "revision": "1", "time": None, "source": "kimi-work"}
        row = {"id": "kimi-work:fictional", "agent": "kimi-work"}
        quota = brain.ModelQuotaError("60")
        with patch.object(brain, "history_sessions", return_value=iter([row])), \
             patch.object(brain, "read_session", return_value=session), \
             patch.object(brain, "model", side_effect=[{"projects": []}, quota]):
            with self.assertRaises(brain.ModelQuotaError):
                brain.backfill(self.db, ["kimi-work"], 0, False, "recent")
        self.assertEqual(self.db.execute("SELECT count(*) FROM summary_chunks").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM reviewed").fetchone()[0], 0)
        with patch.object(brain, "read_session", return_value=session), \
             patch.object(brain, "model_settings", return_value={"chunk_chars": 24000}), \
             patch.object(brain, "model", return_value={"projects": []}) as model:
            result = brain.summarize_session(self.db, row["id"], metadata=row)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(model.call_count, 1)
        self.assertIn('"ordinal":2', model.call_args.args[0][1]["content"])

    def test_http_quota_has_no_provider_payload(self):
        error = HTTPError("https://example.invalid/v1/chat/completions", 429, "Too Many Requests",
                          {"Retry-After": "60"}, None)
        with patch.object(brain, "model_settings", return_value={"url": "https://example.invalid/v1", "name": "fictional"}), \
             patch.object(brain, "urlopen", side_effect=error):
            with self.assertRaisesRegex(brain.ModelQuotaError, "retry after 60s"):
                brain.model([])

    def test_stream_excludes_reasoning_and_requires_complete_reply(self):
        event = b'data: {"choices":[{"delta":{"reasoning_content":"ignore","content":"{}"}}]}\n'
        end = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
        self.assertEqual(brain.stream_choice([event, end, b'data: [DONE]\n'])["message"]["content"], "{}")
        with self.assertRaisesRegex(ValueError, "ended before a final response"):
            brain.stream_choice([event])
        error = b'data: {"error":{"message":"Output token limit reached"}}\n'
        self.assertEqual(brain.stream_choice([event, error])["finish_reason"], "length")


if __name__ == "__main__":
    unittest.main()
