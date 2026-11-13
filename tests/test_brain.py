import importlib.util
import os
import tempfile
import unittest
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

    def test_retry_and_current_conclusion(self):
        first = brain.log_work(self.db, "Fictional Atlas", "First draft", "codex", work_id="first",
                               conclusions=[{"topic": "date", "text": "Friday"}])
        same = brain.log_work(self.db, "Fictional Atlas", "First draft", "codex", work_id="first",
                              conclusions=[{"topic": "date", "text": "Friday"}])
        self.assertFalse(same["written"])
        self.assertEqual(first["project_id"], same["project_id"])
        with patch.object(brain, "now", return_value="2030-01-01T00:00:00+00:00"):
            brain.log_work(self.db, "Fictional Atlas", "New plan", "cowork", work_id="second",
                           conclusions=[{"topic": "date", "text": "Monday"}])
        card = brain.call_tool(self.db, "get_project", {"project": "Fictional Atlas"})["card"]
        self.assertIn("Monday", card.split("## 时间线")[0])
        self.assertIn("Friday", card.split("## 已更新的旧结论")[1])
        self.assertIn("已更新", card)
        self.assertTrue((brain.DATA / "_index.md").is_file())

    def test_model_uses_evidence_and_caches_revision(self):
        session = {"messages": [{"ordinal": 1, "text": "Delivered /tmp/fictional.md", "time": "2030-01-01T00:00:00Z"}],
                   "revision": "1", "time": None, "source": "kimi-work"}
        model = {"projects": [{"name": "Fictional Atlas", "reason": "Evidence", "summary": "Delivered draft", "ordinal": 1,
                               "files": [{"path": "/tmp/fictional.md", "status": "已交付", "ordinal": 1},
                                         {"path": "/tmp/invented.md", "status": "已交付", "ordinal": 1}]}]}
        with patch.object(brain, "read_session", return_value=session), patch.object(brain, "model", return_value=model) as call:
            a = brain.summarize_session(self.db, "kimi-work:fictional")
            b = brain.summarize_session(self.db, "kimi-work:fictional")
        self.assertEqual(a["records"], 1)
        self.assertEqual(b["status"], "unchanged")
        self.assertEqual(call.call_count, 1)
        card = brain.call_tool(self.db, "get_project", {"project": "Fictional Atlas"})["card"]
        self.assertIn("fictional.md", card)
        self.assertNotIn("invented.md", card)
        self.assertIn("kimi-work:fictional#1", card)


if __name__ == "__main__":
    unittest.main()
