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
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import chief  # noqa: E402
import decisions  # noqa: E402
import grants  # noqa: E402
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
                                                   "batch": {"last_at": None, "announced": {}}, "confirm": []})


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

    def test_reply_edit_colon_must_follow_word(self):
        self.assertEqual(decisions.parse_reply("7 edit please change: x", self.OPEN), [])
        self.assertEqual(decisions.parse_reply("7 edit : x", self.OPEN), [(7, "edit", "x")])

    def test_reply_edit_payload_verbatim(self):
        self.assertEqual(decisions.parse_reply("7 edit:  two spaces\n trailing \n", self.OPEN),
                         [(7, "edit", " two spaces\n trailing \n")])
        self.assertEqual(decisions.parse_reply('7 edit: --help "x"\ny', self.OPEN),
                         [(7, "edit", '--help "x"\ny')])

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


CFG = {"manager": {"chat_dm": ["U00000001", "D00000001"]}}
DM, OWNER = "D00000001", "U00000001"
SRC_TS = "%.6f" % (AT.timestamp() + 60)  # the manager's reply, a minute after the decision was filed
LATER = AT + timedelta(seconds=120)


def typed_msg(ts=SRC_TS, text="1 ok", **kw):
    m = {"ts": ts, "user": OWNER, "client_msg_id": "c-1", "text": text}
    m.update(kw)
    return m


class AnswerTest(Base):
    def setUp(self):
        super().setUp()
        sdir = tempfile.TemporaryDirectory()
        self.addCleanup(sdir.cleanup)
        env = mock.patch.dict(os.environ, {"MOTHER_STATE_DIR": sdir.name})
        env.start()
        self.addCleanup(env.stop)
        self.fetched = []

    def fetch_ok(self, msg=None):
        def f(cfg, channel, ts):
            self.fetched.append((channel, ts))
            return typed_msg(ts=ts) if msg is None else msg
        return f

    def answer(self, verb="ok", payload="", source=None, n=1):
        return decisions.answer(self.ws, CFG, AT, n, verb, payload, source or {"via": "pane"})

    def announce(self, *ids, at=AT + timedelta(seconds=30)):
        """Mark decisions announced to the manager at `at` (by default after they were filed, before SRC_TS)."""
        with decisions.locked(self.ws):
            doc = decisions.load(self.ws)
            doc["batch"]["announced"].update({i: hub._iso(at) for i in ids})
            decisions.save(self.ws, doc)

    def reply(self, text="1 ok", ts=SRC_TS, at=LATER, channel=DM, cfg=None, **kw):
        """The manager types `text`; Slack shows it (with `kw` set on the message)."""
        return decisions.answer_reply(self.ws, cfg or CFG, at, channel, ts,
                                      fetch=self.fetch_ok(typed_msg(ts=ts, text=text, **kw)))

    def status(self, i=0):
        return decisions.load(self.ws)["decisions"][i]["status"]

    def grants(self):
        doc, problem = grants.read(self.ws)
        self.assertIsNone(problem)
        return doc["grants"]

    def queue(self):
        return chief._read_queue(self.ws)

    def test_ok_writes_bound_grant_and_queues(self):
        d = self.propose()
        self.announce("D1")
        lines = self.reply()
        self.assertEqual(self.fetched, [(DM, SRC_TS)])  # one read
        self.assertEqual(len(lines), 1)
        self.assertIn("D1", lines[0])
        gs = self.grants()
        self.assertEqual(len(gs), 1)
        g = gs[0]
        self.assertEqual(g["draft_sha"], d["draft_sha"])
        self.assertEqual((g["uses"], g["run"], g["decision"], g["scope"]), (1, "chief:D1", "D1", "chat-reply"))
        self.assertEqual((g["target"], g["thread"]), ("C00000001", "1700000000.000100"))
        self.assertEqual(g["source"], {"channel": DM, "user": OWNER, "ts": SRC_TS})
        self.assertEqual(g["expires"], grants.iso(LATER.timestamp() + 2 * 3600))
        q = self.queue()
        self.assertEqual(len(q), 1)
        canon = json.dumps(REPLY, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(q[0]["source"], "decision")
        self.assertEqual(q[0]["text"], "D1 approved by the manager. Make exactly this one call now, with this input "
                                       "unchanged, then end the turn.\nTool: %s\nInput: %s" % (POST, canon))
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["status"], "approved")
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "idle")

    def test_pane_source_and_expiry_caps(self):
        decisions.propose(self.ws, {"chief": {"decision_hours": 1}}, AT, "PLAT-1", POST, dict(REPLY), "r")
        self.answer()
        g = self.grants()[0]
        self.assertEqual(g["source"], {"channel": "pane", "user": OWNER, "ts": "%.6f" % AT.timestamp()})
        self.assertEqual(g["expires"], grants.iso(AT.timestamp() + 3600))  # the decision's own expiry is earlier

    def test_edit_text_verbatim(self):
        self.propose()
        payload = '--help "x"\ny'
        self.answer("edit", payload)
        g = self.grants()[0]
        want = dict(REPLY, text=payload)
        self.assertEqual(g["draft_sha"], decisions.guard.draft_sha(POST, want))
        d = decisions.load(self.ws)["decisions"][0]
        self.assertEqual((d["input"], d["text"], d["draft_sha"], d["status"]), (want, payload, g["draft_sha"],
                                                                               "approved"))
        self.assertIn(json.dumps(want, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                      self.queue()[0]["text"])

    def test_edit_without_text_field_stays_open(self):
        self.propose(inp={"channel": "C00000001", "blocks": [{"type": "section"}]})
        msg = self.answer("edit", "new")
        self.assertIn("still open", msg)
        self.assertEqual((self.grants(), self.queue()), ([], []))
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["status"], "pending")

    def test_approve_twice_one_grant(self):
        self.propose()
        self.answer()
        msg = self.answer()
        self.assertEqual(msg, "D1 is already approved.")
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual(len(self.queue()), 1)

    def test_forged_manual_line_not_applied(self):
        self.propose()
        self.announce("D1")
        for bad in (typed_msg(app_id="A1"), typed_msg(bot_id="B1"), typed_msg(client_msg_id=None),
                    typed_msg(user="U00000007"), typed_msg(ts="1.000000"), None):
            f = self.fetch_ok(bad) if bad is not None else (lambda c, ch, ts: None)
            lines = decisions.answer_reply(self.ws, CFG, LATER, DM, SRC_TS, fetch=f)
            self.assertEqual(len(lines), 1, bad)
            self.assertIn("I couldn't confirm that reply came from you (", lines[0])
        self.assertEqual((self.grants(), self.queue()), ([], []))
        self.assertEqual(self.status(), "pending")

    def test_slack_source_outside_dm_or_fetch_error(self):
        self.propose()
        self.announce("D1")

        def boom(cfg, channel, ts):
            raise RuntimeError("down")
        for channel, f in (("C00000001", self.fetch_ok()), (OWNER, self.fetch_ok()), (DM, boom)):
            lines = decisions.answer_reply(self.ws, CFG, LATER, channel, SRC_TS, fetch=f)
            self.assertIn("I couldn't confirm", lines[0])
        self.assertEqual(self.grants(), [])

    def test_answer_refuses_a_slack_source(self):
        self.propose()
        msg = self.answer(source={"via": "slack", "channel": DM, "ts": SRC_TS, "text": "1 ok"})
        self.assertIn("still open", msg)
        self.assertEqual(self.grants(), [])

    def test_reply_edit_keeps_newlines_from_raw(self):
        self.propose()
        self.announce("D1")
        self.reply("1 edit: line one\nline two\ttabbed\n")
        d = decisions.load(self.ws)["decisions"][0]
        self.assertEqual(d["text"], "line one\nline two\ttabbed\n")
        self.assertEqual(self.grants()[0]["draft_sha"], decisions.guard.draft_sha(POST, d["input"]))

    def test_reply_edit_over_300_chars(self):
        self.propose()
        self.announce("D1")
        body = "x" * 700
        self.reply("1 edit: " + body)
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["text"], body)
        self.assertEqual(self.status(), "approved")

    def test_reply_edit_unescapes_and_unwraps_links(self):
        self.propose()
        self.announce("D1")
        self.reply("1 edit: a &amp; b &lt;c&gt; &amp;lt; <https://x.test/a?b=1&amp;c=2|docs> "
                   "<https://y.test|https://y.test> <https://z.test> <@U00000002> <#C00000002|plat>")
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["text"],
                         "a & b <c> &lt; docs (https://x.test/a?b=1&c=2) https://y.test https://z.test "
                         "<@U00000002> <#C00000002|plat>")

    def test_reply_verb_comes_from_verified_message(self):
        self.propose()
        self.announce("D1")
        lines = self.reply("1 no")
        self.assertEqual(len(lines), 1)
        self.assertEqual(self.grants(), [])
        self.assertEqual(self.status(), "rejected")

    def test_reply_not_a_reply_is_none(self):
        self.propose()
        self.announce("D1")
        self.assertIsNone(self.reply("call the venue"))
        self.assertEqual((self.grants(), self.status()), ([], "pending"))

    def test_reply_no_with_words_tells_the_worker_both(self):
        self.propose()
        self.announce("D1")
        lines = self.reply("1 no move it to next sprint")
        self.assertEqual(len(lines), 2)
        self.assertEqual([e["text"] for e in self.queue()],
                         ["D1 was rejected by the manager. Don't make that call.",
                          "The manager's words about D1, their instruction to you: move it to next sprint"])
        self.assertEqual(self.status(), "rejected")

    def test_reply_older_than_decision_not_applied(self):
        self.propose()
        self.announce("D1")
        old = "%.6f" % (AT.timestamp() - 3600)
        lines = self.reply("1 ok", ts=old)
        self.assertEqual(len(lines), 1)
        self.assertEqual((self.grants(), self.status()), ([], "pending"))
        lines = self.reply("ok all", ts=old)
        self.assertEqual(len(lines), 1)
        self.assertEqual((self.grants(), self.status()), ([], "pending"))

    def test_reply_before_announce_not_applied(self):
        self.propose(inp=dict(REPLY, text="first"))
        typed = AT + timedelta(seconds=120)  # D2 was filed at AT, the manager types at T, D2 is announced at T+5
        ts = "%.6f" % typed.timestamp()
        self.propose(inp=dict(REPLY, text="second"))
        self.announce("D1", at=AT + timedelta(seconds=30))
        self.announce("D2", at=typed + timedelta(seconds=5))
        tick = typed + timedelta(seconds=30)  # the next tick reads the reply after the announcement
        self.reply("2 ok", ts=ts, at=tick)
        self.assertEqual((self.grants(), self.status(1)), ([], "pending"))
        self.reply("ok all", ts=ts, at=tick)
        self.assertEqual([g["decision"] for g in self.grants()], ["D1"])  # only what the manager had seen
        self.assertEqual([self.status(0), self.status(1)], ["approved", "pending"])

    def test_announced_list_reads_as_none(self):
        self.propose()
        doc = decisions.load(self.ws)
        doc["batch"]["announced"] = ["D1"]  # the old shape: no stamps, so nothing counts as announced
        with decisions.locked(self.ws):
            decisions.save(self.ws, doc)
        self.assertEqual(decisions.load(self.ws)["batch"]["announced"], {})
        self.assertEqual(len(self.reply("1 ok")), 1)
        self.assertEqual((self.grants(), self.status()), ([], "pending"))

    def test_ok_all_only_announced(self):
        self.propose()
        self.propose(inp=dict(REPLY, text="other"))
        self.announce("D1")
        self.assertEqual(len(self.reply("ok all")), 1)
        self.assertEqual([self.status(0), self.status(1)], ["approved", "pending"])
        self.assertEqual([g["decision"] for g in self.grants()], ["D1"])

    def test_unannounced_id_unreadable(self):
        self.propose()
        lines = self.reply("1 ok")
        self.assertEqual(len(lines), 1)
        self.assertEqual((self.grants(), self.status()), ([], "pending"))
        self.assertEqual(lines, self.reply("ok all"))  # the same unreadable line

    def test_pane_answer_not_limited_to_announced(self):
        self.propose()
        self.answer()
        self.assertEqual(self.status(), "approved")

    def crash_leaving_grant(self, n=1):
        """A first answer whose decision save fails: the grant is on disk, the decision still pending."""
        with mock.patch.object(decisions, "save", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.answer(n=n)
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual(self.status(n - 1), "pending")

    def test_retry_edit_replaces_grant(self):
        d = self.propose()
        self.crash_leaving_grant()
        self.assertEqual(self.grants()[0]["draft_sha"], d["draft_sha"])
        self.answer("edit", "second text")
        gs = self.grants()
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0]["draft_sha"], decisions.guard.draft_sha(POST, dict(REPLY, text="second text")))

    def test_no_removes_grant(self):
        self.propose()
        self.crash_leaving_grant()
        self.answer("no")
        self.assertEqual(self.grants(), [])
        self.assertEqual(self.status(), "rejected")

    def test_say_and_expiry_remove_grant(self):
        self.propose()
        self.crash_leaving_grant()
        self.answer("say", "wait")
        self.assertEqual(self.grants(), [])
        self.propose(inp=dict(REPLY, text="other"))
        self.crash_leaving_grant(n=2)
        late = decisions.answer(self.ws, CFG, AT + timedelta(hours=49), 2, "ok", "", {"via": "pane"})
        self.assertEqual(late, "D2 is already expired.")
        self.assertEqual(self.grants(), [])

    def test_removing_keeps_other_decisions_grants(self):
        self.propose()
        self.propose(inp=dict(REPLY, text="other"))
        self.answer(n=1)
        self.answer("no", n=2)
        self.assertEqual([g["decision"] for g in self.grants()], ["D1"])

    def test_born_dead_grant_not_written(self):
        self.propose()
        off = dict(CFG, policy={"grants": {"enabled": False}})
        msg = decisions.answer(self.ws, off, AT, 1, "ok", "", {"via": "pane"})
        self.assertTrue(msg.startswith("D1 is still open: "), msg)
        self.assertTrue(msg.endswith(". Answer again."), msg)
        one_off = dict(CFG, policy={"grants": {"scopes": {"chat-post": {}}}})  # chat-reply isn't in the list
        msg = decisions.answer(self.ws, one_off, AT, 1, "ok", "", {"via": "pane"})
        self.assertTrue(msg.endswith(". Answer again."), msg)
        self.assertEqual((self.grants(), self.queue(), self.status()), ([], [], "pending"))
        # a reply older than the policy's max age (96 h), on a decision that hasn't expired
        decisions.propose(self.ws, {"chief": {"decision_hours": 200}}, AT, "PLAT-1", POST, dict(REPLY, text="b"), "r")
        self.announce("D2")
        lines = self.reply("2 ok", at=AT + timedelta(hours=100))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("D2 is still open: "), lines[0])
        self.assertTrue(lines[0].endswith(". Answer again."), lines[0])
        self.assertEqual((self.grants(), self.status(1)), ([], "pending"))

    def test_default_fetch_reads_one_ts_and_closes(self):
        calls, closed = [], []

        class FakeClient:
            def call(self, name, args):
                calls.append((name, args))
                return {"messages": [typed_msg()]}

            def close(self):
                closed.append(1)
        built = []

        def build(cfg, have):
            built.append(sorted(cfg["hub"]["connections"]))
            return {"chat": FakeClient()}
        cfg = dict(CFG, hub={"connections": {"chat": {"command": "x"}, "mail": {"command": "y"}}})
        self.propose()
        self.announce("D1")
        with mock.patch.object(decisions.hub, "build_clients", build):
            self.assertIn("approved", decisions.answer_reply(self.ws, cfg, LATER, DM, SRC_TS)[0])
        self.assertEqual(built, [["chat"]])  # only the chat server is started
        self.assertEqual(calls, [("get_channel_history", {"channel_id": DM, "oldest": SRC_TS, "latest": SRC_TS,
                                                          "inclusive": True, "limit": 1})])
        self.assertEqual(closed, [1])
        with mock.patch.object(decisions.hub, "build_clients", lambda c, h: {"chat": RuntimeError("no server")}):
            self.assertIn("no server", decisions.answer_reply(self.ws, cfg, LATER, DM, SRC_TS)[0])
        self.assertIn("no hub chat connection", decisions.answer_reply(self.ws, CFG, LATER, DM, SRC_TS)[0])

    def test_expired_or_sent_reports_status(self):
        self.propose()
        for status in ("expired", "sent"):
            with decisions.locked(self.ws):
                doc = decisions.load(self.ws)
                doc["decisions"][0]["status"] = status
                decisions.save(self.ws, doc)
            self.assertEqual(self.answer(), "D1 is already %s." % status)
        self.assertEqual((self.grants(), self.queue()), ([], []))

    def test_pending_past_expiry_is_expired(self):
        self.propose()
        late = decisions.answer(self.ws, CFG, AT + timedelta(hours=49), 1, "ok", "", {"via": "pane"})
        self.assertEqual(late, "D1 is already expired.")
        self.assertEqual(self.grants(), [])

    def test_unknown_decision(self):
        self.assertIn("D9", self.answer(n=9))

    def test_manual_kind_no_grant(self):
        self.propose(tool=MAIL, inp={"to": "a@b.c", "body": "x"})
        msg = self.answer()
        self.assertEqual(msg, "D1 is yours to do by hand (%s isn't something I can send): x" % MAIL)
        self.assertEqual(self.grants(), [])
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["status"], "manual")

    def test_paused_no_grant(self):
        self.propose()
        chief.pause(self.ws)
        self.assertEqual(self.answer(), "D1 is still open: outbound is paused.")
        self.assertEqual((self.grants(), self.queue()), ([], []))
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["status"], "pending")

    def test_no_and_say_queue_text(self):
        self.propose()
        self.propose(inp=dict(REPLY, text="other"))
        self.answer("no", n=1)
        self.answer("say", "wait until Monday", n=2)
        self.assertEqual([(e["source"], e["text"]) for e in self.queue()],
                         [("decision", "D1 was rejected by the manager. Don't make that call."),
                          ("decision", "The manager's words about D2, their instruction to you: wait until Monday")])
        self.assertEqual([d["status"] for d in decisions.load(self.ws)["decisions"]], ["rejected", "redirected"])
        self.assertEqual(self.grants(), [])
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "idle")

    def test_answer_cli(self):
        self.propose()
        out, err = io.StringIO(), io.StringIO()
        code = decisions.main(["answer", "1", "edit", "--via", "pane", "--", "new", "text"], self.ws, CFG, out, err,
                              at=AT)
        self.assertEqual(code, 0, err.getvalue())
        doc = json.loads(out.getvalue())
        self.assertTrue(doc["ok"])
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["text"], "new text")
        for args in (["answer", "1", "ok"], ["answer", "x", "ok", "--via", "pane"],
                     ["answer", "1", "maybe", "--via", "pane"], ["answer", "1", "ok", "--via", "slack"]):
            self.assertEqual(decisions.main(args, self.ws, CFG, io.StringIO(), io.StringIO(), at=AT), 2, args)
        out = io.StringIO()
        self.assertEqual(decisions.main(["answer", "1", "ok", "--via", "typed"], self.ws, CFG, out, io.StringIO(),
                                        at=AT), 1)
        self.assertEqual(json.loads(out.getvalue()), {"ok": False, "message": "D1 is already approved."})


if __name__ == "__main__":
    unittest.main()
