"""Tests for lib/decisions.py — the decision queue.
Run: python3 -m unittest tests.test_decisions -v
"""
import contextlib
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
SIG = "Mother (AI)"
TEAMS = [{"slug": "platform", "tracker": {"project": "PLAT"}, "channels": [{"id": "C00000001", "mode": "draft"}]}]
SIGNED = {"agent": {"name": "Mother"}, "teams": TEAMS}  # C00000001 and PLAT are targets the guard lets a grant reach
REPLY = {"channel": "C00000001", "text": "hello " + SIG, "thread_ts": "1700000000.000100"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        chief.register_worker(self.ws, AT, "PLAT-1", "Plat one")

    def propose(self, key="PLAT-1", tool=POST, inp=None, **kw):
        return decisions.propose(self.ws, SIGNED, AT, key, tool, dict(REPLY) if inp is None else inp,
                                 kw.pop("recommendation", "send it"), **kw)


class ProposeTest(Base):
    def test_propose_chat_reply_kind_and_sha(self):
        d = self.propose(why="asked", urgent=True)
        self.assertEqual(d["id"], "D1")
        self.assertEqual(d["kind"], "chat-reply")
        self.assertEqual(d["target"], "C00000001")
        self.assertEqual(d["thread"], "1700000000.000100")
        self.assertEqual(d["text"], REPLY["text"])
        self.assertEqual(d["draft_sha"], decisions.guard.draft_sha(POST, REPLY))
        self.assertEqual((d["status"], d["urgent"], d["answer"], d["why"]), ("pending", True, None, "asked"))
        self.assertEqual(d["created"], hub._iso(AT))
        self.assertEqual(d["expires"], hub._iso(AT + timedelta(hours=48)))
        self.assertEqual(decisions.pending(self.ws), [d])
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["id"], "D1")

    def test_propose_tracker_target_is_project(self):
        d = self.propose(tool="mcp__tracker__jira__comment-on-issue",
                         inp={"issueIdOrKey": "PLAT-1", "comment": "x " + SIG})
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
        b = self.propose(inp=dict(REPLY, text="other " + SIG))
        self.assertEqual((a["id"], b["id"]), ("D1", "D2"))
        self.assertEqual(decisions.load(self.ws)["next"], 3)

    def test_decision_hours_setting(self):
        d = decisions.propose(self.ws, dict(SIGNED, chief={"decision_hours": 1}), AT, "PLAT-1", POST, dict(REPLY), "r")
        self.assertEqual(d["expires"], hub._iso(AT + timedelta(hours=1)))

    def test_load_defaults(self):
        self.assertEqual(decisions.load(self.ws), {"version": 1, "next": 1, "decisions": [],
                                                   "batch": {"last_at": None, "announced": {}}, "confirm": [], "replied": {},
                                                   "retry_at": {}, "void": []})


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
            code = decisions.main(args, self.ws, SIGNED, out, err)
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


class ColonForm(unittest.TestCase):
    """M2: the colon form is a colon and then whitespace, and its words run to the end of the line."""
    OPEN = {7, 8, 10}

    def test_a_time_is_not_a_redirect(self):
        for t in ("10:30 call with Dana", "7:30 standup", "10:30", "7:change it"):
            self.assertIsNone(decisions.parse_reply(t, self.OPEN), t)

    def test_commas_stay_in_the_words(self):
        self.assertEqual(decisions.parse_reply("7: change it, please", self.OPEN), [(7, "say", "change it, please")])
        self.assertEqual(decisions.parse_reply("7: a; b, c", self.OPEN), [(7, "say", "a; b, c")])

    def test_the_next_line_is_the_next_answer(self):
        self.assertEqual(decisions.parse_reply("7: change it, please\n8 ok", self.OPEN),
                         [(7, "say", "change it, please"), (8, "ok", "")])
        self.assertEqual(decisions.parse_reply("8 ok, 7: change it, please", self.OPEN),
                         [(8, "ok", ""), (7, "say", "change it, please")])

    def test_a_colon_with_no_words_is_unreadable(self):
        for t in ("7:", "7: ", "7:\n8 ok"):
            self.assertEqual(decisions.parse_reply(t, self.OPEN), [], t)


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
        self.assertEqual(decisions.parse_reply("9: wait until Monday", self.OPEN), [(9, "say", "wait until Monday")])

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


CFG = dict(SIGNED, manager={"chat_dm": ["U00000001", "D00000001"]})
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
        decisions.propose(self.ws, dict(SIGNED, chief={"decision_hours": 1}), AT, "PLAT-1", POST, dict(REPLY), "r")
        self.answer()
        g = self.grants()[0]
        self.assertEqual(g["source"], {"channel": "pane", "user": OWNER, "ts": "%.6f" % AT.timestamp()})
        self.assertEqual(g["expires"], grants.iso(AT.timestamp() + 3600))  # the decision's own expiry is earlier

    def test_edit_text_verbatim(self):
        self.propose()
        payload = '--help "x"\ny ' + SIG
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
        self.propose(inp={"channel": "C00000001", "blocks": [{"type": "section", "text": SIG}]})
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
        self.reply("1 edit: line one " + SIG + "\nline two\ttabbed\n")
        d = decisions.load(self.ws)["decisions"][0]
        self.assertEqual(d["text"], "line one " + SIG + "\nline two\ttabbed\n")
        self.assertEqual(self.grants()[0]["draft_sha"], decisions.guard.draft_sha(POST, d["input"]))

    def test_reply_edit_over_300_chars(self):
        self.propose()
        self.announce("D1")
        body = "x" * 700 + " " + SIG
        self.reply("1 edit: " + body)
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["text"], body)
        self.assertEqual(self.status(), "approved")

    def test_reply_edit_unescapes_and_unwraps_links(self):
        self.propose()
        self.announce("D1")
        self.reply("1 edit: a &amp; b &lt;c&gt; &amp;lt; <https://x.test/a?b=1&amp;c=2|docs> "
                   "<https://y.test|https://y.test> <https://z.test> <@U00000002> <#C00000002|plat> " + SIG)
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["text"],
                         "a & b <c> &lt; docs (https://x.test/a?b=1&c=2) https://y.test https://z.test "
                         "<@U00000002> <#C00000002|plat> " + SIG)

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
        self.propose(inp=dict(REPLY, text="first " + SIG))
        typed = AT + timedelta(seconds=120)  # D2 was filed at AT, the manager types at T, D2 is announced at T+5
        ts = "%.6f" % typed.timestamp()
        self.propose(inp=dict(REPLY, text="second " + SIG))
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
        self.propose(inp=dict(REPLY, text="other " + SIG))
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
        self.answer("edit", "second text " + SIG)
        gs = self.grants()
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0]["draft_sha"], decisions.guard.draft_sha(POST, dict(REPLY, text="second text " + SIG)))

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
        self.propose(inp=dict(REPLY, text="other " + SIG))
        self.crash_leaving_grant(n=2)
        late = decisions.answer(self.ws, CFG, AT + timedelta(hours=49), 2, "ok", "", {"via": "pane"})
        self.assertEqual(late, "D2 is already expired.")
        self.assertEqual(self.grants(), [])

    def test_removing_keeps_other_decisions_grants(self):
        self.propose()
        self.propose(inp=dict(REPLY, text="other " + SIG))
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
        decisions.propose(self.ws, dict(SIGNED, chief={"decision_hours": 200}), AT, "PLAT-1", POST, dict(REPLY, text="b " + SIG), "r")
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
        self.assertEqual(msg, "D1 is yours to do by hand (%s isn't something I can send): x. The full text is in the "
                              "Chief pane." % MAIL)
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
        self.propose(inp=dict(REPLY, text="other " + SIG))
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
        code = decisions.main(["answer", "1", "edit", "--via", "pane", "--", "new", "text", SIG], self.ws, CFG, out, err,
                              at=AT)
        self.assertEqual(code, 0, err.getvalue())
        doc = json.loads(out.getvalue())
        self.assertTrue(doc["ok"])
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["text"], "new text " + SIG)
        for args in (["answer", "1", "ok"], ["answer", "x", "ok", "--via", "pane"],
                     ["answer", "1", "maybe", "--via", "pane"], ["answer", "1", "ok", "--via", "slack"]):
            self.assertEqual(decisions.main(args, self.ws, CFG, io.StringIO(), io.StringIO(), at=AT), 2, args)
        out = io.StringIO()
        self.assertEqual(decisions.main(["answer", "1", "ok", "--via", "pane"], self.ws, CFG, out, io.StringIO(),
                                        at=AT), 1)
        self.assertEqual(json.loads(out.getvalue()), {"ok": False, "message": "D1 is already approved."})


class SignatureTest(Base):
    """A draft the guard would never let out is refused when it is filed, and an edit keeps what the guard reads."""
    TRACKER = "mcp__tracker__jira__"

    def setUp(self):
        super().setUp()
        sdir = tempfile.TemporaryDirectory()
        self.addCleanup(sdir.cleanup)
        env = mock.patch.dict(os.environ, {"MOTHER_STATE_DIR": sdir.name})
        env.start()
        self.addCleanup(env.stop)

    def test_propose_refuses_unsigned_chat_and_tracker_comment(self):
        for tool, inp in ((POST, dict(REPLY, text="no mark")),
                          (POST, {"channel": "C00000001", "text": "no mark"}),
                          (self.TRACKER + "comment-on-issue", {"issueIdOrKey": "PLAT-1", "comment": "no mark"})):
            with self.assertRaises(ValueError) as e:
                self.propose(tool=tool, inp=inp)
            self.assertIn("sign", str(e.exception).lower())
            self.assertIn(SIG, str(e.exception))
        self.assertEqual(decisions.load(self.ws)["decisions"], [])

    def test_propose_refuses_when_no_mark_is_configured(self):
        with self.assertRaises(ValueError) as e:
            decisions.propose(self.ws, {}, AT, "PLAT-1", POST, dict(REPLY), "r")
        self.assertIn("agent", str(e.exception))

    def test_propose_accepts_the_configured_signature(self):
        cfg = {"agent": {"signature": ":sparkles: on behalf"}, "teams": TEAMS}
        d = decisions.propose(self.ws, cfg, AT, "PLAT-1", POST, dict(REPLY, text="hi :sparkles: on behalf"), "r")
        self.assertEqual(d["kind"], "chat-reply")

    def test_propose_needs_no_signature_where_the_guard_needs_none(self):
        self.assertEqual(self.propose(tool=MAIL, inp={"to": "a@b.c", "body": "x"})["kind"], "manual")
        d = self.propose(tool=self.TRACKER + "transition-issue", inp={"issueIdOrKey": "PLAT-1", "transitionId": "1"})
        self.assertEqual(d["kind"], "tracker-transition")

    def test_propose_refuses_unsigned_blocks(self):
        with self.assertRaises(ValueError):
            self.propose(inp=dict(REPLY, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "unsigned"}}]))

    def test_propose_cli_says_why(self):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(dict(REPLY, text="no mark")))
        try:
            code = decisions.main(["propose", "PLAT-1", "--tool", POST, "--recommend", "r", "--stdin"], self.ws, SIGNED,
                                  out, err)
        finally:
            sys.stdin = old
        self.assertEqual(code, 1)
        self.assertIn(SIG, err.getvalue())

    def cfg(self):
        return dict(SIGNED, manager={"chat_dm": ["U00000001", "D00000001"]})

    def test_edit_keeps_the_signature_so_the_grant_can_match(self):
        self.propose()
        msg = decisions.answer(self.ws, self.cfg(), AT, 1, "edit", "new words", {"via": "pane"})
        self.assertIn("approved", msg)
        d = decisions.load(self.ws)["decisions"][0]
        self.assertEqual(d["input"]["text"], "new words\n\n" + SIG)
        g = decisions.guard
        self.assertTrue(g.carries_signature(g.classify_mcp(POST, d["input"]), self.cfg()))
        self.assertEqual(d["draft_sha"], g.draft_sha(POST, d["input"]))

    def test_edit_already_signed_stays_as_typed(self):
        self.propose()
        decisions.answer(self.ws, self.cfg(), AT, 1, "edit", "mine " + SIG, {"via": "pane"})
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["input"]["text"], "mine " + SIG)

    def test_edit_with_no_mark_to_keep_refuses(self):
        self.propose()
        cfg = {"manager": {"chat_dm": ["U00000001", "D00000001"]}}
        msg = decisions.answer(self.ws, cfg, AT, 1, "edit", "new words", {"via": "pane"})
        self.assertIn("still open", msg)
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["status"], "pending")

    def test_edit_refuses_a_call_with_blocks_beside_its_text(self):
        """The reader sees the blocks: editing only the text would send something other than what was approved."""
        blocks = dict(REPLY, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "old " + SIG}}])
        self.propose(inp=blocks)
        msg = decisions.answer(self.ws, self.cfg(), AT, 1, "edit", "new " + SIG, {"via": "pane"})
        self.assertIn("still open", msg)
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["input"], blocks)


class DraftTextTest(Base):
    def test_one_field_is_byte_for_byte(self):
        self.assertEqual(decisions.draft_text(REPLY), REPLY["text"])
        self.assertEqual(decisions.draft_text({"inputs": {"issueIdOrKey": "PLAT-1", "comment": " a\nb "}}), " a\nb ")

    def test_several_fields_are_all_shown_and_labelled(self):
        inp = {"channel": "C00000001", "text": "short", "blocks": [{"type": "section"}], "reply_broadcast": True}
        self.assertEqual(decisions.draft_text(inp), 'text: short\n\nblocks: [{"type": "section"}]\n\nreply_broadcast: true')
        inp = {"projectKey": "PLAT", "issueType": "Task", "summary": "s", "description": "d"}
        self.assertEqual(decisions.draft_text(inp), "summary: s\n\ndescription: d")
        self.assertEqual(decisions.draft_text({"to": "a@b.c", "subject": "s", "cc": "x@b.c", "body": "b"}),
                         "subject: s\n\ncc: x@b.c\n\nbody: b")

    def test_edit_view_says_up_front_what_edit_can_do(self):
        one = self.propose()
        self.assertEqual(decisions.edit_view(one, SIGNED), {"edit_refusal": None, "edit_text": REPLY["text"]})
        tool = "mcp__tracker__jira__create-issue"
        several = self.propose(tool=tool, inp={"projectKey": "PLAT", "summary": "s", "description": "d"})
        view = decisions.edit_view(several, SIGNED)
        self.assertIn("summary and description", view["edit_refusal"])
        self.assertIn("2: <your words>", view["edit_refusal"])
        self.assertIsNone(view["edit_text"])
        no_text = self.propose(tool=tool, inp={"projectKey": "PLAT", "summary": "only"})
        self.assertIn("3: <your words>", decisions.edit_view(no_text, SIGNED)["edit_refusal"])

    def test_edit_view_text_leaves_out_a_shown_setting(self):
        d = self.propose(inp=dict(REPLY, reply_broadcast=True))
        self.assertIn("reply_broadcast", decisions.draft_text(d["input"]))
        self.assertEqual(decisions.edit_view(d, SIGNED)["edit_text"], REPLY["text"])

    def test_edit_of_a_create_says_several_fields_not_no_text(self):
        self.propose(tool="mcp__tracker__jira__create-issue", inp={"projectKey": "PLAT", "summary": "s", "description": "d"})
        got = decisions.answer(self.ws, SIGNED, AT, 1, "edit", "new", {"via": "pane"})
        self.assertIn("summary and description", got)

    def set_inbox(self, channel):
        watch = hub.load_watch(self.ws)
        watch.setdefault("follow", {})["inbox"] = channel
        hub.save_watch(self.ws, watch)

    def test_reply_channel_is_the_watchers_when_config_agrees(self):
        cfg = {"manager": {"chat_dm": ["U00000001", "D00000001"]}}
        self.set_inbox("D00000001")
        self.assertEqual(decisions.reply_channel(self.ws, cfg), ("D00000001", None))

    def test_reply_channel_says_when_replies_cannot_work(self):
        cfg = {"manager": {"chat_dm": ["U00000001", "D00000001"]}}
        chan, why = decisions.reply_channel(self.ws, cfg)              # no inbox: replies never arrive
        self.assertEqual(chan, "D00000001")
        self.assertIn("mother hub dms inbox D00000001", why)
        self.set_inbox("D00000002")                                    # a different D id: fails closed, and says so
        chan, why = decisions.reply_channel(self.ws, cfg)
        self.assertIsNone(chan)
        self.assertIn("D00000002", why)
        chan, why = decisions.reply_channel(self.ws, {"manager": {"chat_dm": "U00000001"}})   # only a U id
        self.assertIsNone(chan)
        self.assertIn("no D", why)

    def test_a_reply_is_read_in_the_channel_the_watcher_read(self):
        cfg = {"manager": {"chat_dm": ["U00000001", "D00000002", "D00000009"]}}
        self.set_inbox("D00000009")
        line = {"id": "chat:manual:x:1700000000.000100", "kind": "manual", "source": "chat", "summary": "1 ok"}
        with mock.patch.object(decisions, "_reply", return_value=("not_reply", [])) as got:
            decisions.apply_replies(self.ws, cfg, AT, [line])
        self.assertEqual(got.call_args[0][3], "D00000009")

    def test_edit_refuses_a_call_with_several_fields(self):
        """New text in one field would send the others unread."""
        cfg = dict(SIGNED, manager={"chat_dm": ["U00000001", "D00000001"]})
        inp = dict(REPLY, unfurl_text="more words")
        self.propose(inp=inp)
        sdir = tempfile.TemporaryDirectory()
        self.addCleanup(sdir.cleanup)
        with mock.patch.dict(os.environ, {"MOTHER_STATE_DIR": sdir.name}):
            msg = decisions.answer(self.ws, cfg, AT, 1, "edit", "new " + SIG, {"via": "pane"})
        self.assertIn("still open", msg)
        self.assertIn("unfurl_text", msg)
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["input"], inp)

    def test_full_text_covers_every_string_field(self):
        """Routing ids go to `target`; nothing that is sent is hidden. A chat post's `project` and `to` are shown."""
        inp = {"channel": "C00000001", "text": "t", "project": "x", "to": "y"}
        d = {"tool": POST, "input": inp, "target": "C00000001"}
        self.assertEqual(decisions.draft_text(inp), "t")
        self.assertEqual(decisions.plain_target(d), "C00000001, project x, to y")

    def test_full_text_shows_reply_broadcast(self):
        inp = dict(REPLY, reply_broadcast=True)
        self.assertEqual(decisions.draft_text(inp), "text: %s\n\nreply_broadcast: true" % REPLY["text"])
        self.assertEqual(decisions.draft_text(dict(REPLY, reply_broadcast=False)), REPLY["text"])

    def test_signature_in_a_field_the_guard_does_not_read(self):
        with mock.patch.object(decisions.guard, "carries_signature", return_value=False):
            with self.assertRaises(ValueError) as e:
                self.propose(inp=dict(REPLY))
        self.assertIn("isn't in a field the guard reads", str(e.exception))
        self.assertNotIn("put %s in the text" % SIG, str(e.exception))

    def test_ids_are_never_reused(self):
        self.propose()
        self.propose(inp=dict(REPLY, text="b " + SIG))
        path = chief.chief_dir(self.ws) / "decisions.json"
        doc = json.loads(path.read_text())
        doc["next"] = 1
        path.write_text(json.dumps(doc))
        self.assertEqual(self.propose(inp=dict(REPLY, text="c " + SIG))["id"], "D3")

    def test_ids_are_not_reused_from_the_ledger_after_a_lost_file(self):
        self.propose()
        (chief.chief_dir(self.ws) / "decisions.json").unlink()
        (self.ws / ".mother" / "ledger.json").write_text(json.dumps({"version": 1, "uses": [{"run": "chief:D7"}]}))
        self.assertEqual(self.propose()["id"], "D8")

    def test_ids_stay_above_a_live_grant_after_a_corrupt_file(self):
        cfg = dict(SIGNED, manager={"chat_dm": ["U00000001", "D00000001"]})
        sdir = tempfile.TemporaryDirectory()
        self.addCleanup(sdir.cleanup)
        with mock.patch.dict(os.environ, {"MOTHER_STATE_DIR": sdir.name}):
            self.propose()
            decisions.answer(self.ws, cfg, AT, 1, "ok", "", {"via": "pane"})
            (chief.chief_dir(self.ws) / "decisions.json").write_text("{not json")
            self.assertEqual(self.propose()["id"], "D2")

    def test_ids_read_the_grant_file_as_it_stands(self):
        (self.ws / ".mother" / "grants.json").write_text(json.dumps(
            {"grants": [{"decision": "D9", "run": "chief:D4"}, {"run": "chief:D11"}, "odd", {"decision": 7}]}))
        self.assertEqual(self.propose()["id"], "D12")

    def test_unreadable_grants_and_ledger_do_not_stop_a_filing(self):
        (self.ws / ".mother" / "grants.json").write_text("{not json")
        (self.ws / ".mother" / "ledger.json").write_text("{not json")
        self.assertEqual(self.propose()["id"], "D1")

    def test_plain_target(self):
        cases = ((POST, REPLY, "C00000001, thread_ts 1700000000.000100"),
                 ("mcp__tracker__jira__comment-on-issue", {"issueIdOrKey": "PLAT-7", "comment": "x"}, "PLAT-7"),
                 ("mcp__tracker__jira__comment-on-issue", {"inputs": {"issueIdOrKey": "PLAT-8", "comment": "x"}},
                  "PLAT-8"),
                 ("mcp__tracker__jira__create-issue", {"projectKey": "PLAT", "summary": "s"}, "PLAT"),
                 (MAIL, {"to": ["a@b.c", "d@b.c"], "body": "x"}, "a@b.c, d@b.c"),
                 (MAIL, {"body": "x"}, ""))
        for tool, inp, want in cases:
            d = {"tool": tool, "input": inp, "target": decisions.kind_of(tool, inp)[1]}
            self.assertEqual(decisions.plain_target(d), want, inp)
        self.assertEqual(decisions.plain_target({"tool": MAIL, "input": {"to": "a\n\u202eb"}}), "a b")


class ResumeTest(Base):
    def test_answering_one_of_two_leaves_the_worker_waiting(self):
        cfg = dict(SIGNED, manager={"chat_dm": ["U00000001", "D00000001"]})
        self.propose(tool=MAIL, inp={"to": "a@b.c", "body": "x"})
        self.propose(tool=MAIL, inp={"to": "a@b.c", "body": "y"})
        decisions.answer(self.ws, cfg, AT, 1, "no", "", {"via": "pane"})
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "waiting_decision")
        decisions.answer(self.ws, cfg, AT, 2, "no", "", {"via": "pane"})
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "idle")


class AnswerEnvironment(unittest.TestCase):
    """Fix round 1, C1: behind the guard, a worker's process can't answer; the Chief pane, in the chief's session, can."""

    def setUp(self):
        import tempfile as _t
        self._tmp = _t.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def test_worker_env_is_refused_and_writes_nothing(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"MOTHER_CHIEF_WORKER": "PLAT-1"}), \
                mock.patch.object(decisions, "_answer", side_effect=AssertionError("must not run")):
            code = decisions.main(["answer", "1", "ok", "--via", "pane"], self.ws, {}, out, err)
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_chief_role_env_still_answers(self):
        """hooks/chief.tsx's `$.process.run` inherits the keeper's MOTHER_CHIEF_ROLE (it is how the mod reads its role)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"MOTHER_CHIEF_ROLE": "chief"}), \
                mock.patch.object(decisions, "_answer", return_value=(True, "D1 approved")) as called:
            code = decisions.main(["answer", "1", "ok", "--via", "pane"], self.ws, {}, out, err)
        self.assertEqual(code, 0)
        called.assert_called_once()


class TargetEligibility(Base):
    """Final I2: a call the guard could never let through is refused when it's filed, using the guard's own function."""

    def test_refuses_targets_the_guard_would_deny(self):
        for inp, tool in ((dict(REPLY, channel="D12345678"), POST),                       # a colleague's DM
                          (dict(REPLY, channel="C00000099"), POST),                       # no team has it
                          ({"issueIdOrKey": "OTHER-1", "comment": "x " + SIG},
                           "mcp__tracker__jira__comment-on-issue")):
            with self.assertRaises(ValueError) as e:
                self.propose(inp=inp, tool=tool)
            self.assertIn("the guard refused the call: target not allowed", str(e.exception))
        self.assertEqual(decisions.load(self.ws)["decisions"], [])

    def test_refuses_an_external_or_off_channel(self):
        for extra in ({"external": True}, {"mode": "off"}):
            ch = dict({"id": "C00000001", "mode": "draft"}, **extra)
            cfg = dict(SIGNED, teams=[{"slug": "p", "channels": [ch]}])
            with self.assertRaises(ValueError):
                decisions.propose(self.ws, cfg, AT, "PLAT-1", POST, dict(REPLY), "r")

    def test_refuses_a_scope_with_no_uses(self):
        cfg = dict(SIGNED, policy={"grants": {"scopes": {"chat-reply": {"max_uses_per_source": 0}}}})
        with self.assertRaises(ValueError) as e:
            decisions.propose(self.ws, cfg, AT, "PLAT-1", POST, dict(REPLY), "r")
        self.assertIn("chat-reply", str(e.exception))

    def test_a_manual_kind_has_no_target_to_refuse(self):
        self.assertEqual(self.propose(tool=MAIL, inp={"to": "a@b.c", "body": "x"})["kind"], "manual")

    def test_the_decision_uses_the_guards_function(self):
        with mock.patch.object(decisions.guard, "allowed_targets", return_value=set()) as got:
            with self.assertRaises(ValueError):
                self.propose()
        got.assert_called_once_with("chat-reply", SIGNED)

    def test_no_grant_when_the_target_went_away_after_filing(self):
        self.propose()
        cfg = dict(CFG, teams=[])
        msg = decisions.answer(self.ws, cfg, AT, 1, "ok", "", {"via": "pane"})
        self.assertIn("still open", msg)
        self.assertIn("target not allowed", msg)
        self.assertEqual(decisions.load(self.ws)["decisions"][0]["status"], "pending")

    def closed(self, cfg):
        self.propose()
        decisions.answer(self.ws, CFG, AT, 1, "ok", "", {"via": "pane"})
        chief.drop_queued(self.ws, "PLAT-1", "D1 approved by")
        decisions.settle(self.ws, AT + timedelta(seconds=5), cfg)
        return decisions.load(self.ws), chief._read_queue(self.ws)

    def test_closure_names_the_guards_refusal(self):
        doc, queue = self.closed(dict(CFG, teams=[]))
        self.assertEqual(doc["decisions"][0]["status"], "failed")
        self.assertIn("the guard refused the call: target not allowed", doc["confirm"][0])
        self.assertIn("the guard refused the call: target not allowed", queue[-1]["text"])
        self.assertNotIn("input", doc["confirm"][0] + queue[-1]["text"])

    def test_closure_does_not_claim_the_input_changed(self):
        doc, queue = self.closed(CFG)
        self.assertNotIn("the input wasn't the approved one", queue[-1]["text"])
        self.assertIn("nothing went out", doc["confirm"][0])


class NumberAndWords(unittest.TestCase):
    """Final Minor 1 (ruling T2): a number followed by free words is not a reply."""
    OPEN = {2, 7, 8, 9}

    def test_a_note_starting_with_a_number_is_not_a_reply(self):
        for t in ("2 things for tomorrow", "9 wait until Monday", "D2 things", "2. things", "7 okay then"):
            self.assertIsNone(decisions.parse_reply(t, self.OPEN), t)

    def test_words_for_the_worker_take_a_colon(self):
        self.assertEqual(decisions.parse_reply("9: wait until Monday", self.OPEN), [(9, "say", "wait until Monday")])
        self.assertEqual(decisions.parse_reply("D9 : wait", self.OPEN), [(9, "say", "wait")])
        self.assertEqual(decisions.parse_reply("9:", self.OPEN), [])

    def test_no_with_words_stays(self):
        self.assertEqual(decisions.parse_reply("8 no not now", self.OPEN), [(8, "no", ""), (8, "say", "not now")])

    def test_a_later_clause_with_free_words_applies_nothing(self):
        self.assertEqual(decisions.parse_reply("7 ok, 9 wait", self.OPEN), [])

    def test_still_reply_shaped(self):
        for t in ("7", "7,", "7 ok then", "7 edit"):
            self.assertEqual(decisions.parse_reply(t, self.OPEN), [], t)

    def test_is_reply_line_lets_the_note_through(self):
        line = {"kind": "manual", "source": "chat", "summary": "2 things for tomorrow"}
        self.assertFalse(decisions.is_reply_line(line))

    def test_the_refusal_names_the_form_that_works(self):
        d = {"id": "D2", "tool": "mcp__tracker__jira__create-issue",
             "input": {"projectKey": "PLAT", "summary": "s", "description": "d"}}
        got = decisions.edit_view(d, SIGNED)["edit_refusal"]
        self.assertIn("2: <your words>", got)
        self.assertEqual(decisions.parse_reply("2: <your words>", {2}), [(2, "say", "<your words>")])


POST_TS = "%.6f" % (AT.timestamp() + 40)


def app_msg(ts, text, **kw):
    m = {"ts": ts, "app_id": "A00000001", "bot_id": "B00000001", "text": text}
    m.update(kw)
    return m


class SlackBase(Base):
    def setUp(self):
        super().setUp()
        sdir = tempfile.TemporaryDirectory()
        self.addCleanup(sdir.cleanup)
        env = mock.patch.dict(os.environ, {"MOTHER_STATE_DIR": sdir.name})
        env.start()
        self.addCleanup(env.stop)

    def doc(self):
        return decisions.load(self.ws)

    def cli_sent(self, slack, ids="D1", ts=POST_TS, *extra):
        """`mother decide sent`, the Slack read answered by `slack` (a function of channel and ts)."""
        out, err = io.StringIO(), io.StringIO()
        args = ["sent", "--ids", ids] + (["--ts", ts] if ts else [])
        with mock.patch.object(decisions, "_fetch_slack", lambda cfg, ch, t: slack(ch, t)):
            code = decisions.main(args, self.ws, CFG, out, err, at=AT + timedelta(seconds=41))
        return code, err.getvalue()


class SentProvesAPost(SlackBase):
    """Final I1: a stamp must prove a post."""

    def setUp(self):
        super().setUp()
        self.propose(why="they <asked> & @x")
        self.text = decisions.offer(self.ws, CFG, AT + timedelta(seconds=30))["text"]

    def assert_not_stamped(self):
        self.assertEqual(self.doc()["batch"]["announced"], {})
        self.assertIn("offered", self.doc()["batch"])  # the offer stays: nothing was used up

    def test_the_posted_batch_is_stamped_at_its_ts(self):
        seen = []

        def slack(ch, ts):
            seen.append((ch, ts))
            return app_msg(ts, self.text)
        code, err = self.cli_sent(slack)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen, [(DM, POST_TS)])
        self.assertEqual(self.doc()["batch"]["announced"], {"D1": POST_TS})
        self.assertNotIn("offered", self.doc()["batch"])

    def test_nothing_posted_is_refused(self):
        code, err = self.cli_sent(lambda ch, ts: None)
        self.assertEqual(code, 2)
        self.assertIn("no message", err)
        self.assertNotIn("no batch is waiting", err)
        self.assert_not_stamped()

    def test_other_text_is_refused(self):
        code, err = self.cli_sent(lambda ch, ts: app_msg(ts, self.text + " and one more line"))
        self.assertEqual(code, 2)
        self.assertIn("isn't the batch", err)
        self.assertNotIn("no batch is waiting", err)
        self.assert_not_stamped()

    def test_a_message_the_manager_typed_is_refused(self):
        code, err = self.cli_sent(lambda ch, ts: typed_msg(ts=ts, text=self.text))
        self.assertEqual(code, 2)
        self.assertNotIn("no batch is waiting", err)
        self.assert_not_stamped()

    def test_a_failed_read_is_retryable_and_stamps_nothing(self):
        def slack(ch, ts):
            raise RuntimeError("429")
        code, err = self.cli_sent(slack)
        self.assertEqual(code, 3)
        self.assertIn("429", err)
        self.assert_not_stamped()

    def test_no_live_offer_keeps_its_phrase(self):
        self.assertEqual(self.cli_sent(lambda ch, ts: app_msg(ts, self.text))[0], 0)
        code, err = self.cli_sent(lambda ch, ts: app_msg(ts, self.text))
        self.assertEqual(code, 2)
        self.assertIn("no batch is waiting", err)

    def test_no_ts_is_refused(self):
        code, err = self.cli_sent(lambda ch, ts: self.fail("no read without a ts"), "D1", None)
        self.assertEqual(code, 2)
        self.assertIn("--ts", err)
        self.assert_not_stamped()

    def test_slacks_escaping_does_not_refuse_the_real_post(self):
        self.assertIn("&lt;asked&gt;", self.text)
        once = self.text.replace("&", "&amp;")
        for wire in (self.text, once, once.replace("&amp;lt;", "<").replace("&amp;gt;", ">")):
            self.setUp_again()
            self.assertEqual(self.cli_sent(lambda ch, ts: app_msg(ts, wire))[0], 0, wire)

    def setUp_again(self):
        with decisions.locked(self.ws):
            doc = self.doc()
            doc["batch"] = {"last_at": None, "announced": {}}
            decisions.save(self.ws, doc)
        decisions.offer(self.ws, CFG, AT + timedelta(seconds=30))

    def test_an_earlier_offers_post_stamps_only_what_it_showed(self):
        """A post that may have landed, then a newer offer: the late ts still proves the older text."""
        decisions.propose(self.ws, CFG, AT + timedelta(seconds=31), "PLAT-1", POST, dict(REPLY, text="two " + SIG), "r")
        newer = decisions.offer(self.ws, CFG, AT + timedelta(seconds=40))
        self.assertEqual(newer["ids"], ["D1", "D2"])
        code, err = self.cli_sent(lambda ch, ts: app_msg(ts, self.text), "D1,D2")
        self.assertEqual(code, 2)
        self.assertIn("D2", err)
        self.assertEqual(self.cli_sent(lambda ch, ts: app_msg(ts, self.text), "D1")[0], 0)
        self.assertEqual(self.doc()["batch"]["announced"], {"D1": POST_TS})

    def test_a_worker_process_is_still_refused(self):
        with mock.patch.dict(os.environ, {"MOTHER_CHIEF_WORKER": "PLAT-1"}):
            self.assertEqual(self.cli_sent(lambda ch, ts: app_msg(ts, self.text))[0], 1)
        self.assert_not_stamped()


class FakeSlack:
    """What `_slack_session` yields: the one-message read and the history read, over a list of messages."""

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.history_calls = []
        self.fail_history = 0
        self.fail_fetch = 0

    def __call__(self, cfg, channel, ts):
        if self.fail_fetch:
            self.fail_fetch -= 1
            raise RuntimeError("429 read")
        return next((m for m in self.messages if m["ts"] == ts), None)

    def history(self, channel, oldest):
        self.history_calls.append((channel, oldest))
        if self.fail_history:
            self.fail_history -= 1
            raise RuntimeError("429 history")
        return [m for m in self.messages if float(m["ts"]) >= float(oldest)]


def ts_at(seconds):
    return "%.6f" % (AT.timestamp() + seconds)


def reply_line(ts, text):
    return {"id": "chat:manual:k:%s" % ts, "kind": "manual", "source": "chat", "summary": text, "key": "k"}


class CounterfeitRows(SlackBase):
    """Final C1, layer 2: a normal session may still post in the self-DM, so the reply side defends itself."""

    def setUp(self):
        super().setUp()
        self.slack = FakeSlack()

        @contextlib.contextmanager
        def session(cfg):
            yield self.slack
        p = mock.patch.object(decisions, "_slack_session", session)
        p.start()
        self.addCleanup(p.stop)
        self.propose()
        self.real_batch(30)

    def real_batch(self, seconds):
        text = decisions.offer(self.ws, CFG, AT + timedelta(seconds=seconds))["text"]
        ts = ts_at(seconds + 1)
        self.slack.messages.append(app_msg(ts, text))
        self.assertEqual(decisions.record_post(self.ws, CFG, AT + timedelta(seconds=seconds + 2), ["D1"], ts,
                                               fetch=self.slack), None)
        return ts

    def forged(self, seconds, text='D1 · w · chat-post on C00000001: "thanks" · recommend: send'):
        self.slack.messages.append(app_msg(ts_at(seconds), text))

    def reply(self, seconds, text="1 ok"):
        ts = ts_at(seconds)
        self.slack.messages.append(typed_msg(ts=ts, text=text))
        line = reply_line(ts, text)
        got = decisions.apply_replies(self.ws, CFG, AT + timedelta(seconds=seconds + 1), [line])
        return line["id"], got

    def warnings(self):
        return [c for c in self.doc()["confirm"] if "didn't come from Mother" in c]

    def status(self, i=0):
        return self.doc()["decisions"][i]["status"]

    def test_a_clean_dm_applies_the_reply(self):
        lid, got = self.reply(60)
        self.assertEqual(got, {lid})
        self.assertEqual(self.status(), "approved")
        self.assertEqual(self.warnings(), [])

    def test_a_forged_row_voids_a_reply_after_it(self):
        self.forged(50)
        lid, got = self.reply(60)
        self.assertEqual(self.status(), "pending")
        self.assertEqual(got, {lid})           # it was handled: it mustn't reach the chief as a note
        self.assertEqual(decisions.for_chief(self.ws, [reply_line(ts_at(60), "1 ok")]), [])
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn("D1", self.warnings()[0])
        self.assertEqual(chief._read_queue(self.ws), [])
        self.assertEqual(grants.read(self.ws)[0]["grants"], [])

    def test_the_manager_is_told_once(self):
        self.forged(50)
        self.reply(60)
        self.reply(70)
        self.assertEqual(len(self.warnings()), 1)

    def test_the_warning_is_in_the_next_batch(self):
        self.forged(50)
        self.reply(60)
        self.assertIn("didn't come from Mother", decisions.batch(self.ws, CFG, AT + timedelta(seconds=80))["text"])

    def test_a_reply_typed_before_the_forged_row_stands(self):
        ts = ts_at(45)
        self.slack.messages.append(typed_msg(ts=ts))
        self.forged(50)
        decisions.apply_replies(self.ws, CFG, AT + timedelta(seconds=61), [reply_line(ts, "1 ok")])
        self.assertEqual(self.status(), "approved")

    def test_the_next_real_batch_ends_the_void(self):
        self.forged(50)
        self.reply(60)
        self.real_batch(70)
        self.reply(80)
        self.assertEqual(self.status(), "approved")

    def test_a_reply_read_late_from_inside_the_void_stays_void(self):
        self.forged(50)
        self.reply(55)
        ts = ts_at(60)
        self.slack.messages.append(typed_msg(ts=ts))
        self.real_batch(70)
        decisions.apply_replies(self.ws, CFG, AT + timedelta(seconds=81), [reply_line(ts, "1 ok")])
        self.assertEqual(self.status(), "pending")

    def test_a_forged_row_seen_only_after_the_next_batch_still_voids(self):
        """The watcher can bring the reply after the next batch went out: the scan starts at the batch before the reply."""
        self.forged(50)
        ts = ts_at(60)
        self.slack.messages.append(typed_msg(ts=ts))
        decisions.propose(self.ws, CFG, AT + timedelta(seconds=65), "PLAT-1", POST, dict(REPLY, text="two " + SIG), "r",
                          urgent=True)
        self.real_batch(70)
        decisions.apply_replies(self.ws, CFG, AT + timedelta(seconds=81), [reply_line(ts, "1 ok")])
        self.assertEqual(self.status(), "pending")
        self.assertEqual(len(self.warnings()), 1)

    def test_ok_all_is_held_whole(self):
        decisions.propose(self.ws, CFG, AT + timedelta(seconds=31), "PLAT-1", POST, dict(REPLY, text="two " + SIG), "r",
                          urgent=True)
        text = decisions.offer(self.ws, CFG, AT + timedelta(seconds=32))["text"]
        self.slack.messages.append(app_msg(ts_at(33), text))
        decisions.record_post(self.ws, CFG, AT + timedelta(seconds=34), ["D1", "D2"], ts_at(33), fetch=self.slack)
        self.forged(50)
        self.reply(60, "ok all")
        self.assertEqual((self.status(0), self.status(1)), ("pending", "pending"))

    def test_another_row_is_not_voided(self):
        decisions.propose(self.ws, CFG, AT + timedelta(seconds=31), "PLAT-1", POST, dict(REPLY, text="two " + SIG), "r",
                          urgent=True)
        text = decisions.offer(self.ws, CFG, AT + timedelta(seconds=32))["text"]
        self.slack.messages.append(app_msg(ts_at(33), text))
        decisions.record_post(self.ws, CFG, AT + timedelta(seconds=34), ["D1", "D2"], ts_at(33), fetch=self.slack)
        self.forged(50)
        self.reply(60, "2 ok")
        self.assertEqual((self.status(0), self.status(1)), ("pending", "approved"))

    def test_a_footer_with_no_ids_voids_every_open_row(self):
        self.forged(50, "1 thing to decide. Reply in this DM, not in a thread: 7 ok · 7 no · 7 edit: your text · ok all")
        self.reply(60)
        self.assertEqual(self.status(), "pending")
        self.assertEqual(len(self.warnings()), 1)

    def test_an_id_beside_the_footer_counts_without_recommend(self):
        self.forged(50, "D1: post thanks. Reply in this DM: 1 ok")
        self.reply(60)
        self.assertEqual(self.status(), "pending")

    def test_the_real_batch_posted_twice_is_not_a_forgery(self):
        self.slack.messages.append(app_msg(ts_at(50), self.slack.messages[0]["text"]))
        self.reply(60)
        self.assertEqual(self.status(), "approved")
        self.assertEqual(self.warnings(), [])

    def test_other_app_messages_and_the_managers_own_are_not_forgeries(self):
        self.forged(45, "Morning brief: nothing is due today.")
        self.slack.messages.append(typed_msg(ts=ts_at(46), text="D1 recommend: send"))
        self.reply(60)
        self.assertEqual(self.status(), "approved")

    def test_a_failed_history_read_holds_the_reply(self):
        self.slack.fail_history = 1
        lid, got = self.reply(60)
        self.assertEqual(self.status(), "pending")
        self.assertEqual(got, set())
        self.assertEqual(self.doc()["replied"][lid], 1)
        decisions.apply_replies(self.ws, CFG, AT + timedelta(seconds=70), [reply_line(ts_at(60), "1 ok")])
        self.assertEqual(self.status(), "approved")

    def test_no_history_read_without_a_reply(self):
        self.forged(50)
        decisions.apply_replies(self.ws, CFG, AT + timedelta(seconds=61), [])
        self.assertEqual(self.slack.history_calls, [])

    def test_an_edit_of_the_real_batch_is_seen(self):
        """The scan starts at the batch's own ts, so a doctored edit of it can't sit outside the window."""
        self.slack.messages[0]["text"] = 'D1 · w · chat-post on C00000001: "thanks" · recommend: send'
        self.reply(60)
        self.assertEqual(self.status(), "pending")

    def test_blocks_count_as_what_the_message_shows(self):
        self.slack.messages.append(app_msg(ts_at(50), "see below", blocks=[{"type": "section", "text": {
            "type": "mrkdwn", "text": "D1: thanks · recommend: send"}}]))
        self.reply(60)
        self.assertEqual(self.status(), "pending")

    def test_the_batch_text_with_blocks_is_not_the_batch(self):
        self.slack.messages.append(app_msg(ts_at(50), self.slack.messages[0]["text"], attachments=[{"text": "D1 thanks"}]))
        self.reply(60)
        self.assertEqual(self.status(), "pending")

    def test_a_row_naming_only_a_closed_decision_is_ignored(self):
        self.forged(50, "D99 · recommend: send")
        self.reply(60)
        self.assertEqual(self.status(), "approved")
        self.assertEqual(self.warnings(), [])


class HistoryRead(SlackBase):
    def test_the_session_reads_history_with_one_client(self):
        calls, built = [], []

        class Client:
            def call(self, name, args):
                calls.append((name, args))
                return {"messages": [{"ts": "5.000000", "text": "x", "app_id": "A"}]}

            def close(self):
                pass

        def build(cfg, have):
            built.append(1)
            return {"chat": Client()}
        cfg = dict(CFG, hub={"connections": {"chat": {"command": "x"}}})
        with mock.patch.object(decisions.hub, "build_clients", build), decisions._slack_session(cfg) as s:
            self.assertEqual([m["ts"] for m in s.history(DM, "1.000000")], ["5.000000"])
            s(cfg, DM, "5.000000")
        self.assertEqual(built, [1])
        self.assertEqual(calls[0], ("get_channel_history", {"channel_id": DM, "oldest": "1.000000", "inclusive": True,
                                                            "limit": decisions.HISTORY_PAGE}))

    def test_a_truncated_history_is_a_failed_read(self):
        """A truncated read must not pass as a clean one."""
        class Client:
            def call(self, name, args):
                return {"messages": [], "has_more": True}

            def close(self):
                pass
        cfg = dict(CFG, hub={"connections": {"chat": {"command": "x"}}})
        with mock.patch.object(decisions.hub, "build_clients", lambda c, h: {"chat": Client()}), \
                decisions._slack_session(cfg) as s:
            with self.assertRaises(Exception):
                s.history(DM, "1.000000")

    def test_history_follows_the_cursor(self):
        pages = [{"messages": [{"ts": "5.000000"}], "has_more": True, "response_metadata": {"next_cursor": "n"}},
                 {"messages": [{"ts": "6.000000"}], "has_more": False}]
        seen = []

        class Client:
            def call(self, name, args):
                seen.append(args.get("cursor"))
                return pages.pop(0)

            def close(self):
                pass
        cfg = dict(CFG, hub={"connections": {"chat": {"command": "x"}}})
        with mock.patch.object(decisions.hub, "build_clients", lambda c, h: {"chat": Client()}), \
                decisions._slack_session(cfg) as s:
            self.assertEqual([m["ts"] for m in s.history(DM, "1.000000")], ["5.000000", "6.000000"])
        self.assertEqual(seen, [None, "n"])


class SlackOutage(SlackBase):
    """Final I3: an outage isn't a bad reply. The reply waits, the manager is told once, and it applies when it can."""
    TOLD = "Slack read failed; I'll apply your reply when I can read it"

    def setUp(self):
        super().setUp()
        self.slack = FakeSlack()

        @contextlib.contextmanager
        def session(cfg):
            yield self.slack
        p = mock.patch.object(decisions, "_slack_session", session)
        p.start()
        self.addCleanup(p.stop)
        self.propose()
        text = decisions.offer(self.ws, CFG, AT + timedelta(seconds=30))["text"]
        self.slack.messages.append(app_msg(ts_at(31), text))
        decisions.record_post(self.ws, CFG, AT + timedelta(seconds=32), ["D1"], ts_at(31), fetch=self.slack)
        self.ts = ts_at(60)
        self.line = reply_line(self.ts, "1 ok")
        self.slack.messages.append(typed_msg(ts=self.ts))

    def tick(self, seconds):
        return decisions.apply_replies(self.ws, CFG, AT + timedelta(seconds=seconds), [self.line])

    def fail_three(self):
        self.slack.fail_fetch = 3
        for s in (61, 91, 121):
            self.tick(s)

    def told(self):
        return [c for c in decisions.load(self.ws)["confirm"] if c == self.TOLD]

    def test_three_failures_tell_the_manager_once_and_keep_it_pending(self):
        self.fail_three()
        self.assertEqual(len(self.told()), 1)
        self.assertEqual(self.doc()["decisions"][0]["status"], "pending")
        self.assertNotIn(decisions.UNREADABLE, self.doc()["confirm"])
        self.assertEqual(decisions.waiting_reads(self.ws), {self.line["id"]})
        self.slack.fail_fetch = 5
        self.tick(400)
        self.tick(4000)
        self.assertEqual(len(self.told()), 1)

    def test_it_applies_when_slack_comes_back(self):
        self.fail_three()
        self.assertEqual(self.tick(4000), {self.line["id"]})
        self.assertEqual(self.doc()["decisions"][0]["status"], "approved")

    def test_retries_back_off(self):
        self.fail_three()
        reads = []

        class Counting(FakeSlack):
            def __call__(s, cfg, channel, ts):
                reads.append(ts)
                raise RuntimeError("429")
        self.slack = Counting(self.slack.messages)
        for s in range(150, 400, 30):
            self.tick(s)
        self.assertLess(len(reads), 5)
        self.assertGreaterEqual(len(reads), 1)
        reads.clear()
        self.tick(100000)
        self.assertEqual(len(reads), 1)  # a long gap never stops the retries

    def test_a_reply_that_was_read_and_does_not_parse_gets_the_help_line(self):
        self.fail_three()
        self.slack.messages[-1] = typed_msg(ts=self.ts, text="1 ok 2 3")
        self.tick(4000)
        self.assertIn(decisions.UNREADABLE, self.doc()["confirm"])
        self.assertIs(self.doc()["replied"][self.line["id"]], False)

    def test_the_hold_on_the_chief_ends_after_three(self):
        note = {"id": "chat:manual:n:%s" % ts_at(62), "kind": "manual", "source": "chat", "summary": "a note", "key": "n"}
        self.slack.fail_fetch = 1
        self.tick(61)
        self.assertEqual(decisions.for_chief(self.ws, [self.line, note]), [])       # held, as ruled
        self.slack.fail_fetch = 2
        self.tick(91)
        self.tick(121)
        self.assertEqual(decisions.for_chief(self.ws, [self.line, note]), [note])   # past it, and the reply stays out

    def test_it_gives_up_when_nothing_it_could_answer_is_left(self):
        self.fail_three()
        self.slack.fail_fetch = 99
        decisions.settle(self.ws, AT + timedelta(hours=49))
        self.tick(49 * 3600 + 10)
        self.assertEqual(decisions.waiting_reads(self.ws), set())
        self.assertIs(self.doc()["replied"][self.line["id"]], False)
        self.assertTrue(any("wasn't applied" in c for c in self.doc()["confirm"]))


# (offered, what Slack stored), from a live self-DM. Ids in here are placeholders.
SAME_TEXT = [
    ("\u2705 done", ":white_check_mark: done"),
    (":white_check_mark: done", ":white_check_mark: done"),
    ("\u2705\ufe0f done", ":white_check_mark: done"),
    ("\u2705\u2705 done", ":white_check_mark::white_check_mark: done"),
    ("Done\u2705", "Done:white_check_mark:"),
    ("\u2705Done", ":white_check_mark:Done"),
    ("\U0001F1EE\U0001F1F1", ":flag-il:"),
    ("\U0001F468\u200D\U0001F469\u200D\U0001F467", ":man-woman-girl:"),
    ("\U0001F44D\U0001F3FD ok", ":+1::skin-tone-4: ok"),
    ("\U0001F4AF", ":100:"),
    ("1\ufe0f\u20e3", ":one:"),
    (":notreal:", ":notreal:"),
    ("see example.com", "see <http://example.com|example.com>"),
    ("a@\u200bexample.com", "a@\u200b<http://example.com|example.com>"),
    ("<https://x/y|the plugin>", "<https://x/y|the plugin>"),
    ("see https://x/y", "see <https://x/y>"),
    ("https://x/y?a=1&amp;b=2", "<https://x/y?a=1&amp;b=2>"),
    ("a &lt; b &amp; c &gt; d", "a &lt; b &amp; c &gt; d"),
    ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
    ("&lt;https://x/y|the plugin&gt;", "&lt;<https://x/y>|the plugin&gt;"),
    ("@\u200bchannel", "@\u200bchannel"),
    ("&lt;\u200b!channel&gt;", "&lt;\u200b!channel&gt;"),
    ("end  \nnext", "end  \nnext"),
    ("end\nnext", "end  \nnext"),
    ("\u05e9\u05dc\u05d5\u05dd \u201cq\u201d \u2014 x\u2026 #general @\u200bsomeone", "\u05e9\u05dc\u05d5\u05dd \u201cq\u201d \u2014 x\u2026 #general @\u200bsomeone"),
    ("```x```", "```x```"),
]
NOT_SAME_TEXT = [
    ("ship Tuesday", "ship Friday"),
    ("D7 \u00b7 recommend: send", "D8 \u00b7 recommend: send"),
    ("on C00000001", "on C00000002"),
    ("100 apps", "200 apps"),
    ("at 10:30:45", "at 10:31:45"),
    ("key:one:two", "key:other:two"),
    ("score :100:", "score :200:"),
    ("\u2705 PLAT-1", "\u2705 PLAT-2"),
    ("\u2705 ok", "ok"),
    ("\u2713 D1", "\u2717 D1"),                                  # a tick and a cross are text, not emoji
    ("@\u200bchannel", "@channel"),                              # Slack keeps ZWSP, so one without it isn't the offer
    ("see example.com", "see <http://example.org|example.org>"),
    ("Done\u2705 D1", "Done:white_check_mark: D2"),
]


class SlackShapes(unittest.TestCase):
    def test_what_slack_stores_is_what_was_offered(self):
        for offered, stored in SAME_TEXT:
            with self.subTest(offered=offered):
                self.assertTrue(decisions._same_text(offered, stored), (decisions._norm(offered), decisions._norm(stored)))

    def test_a_post_that_differs_does_not_match(self):
        for offered, stored in NOT_SAME_TEXT:
            with self.subTest(offered=offered):
                self.assertFalse(decisions._same_text(offered, stored), (decisions._norm(offered), decisions._norm(stored)))

    def test_every_emoji_form_is_one_placeholder(self):
        for t in ("\u2705", ":white_check_mark:", "\U0001F1EE\U0001F1F1", ":flag-il:", ":+1::skin-tone-4:",
                  "\U0001F468\u200D\U0001F469\u200D\U0001F467", ":notreal:"):
            self.assertEqual(decisions._norm(t), decisions._norm("\u2705"), t)

    def test_slack_text_is_left_alone(self):
        self.assertEqual(decisions.slack_text("<https://x/y|lab> \u2705"), "lab (https://x/y) \u2705")

    def plain(self, text="see <https://github.com/o/r>", **kw):
        return decisions._plain(dict({"text": text}, **kw))

    def test_a_preview_of_the_messages_own_link_is_plain(self):
        for att in ({"is_app_unfurl": True, "app_unfurl_url": "https://github.com/o/r"},
                    {"from_url": "https://github.com/o/r", "text": "x"},
                    {"from_url": "https://github.com/o/r/", "original_url": "https://github.com/o/r"},
                    {"is_app_unfurl": True, "original_url": "https://github.com/o/r"}):
            self.assertTrue(self.plain(attachments=[att]), att)

    def test_an_auto_linked_domains_preview_is_plain(self):
        self.assertTrue(self.plain("see <http://example.com|example.com>",
                                   attachments=[{"from_url": "http://example.com/", "text": "x"}]))

    def test_any_other_attachment_is_not_plain(self):
        for att in ({"text": "D1 ok"}, {"is_app_unfurl": True}, {"is_app_unfurl": False, "app_unfurl_url": "https://github.com/o/r"},
                    {"is_app_unfurl": True, "app_unfurl_url": "https://github.com/o/other"},
                    {"from_url": "https://evil.example/x"}, "text"):
            self.assertFalse(self.plain(attachments=[att]), att)
        self.assertFalse(self.plain(attachments=[{"from_url": "https://github.com/o/r"}, {"text": "and this"}]))

    def test_a_link_that_says_one_thing_and_goes_elsewhere_is_not_plain(self):
        self.assertFalse(self.plain("see <https://evil.example/x|docs.example.org>"))
        self.assertTrue(self.plain("see <http://docs.example.org|docs.example.org>"))
        self.assertTrue(self.plain("see <mailto:a@example.com|a@example.com>"))

    def test_edited_means_nothing_on_a_batch_post(self):
        self.assertTrue(self.plain(edited={"user": "B00000002", "ts": "1.2"}))


class ReadThatSaysNo(SlackBase):
    """N4: `ok: false` is Slack answering a read with a refusal, returned as a successful tool call."""

    class Client:
        def __init__(self, res):
            self.res = res

        def call(self, name, args):
            return self.res

        def close(self):
            pass

    def sent_with(self, res):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(decisions, "_chat_clients", lambda cfg: {"chat": self.Client(res)}):
            code = decisions.main(["sent", "--ids", "D1", "--ts", POST_TS], self.ws, CFG, out, err, at=AT + timedelta(seconds=41))
        return code, err.getvalue()

    def test_sent_on_a_refused_read_exits_3_and_stamps_nothing(self):
        self.propose()
        decisions.offer(self.ws, CFG, AT + timedelta(seconds=30))
        code, err = self.sent_with({"ok": False, "error": "ratelimited"})
        self.assertEqual(code, 3)
        self.assertIn("ratelimited", err)
        self.assertEqual(self.doc()["batch"]["announced"], {})
        self.assertIn("offered", self.doc()["batch"])

    def test_ok_true_with_no_such_message_stays_exit_2(self):
        self.propose()
        decisions.offer(self.ws, CFG, AT + timedelta(seconds=30))
        self.assertEqual(self.sent_with({"ok": True, "messages": []})[0], 2)

    def test_a_reply_on_a_refused_read_is_retried_not_recorded(self):
        self.propose()
        text = decisions.offer(self.ws, CFG, AT + timedelta(seconds=30))["text"]
        decisions.record_post(self.ws, CFG, AT + timedelta(seconds=41), ["D1"], POST_TS, fetch=lambda c, ch, t: app_msg(t, text))
        ts = ts_at(60)
        with mock.patch.object(decisions, "_chat_clients", lambda cfg: {"chat": self.Client({"ok": False, "error": "ratelimited"})}):
            state, lines = decisions._reply(self.ws, CFG, AT + timedelta(seconds=70), DM, ts,
                                            lambda c, ch, t: decisions._read_message({"chat": self.Client({"ok": False, "error": "ratelimited"})}, ch, t))
        self.assertEqual(state, "retry")
        self.assertIn("ratelimited", lines[0])


class FinalMinors(SlackBase):
    def test_a_worker_files_only_under_its_own_key(self):
        """Minor 3: the approval instruction would land with the other worker."""
        chief.register_worker(self.ws, AT, "PLAT-2", "Other")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"MOTHER_CHIEF_WORKER": "PLAT-2"}), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(REPLY))):
            code = decisions.main(["propose", "PLAT-1", "--tool", POST, "--recommend", "r", "--stdin"], self.ws, SIGNED,
                                  out, err, at=AT)
        self.assertEqual(code, 1)
        self.assertIn("PLAT-2", err.getvalue())
        self.assertEqual(self.doc()["decisions"], [])

    def test_approved_says_when_no_run_can_start(self):
        """Minor 4: "will make that one call" isn't true over budget or held."""
        self.propose()
        with mock.patch.object(decisions.chief, "refusal", return_value="over today's budget ($5.00 of $5.00)"):
            msg = decisions.answer(self.ws, CFG, AT, 1, "ok", "", {"via": "pane"})
        self.assertIn("approved", msg)
        self.assertIn("over today's budget", msg)
        self.assertNotIn("will make that one call", msg)

    def test_approved_says_when_the_worker_is_held(self):
        self.propose()
        chief.update_worker(self.ws, "PLAT-1", held=True)
        msg = decisions.answer(self.ws, CFG, AT, 1, "ok", "", {"via": "pane"})
        self.assertIn("held", msg)
        self.assertNotIn("will make that one call", msg)

    def test_typed_is_gone(self):
        """Minor 7: ruling F2b dropped the typed path."""
        self.propose()
        self.assertEqual(decisions.VIAS, ("pane",))
        err = io.StringIO()
        self.assertEqual(decisions.main(["answer", "1", "ok", "--via", "typed"], self.ws, CFG, io.StringIO(), err, at=AT), 2)
        self.assertNotIn("typed", decisions.USAGE)
        self.assertNotIn("typed", err.getvalue())
        self.assertIn("still open", decisions.answer(self.ws, CFG, AT, 1, "ok", "", {"via": "typed"}))
        self.assertEqual(self.doc()["decisions"][0]["status"], "pending")

    def test_a_config_problem_is_named_not_blamed_on_the_dm(self):
        """Minor 9: with follow.inbox and chat_dm apart, the reply was in the DM; the config is what's wrong."""
        watch = hub.load_watch(self.ws)
        watch.setdefault("follow", {})["inbox"] = "D00000002"
        hub.save_watch(self.ws, watch)
        self.propose()

        @contextlib.contextmanager
        def session(cfg):
            yield FakeSlack()
        with mock.patch.object(decisions, "_slack_session", session):
            decisions.apply_replies(self.ws, CFG, LATER, [reply_line(SRC_TS, "1 ok")])
        said = " ".join(self.doc()["confirm"])
        self.assertNotIn("it isn't in your own DM", said)
        self.assertIn("D00000002", said)


if __name__ == "__main__":
    unittest.main()
