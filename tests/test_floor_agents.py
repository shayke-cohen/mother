"""Tests for lib/floor.py's other agents: identity, Codex hook events, tmux send.
Run: python3 -m unittest tests.test_floor_agents -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import floor  # noqa: E402

AT = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
SID = "019a1b2c-3d4e-7f00-8a11-223344556677"


class AgentsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"TMUX_PANE": "%7", "TMUX": "/tmp/tmux-501/dev3,123,0"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DEV3_TASK_ID", None)

    def ev(self, name, **kw):
        return floor.codex_event(self.ws, AT, dict(session_id=SID, cwd="/nowhere", hook_event_name=name, **kw))

    def test_identity_from_env(self):
        got = floor.identify_env({"TMUX_PANE": "%3", "TMUX": "/s,1,0", "DEV3_TASK_ID": "abc-1"})
        self.assertEqual((got["surface"], got["tmux"], got["dev3_task"]), ("dev3", {"pane": "%3", "socket": "/s"}, "abc-1"))
        self.assertEqual(floor.identify_env({"TMUX_PANE": "bad; rm"}), {"surface": "cli"})

    def test_dev3_title_and_overview(self):
        with mock.patch.object(floor, "_dev3_task", return_value={"title": "#46 Plan Nano", "overview": "Goal: finish #4675"}):
            got = floor.identify_env({"DEV3_TASK_ID": "abc"})
        self.assertEqual((got["title"], got["recap"]), ("#46 Plan Nano", "Goal: finish #4675"))

    def test_codex_stop_waiting_line_asks(self):
        card = self.ev("Stop", last_assistant_message="Done the refactor.\nWaiting on: the user — say whether to push")
        self.assertEqual((card["agent"], card["surface"], card["asks"], card["question"]), ("codex", "tmux", True, "say whether to push"))
        self.assertEqual(card["tmux"], {"pane": "%7", "socket": "/tmp/tmux-501/dev3"})

    def test_codex_stop_without_ask_and_prompt_clears(self):
        self.assertFalse(self.ev("Stop", last_assistant_message="All green.\nWaiting on: nothing — done")["asks"])
        self.ev("PermissionRequest", tool_name="Bash", tool_input={"command": "git push"})
        self.assertFalse(self.ev("UserPromptSubmit", prompt="go")["asks"])

    def test_codex_permission_then_post_tool_clears(self):
        card = self.ev("PermissionRequest", tool_name="Bash", tool_input={"command": "git push origin x"})
        self.assertEqual((card["asks"], card["question"]), (True, "Approve Bash: git push origin x"))
        self.assertFalse(self.ev("PostToolUse", tool_name="Bash")["asks"])
        self.assertIsNone(self.ev("PostToolUse", tool_name="Bash"))   # nothing to clear: no write

    def test_codex_session_end_removes_card_and_junk_is_ignored(self):
        self.ev("SessionStart")
        self.ev("SessionEnd")
        self.assertEqual(floor.list_cards(self.ws, AT), [])
        self.assertIsNone(floor.codex_event(self.ws, AT, {"hook_event_name": "Stop", "session_id": "../../x"}))
        self.assertIsNone(floor.codex_event(self.ws, AT, "nope"))

    def test_codex_hook_never_fails(self):
        with mock.patch.object(sys, "stdin", __import__("io").StringIO("{not json")):
            self.assertEqual(floor.main(["codex-hook"], self.ws, {}, AT), 0)

    def test_send_types_into_live_pane(self):
        self.ev("Stop", last_assistant_message="x\nWaiting on: the user — pick one")
        calls = []
        def run(argv):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="%7\n")
        got = floor.send(self.ws, {"session_id": SID, "text": "Yes,\nship it"}, run)
        self.assertEqual(got["pane"], "%7")
        self.assertEqual(calls[1], ["tmux", "-S", "/tmp/tmux-501/dev3", "send-keys", "-t", "%7", "-l", "--", "Yes, ship it"])
        self.assertEqual(calls[2][-1], "Enter")

    def test_send_refuses_gone_pane_and_no_pane(self):
        self.ev("SessionStart")
        with self.assertRaisesRegex(ValueError, "gone"):
            floor.send(self.ws, {"session_id": SID, "text": "hi"}, lambda a: SimpleNamespace(returncode=1, stdout=""))
        os.environ.pop("TMUX_PANE")
        self.ev("SessionStart")
        with self.assertRaisesRegex(ValueError, "no tmux pane"):
            floor.send(self.ws, {"session_id": SID, "text": "hi"}, lambda a: SimpleNamespace(returncode=0, stdout="%7"))

    def test_codex_setup_is_idempotent(self):
        cfg = self.ws / "config.toml"
        cfg.write_text("[features]\nhooks = true\n")
        self.assertIn("added", floor.codex_setup("/bin/mother", cfg, False))
        self.assertIn("already", floor.codex_setup("/bin/mother", cfg, False))
        text = cfg.read_text()
        self.assertEqual(text.count("[[hooks.Stop]]"), 1)
        self.assertIn('command = "/bin/mother floor codex-hook"', text)


if __name__ == "__main__":
    unittest.main()
