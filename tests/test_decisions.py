"""Tests for lib/decisions.py — the decision queue.
Run: python3 -m unittest tests.test_decisions -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import chief  # noqa: E402
import decisions  # noqa: E402
import hub  # noqa: E402

UTC = timezone.utc
AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
POST = "mcp__chat__slack__post_message"
MAIL = "mcp__mail__gmail__send-gmail-message"
REPLY = {"channel": "C00000001", "text": "hello", "thread_ts": "1700000000.000100"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        chief.register_worker(self.ws, AT, "PLAT-1", "Plat one")

    def propose(self, key="PLAT-1", tool=POST, inp=None, **kw):
        return decisions.propose(self.ws, {}, AT, key, tool, dict(REPLY) if inp is None else inp,
                                 kw.pop("recommendation", "send it"), **kw)


class ProposeTest(Base):
    def test_propose_chat_reply_kind_and_sha(self):
        d = self.propose(why="asked", urgent=True)
        self.assertEqual(d["id"], "D1")
        self.assertEqual(d["kind"], "chat-reply")
        self.assertEqual(d["target"], "C00000001")
        self.assertEqual(d["thread"], "1700000000.000100")
        self.assertEqual(d["text"], "hello")
        self.assertEqual(d["draft_sha"], decisions.guard.draft_sha(POST, REPLY))
        self.assertEqual((d["status"], d["urgent"], d["answer"], d["why"]), ("pending", True, None, "asked"))
        self.assertEqual(d["created"], hub._iso(AT))
        self.assertEqual(d["expires"], hub._iso(AT + timedelta(hours=48)))
        self.assertEqual(decisions.pending(self.ws), [d])
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["id"], "D1")

    def test_propose_tracker_target_is_project(self):
        d = self.propose(tool="mcp__tracker__jira__comment-on-issue",
                         inp={"issueIdOrKey": "PLAT-1", "comment": "x"})
        self.assertEqual((d["kind"], d["target"], d["thread"]), ("tracker-comment", "PLAT", None))

    def test_propose_email_is_manual(self):
        d = self.propose(tool=MAIL, inp={"to": "a@b.c", "body": "x"})
        self.assertEqual(d["kind"], "manual")
        self.assertIsNone(d["target"])

    def test_propose_sets_waiting_decision(self):
        self.propose()
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "waiting_decision")

    def test_propose_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            self.propose(key="NOPE-1")
        self.assertEqual(decisions.load(self.ws)["decisions"], [])

    def test_propose_duplicate_raises(self):
        self.propose()
        with self.assertRaises(ValueError):
            self.propose()
        self.assertEqual(len(decisions.load(self.ws)["decisions"]), 1)

    def test_ids_increase_across_calls(self):
        a = self.propose()
        b = self.propose(inp=dict(REPLY, text="other"))
        self.assertEqual((a["id"], b["id"]), ("D1", "D2"))
        self.assertEqual(decisions.load(self.ws)["next"], 3)

    def test_decision_hours_setting(self):
        d = decisions.propose(self.ws, {"chief": {"decision_hours": 1}}, AT, "PLAT-1", POST, dict(REPLY), "r")
        self.assertEqual(d["expires"], hub._iso(AT + timedelta(hours=1)))

    def test_load_defaults(self):
        self.assertEqual(decisions.load(self.ws), {"version": 1, "next": 1, "decisions": [],
                                                   "batch": {"last_at": None, "announced": []}, "confirm": []})


class EditedInputTest(unittest.TestCase):
    def test_edited_input_replaces_text_key(self):
        inp = {"channel": "C00000001", "text": "old", "body": "later key"}
        out = decisions.edited_input(inp, "new")
        self.assertEqual(out, {"channel": "C00000001", "text": "new", "body": "later key"})
        self.assertEqual(inp["text"], "old")
        inp = {"inputs": {"issueIdOrKey": "PLAT-1", "comment": "old"}}
        self.assertEqual(decisions.edited_input(inp, "new"), {"inputs": {"issueIdOrKey": "PLAT-1", "comment": "new"}})
        self.assertEqual(inp["inputs"]["comment"], "old")

    def test_edited_input_blocks_raises(self):
        with self.assertRaises(ValueError):
            decisions.edited_input({"channel": "C00000001", "blocks": [{"type": "section"}]}, "new")

    def test_edited_input_no_text_key_raises(self):
        with self.assertRaises(ValueError):
            decisions.edited_input({"channel": "C00000001"}, "new")


class CliTest(Base):
    def run_cli(self, args, stdin):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            code = decisions.main(args, self.ws, {}, out, err)
        finally:
            sys.stdin = old
        return code, out.getvalue(), err.getvalue()

    def test_propose_cli(self):
        code, out, _ = self.run_cli(["propose", "PLAT-1", "--tool", POST, "--recommend", "send", "--why", "w",
                                     "--urgent", "--stdin"], json.dumps(REPLY))
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertTrue(doc["ok"])
        self.assertEqual((doc["decision"]["id"], doc["decision"]["urgent"], doc["decision"]["why"]), ("D1", True, "w"))

    def test_cli_usage_errors_exit_2(self):
        for args, stdin in ((["propose", "PLAT-1", "--recommend", "r", "--stdin"], "{}"),
                            (["propose", "PLAT-1", "--tool", POST, "--stdin"], "{}"),
                            (["propose", "PLAT-1", "--tool", POST, "--recommend", "r"], "{}"),
                            (["propose", "--tool", POST, "--recommend", "r", "--stdin"], "{}"),
                            (["propose", "PLAT-1", "--tool", POST, "--recommend", "r", "--stdin"], "not json"),
                            (["propose", "PLAT-1", "--tool", POST, "--recommend", "r", "--stdin"], "[1]"),
                            (["bogus"], "")):
            self.assertEqual(self.run_cli(args, stdin)[0], 2, args)

    def test_cli_value_error_exits_1(self):
        code, _, err = self.run_cli(["propose", "NOPE", "--tool", POST, "--recommend", "r", "--stdin"],
                                    json.dumps(REPLY))
        self.assertEqual(code, 1)
        self.assertIn("NOPE", err)


class ReplyGrammarTest(unittest.TestCase):
    OPEN = {7, 8, 9}

    def test_reply_single_ok(self):
        self.assertEqual(decisions.parse_reply("7 ok", self.OPEN), [(7, "ok", "")])
        self.assertEqual(decisions.parse_reply("D7 OK", self.OPEN), [(7, "ok", "")])

    def test_reply_ok_list(self):
        self.assertEqual(decisions.parse_reply("ok 7 9", self.OPEN), [(7, "ok", ""), (9, "ok", "")])

    def test_reply_design_example(self):
        self.assertEqual(decisions.parse_reply("ok 7 9, 8 no keep it but move to next sprint", self.OPEN),
                         [(7, "ok", ""), (9, "ok", ""), (8, "no", ""), (8, "say", "keep it but move to next sprint")])

    def test_reply_edit_rest_of_message(self):
        self.assertEqual(decisions.parse_reply("7 edit: line1\nline2", self.OPEN), [(7, "edit", "line1\nline2")])
        self.assertEqual(decisions.parse_reply("8 ok, 7 edit: a, b; c", self.OPEN),
                         [(8, "ok", ""), (7, "edit", "a, b; c")])

    def test_reply_edit_unreadable(self):
        for t in ("7 edit", "7 edit:", "7 edit: ", "7 edit no colon"):
            self.assertEqual(decisions.parse_reply(t, self.OPEN), [], t)

    def test_reply_ok_all(self):
        self.assertEqual(decisions.parse_reply("ok all", self.OPEN), [(7, "ok", ""), (8, "ok", ""), (9, "ok", "")])
        self.assertEqual(decisions.parse_reply("ok all", set()), [])

    def test_reply_no_list_and_say(self):
        self.assertEqual(decisions.parse_reply("no 7 8", self.OPEN), [(7, "no", ""), (8, "no", "")])
        self.assertEqual(decisions.parse_reply("9 wait until Monday", self.OPEN), [(9, "say", "wait until Monday")])

    def test_reply_duplicates_kept(self):
        self.assertEqual(decisions.parse_reply("ok 7, 7 no", self.OPEN), [(7, "ok", ""), (7, "no", "")])

    def test_reply_not_a_reply_returns_none(self):
        for t in ("look at PLAT-1", "", "  ", "7th floor", "ok then", "no problem"):
            self.assertIsNone(decisions.parse_reply(t, self.OPEN), t)

    def test_reply_partial_parse_applies_nothing(self):
        self.assertEqual(decisions.parse_reply("ok 7 and also ping Dana", {7}), [])
        self.assertEqual(decisions.parse_reply("7 ok 8", {7}), [])
        self.assertEqual(decisions.parse_reply("ok 12", {7}), [])
        self.assertEqual(decisions.parse_reply("7 ok, 12 no", {7}), [])


if __name__ == "__main__":
    unittest.main()
