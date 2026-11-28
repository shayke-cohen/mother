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
        spool = mock.patch.dict(os.environ, {"MOTHER_FLOOR_SPOOL": str(self.ws / "no-spool")})
        spool.start()
        self.addCleanup(spool.stop)
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

    def test_spool_then_fold_keeps_order_and_env(self):
        folder = self.ws / "spool"
        ev = lambda name, **kw: dict(session_id=SID, cwd="/nowhere", hook_event_name=name, **kw)
        floor.spool_event(ev("PermissionRequest", tool_name="Bash", tool_input={"command": "git push"}), {"TMUX_PANE": "%4"}, folder)
        floor.spool_event(ev("PostToolUse", tool_name="Bash"), {"TMUX_PANE": "%4"}, folder)
        floor.spool_event(ev("Stop", last_assistant_message="ok\nWaiting on: the user — merge it?"), {"TMUX_PANE": "%4"}, folder)
        self.assertEqual(floor.fold_spool(self.ws, AT, folder), 3)
        self.assertEqual(list(folder.glob("*.json")), [])
        card = floor.list_cards(self.ws, AT)[0]
        self.assertEqual((card["question"], card["tmux"]), ("merge it", {"pane": "%4"}))

    def test_spool_refuses_junk_and_a_shared_folder(self):
        folder = self.ws / "spool"
        self.assertIsNone(floor.spool_event({"hook_event_name": "Stop", "session_id": "../x"}, {}, folder))
        folder.mkdir(exist_ok=True)
        os.chmod(folder, 0o777)
        self.assertIsNone(floor.spool_event(dict(session_id=SID, hook_event_name="Stop"), {}, folder))
        (folder / "1.json").write_text(json.dumps({"event": dict(session_id=SID, hook_event_name="SessionStart")}))
        self.assertEqual(floor.fold_spool(self.ws, AT, folder), 0)   # another user could have written it: never read

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


class LiveTerminalsTest(unittest.TestCase):
    """lib/chief.py live_terminals: Claude terminals that run now but wrote no card (started before their mod did)."""
    def setUp(self):
        import chief
        self.chief = chief
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws, self.home = self.root / "ws", self.root / "home"
        spool = mock.patch.dict(os.environ, {"MOTHER_FLOOR_SPOOL": str(self.root / "no-spool")})
        spool.start()
        self.addCleanup(spool.stop)

    def transcript(self, cwd, sid, lines=()):
        folder = self.home / ".claude" / "projects" / self.chief.project_slug(cwd)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (sid + ".jsonl")).write_text("".join(json.dumps(x) + "\n" for x in lines) or "{}\n")

    def test_ids_titles_dev3_and_rows(self):
        a, b, c = ("%08d-0000-4000-8000-000000000000" % n for n in range(3))
        self.transcript("/w/dev3", a)
        self.transcript("/w/home", b, [{"type": "ai-title", "aiTitle": "Old name"}, {"type": "custom-title", "customTitle": "Plan \"x\""}])
        self.transcript("/w/bare", c)
        procs = [{"pid": 1, "args": "claude --session-id %s --model m" % a, "cwd": "/w/dev3",
                  "env": {"DEV3_TASK_ID": "215bca19-6f44-4cce-b6e7-957692f9327c", "TMUX_PANE": "%7", "TMUX": "/tmp/tmux-501/dev3,1,2"}},
                 {"pid": 2, "args": "claude", "cwd": "/w/home", "env": {}},          # no id: its folder's newest transcript
                 {"pid": 3, "args": "claude --resume %s" % c, "cwd": "/w/bare", "env": {}},
                 {"pid": 4, "args": "claude --session-id %s" % a, "cwd": "/w/dev3", "env": {}}]   # the same session twice
        with mock.patch.object(floor, "_dev3_task", return_value={"title": "#170 Nano lane", "overview": "Fixing tabs"}) as asked:
            got = {t["session_id"]: t for t in self.chief.live_terminals(self.ws, self.home, procs)}
            again = self.chief.live_terminals(self.ws, self.home, procs)
        self.assertEqual(asked.call_count, 1)                     # cached per session while its pid holds
        self.assertEqual(len(again), 3)
        self.assertEqual(sorted(got), [a, b, c])
        self.assertEqual((got[a]["surface"], got[a]["dev3_task"], got[a]["title"], got[a]["recap"]),
                         ("dev3", "215bca19-6f44-4cce-b6e7-957692f9327c", "#170 Nano lane", "Fixing tabs"))
        self.assertEqual(got[a]["tmux"], {"pane": "%7", "socket": "/tmp/tmux-501/dev3"})
        self.assertEqual(got[b]["title"], 'Plan "x"')             # the custom title beats the AI one
        self.assertEqual(got[c]["title"], "Terminal · bare")
        now = datetime.now(timezone.utc)
        rows = self.chief.agent_status(self.ws, now, self.root / "app", pinned=[{"cli_session_id": c}], terminals=list(got.values()))
        by = {r["cli_session_id"]: r for r in rows}
        self.assertEqual(sorted(by), [a, b])                      # a pinned one is drawn as pinned, not here
        self.assertEqual((by[a]["surface"], by[a]["dev3_task"], by[a]["last_report"]), ("dev3", got[a]["dev3_task"], "Fixing tabs"))

    def test_a_card_wins_and_a_fixed_clock_scans_nothing(self):
        a = "%08d-0000-4000-8000-000000000000" % 7
        folder = floor.floor_dir(self.ws)
        folder.mkdir(parents=True, exist_ok=True)
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (folder / (a + ".json")).write_text(json.dumps({"session_id": a, "asks": False, "question": "", "options": [],
                                                        "waiting_on": None, "at": iso, "agent": "claude", "title": "From card"}))
        term = {"session_id": a, "agent": "claude", "surface": "cli", "cwd": "/w", "at": iso, "title": "From scan"}
        rows = self.chief.agent_status(self.ws, datetime.now(timezone.utc), self.root / "app", pinned=[], terminals=[term])
        self.assertEqual([r["title"] for r in rows], ["From card"])
        with mock.patch.object(self.chief, "live_terminals", side_effect=AssertionError("scanned")):
            self.assertEqual(self.chief._scan_terminals(self.ws, datetime(2020, 1, 1, tzinfo=timezone.utc)), [])


class AgentStatusTest(unittest.TestCase):
    """lib/chief.py agent_status: the Sessions tab's Other agents rows."""
    def setUp(self):
        import chief
        self.chief = chief
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "ws"
        self.app = Path(self._tmp.name) / "app" / "acct" / "org"
        self.app.mkdir(parents=True)
        spool = mock.patch.dict(os.environ, {"MOTHER_FLOOR_SPOOL": str(Path(self._tmp.name) / "no-spool"),
                                             "MOTHER_TERMINAL_SCAN": "0"})   # this machine's own terminals stay out
        spool.start()
        self.addCleanup(spool.stop)

    def card(self, sid, at, **kw):
        folder = floor.floor_dir(self.ws)
        folder.mkdir(parents=True, exist_ok=True)
        doc = dict({"session_id": sid, "asks": False, "question": "", "options": [], "waiting_on": None, "at": at}, **kw)
        (folder / (sid + ".json")).write_text(json.dumps(doc))

    def app_file(self, local, cli, title, archived=False):
        (self.app / (local + ".json")).write_text(json.dumps({"cliSessionId": cli, "title": title, "cwd": "/w/repo",
                                                               "isArchived": archived, "lastTurnReport": "Done, PR open"}))

    def test_rows_match_desktop_skip_routines_archived_stale_and_pinned(self):
        now = datetime.now(timezone.utc)
        iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = ["%08d-0000-4000-8000-000000000000" % n for n in range(6)]
        self.card(ids[0], iso, agent="claude", surface="cli")                       # a desktop session nobody pinned
        self.app_file("local_desk", ids[0], "ASC Node issue 160")
        self.card(ids[1], iso, agent="claude", surface="dev3", title="#12 Fix login", dev3_task="abc12345", recap="Tests pass")
        self.card(ids[2], iso, agent="claude", surface="cli")                       # a scheduled task's run
        self.app_file("local_tick", ids[2], "Mother: tick")
        (self.app / "scheduled-tasks.json").write_text(json.dumps({"scheduledTasks": [{"displayName": "Mother: tick"}]}))
        self.card(ids[3], "2000-01-01T00:00:00Z", agent="claude", surface="cli")    # stale
        self.card(ids[4], iso, agent="claude", surface="cli")                       # archived in the app
        self.app_file("local_arch", ids[4], "Old", archived=True)
        self.card(ids[5], iso)                                                      # no agent: an old-style card
        rows = self.chief.agent_status(self.ws, now, self.app.parent.parent, pinned=[])
        by = {r["cli_session_id"]: r for r in rows}
        self.assertEqual(by[ids[0]]["title"], "ASC Node issue 160")
        self.assertEqual((by[ids[0]]["surface"], by[ids[0]]["open_link"]), ("desktop", "claude://claude.ai/epitaxy/local_desk"))
        self.assertEqual((by[ids[1]]["surface"], by[ids[1]]["dev3_task"], by[ids[1]]["last_report"]), ("dev3", "abc12345", "Tests pass"))
        self.assertNotIn(ids[2], by)
        self.assertNotIn(ids[3], by)
        self.assertEqual(sorted(by), sorted([ids[0], ids[1]]))   # archived, stale, a routine and an old-style card are left out
        pinned = self.chief.agent_status(self.ws, now, self.app.parent.parent, pinned=[{"cli_session_id": ids[0]}])
        self.assertEqual([r["cli_session_id"] for r in pinned], [ids[1]])
