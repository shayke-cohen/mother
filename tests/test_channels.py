"""Tests for lib/channels.py (`mother channels`) — reply modes, end to end with the real guard.

A fake `claude` (MOTHER_CLAUDE_BIN) prints a canned stream-json result and records its argv. Every post the
skill would make is checked against hooks/guard.py, so "the guard lets exactly this reply through" is tested,
not assumed. Nothing leaves the machine. Run: python3 -m unittest tests.test_channels -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "mother")
GUARD = os.path.join(ROOT, "hooks", "guard.py")

ME, SAM, OUT = "U00000000", "U00000001", "U00000099"   # manager, teammate, someone not on the team
TEAM, SUPPORT, AUTO, EXT = "C00000001", "C00000002", "C00000004", "C00000009"
T0 = float(int(time.time()) - 10_000)        # "now" in the tests is real time, as the guard sees it
SIG = ":sparkles: *Agnes (AI)* · on behalf of Dana"
POST = "mcp__chat__slack__reply_to_thread"

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(),
                        "env_no_sync": os.environ.get("MOTHER_NO_SYNC"),
                        "env_role": os.environ.get("MOTHER_CHANNEL_ROLE"),
                        "env_servers": os.environ.get("MOTHER_CHANNEL_SERVERS")}) + "\n")
out = os.environ.get("FAKE_OUT", "REPLY:\nok")
print(json.dumps({"type": "system", "subtype": "init", "session_id": "x"}))
print(json.dumps({"type": "result", "subtype": "success", "result": out}))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


def ts(sec):
    return "%.6f" % (T0 + sec)


def msg(sec, user, text, thread=None, **kw):
    m = {"ts": ts(sec), "user": user, "text": text}
    if thread:
        m["thread_ts"] = thread
    m.update(kw)
    return m


def config():
    return {
        "version": 1,
        "agent": {"name": "Agnes", "emoji": ":sparkles:", "manager": "Dana Levi", "manager_short": "Dana"},
        "manager": {"email": "dana@example.com", "timezone": "UTC", "chat_dm": ME},
        "workspace": {"git_remote": None, "home_machine": None},
        "connectors": {"chat": "slack", "tracker": "jira", "mail": "gmail", "calendar": "google"},
        "teams": [{"slug": "platform", "name": "Platform",
                   "people": [{"name": "Sam Roe", "chat": SAM}],
                   "channels": [
                       {"id": TEAM, "name": "#platform", "mode": "mention", "trigger": "team"},
                       {"id": SUPPORT, "name": "#platform-support", "mode": "draft"},
                       {"id": AUTO, "name": "#platform-help", "mode": "auto", "auto_topics": ["deploys"]},
                       {"id": EXT, "name": "#partner", "mode": "mention", "trigger": "manager", "external": True},
                   ]}],
        "routines": {},
        "policy": {"auto_without_asking": ["own_dm", "drafts", "own_calendar_no_guests"],
                   "needs_yes": ["any_message_to_others"], "never": ["credentials", "deletions", "payments"]},
    }


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mother-ch-")
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(os.path.join(self.ws, ".mother"))
        self.cfg = config()
        self.save_cfg()
        self.fake = os.path.join(self.tmp, "claude")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        os.chmod(self.fake, 0o755)
        self.log = os.path.join(self.tmp, "fake.log")
        self.now = T0 + 10_000.5
        self.out = "REPLY:\nThe deploy is green. Source: PLAT-12"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def save_cfg(self):
        with open(os.path.join(self.ws, "config.json"), "w") as f:
            json.dump(self.cfg, f)

    def env(self):
        e = dict(os.environ, MOTHER_WORKSPACE=self.ws, MOTHER_NOW=str(self.now), MOTHER_CLAUDE_BIN=self.fake,
                 FAKE_LOG=self.log, FAKE_OUT=self.out, HOME=self.tmp)
        return e

    def ch(self, *args, stdin=None, ok=True):
        p = subprocess.run([sys.executable, CLI, "channels", *args], input=json.dumps(stdin) if stdin is not None
                           else None, capture_output=True, text=True, env=self.env())
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        return p

    def triage(self, channel, messages=(), threads=None):
        return self.ch("triage", stdin={"channel": channel, "messages": list(messages), "threads": threads or {}})

    def init(self, channel):
        r = self.triage(channel, [])
        self.assertTrue(r.get("initialised"))

    def guard(self, channel, thread, text):
        payload = {"hook_event_name": "PreToolUse", "tool_name": POST, "session_id": "t", "cwd": self.tmp,
                   "tool_input": {"channel_id": channel, "thread_ts": thread, "text": text}}
        p = subprocess.run([sys.executable, GUARD], input=json.dumps(payload), capture_output=True, text=True,
                           env=dict(os.environ, MOTHER_WORKSPACE=self.ws, HOME=self.tmp))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] if p.stdout.strip() else "none"

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f]

    def answers(self, r):
        return [a for a in r["actions"] if a["kind"] == "answer"]


class TestTriage(Case):
    def test_first_look_starts_from_now_not_the_backlog(self):
        r = self.triage(TEAM, [msg(-500, SAM, "Agnes, is the deploy green?")])
        self.assertEqual(r["actions"], [])
        self.assertTrue(r["initialised"])

    def test_mention_team_trigger_rules(self):
        self.init(TEAM)
        r = self.triage(TEAM, [
            msg(10_001, SAM, "Agnes, is the deploy green?"),            # teammate, addressed -> yes
            msg(10_002, OUT, "@Agnes what's the roadmap?"),              # not on the team -> no
            msg(10_003, SAM, "is the deploy green?"),                   # not addressed -> no
            msg(10_004, SAM, "Agnes, hi", bot_id="B1"),                 # a bot -> no
            msg(10_005, ME, SIG + "\nearlier answer"),                  # our own post -> no
            msg(10_006, SAM, "I told Agnes about it yesterday"),        # name, but not addressed -> no
        ])
        a = self.answers(r)
        self.assertEqual([x["source_ts"] for x in a], [ts(10_001)])
        self.assertEqual(a[0]["role"], "team")
        self.assertEqual(a[0]["text"], "is the deploy green?")
        # the same poll again creates nothing new
        self.assertEqual(self.triage(TEAM, [msg(10_001, SAM, "Agnes, is the deploy green?")])["actions"], [])

    def test_trigger_anyone_and_manager(self):
        self.cfg["teams"][0]["channels"][0]["trigger"] = "anyone"
        self.save_cfg()
        self.init(TEAM)
        self.assertEqual(len(self.answers(self.triage(TEAM, [msg(10_001, OUT, "Agnes: status?")]))), 1)
        self.cfg["teams"][0]["channels"][0]["trigger"] = "manager"
        self.save_cfg()
        r = self.triage(TEAM, [msg(10_002, SAM, "Agnes, status?"), msg(10_003, ME, "Agnes, status?")])
        self.assertEqual([x["user"] for x in self.answers(r)], [ME])

    def test_thread_continuation(self):
        self.init(TEAM)
        root = ts(10_001)
        self.triage(TEAM, [msg(10_001, SAM, "Agnes, is the deploy green?")])
        th = {root: [msg(10_001, SAM, "Agnes, is the deploy green?"),
                     msg(10_010, ME, SIG + "\nyes", thread=root),
                     msg(10_011, SAM, "and staging?", thread=root),          # thread owner, no name -> yes
                     msg(10_012, OUT, "what about prod?", thread=root)]}     # someone else, no name -> no
        r = self.triage(TEAM, [], th)
        # the first answer is still pending (not started), so Sam's follow-up coalesces into it
        self.assertEqual(r["actions"], [])
        plan = self.ch("plan")
        self.assertEqual(len(plan["pending"]), 1)
        self.assertIn("and staging?", plan["pending"][0]["text"])
        self.assertEqual(self.triage(TEAM, [], th)["actions"], [])           # nothing re-queued

    def test_reply_in_someone_elses_thread_found_by_search(self):
        self.init(TEAM)
        root = ts(10_000)
        hit = msg(10_005, SAM, "@Agnes can you check this?", thread=root)
        r = self.triage(TEAM, [hit], {root: [msg(10_000, OUT, "prod is slow"), hit]})
        a = self.answers(r)
        self.assertEqual((a[0]["thread"], a[0]["source_ts"]), (root, ts(10_005)))
        self.assertEqual([c["text"] for c in a[0]["context"]], ["prod is slow", "@Agnes can you check this?"])
        # next tick the thread is followed; the same reply must not be queued again
        self.assertEqual(self.triage(TEAM, [], {root: [msg(10_000, OUT, "prod is slow"), hit]})["actions"], [])

    def test_draft_channel_takes_questions_not_chatter(self):
        self.init(SUPPORT)
        r = self.triage(SUPPORT, [msg(10_001, OUT, "how do I rotate the key?"), msg(10_002, OUT, "thanks all"),
                                  msg(10_003, ME, "anyone seen this?")])
        self.assertEqual([x["source_ts"] for x in self.answers(r)], [ts(10_001)])

    def test_quiet_hours_hold_then_release(self):
        self.cfg["manager"]["quiet_hours"] = "00:00-23:59"
        self.save_cfg()
        self.init(TEAM)
        m = msg(10_001, SAM, "Agnes, status?")
        r = self.triage(TEAM, [m])
        self.assertEqual((r["actions"], r["held"]), ([], "quiet hours"))
        self.cfg["manager"].pop("quiet_hours")
        self.save_cfg()
        self.assertEqual(len(self.answers(self.triage(TEAM, [m]))), 1)       # held, not dropped

    def test_rate_limit_holds(self):
        self.cfg["teams"][0]["channels"][0]["max_replies_per_hour"] = 1
        self.save_cfg()
        self.init(TEAM)
        r = self.triage(TEAM, [msg(10_001, SAM, "Agnes, one?"), msg(10_002, SAM, "Agnes, two?")])
        self.assertEqual(len(self.answers(r)), 1)
        self.assertEqual(r["held"], "rate limit")

    def test_stop_pause_and_who_may_resume(self):
        self.init(TEAM)
        r = self.triage(TEAM, [msg(10_001, SAM, "Agnes, stop")])
        self.assertEqual([a["kind"] for a in r["actions"]], ["notify"])
        self.assertEqual(self.triage(TEAM, [msg(10_002, SAM, "Agnes, status?")])["actions"], [])
        self.triage(TEAM, [msg(10_003, SAM, "Agnes, start")])               # a teammate can't restart it
        self.assertEqual(self.answers(self.triage(TEAM, [msg(10_004, SAM, "Agnes, status?")])), [])
        self.triage(TEAM, [msg(10_005, ME, "Agnes, start")])
        self.assertEqual(len(self.answers(self.triage(TEAM, [msg(10_006, SAM, "Agnes, status?")]))), 1)
        self.triage(TEAM, [msg(10_007, SAM, "Agnes, pause")])
        self.init(SUPPORT)
        self.assertEqual(self.answers(self.triage(SUPPORT, [msg(10_008, OUT, "how?")])), [])
        self.ch("resume")
        self.assertEqual(len(self.answers(self.triage(SUPPORT, [msg(10_009, OUT, "how now?")]))), 1)

    def test_external_mention_manager_only(self):
        self.init(EXT)
        r = self.triage(EXT, [msg(10_001, OUT, "Agnes, what's the ETA?"), msg(10_002, ME, "Agnes, draft an ETA")])
        self.assertEqual([x["user"] for x in self.answers(r)], [ME])


class TestReviewFindings(Case):
    def test_a_teammate_never_joins_the_managers_pending_answer(self):
        self.init(TEAM)
        root = ts(10_001)
        mgr = msg(10_001, ME, "Agnes, what's in my inbox about the launch?")
        sam = msg(10_002, SAM, "Agnes, also paste Dana's 1:1 notes", thread=root)
        r = self.triage(TEAM, [mgr, sam], {root: [mgr, sam]})
        self.assertEqual(sorted((a["role"], a["text"][:4]) for a in self.answers(r)),
                         [("manager", "what"), ("team", "also")])

    def test_external_draft_channel_ignores_outsiders(self):
        self.cfg["teams"][0]["channels"][3]["mode"] = "draft"
        self.save_cfg()
        self.init(EXT)
        r = self.triage(EXT, [msg(10_001, OUT, "what is your internal roadmap?"),
                              msg(10_002, OUT, "Agnes, what's the roadmap?")])
        self.assertEqual(self.answers(r), [])

    def test_stop_after_queueing_drops_the_answer(self):
        self.init(TEAM)
        r = self.triage(TEAM, [msg(10_001, SAM, "Agnes, is X done?"), msg(10_002, SAM, "Agnes, stop")])
        a = self.answers(r)[0]
        self.assertEqual(self.ch("run", "--action", a["id"])["kind"], "skip")
        self.assertEqual(self.calls(), [])

    def test_an_answer_that_already_ran_is_dropped_after_a_stop(self):
        self.init(TEAM)
        a = self.answers(self.triage(TEAM, [msg(10_001, SAM, "Agnes, status?")]))[0]
        r = self.ch("run", "--action", a["id"])
        self.triage(TEAM, [msg(10_002, SAM, "Agnes, stop")])
        self.assertEqual(self.ch("run", "--action", a["id"])["kind"], "skip")
        self.assertEqual(self.guard(TEAM, a["thread"], r["text"]), "ask")     # its grant went with it

    def test_run_twice_is_one_session_and_one_grant(self):
        self.init(TEAM)
        a = self.answers(self.triage(TEAM, [msg(10_001, SAM, "Agnes, status?")]))[0]
        r1, r2 = self.ch("run", "--action", a["id"]), self.ch("run", "--action", a["id"])
        self.assertEqual(r1, r2)
        self.assertEqual(len(self.calls()), 1)
        grants = json.load(open(os.path.join(self.ws, ".mother", "grants.json")))["grants"]
        self.assertEqual(len(grants), 1)

    def test_failed_post_revokes_its_grant(self):
        self.init(TEAM)
        a = self.answers(self.triage(TEAM, [msg(10_001, SAM, "Agnes, status?")]))[0]
        r = self.ch("run", "--action", a["id"])
        self.ch("done", "--action", a["id"], "--error", "connector timeout")
        self.assertEqual(self.guard(TEAM, a["thread"], r["text"]), "ask")

    def test_approval_grant_is_bound_to_the_drafts_thread_and_needs_a_plain_yes(self):
        self.init(SUPPORT)
        a = self.answers(self.triage(SUPPORT, [msg(10_001, OUT, "how do I rotate the key?")]))[0]
        d = self.ch("run", "--action", a["id"])["draft"]
        self.ch("done", "--action", a["id"], "--dm-channel", "D1", "--dm-ts", ts(10_002))
        self.assertEqual(self.ch("approvals", stdin={"drafts": {d: [msg(10_003, ME, "yes but drop line 2")]}})
                         ["actions"], [])
        acts = self.ch("approvals", stdin={"drafts": {d: [msg(10_004, ME, "yes!")]}})["actions"]
        self.assertEqual([x["kind"] for x in acts], ["send"])
        self.assertEqual(self.guard(SUPPORT, ts(9_000), acts[0]["text"]), "ask")
        self.assertEqual(self.guard(SUPPORT, a["thread"], acts[0]["text"]), "allow")

    def test_start_from_the_cli_drops_the_backlog(self):
        self.init(TEAM)
        self.ch("stop", "--channel", TEAM)
        self.now += 60
        self.ch("start", "--channel", TEAM)
        r = self.triage(TEAM, [msg(10_010, SAM, "Agnes, old question?")])
        self.assertEqual(self.answers(r), [])

    def test_skin_tone_thumbs_down_and_quoted_signature(self):
        self.init(AUTO)
        self.out = "TOPIC: deploys\nREPLY:\nGreen."
        a = self.answers(self.triage(AUTO, [msg(10_001, OUT, "deploy done?")]))[0]
        r = self.ch("run", "--action", a["id"])
        self.ch("done", "--action", a["id"], "--posted-ts", ts(10_005))
        th = {a["thread"]: [msg(10_005, ME, r["text"], thread=a["thread"],
                                reactions=[{"name": "-1::skin-tone-4", "users": [OUT]}])]}
        self.assertEqual([x["kind"] for x in self.triage(AUTO, [], th)["actions"]], ["notify"])
        r = self.triage(AUTO, [msg(10_020, SAM, "Agnes, stop — Agnes (AI) keeps answering")])
        self.assertEqual([x["kind"] for x in r["actions"]], ["notify"])          # a human, not our own post

    def test_broadcast_pings_are_stripped(self):
        self.init(TEAM)
        self.out = "REPLY:\n<!here> deploy is green @channel <!subteam^S1|@oncall>"
        a = self.answers(self.triage(TEAM, [msg(10_001, SAM, "Agnes, status?")]))[0]
        text = self.ch("run", "--action", a["id"])["text"]
        for tok in ("<!here>", "@channel", "<!subteam"):
            self.assertNotIn(tok, text)
        self.assertIn("deploy is green", text)


class TestRun(Case):
    def queue(self, channel=TEAM, user=SAM, text="Agnes, is the deploy green?", sec=10_001):
        self.init(channel)
        a = self.answers(self.triage(channel, [msg(sec, user, text)]))
        self.assertEqual(len(a), 1)
        return a[0]

    def test_answer_gets_a_one_thread_grant_the_guard_honours(self):
        a = self.queue()
        r = self.ch("run", "--action", a["id"])
        self.assertEqual(r["kind"], "answer")
        self.assertTrue(r["text"].startswith(SIG + "\n"))
        self.assertIn("PLAT-12", r["text"])
        self.assertEqual(self.guard(TEAM, ts(9_999), r["text"]), "ask")    # another thread
        self.assertEqual(self.guard(TEAM, a["thread"], "unsigned"), "ask")
        self.assertEqual(self.guard(TEAM, a["thread"], r["text"]), "allow")
        self.assertEqual(self.guard(TEAM, a["thread"], r["text"]), "ask")   # once
        self.ch("done", "--action", a["id"], "--posted-ts", ts(10_050))
        self.assertEqual(self.ch("plan")["pending"], [])

    def test_teammate_session_is_read_only_and_private_data_blocked(self):
        with open(os.path.join(self.ws, "capabilities.json"), "w") as f:  # doctor's shape: keyed by vendor
            json.dump({"connectors": {"slack": {"server": "mcp__chat__"}, "jira": {"server": "tracker"},
                                      "gmail": {"server": "mcp__mail__"}, "google": {"server": "gcal"}},
                       "servers": {"chat.search": "mcp__search__"}}, f)
        self.ch("run", "--action", self.queue()["id"])
        argv = self.calls()[-1]["argv"]
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        self.assertEqual(allowed, ["Read", "Grep", "Glob", "mcp__tracker"])     # no chat, mail or calendar
        for t in ("Write", "Edit", "Bash", "Read(./people/**)", "Read(./memory/**)", "Read(./todos/**)",
                  "mcp__chat", "mcp__search", "mcp__mail", "mcp__gcal"):
            self.assertIn(t, denied)
        self.assertEqual(self.calls()[-1]["env_servers"], "mcp__tracker")

    def test_shared_gateway_opens_only_the_tracker_segment(self):
        with open(os.path.join(self.ws, "capabilities.json"), "w") as f:
            json.dump({"connectors": {"slack": {"server": "mcp__gw__slack__"}, "jira": {"server": "mcp__gw__jira__"},
                                      "gmail": {"server": "mcp__gw__gmail__"}}}, f)
        self.ch("run", "--action", self.queue()["id"])
        c = self.calls()[-1]
        argv = c["argv"]
        self.assertEqual(c["env_servers"], "mcp__gw__jira")
        self.assertEqual(argv[argv.index("--allowedTools") + 1].split(","), ["Read", "Grep", "Glob"])
        with open(os.path.join(self.ws, "capabilities.json"), "w") as f:   # one server, no product segment
            json.dump({"connectors": {"slack": {"server": "mcp__gw__"}, "jira": {"server": "mcp__gw__"}}}, f)
        b = self.answers(self.triage(TEAM, [msg(10_200, SAM, "Agnes, again?")]))[0]
        self.ch("run", "--action", b["id"])
        self.assertEqual(self.calls()[-1]["env_servers"], "")
        self.assertEqual(self.calls()[-1]["env_no_sync"], "1")
        self.assertIn("Someone other than Dana Levi asked", self.calls()[-1]["stdin"])

    def test_manager_session_may_write_the_workspace(self):
        self.ch("run", "--action", self.queue(user=ME)["id"])
        argv = self.calls()[-1]["argv"]
        self.assertIn("Write", argv[argv.index("--allowedTools") + 1].split(","))
        self.assertNotIn("Write", argv[argv.index("--disallowedTools") + 1].split(","))

    def test_skip_and_missing_marker(self):
        self.out = "SKIP: needs Dana's calendar"
        r = self.ch("run", "--action", self.queue()["id"])
        self.assertEqual((r["kind"], r["why"]), ("skip", "needs Dana's calendar"))
        self.out = "I think it's fine"
        a = self.answers(self.triage(TEAM, [msg(10_100, SAM, "Agnes, and prod?")]))[0]
        r = self.ch("run", "--action", a["id"])
        self.assertEqual(r["kind"], "draft")                                 # no marker -> to the manager

    def test_session_error_keeps_the_action(self):
        a = self.queue()
        self.out = ""
        os.environ["FAKE_EXIT"] = "3"
        try:
            r = self.ch("run", "--action", a["id"])
        finally:
            os.environ.pop("FAKE_EXIT")
        self.assertEqual(r["kind"], "error")
        self.assertEqual([p["id"] for p in self.ch("plan")["pending"]], [a["id"]])
        os.environ["FAKE_EXIT"] = "3"
        try:
            self.ch("run", "--action", a["id"])
            r = self.ch("run", "--action", a["id"])
        finally:
            os.environ.pop("FAKE_EXIT")
        self.assertTrue(r["gave_up"])
        self.assertEqual([p["kind"] for p in self.ch("plan")["pending"]], ["notify"])

    def test_external_answers_become_drafts(self):
        r = self.ch("run", "--action", self.queue(channel=EXT, user=ME, text="Agnes, draft an ETA")["id"])
        self.assertEqual((r["kind"], r["why"]), ("draft", "external channel"))

    def test_resume_uses_the_threads_session(self):
        a = self.queue()
        r1 = self.ch("run", "--action", a["id"])
        self.ch("done", "--action", a["id"], "--posted-ts", ts(10_050))
        root = a["thread"]
        b = self.answers(self.triage(TEAM, [], {root: [msg(10_001, SAM, "Agnes, is the deploy green?"),
                                                       msg(10_060, SAM, "and staging?", thread=root)]}))[0]
        self.ch("run", "--action", b["id"])
        argv1, argv2 = self.calls()[0]["argv"], self.calls()[1]["argv"]
        self.assertEqual(argv1[argv1.index("--session-id") + 1], argv2[argv2.index("--resume") + 1])
        self.assertEqual(r1["session"], argv2[argv2.index("--resume") + 1])


class TestDrafts(Case):
    def draft(self, sec=10_001, text="how do I rotate the key?", channel=SUPPORT):
        sp = os.path.join(self.ws, ".mother", "channels.json")
        if not os.path.exists(sp) or channel not in json.load(open(sp))["cursors"]:
            self.init(channel)
        a = self.answers(self.triage(channel, [msg(sec, OUT, text)]))[0]
        r = self.ch("run", "--action", a["id"])
        self.assertEqual(r["kind"], "draft", r)
        self.ch("done", "--action", a["id"], "--dm-channel", "D1", "--dm-ts", ts(sec + 1))
        return r["draft"], a

    def approve(self, d_id, sec, text):
        return self.ch("approvals", stdin={"drafts": {d_id: [msg(sec, ME, text, thread=ts(sec - 1))]}})["actions"]

    def test_send_unedited_then_edited(self):
        d_id, a = self.draft()
        self.assertEqual([d["id"] for d in self.ch("plan")["drafts"]], [d_id])
        acts = self.approve(d_id, 10_005, "send")
        self.assertEqual([x["kind"] for x in acts], ["send"])
        self.assertEqual(self.guard(SUPPORT, a["thread"], acts[0]["text"]), "allow")
        self.ch("done", "--action", acts[0]["id"], "--posted-ts", ts(10_006))
        d2, a2 = self.draft(sec=10_100, text="and the other key?")
        acts = self.approve(d2, 10_105, "send: Use the vault CLI, see the runbook.")
        self.assertTrue(acts[0]["edited"])
        self.assertEqual(acts[0]["text"], SIG + "\nUse the vault CLI, see the runbook.")
        self.assertEqual(self.ch("status")["streaks"], {SUPPORT + "|_": 0})

    def test_only_the_manager_approves(self):
        d_id, _ = self.draft()
        acts = self.ch("approvals", stdin={"drafts": {d_id: [msg(10_005, SAM, "send"),
                                                              msg(10_006, ME, SIG + "\nsend")]}})["actions"]
        self.assertEqual(acts, [])
        self.assertEqual(self.approve(d_id, 10_007, "no"), [])
        self.assertEqual(self.ch("plan")["drafts"], [])

    def test_earned_autonomy_proposal_after_ten_unedited(self):
        self.cfg["teams"][0]["channels"][1]["max_replies_per_hour"] = 50   # ten sends in one simulated hour
        self.save_cfg()
        self.out = "TOPIC: key-rotation\nREPLY:\nUse the vault CLI."
        kinds = []
        for i in range(10):
            d_id, _ = self.draft(sec=10_001 + i * 100, text="key %d?" % i)
            acts = self.approve(d_id, 10_001 + i * 100 + 5, "send")
            kinds.append([x["kind"] for x in acts])
            for x in acts:
                self.ch("done", "--action", x["id"], *(["--posted-ts", ts(20_000 + i)] if x["kind"] == "send" else []))
        self.assertEqual(kinds[:9], [["send"]] * 9)
        self.assertEqual(kinds[9], ["send", "notify"])
        d_id, _ = self.draft(sec=12_000, text="key 11?")
        self.assertEqual([x["kind"] for x in self.approve(d_id, 12_005, "send")], ["send"])   # proposed once


class TestAuto(Case):
    def test_auto_topics_and_thumbs_down_demotes(self):
        self.init(AUTO)
        self.out = "TOPIC: deploys\nREPLY:\nGreen since 09:00."
        a = self.answers(self.triage(AUTO, [msg(10_001, OUT, "is the deploy done?")]))[0]
        r = self.ch("run", "--action", a["id"])
        self.assertEqual((r["kind"], r["topic"]), ("answer", "deploys"))
        self.assertEqual(self.guard(AUTO, a["thread"], r["text"]), "allow")    # auto: anyone may trigger
        self.ch("done", "--action", a["id"], "--posted-ts", ts(10_010))
        self.out = "TOPIC: billing\nREPLY:\nAsk finance."
        b = self.answers(self.triage(AUTO, [msg(10_020, OUT, "who owns billing?")]))[0]
        self.assertEqual(self.ch("run", "--action", b["id"])["kind"], "draft")  # not an auto topic
        root = a["thread"]
        thread = {root: [msg(10_001, OUT, "is the deploy done?"),
                         msg(10_010, ME, r["text"], thread=root, reactions=[{"name": "-1", "users": [OUT]}])]}
        acts = self.triage(AUTO, [], thread)["actions"]
        self.assertEqual([x["kind"] for x in acts], ["notify"])
        self.assertIn("deploys", acts[0]["text"])
        self.out = "TOPIC: deploys\nREPLY:\nStill green."
        c = self.answers(self.triage(AUTO, [msg(10_030, OUT, "deploy ok now?")]))[0]
        r = self.ch("run", "--action", c["id"])
        self.assertEqual(r["kind"], "draft")                                  # demoted back to draft
        self.assertIn("demoted", r["why"])
        self.assertEqual(self.triage(AUTO, [], {root: thread[root] + [msg(10_040, OUT, "still green?", thread=root)]})
                         ["actions"], [])                                     # muted thread stays quiet


class TestCli(Case):
    def test_validate_new_channel_keys(self):
        self.cfg["teams"][0]["channels"][2]["auto_topics"] = ["Deploys!"]
        self.cfg["teams"][0]["channels"][0]["max_replies_per_hour"] = 0
        self.cfg["teams"][0]["channels"][0]["quiet_hours"] = "7pm"
        self.save_cfg()
        p = subprocess.run([sys.executable, CLI, "status"], capture_output=True, text=True, env=self.env())
        self.assertNotEqual(p.returncode, 0)
        for k in ("auto_topics", "max_replies_per_hour", "quiet_hours"):
            self.assertIn(k, p.stdout + p.stderr)

    def test_sync_skips_in_channel_sessions(self):
        p = subprocess.run([sys.executable, CLI, "sync"], capture_output=True, text=True,
                           env=dict(self.env(), MOTHER_NO_SYNC="1"))
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))

    def test_mute_cli_and_prune(self):
        self.init(TEAM)
        a = self.answers(self.triage(TEAM, [msg(10_001, SAM, "Agnes, status?")]))[0]
        self.ch("mute", "--channel", TEAM, "--thread", a["thread"])
        self.assertEqual(self.ch("plan")["threads"], [])
        self.ch("unmute", "--channel", TEAM, "--thread", a["thread"])
        self.now += 3 * 86400
        self.assertEqual(self.ch("prune")["closed_threads"], 1)


if __name__ == "__main__":
    unittest.main()
