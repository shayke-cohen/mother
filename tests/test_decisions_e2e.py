"""One decision end to end: the real `mother decide` CLI, the real hooks/guard.py on synthetic hook payloads, and the real
ledger and `settle`. No network and no real `claude`. Each half has its own tests; this is what breaks when a grant's shape,
source, owner or signature handling drifts between them.
Run: python3 -m unittest tests.test_decisions_e2e -v
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import chief  # noqa: E402
import decisions  # noqa: E402
import grants  # noqa: E402
import hub_sources  # noqa: E402

GUARD = os.path.join(ROOT, "hooks", "guard.py")
MOTHER = os.path.join(ROOT, "bin", "mother")
SIG = ":sparkles: *Agnes (AI)* · on behalf of Dana"
POST = "mcp__chat__slack__post_message"
DM, OWNER, TEAM = "D00000001", "U00000001", "C00000001"
CHIEF_SID, WORKER_SID = "chief-0000", "worker-0000"
REPLY = {"channel_id": TEAM, "thread_ts": "1700000000.000100", "text": "We ship Tuesday.\n" + SIG}
CONFIG = {"version": 1, "agent": {"name": "Agnes", "emoji": ":sparkles:", "manager": "Dana Levi", "manager_short": "Dana"},
          "manager": {"email": "d@example.com", "timezone": "Europe/London", "workdays": ["Mon"],
                      "chat_dm": [OWNER, DM]},
          "workspace": {"git_remote": None, "home_machine": None},
          "connectors": {"chat": "slack", "tracker": "jira", "mail": "gmail", "calendar": "google", "code": "github"},
          "teams": [{"slug": "platform", "name": "Platform", "tracker": {"project": "PLAT", "board": 1234},
                     "channels": [{"id": TEAM, "name": "#platform", "mode": "draft"}]}],
          # the switches a home workspace runs with: a worker and the chief must get nothing from them
          "policy": {"autonomy": "functional", "shell_unasked": True, "shell_in_bypass": True, "bypass_passes_asks": True,
                     "auto_without_asking": ["own_dm", "drafts"], "needs_yes": ["any_message_to_others"],
                     "never": ["credentials"]}}

# What a live self-DM stored, read back on 2026-11-05 (ids here are placeholders). Longest first: a family is one name.
SLACK_EMOJI = (("\U0001F468\u200D\U0001F469\u200D\U0001F467", ":man-woman-girl:"), ("\U0001F44D\U0001F3FD", ":+1::skin-tone-4:"),
               ("\U0001F1EE\U0001F1F1", ":flag-il:"), ("\u2705", ":white_check_mark:"), ("\u2728", ":sparkles:"),
               ("\U0001F468", ":man:"), ("\U0001F469", ":woman:"), ("\U0001F467", ":girl:"))
_AUTOLINK = re.compile(r"(https?://[^\s<>|&\"]+)|(?<![\w/.:\-])((?:[a-z0-9\-]+\.)+(?:com|org|net|io)(?:/[^\s<>|&\"]*)?)")


def slack_stores(text):
    """A post's `text` as Slack stores it: shortcode emoji, bare & < > escaped, URLs wrapped and bare domains auto-linked.
    Links already written as links, trailing spaces and zero-width spaces stay."""
    for e, code in SLACK_EMOJI:
        text = text.replace(e, code)
    parts = re.split(r"(<https?:[^>]*>)", text)
    for i in range(0, len(parts), 2):
        t = re.sub(r"&(?!(?:amp|lt|gt);)", "&amp;", parts[i]).replace("<", "&lt;").replace(">", "&gt;")
        parts[i] = _AUTOLINK.sub(lambda m: "<%s>" % m.group(1) if m.group(1) else "<http://%s|%s>" % (m.group(2), m.group(2)), t)
    return "".join(parts)


def rich_text(text):
    return [{"type": "rich_text", "block_id": "b1", "elements": [{"type": "rich_text_section",
                                                                  "elements": [{"type": "text", "text": text}]}]}]


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mother-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws, home = os.path.join(self.tmp, "ws"), os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.ws, ".mother", "chief"))
        os.makedirs(home)
        with open(os.path.join(self.ws, "config.json"), "w") as f:
            json.dump(CONFIG, f)
        with open(os.path.join(self.ws, ".mother", "chief", "chief.json"), "w") as f:
            json.dump({"session_id": CHIEF_SID}, f)
        base = {k: v for k, v in os.environ.items() if not k.startswith("MOTHER_") and k != "CLAUDE_CONFIG_DIR"}
        base.update(HOME=home, MOTHER_WORKSPACE=self.ws, MOTHER_NO_SYNC="1", MOTHER_STATE_DIR=os.path.join(home, ".mother"),
                    MOTHER_CLAUDE_BIN=os.path.join(ROOT, "tests", "fixtures", "fake_claude.py"),
                    FAKE_CLAUDE_LOG=os.path.join(self.tmp, "fake.log"))
        self.env, self.worker_env = dict(base), dict(base, MOTHER_CHIEF_WORKER="PLAT-1")
        self.chief_env = dict(base, MOTHER_CHIEF_ROLE="chief")
        # in this process too: the keys, the state dir and the worker registry must be the temp ones
        p = mock.patch.dict(os.environ, {"HOME": home, "MOTHER_WORKSPACE": self.ws, "MOTHER_NO_SYNC": "1",
                                         "MOTHER_STATE_DIR": base["MOTHER_STATE_DIR"]})
        p.start()
        self.addCleanup(p.stop)
        self.path = Path(self.ws)
        chief.register_worker(self.path, datetime.now(timezone.utc), "PLAT-1", "Plat one")
        self.slack = []  # what Slack shows: every read in this test answers from here

    # the pieces --------------------------------------------------------------------------------------------------

    def guard(self, tool, inp, env, sid):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp, "session_id": sid, "cwd": self.ws,
                   "permission_mode": "bypassPermissions"}
        p = subprocess.run([sys.executable, GUARD], input=json.dumps(payload), capture_output=True, text=True, env=env)
        if not p.stdout.strip():
            return "none"
        return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]

    def worker_call(self, inp, tool=POST):
        return self.guard(tool, inp, self.worker_env, WORKER_SID)

    def mother(self, *args, env=None, stdin=None):
        p = subprocess.run([sys.executable, MOTHER] + list(args), input=stdin, capture_output=True, text=True,
                           env=env or self.env)
        return p.returncode, p.stdout, p.stderr

    def propose(self, inp=None, tool=POST):
        return self.mother("decide", "propose", "PLAT-1", "--tool", tool, "--recommend", "send", "--why", "they asked",
                           "--stdin", env=self.worker_env, stdin=json.dumps(REPLY if inp is None else inp))

    def fetch(self, cfg, channel, ts):
        return next((m for m in self.slack if m["ts"] == ts and channel == DM), None)

    def batch(self):
        code, out, err = self.mother("decide", "batch", "--json", env=self.chief_env)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def sent(self, ids, ts):
        """`mother decide sent`, in this process so its Slack read can be faked from the test's side: production has no
        switch that turns the read off."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(decisions, "_fetch_slack", self.fetch):
            code = decisions.main(["sent", "--ids", ids, "--ts", ts], self.path, CONFIG, out, err)
        return code, err.getvalue()

    def app_post(self, text):
        ts = "%.6f" % time.time()
        # `user` is the manager's own id on an app post: it alone never proves a human typed it
        self.slack.append({"type": "message", "ts": ts, "user": OWNER, "app_id": "A00000001", "bot_id": "B00000001",
                           "bot_profile": {"id": "B00000001", "app_id": "A00000001", "name": "connector"},
                           "text": slack_stores(text), "blocks": rich_text(text)})
        time.sleep(0.002)
        return ts

    def unfurl(self, ts, url="https://github.com/o/r", **att):
        """The preview that lands seconds after a post: an attachment, and `edited`, though nobody edited anything."""
        m = next(x for x in self.slack if x["ts"] == ts)
        m["edited"] = {"user": "B00000002", "ts": "%.6f" % time.time()}
        m["attachments"] = [dict({"color": "24292f", "bot_id": "B00000002", "app_unfurl_url": url, "is_app_unfurl": True,
                                  "app_id": "A00000002", "id": 1, "fallback": "o/r", "text": "repo description",
                                  "title": "o/r"}, **att)]

    def typed(self, text):
        ts = "%.6f" % time.time()
        self.slack.append({"ts": ts, "user": OWNER, "client_msg_id": "c-1", "text": text, "blocks": rich_text(text)})
        time.sleep(0.002)
        return ts

    def post_batch(self):
        got = self.batch()
        ts = self.app_post(got["text"])
        code, err = self.sent(",".join(got["ids"]), ts)
        self.assertEqual(code, 0, err)
        return ts

    def tick_replies(self, *line_ts):
        session = self.session()
        lines = [{"id": "chat:manual:k:%s" % ts, "kind": "manual", "source": "chat", "key": "k",
                  "summary": next(m["text"] for m in self.slack if m["ts"] == ts)} for ts in line_ts]
        with mock.patch.object(decisions, "_slack_session", session):
            decisions.apply_replies(self.path, CONFIG, datetime.now(timezone.utc), lines)

    def session(self):
        slack, fetch = self.slack, self.fetch

        class Reads:
            def __call__(self, cfg, channel, ts):
                return fetch(cfg, channel, ts)

            def history(self, channel, oldest):
                return [m for m in slack if float(m["ts"]) >= float(oldest)]

        @contextlib.contextmanager
        def open_(cfg):
            yield Reads()
        return open_

    def decision(self, i="D1"):
        return next(d for d in decisions.load(self.path)["decisions"] if d["id"] == i)

    def ledger(self):
        with open(os.path.join(self.ws, ".mother", "ledger.json")) as f:
            return json.load(f)["uses"]

    def settle(self):
        """The run is over: its approval left the queue, as a real run takes it."""
        chief.drop_queued(self.path, "PLAT-1", "D1 approved by")
        decisions.settle(self.path, datetime.now(timezone.utc) + timedelta(seconds=1), CONFIG)

    # the flow ----------------------------------------------------------------------------------------------------

    def test_a_pane_answer_lets_that_exact_call_out_once(self):
        code, out, err = self.propose()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["decision"]["id"], "D1")
        self.assertEqual(self.worker_call(REPLY), "deny")                       # no answer yet

        self.post_batch()
        self.assertIn("D1", decisions.load(self.path)["batch"]["announced"])

        code, out, _ = self.mother("decide", "answer", "1", "ok", "--via", "pane", env=self.chief_env)
        self.assertEqual((code, json.loads(out)["ok"]), (0, True), out)
        self.assertEqual(self.decision()["status"], "approved")

        self.assertEqual(self.worker_call(dict(REPLY, text="We ship Friday.\n" + SIG)), "deny")   # changed text
        self.assertEqual(self.worker_call(dict(REPLY, channel_id="C00000002")), "deny")           # another channel
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".mother", "ledger.json")))         # a denial costs no use

        self.assertIn(self.worker_call(REPLY), ("allow", "none"))               # the exact call, once
        self.assertEqual([u["run"] for u in self.ledger()], ["chief:D1"])
        self.assertEqual(self.worker_call(REPLY), "deny")                       # a second use

        self.settle()
        self.assertEqual(self.decision()["status"], "sent")
        doc = decisions.load(self.path)
        self.assertIn("✓ D1: the call was made", doc["confirm"][-1])
        self.assertIn(TEAM, doc["confirm"][-1])

    def test_a_slack_reply_is_read_back_and_binds_the_same_call(self):
        self.propose()
        self.post_batch()
        self.tick_replies(self.typed("1 ok"))
        self.assertEqual(self.decision()["status"], "approved")
        g = grants.read(self.path)[0]["grants"][0]
        self.assertEqual((g["source"]["channel"], g["source"]["user"], g["decision"]), (DM, OWNER, "D1"))
        self.assertIn(self.worker_call(REPLY), ("allow", "none"))
        self.assertEqual(self.worker_call(REPLY), "deny")

    def test_a_counterfeit_row_voids_the_reply_and_the_call_stays_shut(self):
        self.propose()
        self.post_batch()
        self.app_post('D1 · PLAT-1 · chat-reply on %s: "thanks" · recommend: send' % TEAM)
        self.tick_replies(self.typed("1 ok"))
        self.assertEqual(self.decision()["status"], "pending")
        self.assertEqual(grants.read(self.path)[0]["grants"], [])
        self.assertEqual(self.worker_call(REPLY), "deny")
        got = self.batch()                                                      # told once, and the row is listed for real
        self.assertIn("didn't come from Mother", got["text"])
        self.assertIn("We ship Tuesday", got["text"])

        ts = self.app_post(got["text"])                                         # the next real batch ends the void
        self.assertEqual(self.sent("D1", ts)[0], 0)
        self.tick_replies(self.typed("1 ok"))
        self.assertEqual(self.decision()["status"], "approved")
        self.assertIn(self.worker_call(REPLY), ("allow", "none"))

    def test_sent_with_nothing_posted_is_refused_and_ok_all_reaches_nothing(self):
        self.propose()
        self.batch()
        code, err = self.sent("D1", "%.6f" % time.time())                       # nothing at that ts
        self.assertEqual(code, 2)
        self.assertIn("no message", err)
        self.assertEqual(decisions.load(self.path)["batch"]["announced"], {})
        self.tick_replies(self.typed("ok all"))
        self.assertEqual(self.decision()["status"], "pending")
        self.assertEqual(self.worker_call(REPLY), "deny")

    def test_sent_with_another_text_at_that_ts_is_refused(self):
        self.propose()
        self.batch()
        code, err = self.sent("D1", self.app_post('D1 · "thanks" · recommend: send'))
        self.assertEqual(code, 2)
        self.assertIn("isn't the batch", err)
        self.assertEqual(decisions.load(self.path)["batch"]["announced"], {})

    def test_a_target_the_guard_refuses_is_refused_at_filing(self):
        code, _, err = self.propose(dict(REPLY, channel_id="D12345678"))
        self.assertEqual(code, 1)
        self.assertIn("the guard refused the call: target not allowed", err)
        self.assertEqual(decisions.load(self.path)["decisions"], [])

    def test_a_refused_target_closes_with_the_guards_reason(self):
        """Approved, then the channel left config: the guard really does refuse, and the closure says why."""
        self.propose()
        self.mother("decide", "answer", "1", "ok", "--via", "pane", env=self.chief_env)
        cfg = dict(CONFIG, teams=[{"slug": "platform", "name": "Platform", "tracker": {"project": "PLAT", "board": 1234},
                                   "channels": []}])
        with open(os.path.join(self.ws, "config.json"), "w") as f:
            json.dump(cfg, f)
        self.assertEqual(self.worker_call(REPLY), "deny")
        chief.drop_queued(self.path, "PLAT-1", "D1 approved by")
        decisions.settle(self.path, datetime.now(timezone.utc) + timedelta(seconds=1), cfg)
        self.assertEqual(self.decision()["status"], "failed")
        self.assertIn("the guard refused the call: target not allowed", decisions.load(self.path)["confirm"][-1])

    def test_only_the_offer_reaches_the_self_dm(self):
        self.propose()
        text = self.batch()["text"]
        forged = text.replace("We ship Tuesday", "We ship Friday")
        self.assertEqual(self.worker_call({"channel_id": DM, "text": text}), "deny")
        self.assertEqual(self.guard(POST, {"channel_id": DM, "text": forged}, self.chief_env, CHIEF_SID), "deny")
        self.assertEqual(self.guard(POST, {"channel_id": DM, "text": text}, self.chief_env, CHIEF_SID), "allow")
        self.assertEqual(self.guard(POST, {"channel_id": DM, "text": forged}, self.env, "someone"), "allow")  # normal: green
        self.assertEqual(self.sent("D1", self.app_post(text))[0], 0)
        self.assertEqual(self.guard(POST, {"channel_id": DM, "text": text}, self.chief_env, CHIEF_SID), "deny")  # used up

    # what Slack really stores ----------------------------------------------------------------------------------------

    EVERYTHING = dict(REPLY, text="Ship \u2705 \U0001F1EE\U0001F1F1 \U0001F468\u200D\U0001F469\u200D\U0001F467 \U0001F44D\U0001F3FD "
                                  "docs.example.org a < b & c > d @someone\n" + SIG)
    LINKED = dict(REPLY, text="See https://github.com/o/r\n" + SIG)

    def test_the_fake_stores_what_slack_stores(self):
        for posted, stored in (("\u2705", ":white_check_mark:"), (":white_check_mark:", ":white_check_mark:"),
                               ("\U0001F1EE\U0001F1F1", ":flag-il:"), (":notreal:", ":notreal:"),
                               ("\U0001F468\u200D\U0001F469\u200D\U0001F467", ":man-woman-girl:"),
                               ("example.com", "<http://example.com|example.com>"), ("<https://x/y|the plugin>", "<https://x/y|the plugin>"),
                               ("https://x/y", "<https://x/y>"), ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
                               ("@\u200bchannel", "@\u200bchannel"), ("<\u200b!channel>", "&lt;\u200b!channel&gt;"),
                               ("end  ", "end  "), ("#general and @someone", "#general and @someone")):
            self.assertEqual(slack_stores(posted), stored)

    def test_a_batch_survives_what_slack_does_to_its_text(self):
        self.assertEqual(self.propose(self.EVERYTHING)[0], 0)
        self.post_batch()                                                       # `sent` exits 0 on what Slack stored
        self.assertIn("D1", decisions.load(self.path)["batch"]["announced"])
        self.tick_replies(self.typed("1 ok"))                                   # and the scan doesn't call it a counterfeit
        self.assertEqual(self.decision()["status"], "approved")
        self.assertEqual(decisions.load(self.path)["void"], [])
        self.assertIn(self.worker_call(self.EVERYTHING), ("allow", "none"))

    def test_a_preview_landing_after_sent_voids_nothing(self):
        self.propose(self.LINKED)
        ts = self.post_batch()
        self.unfurl(ts)
        self.tick_replies(self.typed("1 ok"))
        self.assertEqual(decisions.load(self.path)["void"], [])
        self.assertEqual(self.decision()["status"], "approved")
        self.assertNotIn("didn't come from Mother", " ".join(decisions.load(self.path)["confirm"]))

    def test_a_preview_landing_before_sent_is_still_the_batch(self):
        self.propose(self.LINKED)
        got = self.batch()
        ts = self.app_post(got["text"])
        self.unfurl(ts)
        self.assertEqual(self.sent("D1", ts)[0], 0)

    def test_an_attachment_that_is_no_preview_of_its_links_is_refused(self):
        self.propose(self.LINKED)
        got = self.batch()
        for url, extra in (("https://github.com/o/other", {}), ("https://github.com/o/r", {"is_app_unfurl": False})):
            ts = self.app_post(got["text"])
            self.unfurl(ts, url, **extra)
            self.assertEqual(self.sent("D1", ts)[0], 2)
        self.assertEqual(decisions.load(self.path)["batch"]["announced"], {})

    def test_the_guard_wants_the_offer_verbatim_and_lets_the_flags_through(self):
        """The chief posts `batch --json` as printed, before Slack changes anything: exact is right on the way out, and a
        canonical compare there would let a doctored post through."""
        self.propose(self.EVERYTHING)
        text = self.batch()["text"]
        call = {"channel_id": DM, "text": text, "unfurl_links": False, "unfurl_media": False}
        self.assertEqual(self.guard(POST, call, self.chief_env, CHIEF_SID), "allow")
        self.assertEqual(self.guard(POST, dict(call, text=slack_stores(text)), self.chief_env, CHIEF_SID), "deny")

    def test_an_app_post_as_the_manager_is_never_the_managers_reply(self):
        """The whole way: the watcher's human filter drops it, and a line naming it anyway applies nothing."""
        self.propose()
        self.post_batch()
        ts = self.app_post("1 ok")
        self.assertEqual(next(m for m in self.slack if m["ts"] == ts)["user"], OWNER)

        slack = self.slack

        class Client:
            def call(self, name, args):
                if name.endswith("get_channel_history"):
                    return {"ok": True, "messages": [m for m in slack if float(m["ts"]) > float(args.get("oldest", 0))]}
                return {"ok": True, "messages": {"matches": []}}
        at = datetime.now(timezone.utc)
        got = hub_sources.ChatSlack.poll(Client(), {"ts": "%.6f" % (at.timestamp() - 60), "threads": {}},
                                         {"channels": [], "inbox": DM, "discovery": "poll"},
                                         {"email": "d@example.com", "chat": OWNER, "code": "dana"}, at)
        self.assertEqual([l for l in got["lines"] if l.get("kind") == "manual"], [])

        self.tick_replies(ts)
        self.assertEqual(self.decision()["status"], "pending")
        self.assertEqual(grants.read(self.path)[0]["grants"], [])
        self.assertEqual(self.worker_call(REPLY), "deny")

    def test_tick_names_the_offers_content_without_making_an_offer(self):
        def tick():
            code, out, err = self.mother("chief", "tick", "--json", env=self.chief_env)
            self.assertEqual(code, 0, err)
            return json.loads(out)
        self.assertIsNone(tick()["batch_key"])
        self.propose()
        first = tick()
        self.assertTrue(first["batch"] and first["batch_key"])
        self.assertNotIn("offered", decisions.load(self.path)["batch"])
        self.assertEqual(tick()["batch_key"], first["batch_key"])
        self.propose(dict(REPLY, text="Other words.\n" + SIG))
        self.assertNotEqual(tick()["batch_key"], first["batch_key"])


if __name__ == "__main__":
    unittest.main()
