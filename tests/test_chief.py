"""Tests for lib/chief.py — the tick and leader rule, triage decisions, status, pinned sessions, TL;DRs, spend.
Run: python3 -m unittest tests.test_chief -v
"""
import base64
import contextlib
import fcntl
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import chief  # noqa: E402
import habits  # noqa: E402
import hub  # noqa: E402
import now as nowmod  # noqa: E402

UTC = timezone.utc
AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def seed_sessions(self, sessions):
        with hub.locked(self.ws):
            hub.save_json(self.ws, "sessions.json", {"version": 1, "sessions": sessions})






class TestSpend(Base):
    CFG = {"manager": {"timezone": "Asia/Jerusalem"}}

    def test_spend_rolls_by_local_date(self):
        tz = nowmod.Schedule.from_config(self.CFG).tz
        at = datetime(2026, 11, 1, 22, 30, tzinfo=UTC)  # 00:30 on 11-02 in Jerusalem
        chief.add_spend(self.ws, at, tz, 1.25)
        chief.add_spend(self.ws, at, tz, 0.75)
        self.assertEqual(chief.spent_today(self.ws, at, tz), 2.0)
        self.assertEqual(chief.spent_today(self.ws, datetime(2026, 11, 1, 20, 0, tzinfo=UTC), tz), 0.0)
        doc = json.loads((chief.chief_dir(self.ws) / "spend.json").read_text())
        self.assertEqual(doc, {"2026-11-02": 2.0})

    def test_spend_keeps_14_days(self):
        tz = nowmod.Schedule.from_config(self.CFG).tz
        for d in range(1, 21):
            chief.add_spend(self.ws, datetime(2026, 10, d, 9, 0, tzinfo=UTC), tz, 1.0)
        doc = json.loads((chief.chief_dir(self.ws) / "spend.json").read_text())
        self.assertEqual(len(doc), 14)
        self.assertIn("2026-10-20", doc)
        self.assertNotIn("2026-10-06", doc)


class TestPausedSettings(Base):
    def test_paused(self):
        self.assertFalse(chief.paused(self.ws))
        (self.ws / ".mother").mkdir(parents=True)
        (self.ws / ".mother" / "PAUSED").write_text("")
        self.assertTrue(chief.paused(self.ws))
        chief.resume(self.ws)  # leaves the global switch alone
        self.assertTrue((self.ws / ".mother" / "PAUSED").exists())
        (self.ws / ".mother" / "PAUSED").unlink()
        self.assertFalse(chief.paused(self.ws))
        chief.pause(self.ws)
        self.assertTrue((chief.chief_dir(self.ws) / "PAUSED").exists())
        self.assertTrue(chief.paused(self.ws))
        chief.resume(self.ws)
        self.assertFalse(chief.paused(self.ws))
        chief.resume(self.ws)  # idempotent

    def test_settings_defaults_and_overrides(self):
        s = chief.settings({})
        self.assertEqual(s, {"daily_budget_usd": 20, "tldr_model": "sonnet", "triage_model": "sonnet", "refresh_minutes": 5})
        self.assertEqual([chief.settings({"chief": {"refresh_minutes": v}})["refresh_minutes"] for v in (1, 60, 0, 61, "5", True, 2.5)],
                         [1, 60, 5, 5, 5, 5, 5])
        self.assertEqual(chief.settings({"chief": {"triage_model": "gpt"}})["triage_model"], "sonnet")
        self.assertEqual(chief.settings({"chief": {"triage_model": "opus"}})["triage_model"], "opus")
        s = chief.settings({"chief": {"daily_budget_usd": 5, "max_workers": 5, "batch_minutes": 30, "bogus": 1}})
        self.assertEqual(s, {"daily_budget_usd": 5, "tldr_model": "sonnet", "triage_model": "sonnet", "refresh_minutes": 5})


def line(**kw):
    d = {"source": "tracker", "kind": "update", "key": "PLAT-1", "at": "2026-10-29T09:00:00Z",
         "also": [], "title": "t", "who": "Agnes", "link": "https://example.com/PLAT-1", "summary": "s"}
    d.update(kw)
    return hub.make_line(**d)


class TestTick(Base):
    def inbox(self, *ls):
        hub.append_inbox(self.ws, list(ls))

    def mute(self, key):
        with hub.locked(self.ws):
            w = hub.load_watch(self.ws)
            w["mute"]["keys"].append(key)
            hub.save_watch(self.ws, w)

    def cursor(self):
        return hub._hub_doc(self.ws).get("inbox_cursor")



    def test_tick_sends_desktop_key_to_judge(self):
        self.seed_sessions({"PLAT-3": {"session_id": "local_x", "state": "live", "unseen": 0}})
        a = line(key="PLAT-3")
        self.inbox(a)
        r = chief.tick(self.ws, {}, AT)
        self.assertEqual(r["decided"], [])
        self.assertEqual(r["judge"], [a["id"]])
        self.assertIsNone(self.cursor())
        self.assertEqual(hub._sessions_doc(self.ws)["sessions"]["PLAT-3"]["unseen"], 0)

    def test_tick_never_logs_manual(self):
        self.mute("PLAT-1")
        a = line(kind="manual")
        self.inbox(a)
        r = chief.tick(self.ws, {}, AT)
        self.assertEqual(r["decided"], [])
        self.assertEqual(r["judge"], [a["id"]])

    def test_lines_marks_session_kind(self):
        self.seed_sessions({"PLAT-3": {"session_id": "local_x", "state": "live"},
                            "PLAT-4": {"session_id": "u", "state": "live", "kind": "cli", "also": ["PLAT-9"]}})
        self.inbox(line(key="PLAT-3"), line(key="PLAT-4"), line(key="PLAT-9"), line(key="PLAT-5"))
        got = {l["key"]: l["session"] for l in chief.lines(self.ws)}
        self.assertEqual(got, {"PLAT-3": "desktop", "PLAT-4": None, "PLAT-9": None, "PLAT-5": None})  # a worker row is none
        first = chief.lines(self.ws)[0]
        self.assertEqual(sorted(first), sorted(["id", "key", "kind", "title", "who", "link", "summary", "also",
                                                "session", "source", "channel", "to_manager", "habits"]))

    def test_tick_stops_at_first_judgment_line(self):
        self.mute("PLAT-1")
        a, b, c = line(key="PLAT-1"), line(key="PLAT-7"), line(key="PLAT-2")
        self.inbox(a, b, c)
        r = chief.tick(self.ws, {}, AT)
        self.assertEqual([(d["line_id"], d["decision"]) for d in r["decided"]], [(a["id"], "log")])
        self.assertEqual(r["judge"], [b["id"], c["id"]])
        self.assertEqual(self.cursor(), a["id"])

    def test_what_the_old_chief_left_on_disk_is_ignored(self):
        """Worker rows, a queue, decisions and the chief's own files from before 0.18.0: nothing reads them, nothing
        starts, and no line is routed to a worker that no longer runs."""
        self.seed_sessions({"PLAT-2": {"session_id": "u", "state": "live", "kind": "cli", "status": "working",
                                       "unseen": 0, "also": []},
                            "PLAT-3": {"session_id": "local_x", "state": "live", "unseen": 0}})
        d = chief.chief_dir(self.ws)
        d.mkdir(parents=True, exist_ok=True)
        (d / "queue.jsonl").write_text('{"key": "PLAT-2", "text": "x", "source": "say"}\nnot json\n')
        (d / "decisions.json").write_text('{"decisions": [{"id": "D1", "key": "PLAT-2", "status": "pending"}]}')
        (d / "decisions.jsonl").write_text("not json\n")
        (d / "chief.json").write_text('{"session_id": "old"}')
        (d / "wake.json").write_text("{")
        a = line(key="PLAT-2")
        self.inbox(a)
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a tick starts nothing")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("a tick starts nothing")):
            r = chief.tick(self.ws, {"chief": {"max_workers": 3, "batch_minutes": 30}}, AT, session="s-1")
            self.assertEqual(r, {"paused": False, "decided": [], "judge": [a["id"]]})
            got = chief.triage_decide(self.ws, {}, AT, r["judge"], [])
        self.assertEqual(got["now"], [a["id"]])
        doc = chief.status_doc(self.ws, {}, AT)
        self.assertEqual([u["key"] for u in doc["inbox"]], ["PLAT-2"])  # an Inbox item, not a route to the worker
        self.assertNotIn("workers", doc)
        self.assertNotIn("decisions", doc)
        rows = hub._sessions_doc(self.ws)["sessions"]
        self.assertEqual((rows["PLAT-2"]["state"], rows["PLAT-2"]["unseen"]), ("dropped", 0))
        self.assertEqual(rows["PLAT-3"]["state"], "live")
        self.assertEqual((d / "queue.jsonl").read_text().count("\n"), 2)  # left as it was

    def test_tick_never_moves_cursor_past_judge_line(self):
        self.mute("PLAT-1")
        a = line(key="PLAT-1", at="2026-10-29T09:00:00Z")
        j = line(key="PLAT-7")
        c = line(key="PLAT-1", at="2026-10-29T08:00:00Z")
        self.inbox(a, j, c)
        r = chief.tick(self.ws, {}, AT)
        self.assertEqual(r["decided"], [])
        self.assertEqual(r["judge"][0], a["id"])
        self.assertIn(j["id"], [l["id"] for l in chief.lines(self.ws)])
        self.assertIn(j["id"], r["judge"])





    def test_tick_judge_capped_at_20(self):
        self.inbox(*[line(key="PLAT-%d" % (100 + i)) for i in range(25)])
        self.assertEqual(len(chief.tick(self.ws, {}, AT)["judge"]), 20)





    def test_tick_paused_only_heartbeats(self):
        self.inbox(line(key="PLAT-7"))
        chief.pause(self.ws)
        r = chief.tick(self.ws, {}, AT)
        self.assertEqual(r, {"paused": True, "decided": [], "judge": []})
        hb = json.loads((chief.chief_dir(self.ws) / "heartbeat.json").read_text())
        self.assertEqual(hb, {"at": hub._iso(AT), "pid": os.getpid()})
        self.assertIsNone(self.cursor())


def verdict(l, is_work=True, needs_attention=True, title="T", tldr="D", **extra):
    return dict({"line_id": l["id"], "key": l["key"], "is_work": is_work, "needs_attention": needs_attention,
                 "title": title, "tldr": tldr}, **extra)


class TestLeader(Base):
    """`tick --as <session>`: one desktop session leads, and code decides the judge lines."""
    inbox = TestTick.inbox

    def main(self, *args, at=AT, stdin=None, cfg=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin or "")):
            code = chief.main(list(args), self.ws, cfg or {}, at)
        return code, out.getvalue(), err.getvalue()

    def beat(self):
        return json.loads((chief.chief_dir(self.ws) / "heartbeat.json").read_text())

    def test_as_leads_when_no_heartbeat(self):
        a = line(key="PLAT-7")
        self.inbox(a)
        code, out, _ = self.main("tick", "--json", "--as", "s-1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["judge"], [a["id"]])
        self.assertEqual(self.beat(), {"at": hub._iso(AT), "pid": os.getpid(), "session_id": "s-1"})

    def test_as_skips_under_another_sessions_fresh_heartbeat(self):
        self.inbox(line(key="PLAT-7"))
        self.main("tick", "--json", "--as", "s-1")
        code, out, _ = self.main("tick", "--json", "--as", "s-2", at=AT + timedelta(seconds=60))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"skipped": "another session leads"})
        self.assertEqual(self.beat()["session_id"], "s-1")
        self.assertEqual(self.beat()["at"], hub._iso(AT))  # a skipped pass writes nothing


    def test_as_takes_over_a_stale_heartbeat(self):
        self.main("tick", "--json", "--as", "s-1")
        later = AT + timedelta(seconds=chief.ALIVE_SECONDS)
        code, out, _ = self.main("tick", "--json", "--as", "s-2", at=later)
        self.assertNotIn("skipped", json.loads(out))
        self.assertEqual(self.beat(), {"at": hub._iso(later), "pid": os.getpid(), "session_id": "s-2"})

    def test_as_same_session_keeps_leading(self):
        self.main("tick", "--json", "--as", "s-1")
        later = AT + timedelta(seconds=60)
        code, out, _ = self.main("tick", "--json", "--as", "s-1", at=later)
        self.assertNotIn("skipped", json.loads(out))
        self.assertEqual(self.beat()["at"], hub._iso(later))

    def test_tick_without_as_never_skips(self):
        self.main("tick", "--json", "--as", "s-1")
        code, out, _ = self.main("tick", "--json", at=AT + timedelta(seconds=10))
        self.assertNotIn("skipped", json.loads(out))
        self.assertEqual(self.beat(), {"at": hub._iso(AT + timedelta(seconds=10)), "pid": os.getpid()})

    def test_status_says_who_leads(self):
        self.main("tick", "--json", "--as", "s-1")
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["led_by"], "s-1")
        text = self.main("status")[1]
        self.assertIn("chief: led by a desktop session (heartbeat", text)
        self.assertNotIn("worker", text)
        self.assertNotIn("decide", text)
        later = AT + timedelta(seconds=chief.ALIVE_SECONDS)   # the heartbeat went stale: nobody leads
        self.assertIsNone(chief.status_doc(self.ws, {}, later)["led_by"])
        self.assertIn("chief: no session is leading (heartbeat", self.main("status", at=later)[1])
        chief.tick(self.ws, {}, later)                        # a heartbeat with no session id names no leader
        self.assertIsNone(chief.status_doc(self.ws, {}, later)["led_by"])

    def test_decide_logs_fyi_and_makes_the_rest_now(self):
        a, b = line(key="PLAT-7"), line(key="PLAT-8", title="Build broke", summary="red since noon")
        c, m = line(key="PLAT-9"), line(source="manual", kind="manual", key="PLAT-3", title="look at this")
        self.inbox(a, b, c, m)
        judge = chief.tick(self.ws, {}, AT, session="s-1")["judge"]
        self.assertEqual(judge, [a["id"], b["id"], c["id"], m["id"]])
        body = {"judge": judge, "verdicts": [verdict(a, needs_attention=False, title="Thanks"),
                                             verdict(c, title="Review due", tldr="needs a look today"),
                                             verdict(m, needs_attention=False)]}  # a manual line is never logged
        code, out, _ = self.main("triage", "--stdin", "--decide", stdin=json.dumps(body))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"paused": False, "logged": [a["id"]], "now": [b["id"], c["id"], m["id"]]})
        h = hub._hub_doc(self.ws)
        self.assertEqual(h["inbox_cursor"], m["id"])
        self.assertEqual([(u["key"], u["title"], u["why"], u["link"]) for u in h["updates"]], [
            ("PLAT-8", "Build broke", "red since noon", "https://example.com/PLAT-1"),
            ("PLAT-9", "Review due", "needs a look today", "https://example.com/PLAT-1"),
            ("PLAT-3", "T", "D", "https://example.com/PLAT-1")])
        self.assertEqual(chief.tick(self.ws, {}, AT, session="s-1")["judge"], [])
        doc = chief.status_doc(self.ws, {}, AT)
        self.assertEqual(doc["inbox_count"], 3)
        self.assertEqual({r["key"] for r in doc["inbox"]}, {"PLAT-8", "PLAT-9", "PLAT-3"})

    def test_decide_with_no_verdicts_keeps_every_line_as_now(self):
        a, b = line(key="PLAT-7"), line(key="PLAT-8")
        self.inbox(a, b)
        judge = chief.tick(self.ws, {}, AT, session="s-1")["judge"]
        r = chief.triage_decide(self.ws, {}, AT, judge, [])
        self.assertEqual((r["logged"], r["now"]), ([], [a["id"], b["id"]]))

    def test_decide_stores_the_richer_verdict_and_status_returns_it(self):
        a, b, c = line(key="PLAT-7"), line(key="PLAT-8"), line(key="PLAT-9")
        self.inbox(a, b, c)
        judge = chief.tick(self.ws, {}, AT, session="s-1")["judge"]
        acts = [{"label": "Draft  reply\u202e", "prompt": "Read the thread,\nthen draft a reply; don't send"},
                {"label": "x" * 40, "prompt": "p" * 400}, {"label": "", "prompt": "no label"}, "junk",
                {"label": "Third", "prompt": "3"}, {"label": "Fourth", "prompt": "4"}]
        body = {"judge": judge, "verdicts": [
            verdict(a, kind="decide", on="manager", urgency="later", actions=acts),
            verdict(b, kind="bogus", on=7, urgency="now"),            # bad enums fall back
            verdict(c)]}                                              # the older four-field verdict
        self.assertEqual(self.main("triage", "--stdin", "--decide", stdin=json.dumps(body))[0], 0)
        code, out, _ = self.main("status", "--json")
        rows = json.loads(out)["inbox"]
        self.assertEqual([r["key"] for r in rows], ["PLAT-8", "PLAT-9", "PLAT-7"])   # now, today (none), later
        got = {r["key"]: (r["kind"], r["on"], r["urgency"], r["actions"]) for r in rows}
        self.assertEqual(got["PLAT-8"], ("act", "manager", "now", []))
        self.assertEqual(got["PLAT-9"], (None, None, None, []))
        self.assertEqual(got["PLAT-7"], ("decide", "manager", "later", [
            {"label": "Draft reply", "prompt": "Read the thread, then draft a reply; don't send"},
            {"label": "x" * 24, "prompt": "p" * 300}, {"label": "Third", "prompt": "3"}]))
        stored = json.loads((chief.chief_dir(self.ws) / "triage.json").read_text())
        self.assertEqual(sorted(stored["items"]), ["U1", "U2"])       # by update id; nothing for the old shape
        self.assertNotIn("kind", hub._hub_doc(self.ws)["updates"][0])  # the hub's own schema is untouched
        # the same key decided again with no verdict drops what was stored for it
        d = line(key="PLAT-7", at="2026-10-29T10:00:00Z")
        self.inbox(d)
        chief.triage_decide(self.ws, {}, AT, chief.tick(self.ws, {}, AT, session="s-1")["judge"], [])
        self.assertEqual(sorted(json.loads((chief.chief_dir(self.ws) / "triage.json").read_text())["items"]), ["U2"])

    def test_fyi_logs_and_manual_and_error_never_do(self):
        a, m = line(key="PLAT-7"), line(source="manual", kind="manual", key="PLAT-3")
        e = line(source="tracker", kind="error", key="watcher:tracker")
        self.inbox(a, m, e)
        judge = chief.tick(self.ws, {}, AT, session="s-1")["judge"]
        fyi = dict(needs_attention=False, kind="fyi", on="other", urgency="later", actions=[])
        r = chief.triage_decide(self.ws, {}, AT, judge, [verdict(a, **fyi), verdict(m, **fyi), verdict(e, **fyi)])
        self.assertEqual((r["logged"], r["now"]), ([a["id"]], [m["id"], e["id"]]))

    def test_context_is_names_only_and_capped(self):
        cfg = {"agent": {"manager": "Dana Levi", "manager_short": "Dana"},
               "manager": {"email": "dana@example.com", "chat_dm": "U00000000"},
               "teams": [{"slug": "platform", "name": "Platform team", "tracker": {"project": "PLAT"},
                          "repos": ["example-org/platform-api"],
                          "people": [{"name": "Sam Roe", "email": "sam@example.com", "chat": "U00000001"}, {"name": ""}]}],
               "hub": {"watch": {"tracker": {"projects": ["OPS"]}, "code": {"repos": ["example-org/web"]}}}}
        code, out, _ = self.main("context", "--json", cfg=cfg)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {
            "version": 1, "manager": {"name": "Dana Levi", "short": "Dana"},
            "teams": [{"name": "Platform team", "people": ["Sam Roe"]}],
            "projects": ["PLAT", "OPS"], "repos": ["example-org/platform-api", "example-org/web"]})
        for secret in ("example.com", "U0000"):
            self.assertNotIn(secret, out)
        self.assertEqual(chief.triage_context(self.ws, {}), {
            "version": 1, "manager": {"name": "", "short": ""}, "teams": [], "projects": [], "repos": []})
        big = {"teams": [{"name": "Team %d" % t, "people": [{"name": "Person %d-%d %s" % (t, i, "x" * 30)} for i in range(30)]}
                         for t in range(12)]}
        doc = chief.triage_context(self.ws, big)
        self.assertLessEqual(len(json.dumps(doc, separators=(",", ":"))), chief.CONTEXT_MAX)
        self.assertTrue(doc["teams"])

    def test_lines_say_source_channel_and_whether_the_manager_is_named(self):
        cfg = {"agent": {"manager": "Dana Levi", "manager_short": "Dana"}, "manager": {"chat_user": "U0DDDDDDD"},
               "teams": [{"channels": [{"id": "C0AAAAAAA", "name": "platform"}]}]}
        self.inbox(line(source="chat", key="C0AAAAAAA/1700000000.000100", summary="<@U0DDDDDDD> can you approve?"),
                   line(source="chat", key="D0DDDDDDD/dm", summary="hi"),
                   line(key="PLAT-4", summary="dana, please look"), line(key="PLAT-5", summary="Danas build"))
        got = [(l["source"], l["channel"], l["to_manager"]) for l in chief.lines(self.ws, cfg)]
        self.assertEqual(got, [("chat", "platform", "mentioned"), ("chat", None, "addressed"),
                               ("tracker", None, "mentioned"), ("tracker", None, None)])

    def test_decide_needs_the_object_shape(self):
        self.assertEqual(self.main("triage", "--stdin", "--decide", stdin="[]")[0], 2)




CLI_A, CLI_B, CLI_C = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                       "33333333-3333-4333-8333-333333333333")


def row(sid, pinned=True, **kw):
    d = {"sessionId": sid, "title": "Title " + sid[-1], "cwd": "/work/repo-" + sid[-1], "isArchived": False,
         "link": "claude://example/" + sid}
    if pinned is not None:
        d["pinned"] = pinned
    d.update(kw)
    return d


class TestPinned(Base):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = Path(tmp.name)

    def session_file(self, sid, cli, nested=True, **kw):
        d = self.app / "acct-0" / "org-0" if nested else self.app
        d.mkdir(parents=True, exist_ok=True)
        doc = {"sessionId": sid, "cliSessionId": cli, "cwd": "/work/repo", "isArchived": False,
               "lastActivityAt": 1793620800000, "title": "file title"}
        doc.update(kw)
        (d / (sid + ".json")).write_text(json.dumps(doc))

    def snapshot(self):
        return json.loads((chief.chief_dir(self.ws) / "desktop.json").read_text())

    def test_pinned_snapshot_keeps_pinned_only(self):
        for sid, cli in (("local_a", CLI_A), ("local_b", CLI_B), ("local_c", CLI_C)):
            self.session_file(sid, cli)
        n = chief.pinned_snapshot(self.ws, AT, [row("local_a"), row("local_b", pinned=False),
                                                row("local_c", pinned=None), "junk", {"pinned": True}],
                                  app_dir=self.app)
        self.assertEqual(n, 1)
        doc = self.snapshot()
        self.assertEqual(doc["at"], hub._iso(AT))
        self.assertEqual(doc["sessions"], [{"session_id": "local_a", "cli_session_id": CLI_A, "title": "Title a",
                                            "cwd": "/work/repo-a", "link": "claude://example/local_a"}])
        # a later snapshot replaces the earlier one whole
        self.assertEqual(chief.pinned_snapshot(self.ws, AT, [], app_dir=self.app), 0)
        self.assertEqual(self.snapshot()["sessions"], [])

    def test_pinned_snapshot_reads_cli_id_and_drops_archived(self):
        self.session_file("local_a", CLI_A)
        self.session_file("local_b", CLI_B, nested=False)
        self.session_file("local_c", CLI_C, isArchived=True)
        self.session_file("local_d", "not a uuid")
        self.session_file("local_e", CLI_A)            # the row says archived
        rows = [row("local_a"), row("local_b"), row("local_c"), row("local_d"), row("local_e", isArchived=True),
                row("local_missing"), row("../local_a"), row("local_a/../x")]
        self.assertEqual(chief.pinned_snapshot(self.ws, AT, rows, app_dir=self.app), 2)
        self.assertEqual([(s["session_id"], s["cli_session_id"]) for s in self.snapshot()["sessions"]],
                         [("local_a", CLI_A), ("local_b", CLI_B)])
        # a row's own cli id field is never trusted: the id comes from the app's file
        self.assertEqual(chief.pinned_snapshot(self.ws, AT, [row("local_a", cliSessionId=CLI_C)], app_dir=self.app), 1)
        self.assertEqual(self.snapshot()["sessions"][0]["cli_session_id"], CLI_A)

    def test_desktop_status_fields(self):
        self.session_file("local_a", CLI_A, lastTurnReport="Merged the fix. " + "r" * 300,
                          lastActivityAt=1793620800000)
        self.session_file("local_b", CLI_B)
        chief.pinned_snapshot(self.ws, AT, [row("local_a", cwd="/work/example-api", title="Fix the login bug"),
                                            row("local_b", link=None)], app_dir=self.app)
        got = chief.desktop_status(self.ws, self.app)
        self.assertEqual(got[0], {
            "session_id": "local_a", "cli_session_id": CLI_A, "title": "Fix the login bug", "cwd": "/work/example-api",
            "repo": "example-api", "last_activity_at": "2026-11-02T12:00:00Z",
            "last_report": ("Merged the fix. " + "r" * 300)[:200], "open_link": "claude://example/local_a"})
        self.assertEqual((got[1]["last_report"], got[1]["open_link"]), ("", None))
        # a session archived since the snapshot drops out
        self.session_file("local_b", CLI_B, isArchived=True)
        self.assertEqual([d["session_id"] for d in chief.desktop_status(self.ws, self.app)], ["local_a"])
        # no snapshot, no rows; and status_doc carries them
        self.assertEqual(chief.desktop_status(Path(self._tmp.name) / "nope", self.app), [])
        with mock.patch.object(chief, "default_app_dir", return_value=self.app):
            self.assertEqual([d["session_id"] for d in chief.status_doc(self.ws, {}, AT)["desktop"]], ["local_a"])

    def test_desktop_status_caps_and_caches_app_files(self):
        self.session_file("local_a", CLI_A, lastTurnReport="first")
        self.session_file("local_b", CLI_B)
        chief.pinned_snapshot(self.ws, AT, [row("local_a"), row("local_b")], app_dir=self.app)
        self.assertEqual(len(chief.desktop_status(self.ws, self.app)), 2)
        cache = json.loads((chief.chief_dir(self.ws) / "desktop-cache.json").read_text())
        self.assertEqual(sorted(os.path.basename(k) for k in cache), ["local_a.json", "local_b.json"])
        # an unchanged file is not parsed again
        with mock.patch.object(chief, "_read_obj", wraps=chief._read_obj) as reads:
            chief.desktop_status(self.ws, self.app)
        self.assertFalse(any(str(c.args[0]).endswith(("local_a.json", "local_b.json")) for c in reads.call_args_list))
        # a changed file is (mtime or size moves)
        self.session_file("local_a", CLI_A, lastTurnReport="second, longer")
        self.assertEqual(chief.desktop_status(self.ws, self.app)[0]["last_report"], "second, longer")
        # a file past the 2 MB cap is skipped, never parsed: the session drops out
        self.session_file("local_b", CLI_B, padding="x" * (chief.APP_FILE_MAX + 1))
        self.assertEqual([d["session_id"] for d in chief.desktop_status(self.ws, self.app)], ["local_a"])
        self.assertEqual(chief.pinned_snapshot(self.ws, AT, [row("local_b")], app_dir=self.app), 0)

    def test_ids_must_match_whole(self):
        self.session_file("local_a", CLI_A + "\n")
        self.assertEqual(chief.pinned_snapshot(self.ws, AT, [row("local_a"), row("local_a\n")], app_dir=self.app), 0)

    def test_cli_pinned_reads_rows_on_stdin(self):
        self.session_file("local_a", CLI_A)
        for stdin, code in ((json.dumps([row("local_a"), row("local_b", pinned=False)]), 0), ("{bad", 2),
                            (json.dumps({"rows": []}), 2)):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(chief, "default_app_dir", return_value=self.app), \
                    mock.patch("sys.stdin", io.StringIO(stdin)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                got = chief.main(["pinned", "--stdin"], self.ws, {}, AT)
            self.assertEqual(got, code, (stdin, err.getvalue()))
            if code == 0:
                self.assertEqual(json.loads(out.getvalue()), {"pinned": 1})
        self.assertEqual(len(self.snapshot()["sessions"]), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(chief.main(["pinned"], self.ws, {}, AT), 2)          # --stdin is required


class TestMain(Base):
    def main(self, *args, cfg=None, at=AT):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chief.main(list(args), self.ws, {} if cfg is None else cfg, at)
        return code, out.getvalue(), err.getvalue()

    def test_main_status_json_shape(self):
        code, out, _ = self.main("status", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(set(doc), {"version", "at", "paused", "chief", "budget", "desktop", "led_by", "inbox",
                                    "inbox_count", "cases", "relay", "guard", "health", "habits", "quieted"})
        self.assertEqual((doc["habits"], doc["quieted"]), ({"on": True}, {"count_7d": 0, "recent": []}))
        self.assertEqual((doc["led_by"], doc["inbox"], doc["inbox_count"]), (None, [], 0))
        self.assertEqual(doc["desktop"], [])                    # no snapshot yet
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["at"], hub._iso(AT))
        self.assertIs(doc["paused"], False)
        self.assertEqual(doc["chief"], {"alive": False, "heartbeat_at": None})
        self.assertEqual(doc["budget"], {"spent": 0.0, "limit": 20.0, "unreadable": False, "over": False})


    def test_main_status_paused(self):
        chief.pause(self.ws)
        self.assertIs(json.loads(self.main("status", "--json")[1])["paused"], True)





    def test_main_usage_exit_2(self):
        gone = [[v] for v in ("run", "say", "hold", "release", "keep", "install", "uninstall", "role", "post-log")]
        for args in [[], ["bogus"], ["status", "PLAT-1"], ["lines", "--detach"], ["tick", "--as"], ["tick", "--as", " "],
                     ["triage", "--stdin"]] + gone:
            code, out, err = self.main(*args)
            self.assertEqual(code, 2, args)
            self.assertIn("usage", (out + err).lower(), args)
        code, out, _ = self.main("help")
        self.assertEqual(code, 0)
        for word in ("worker", "install", "decide batch", "role"):
            self.assertNotIn(word, out)






    def test_main_pause_resume(self):
        self.assertEqual(self.main("pause")[0], 0)
        self.assertTrue(chief.paused(self.ws))
        self.assertEqual(self.main("resume")[0], 0)
        self.assertFalse(chief.paused(self.ws))

    def test_main_lines_and_tick_json(self):
        a = line(key="PLAT-5")
        hub.append_inbox(self.ws, [a])
        code, out, _ = self.main("lines", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([l["id"] for l in json.loads(out)], [a["id"]])
        code, out, _ = self.main("tick", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["judge"], [a["id"]])











class TestHealth(Base):
    main = TestMain.main

    def seed_updates(self, *keys):
        with hub.locked(self.ws):
            hub.save_json(self.ws, "hub.json", {"version": 1, "updates": [
                {"id": "U%d" % (n + 1), "key": k, "title": "T " + k, "why": "w", "link": None,
                 "at": hub._iso(AT - timedelta(minutes=n)), "state": "open"} for n, k in enumerate(keys)]})

    def unsent(self, name, text="report\n"):
        (self.ws / ".mother").mkdir(exist_ok=True)
        (self.ws / ".mother" / name).write_text(text)

    def test_health_of_an_empty_workspace(self):
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["health"], {
            "guard": "on", "watcher": None, "watcher_errors": [], "watcher_error_count": 0, "undelivered": []})

    def test_guard_states_and_paused_wins(self):
        state = self.ws / ".mother"
        state.mkdir()
        (state / "GUARD_OFF").write_text("")
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["health"]["guard"], "off")
        (state / "PAUSED").write_text("")
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["health"]["guard"], "paused")
        (state / "GUARD_OFF").unlink()
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["health"]["guard"], "paused")

    def test_watcher_errors_leave_the_inbox_and_its_count(self):
        self.seed_updates("PLAT-1", "watcher:chat", "watcher:mail")
        doc = chief.status_doc(self.ws, {}, AT)
        self.assertEqual(([u["key"] for u in doc["inbox"]], doc["inbox_count"]), (["PLAT-1"], 1))
        h = doc["health"]
        self.assertEqual(([u["id"] for u in h["watcher_errors"]], h["watcher_error_count"]), (["U2", "U3"], 2))
        self.assertEqual(set(h["watcher_errors"][0]), {"id", "key", "title", "why", "link", "links", "at", "session",
                                                        "source", "who", "label", "kind", "on", "urgency", "actions",
                                                        "habit_note"})

    def test_watcher_heartbeat_and_sources(self):
        with hub.locked(self.ws):
            hub.save_json(self.ws, "watcher.json", {
                "at": hub._iso(AT - timedelta(minutes=30)), "every_seconds": 300, "active": True, "sources": {
                    "chat": {"status": "ok", "at": hub._iso(AT - timedelta(minutes=30))},
                    "mail": {"status": "couldnt_check", "reason": "sign-in\u202e expired\nagain",
                             "since": "2026-11-01T07:00:00Z"}, "junk": "x"}})
        w = chief.status_doc(self.ws, {}, AT)["health"]["watcher"]
        self.assertEqual((w["at"], w["every_seconds"], w["active"], w["stale"]),
                         (hub._iso(AT - timedelta(minutes=30)), 300, True, True))
        self.assertEqual(sorted(w["sources"]), ["chat", "mail"])
        self.assertEqual(w["sources"]["chat"]["status"], "ok")
        self.assertEqual(w["sources"]["mail"], {"status": "couldnt_check", "reason": "sign-in expired again",
                                                "since": "2026-11-01T07:00:00Z", "at": None})
        with hub.locked(self.ws):   # outside active hours an old poll is not stale
            hub.save_json(self.ws, "watcher.json", {"at": hub._iso(AT - timedelta(hours=9)), "active": False})
        self.assertIs(chief.status_doc(self.ws, {}, AT)["health"]["watcher"]["stale"], False)

    def test_undelivered_lists_matching_files_newest_first(self):
        self.unsent("brief-unsent-2026-10-30.md", "old\n")
        self.unsent("team-digest-unsent-2026-11-01.md", "newer\n")
        self.unsent("brief-unsent-2026-11-01.md")
        for other in ("Brief-unsent-2026-11-01.md", "brief-unsent-2026-11-01.txt", "brief-unsent-20261101.md",
                      "now.json"):
            self.unsent(other)
        (self.ws / ".mother" / "dots-unsent-2026-11-01.md").mkdir()      # a folder is not a report
        got = chief.status_doc(self.ws, {}, AT)["health"]["undelivered"]
        self.assertEqual(got, [
            {"name": "team-digest-unsent-2026-11-01.md", "routine": "team-digest", "date": "2026-11-01", "bytes": 6},
            {"name": "brief-unsent-2026-11-01.md", "routine": "brief", "date": "2026-11-01", "bytes": 7},
            {"name": "brief-unsent-2026-10-30.md", "routine": "brief", "date": "2026-10-30", "bytes": 4}])

    def test_report_prints_one_file(self):
        self.unsent("brief-unsent-2026-11-01.md", "# Brief\nline two\n")
        self.assertEqual(self.main("report", "brief-unsent-2026-11-01.md"), (0, "# Brief\nline two\n", ""))

    def test_report_bad_names_exit_2(self):
        self.unsent("brief-unsent-2026-11-01.md")
        (self.ws / "config.json").write_text("{}")
        (self.ws / ".mother" / "sub").mkdir()
        (self.ws / ".mother" / "sub" / "brief-unsent-2026-11-01.md").write_text("x")
        self.unsent("big-unsent-2026-11-01.md", "x" * (chief.UNSENT_MAX_BYTES + 1))
        self.unsent("edge-unsent-2026-11-01.md", "x" * chief.UNSENT_MAX_BYTES)
        os.symlink(str(self.ws / "config.json"), str(self.ws / ".mother" / "link-unsent-2026-11-01.md"))
        bad = ["../config.json", "config.json", "now.json", "sub/brief-unsent-2026-11-01.md",
               "../.mother/brief-unsent-2026-11-01.md", "/etc/passwd", "brief-unsent-2026-11-02.md",
               "BRIEF-unsent-2026-11-01.md", "brief-unsent-2026-11-01.md\n", "big-unsent-2026-11-01.md",
               "link-unsent-2026-11-01.md", ""]
        for name in bad:
            code, out, err = self.main("report", name)
            self.assertEqual((code, out), (2, ""), name)
            self.assertIn("not an undelivered report", err)
        self.assertEqual(self.main("report")[0], 2)                                       # no name
        self.assertEqual(self.main("report", "a-unsent-2026-11-01.md", "b-unsent-2026-11-01.md")[0], 2)
        self.assertEqual(self.main("report", "edge-unsent-2026-11-01.md")[0], 0)          # exactly 64 KB reads
        self.assertEqual(self.main("status", "x")[0], 2)                                  # no other verb takes a name


class TestStatusAndText(Base):
    def setUp(self):
        super().setUp()
        self.tz = nowmod.Schedule.from_config({}).tz

    def test_status_budget_flags(self):
        b = chief.status_doc(self.ws, {}, AT)["budget"]
        self.assertEqual((b["unreadable"], b["over"]), (False, False))
        chief.add_spend(self.ws, AT, self.tz, 20)
        self.assertIs(chief.status_doc(self.ws, {}, AT)["budget"]["over"], True)
        (chief.chief_dir(self.ws) / "spend.json").write_text("{bad")
        b = chief.status_doc(self.ws, {}, AT)["budget"]
        self.assertEqual((b["unreadable"], b["over"], b["spent"]), (True, True, 0.0))

    def now_item(self, key, who="", summary="s", title="t"):
        a = line(key=key, who=who, summary=summary, title=title)
        hub.append_inbox(self.ws, [a])
        hub.decide(self.ws, {}, AT, [{"line_id": a["id"], "key": key, "decision": "now", "title": title, "why": summary}])

    def test_inbox_items_carry_source_who_and_a_human_label(self):
        cfg = {"teams": [{"channels": [{"id": "C0AAAAAAA", "name": "platform"}]}]}
        self.now_item("C0AAAAAAA/1700000000.000100", who="Dana <U0DDDDDDD>")
        self.now_item("C0BBBBBBB/1700000000.000200", who="Sam")
        self.now_item("C0CCCCCCC/1700000000.000300")
        self.now_item("D0DDDDDDD/dm", who="Robin")
        self.now_item("mail:1a2b3c", who="Dana")
        self.now_item("PLAT-9")
        self.now_item("example/api#45")
        self.now_item("cal:evt1")
        got = {r["key"]: (r["source"], r["who"], r["label"]) for r in chief.status_doc(self.ws, cfg, AT)["inbox"]}
        self.assertEqual(got, {
            "C0AAAAAAA/1700000000.000100": ("chat", "Dana", "#platform"),
            "C0BBBBBBB/1700000000.000200": ("chat", "Sam", "Sam"),
            "C0CCCCCCC/1700000000.000300": ("chat", "", "chat thread"),
            "D0DDDDDDD/dm": ("chat", "Robin", "Robin"),
            "mail:1a2b3c": ("mail", "Dana", "mail thread"),
            "PLAT-9": ("tracker", "", "PLAT-9"),
            "example/api#45": ("code", "", "example/api#45"),
            "cal:evt1": ("calendar", "", "calendar"),
        })
        for label in (v[2] for v in got.values()):
            self.assertNotRegex(label, r"1700000000|1a2b3c|C0[A-Z]{7}")

    def test_inbox_why_keeps_300_characters(self):
        self.now_item("PLAT-7", summary="word " * 80)
        why = chief.status_doc(self.ws, {}, AT)["inbox"][0]["why"]
        self.assertEqual(len(why), 300)
        self.assertEqual(chief.WHY_MAX, 300)

    def test_status_guard_state(self):
        m = self.ws / ".mother"
        m.mkdir(exist_ok=True)
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["guard"], "on")
        (m / "GUARD_OFF").write_text("")
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["guard"], "off")
        (m / "PAUSED").write_text("")
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["guard"], "paused")   # PAUSED wins
        (m / "GUARD_OFF").unlink()
        self.assertEqual(chief.status_doc(self.ws, {}, AT)["guard"], "paused")

    # a `now` for a live desktop key is a route to it (unseen + 1), not a U<n> update
    def test_desktop_key_now_is_stored_as_route(self):
        self.seed_sessions({"PLAT-3": {"session_id": "local_x", "state": "live", "unseen": 0}})
        a = line(key="PLAT-3")
        hub.append_inbox(self.ws, [a])
        res = hub.decide(self.ws, {}, AT, [{"line_id": a["id"], "key": "PLAT-3", "decision": "now", "why": "w"}])
        self.assertEqual(res[0]["decision"], "route")
        self.assertNotIn("update_id", res[0])
        self.assertEqual(hub._sessions_doc(self.ws)["sessions"]["PLAT-3"]["unseen"], 1)
        self.assertEqual(hub._hub_doc(self.ws)["updates"], [])

    def test_text_output(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chief.main(["status"], self.ws, {}, AT), 0)
        self.assertEqual(out.getvalue().splitlines(), ["chief: no session is leading (heartbeat never)",
                                                       "budget: $0.00 of $20.00 today", "pinned: 0   inbox: 0"])
        a = line(key="PLAT-5", title="Ping")
        hub.append_inbox(self.ws, [a])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chief.main(["lines"], self.ws, {}, AT), 0)
            self.assertEqual(chief.main(["tick"], self.ws, {}, AT), 0)
        self.assertEqual(out.getvalue().splitlines(), ["%s  PLAT-5  [-]  Agnes: Ping" % a["id"], "decided 0, judge 1"])
        chief.pause(self.ws)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chief.main(["tick"], self.ws, {}, AT)
        self.assertEqual(out.getvalue(), "paused: heartbeat only\n")


# ---- the Chief pane's TL;DR: session tails and the cache ----------------------------------------------
def msg(role, content, **kw):
    d = {"type": role, "uuid": kw.pop("uuid", "u-" + role), "message": {"role": role, "content": content}}
    d.update(kw)
    return json.dumps(d)


class TestTldr(Base):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.app = Path(tmp.name) / "app"
        self.app.mkdir()

    def transcript(self, cwd, sid, lines):
        d = self.home / ".claude" / "projects" / chief.project_slug(str(cwd))
        d.mkdir(parents=True, exist_ok=True)
        p = d / (sid + ".jsonl")
        p.write_text("".join(l + "\n" for l in lines))
        return p

    def tails(self, cfg=None):
        return chief.session_tails(self.ws, cfg or {}, home=self.home, app_dir=self.app)

    def pin(self, report="Merged the fix, CI green"):
        d = self.app / "acct" / "org"
        d.mkdir(parents=True, exist_ok=True)
        (d / "local_a.json").write_text(json.dumps({"sessionId": "local_a", "cliSessionId": CLI_A, "cwd": "/work/api",
                                                    "isArchived": False, "lastTurnReport": report}))
        chief.pinned_snapshot(self.ws, AT, [row("local_a", cwd="/work/api")], app_dir=self.app)

    def test_project_slug(self):
        self.assertEqual(chief.project_slug("/Users/m/my.work_space"), "-Users-m-my-work-space")

    def test_tail_reads_a_pinned_sessions_transcript(self):
        self.pin()
        p = self.transcript("/work/api", CLI_A, [
            "not json",
            msg("user", "Fix the login bug in the handler", uuid="u1"),
            msg("assistant", [{"type": "text", "text": "Patched the handler."},
                              {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}], uuid="u2"),
            msg("user", [{"type": "tool_result", "content": "secret tool output"}], uuid="u3"),
            msg("assistant", [{"type": "text", "text": "Side quest"}], uuid="u4", isSidechain=True),
            msg("assistant", [{"type": "text", "text": "Tests pass; ready for review."}], uuid="u5"),
        ])
        doc = self.tails()
        self.assertEqual((doc["version"], doc["model"]), (1, "sonnet"))
        [s] = doc["sessions"]
        st = p.stat()
        self.assertEqual((s["id"], s["kind"]), (CLI_A, "pinned"))
        self.assertNotIn("key", s)
        self.assertEqual(s["fingerprint"], "t:%d:%d" % (st.st_size, st.st_mtime_ns))
        self.assertEqual(s["text"], "user: Fix the login bug in the handler\n"
                                    "assistant: Patched the handler. [ran Bash]\n"
                                    "assistant: Tests pass; ready for review.")
        self.assertNotIn("secret tool output", s["text"])
        self.assertNotIn("Side quest", s["text"])
        self.assertNotIn("cached", s)

    def test_tail_caps_text_and_keeps_the_newest(self):
        self.pin()
        self.transcript("/work/api", CLI_A,
                        [msg("assistant", "m%d " % i + "x" * 3000, uuid="u%d" % i) for i in range(10)] +
                        [msg("assistant", "the newest", uuid="last")])
        [s] = self.tails()["sessions"]
        self.assertLessEqual(len(s["text"]), chief.TAIL_MAX)
        self.assertTrue(s["text"].endswith("assistant: the newest"))
        self.assertNotIn("m0 ", s["text"])

    def test_tail_falls_back_to_the_report_and_skips_worker_rows(self):
        # a worker row left in sessions.json from before 0.18.0, progress and all: no entry
        self.seed_sessions({"PLAT-1": {"session_id": CLI_B, "state": "live", "kind": "cli", "progress": "patched"}})
        self.pin()
        got = {s["kind"]: s for s in self.tails()["sessions"]}
        self.assertEqual(sorted(got), ["pinned"])
        self.assertEqual((got["pinned"]["id"], got["pinned"]["text"]), (CLI_A, "Merged the fix, CI green"))
        self.assertTrue(got["pinned"]["fingerprint"].startswith("r:"))
        # a pinned session's own transcript wins over its report when the file is there
        self.transcript("/work/api", CLI_A, [msg("assistant", "Opened the PR")])
        [p] = [s for s in self.tails()["sessions"] if s["kind"] == "pinned"]
        self.assertEqual(p["text"], "assistant: Opened the PR")
        self.assertTrue(p["fingerprint"].startswith("t:"))

    def test_tldr_model_setting(self):
        for given, want in ((None, "sonnet"), ("opus", "opus"), ("sonnet", "sonnet"), ("haiku", "sonnet"),
                            ("claude-x", "sonnet"), (3, "sonnet")):
            cfg = {} if given is None else {"chief": {"tldr_model": given}}
            self.assertEqual(self.tails(cfg)["model"], want, given)

    def test_store_and_cached_round_trip(self):
        self.pin()
        p = self.transcript("/work/api", CLI_A, [msg("assistant", "Working on it")])
        [s] = self.tails()["sessions"]
        n = chief.tldr_store(self.ws, AT, [
            {"id": s["id"], "fingerprint": s["fingerprint"], "model": "sonnet", "tldr": "Fixing it.\nTests next.\nThird line",
             "chips": [{"kind": "reply", "text": "Ship it"}, {"kind": "rm -rf"}, {"kind": "reply"}, "junk",
                       {"kind": "hold"}, {"kind": "open"}, {"kind": "mark_seen"}]},
            {"id": "not-a-live-session", "fingerprint": "x", "tldr": "x", "chips": []},
            "junk",
        ], home=self.home, app_dir=self.app)
        self.assertEqual(n, 1)
        [s] = self.tails()["sessions"]
        self.assertEqual(s["cached"], {"tldr": "Fixing it.\nTests next.",
                                       "chips": [{"kind": "reply", "text": "Ship it"}, {"kind": "open"}]})
        # the session moved: the cached entry no longer applies
        p.write_text(p.read_text() + msg("assistant", "Done now") + "\n")
        [s] = self.tails()["sessions"]
        self.assertNotIn("cached", s)

    def test_clean_tldr_strips_control_and_bidi(self):
        got = chief.clean_tldr("Done‮ gnihtyreve\x1b[31m red\r\nnext⁦\x07\x9b",
                               [{"kind": "reply", "text": "Ship‏ it\x00"}, {"kind": "reply", "text": "‮\x1b"}])
        self.assertEqual(got, {"tldr": "Done gnihtyreve[31m red\nnext", "chips": [{"kind": "reply", "text": "Ship it"}]})

    def test_cache_is_keyed_by_model_and_store_prunes_gone_sessions(self):
        self.pin()
        self.transcript("/work/api", CLI_A, [msg("assistant", "Working")])
        [s] = self.tails()["sessions"]
        entry = {"id": s["id"], "fingerprint": s["fingerprint"], "tldr": "ok", "chips": [], "model": "sonnet"}
        chief.tldr_store(self.ws, AT, [entry], home=self.home, app_dir=self.app)
        self.assertIn("cached", self.tails()["sessions"][0])
        opus = {"chief": {"tldr_model": "opus"}}
        self.assertNotIn("cached", self.tails(opus)["sessions"][0])          # another model: not a hit
        # an entry with no model (an older pane) is no hit either
        chief.tldr_store(self.ws, AT, [dict(entry, model=None)], home=self.home, app_dir=self.app)
        self.assertNotIn("cached", self.tails()["sessions"][0])
        # a session that is gone is pruned from the file on the next store, even with nothing new for it
        chief.tldr_store(self.ws, AT, [entry], home=self.home, app_dir=self.app)
        chief.pinned_snapshot(self.ws, AT, [], app_dir=self.app)   # no longer pinned
        chief.tldr_store(self.ws, AT, [], home=self.home, app_dir=self.app)
        self.assertEqual(json.loads((chief.chief_dir(self.ws) / "tldr.json").read_text()), {})

    def test_contract_says_tails_go_to_the_model_api(self):
        text = (Path(ROOT) / "docs" / "CONTRACT.md").read_text()
        self.assertIn("pinned desktop sessions included, are sent to the model API", text)

    def test_cli_tail_and_tldr_verbs(self):
        self.pin()
        self.transcript("/work/api", CLI_A, [msg("assistant", "Working")])
        out = io.StringIO()
        with mock.patch.object(chief, "default_app_dir", return_value=self.app), \
                mock.patch.object(Path, "home", return_value=self.home), contextlib.redirect_stdout(out):
            self.assertEqual(chief.main(["tail", "--json"], self.ws, {}, AT), 0)
        [s] = json.loads(out.getvalue())["sessions"]
        for stdin, code in ((json.dumps([{"id": s["id"], "fingerprint": s["fingerprint"], "tldr": "ok", "chips": []}]), 0),
                            ("{bad", 2), (json.dumps({"x": 1}), 2)):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(chief, "default_app_dir", return_value=self.app), \
                    mock.patch.object(Path, "home", return_value=self.home), \
                    mock.patch("sys.stdin", io.StringIO(stdin)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(chief.main(["tldr", "--stdin"], self.ws, {}, AT), code, stdin)
            if code == 0:
                self.assertEqual(json.loads(out.getvalue()), {"stored": 1, "cards": {CLI_A: {"tldr": "ok", "chips": [], "at": hub._iso(AT)}}})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(chief.main(["tldr"], self.ws, {}, AT), 2)
        doc = json.loads((chief.chief_dir(self.ws) / "tldr.json").read_text())
        self.assertEqual(list(doc), [s["id"]])


class TestModelSpendAndText(Base):
    inbox = TestTick.inbox

    def spend(self, stdin, cfg=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chief.main(["spend", "--stdin"], self.ws, cfg or {}, AT)
        return code, out.getvalue()

    def spent(self):
        return chief.spent_today(self.ws, AT, UTC)


    def test_spend_counts_model_calls(self):
        self.assertEqual(chief.MODEL_PRICES, {"sonnet": (3, 15), "opus": (15, 75)})
        code, out = self.spend(json.dumps({"model": "sonnet", "input_tokens": 1000000, "output_tokens": 1000000}))
        self.assertEqual(code, 0)
        self.assertAlmostEqual(json.loads(out)["added"], 18.0)
        self.assertAlmostEqual(self.spent(), 18.0)
        self.spend(json.dumps({"model": "opus", "input_tokens": 1000, "output_tokens": 1000}))
        self.assertAlmostEqual(self.spent(), 18.0 + 0.015 + 0.075)

    def test_spend_without_usage_counts_the_tldr_cap(self):
        for doc in ({"model": "sonnet"}, {"model": "sonnet", "input_tokens": None, "output_tokens": 5},
                    {"model": "sonnet", "input_tokens": -1, "output_tokens": 5},
                    {"model": "sonnet", "input_tokens": True, "output_tokens": 5}):
            before = self.spent()
            self.assertEqual(self.spend(json.dumps(doc))[0], 0)
            self.assertAlmostEqual(self.spent() - before, chief.TLDR_MAX_TOKENS * 15 / 1e6, places=5, msg=doc)

    def test_spend_unknown_model_counts_at_the_dearest_price(self):
        self.spend(json.dumps({"model": "other", "input_tokens": 1000000, "output_tokens": 0}))
        self.assertAlmostEqual(self.spent(), 15.0)
        self.assertEqual(self.spend(json.dumps({"model": ["x"], "input_tokens": 1000000, "output_tokens": 0}))[0], 0)
        self.assertAlmostEqual(self.spent(), 30.0)

    def test_spend_usage_errors(self):
        for stdin in ("{bad", "[]", json.dumps("x")):
            self.assertEqual(self.spend(stdin)[0], 2, stdin)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(chief.main(["spend"], self.ws, {}, AT), 2)
        self.assertEqual(self.spent(), 0.0)

    def test_spend_never_overwrites_an_unreadable_file(self):
        path = chief.chief_dir(self.ws) / "spend.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{torn")
        self.assertEqual(self.spend(json.dumps({"model": "sonnet", "input_tokens": 1, "output_tokens": 1}))[0], 1)
        self.assertEqual(path.read_text(), "{torn")   # still refuses runs, rather than reading $0 from here on

    def test_spend_reaches_the_budget(self):
        cfg = {"chief": {"daily_budget_usd": 1}}
        self.assertIs(chief.status_doc(self.ws, cfg, AT)["budget"]["over"], False)
        self.spend(json.dumps({"model": "opus", "input_tokens": 0, "output_tokens": 20000}), cfg)
        self.assertIs(chief.status_doc(self.ws, cfg, AT)["budget"]["over"], True)



    def test_triage_haiku_config_reads_sonnet(self):
        self.assertEqual(chief.settings({"chief": {"triage_model": "haiku"}})["triage_model"], "sonnet")
        self.assertEqual(chief.settings({"chief": {"triage_model": "opus"}})["triage_model"], "opus")

    def test_safe_strips_line_separators(self):
        self.assertEqual(chief._safe("a\u2028b\u2029c\nd"), "abc\nd")

    def test_safe_tab_to_space(self):
        self.assertEqual(chief._safe("a\tb"), "a b")
        self.assertEqual(chief.clean_tldr("a\tb", [])["tldr"], "a b")


class PortraitTest(Base):
    """`mother chief portrait --json`: the workspace's own picture when it is a small PNG, else the bundled one."""

    @staticmethod
    def png(width, height, pad=0):
        return (chief.PNG_MAGIC + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")
                + b"\x08\x06\x00\x00\x00" + b"\x00" * (4 + pad))

    def custom(self, data):
        (self.ws / ".mother").mkdir(exist_ok=True)
        (self.ws / ".mother" / "portrait.png").write_bytes(data)

    def run_verb(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = chief.main(["portrait", "--json"], self.ws, {}, AT)
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None)

    def test_portrait_custom_bundled_oversized_and_not_a_png(self):
        code, doc = self.run_verb()                              # no custom file: the bundled picture
        self.assertEqual((code, doc["version"], doc["source"], doc["width"], doc["height"]), (0, 1, "bundled", 256, 256))
        self.assertEqual(base64.b64decode(doc["png"]), chief.BUNDLED_PORTRAIT.read_bytes())
        wide = self.png(800, 450)
        self.custom(wide)
        code, doc = self.run_verb()
        self.assertEqual((code, doc["source"], doc["width"], doc["height"]), (0, "custom", 800, 450))
        self.assertEqual(base64.b64decode(doc["png"]), wide)
        self.custom(self.png(800, 450, pad=chief.PORTRAIT_MAX))  # past 2 MiB: the bundled one
        self.assertEqual(self.run_verb()[1]["source"], "bundled")
        self.custom(b"GIF89a" + b"\x00" * 64)                    # not a PNG: the bundled one
        self.assertEqual(self.run_verb()[1]["source"], "bundled")
        self.assertIsNone(chief.portrait_doc(self.ws, self.ws / "missing.png"))   # neither reads
        with mock.patch.object(chief, "BUNDLED_PORTRAIT", self.ws / "missing.png"), \
                mock.patch.object(chief.portrait_doc, "__defaults__", (self.ws / "missing.png", chief.BUNDLED_SVG)):
            self.assertEqual(self.run_verb(), (1, None))

    @staticmethod
    def jpeg(width, height, pad=0, sof=0xC2):
        app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
        frame = bytes([0xFF, sof]) + (11).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00"
        return b"\xff\xd8" + app0 + frame + b"\x00" * pad + b"\xff\xd9"

    def test_portrait_jpeg_and_the_bundled_svg(self):
        _, doc = self.run_verb()                                 # bundled: the vector's own markup, no jpeg
        self.assertEqual(doc["svg"], chief.BUNDLED_SVG.read_text(encoding="utf-8"))
        self.assertTrue(doc["svg"].lstrip().startswith("<svg"))
        self.assertNotIn("jpeg", doc)
        (self.ws / ".mother").mkdir(exist_ok=True)
        small = self.jpeg(800, 450)
        (self.ws / ".mother" / "portrait.jpg").write_bytes(small)
        _, doc = self.run_verb()                                 # a custom jpeg alone: custom, over the bundled png
        self.assertEqual((doc["source"], doc["jpeg_width"], doc["jpeg_height"], doc["width"]), ("custom", 800, 450, 256))
        self.assertEqual(base64.b64decode(doc["jpeg"]), small)
        self.assertIn("svg", doc)                                # still there
        (self.ws / ".mother" / "portrait.jpg").write_bytes(self.jpeg(640, 480, sof=0xC0))
        self.assertEqual(self.run_verb()[1]["jpeg_height"], 480)  # a baseline frame reads too
        self.custom(self.png(800, 450))
        (self.ws / ".mother" / "portrait.jpg").write_bytes(self.jpeg(800, 450, pad=100_000))   # past 120000 base64 characters
        _, doc = self.run_verb()
        self.assertEqual((doc["source"], doc["width"], "jpeg" in doc, "svg" in doc), ("custom", 800, False, True))
        (self.ws / ".mother" / "portrait.jpg").write_bytes(b"not a jpeg at all")
        self.assertNotIn("jpeg", self.run_verb()[1])


if __name__ == "__main__":
    unittest.main()


class TestCases(Base):
    """A case: a session that owns one inbox item (claim), gets its follow-ups (relays) and closes it (hub done)."""
    A = "11111111-1111-4111-8111-111111111111"
    B = "22222222-2222-4222-8222-222222222222"

    def now_item(self, key, at=AT, title="t"):
        a = line(key=key, title=title, at=hub._iso(at))
        hub.append_inbox(self.ws, [a])
        return hub.decide(self.ws, {}, at, [{"line_id": a["id"], "key": key, "decision": "now", "title": title, "why": "w"}])[0]

    def follow_up(self, key, n, at=AT, who="Dana <U0DDDDDDD>", summary="any news?"):
        a = line(key=key, at=hub._iso(at + timedelta(seconds=n)), who=who, summary=summary, title="re")
        hub.append_inbox(self.ws, [a])
        chief.triage_decide(self.ws, {}, at + timedelta(seconds=n), [a["id"]], [])
        return a

    def claim(self, key, cli, at=AT, **kw):
        return chief.claim(self.ws, {}, at, dict({"key": key, "session_id": cli, "title": "Fix it", "link": "https://example.com/1"}, **kw))

    def rows(self):
        return hub._sessions_doc(self.ws)["sessions"]

    def test_claim_makes_a_live_case_and_links_the_update(self):
        uid = self.now_item("PLAT-7")["update_id"]
        got = self.claim("PLAT-7", self.A)
        self.assertEqual(got, {"ok": True, "key": "PLAT-7", "session_id": self.A, "update_id": uid, "also": []})
        r = self.rows()["PLAT-7"]
        self.assertEqual((r["state"], r["kind"], r["cli_session_id"], r["session_id"], r["opened_at"], r["unseen"]),
                         ("live", "case", self.A, self.A, hub._iso(AT), 0))
        u = hub._hub_doc(self.ws)["updates"][0]
        self.assertEqual((u["id"], u["state"], u["session_id"]), (uid, "open", self.A))   # kept, and linked
        item = chief.inbox_items(self.ws)[0]
        self.assertEqual(item["session"], {"key": "PLAT-7", "title": "Fix it", "kind": "case", "cli_session_id": self.A})
        self.assertEqual(self.claim("PLAT-7", self.A)["key"], "PLAT-7")                    # again, same session: fine

    def test_claim_refusals(self):
        for doc, why in (({"key": "PLAT-7", "session_id": self.A}, "no open inbox item"),
                         ({"key": "bad key", "session_id": self.A}, "not a hub key"),
                         ({"key": "PLAT-7", "session_id": "nope"}, "not a session id"), ([], "JSON object")):
            with self.assertRaises(ValueError) as e:
                chief.claim(self.ws, {}, AT, doc)
            self.assertIn(why, str(e.exception))
        self.assertEqual(self.rows(), {})

    def test_claim_repoint_refused_until_the_row_is_stale(self):
        self.now_item("PLAT-7")
        self.claim("PLAT-7", self.A)
        with self.assertRaises(ValueError) as e:
            self.claim("PLAT-7", self.B, at=AT + timedelta(days=6))
        self.assertIn("live in another session", str(e.exception))
        self.assertEqual(self.rows()["PLAT-7"]["cli_session_id"], self.A)
        self.claim("PLAT-7", self.B, at=AT + timedelta(days=8))                            # older than 7 days: allowed
        r = self.rows()["PLAT-7"]
        self.assertEqual((r["cli_session_id"], r["state"], r["opened_at"]), (self.B, "live", hub._iso(AT + timedelta(days=8))))

    def ack(self, cli, lines):
        return chief.relay_ack(self.ws, AT, cli, [l["id"] for l in lines])

    def test_relays_read_without_removing_and_ack_only_this_sessions(self):
        self.now_item("PLAT-7")
        self.now_item("PLAT-8")
        self.claim("PLAT-7", self.A)
        self.claim("PLAT-8", self.B)
        a = self.follow_up("PLAT-7", 10)
        b = self.follow_up("PLAT-8", 20)
        self.assertEqual(self.rows()["PLAT-7"]["unseen"], 1)
        self.assertEqual(len(hub._hub_doc(self.ws)["updates"]), 2)                         # a route, no new item
        got = chief.relays(self.ws, AT, self.A)
        self.assertEqual(got, [{"id": a["id"], "key": "PLAT-7", "at": hub._iso(AT + timedelta(seconds=10)), "who": "Dana",
                                "title": "re", "summary": "any news?", "link": a["link"]}])
        self.assertEqual(chief.relays(self.ws, AT, self.A), got)                           # a read removes nothing
        self.assertEqual((self.rows()["PLAT-7"]["unseen"], chief.cases(self.ws)[0]["unseen"]), (1, 1))
        self.assertEqual(chief.relay_ack(self.ws, AT, self.A, [b["id"], "nope", 7]), 0)    # another session's line: ignored
        self.assertEqual([c["unseen"] for c in chief.cases(self.ws)], [1, 1])
        self.assertEqual(self.ack(self.A, got), 1)
        self.assertEqual(self.ack(self.A, got), 0)                                         # acknowledged once
        self.assertEqual(chief.relays(self.ws, AT, self.A), [])
        self.assertEqual(self.rows()["PLAT-7"]["unseen"], 0)
        self.assertEqual(self.rows()["PLAT-8"]["unseen"], 1)                               # the other session's still waits
        self.assertEqual([c["unseen"] for c in chief.cases(self.ws)], [0, 1])
        for n in range(30, 42):
            self.follow_up("PLAT-7", n)
        more = chief.relays(self.ws, AT, self.A)
        self.assertEqual(len(more), 12)
        self.assertEqual(self.ack(self.A, more[:10]), 10)                                  # only the ids named
        self.assertEqual(self.rows()["PLAT-7"]["unseen"], 2)
        self.assertEqual(chief.relays(self.ws, AT, self.A), more[10:])

    def test_relay_lines_survive_the_hubs_routed_cap_and_are_cleaned(self):
        self.now_item("PLAT-7")
        self.claim("PLAT-7", self.A)
        self.follow_up("PLAT-7", 5, who="Da\u202ena", summary="x" * 500)
        for n in range(hub.ROUTED_MAX + 5):                                                # push it out of hub.json's routed
            l = line(key="LOG-%d" % n, at=hub._iso(AT + timedelta(seconds=100 + n)))
            hub.append_inbox(self.ws, [l])
            hub.decide(self.ws, {}, AT, [{"line_id": l["id"], "key": l["key"], "decision": "log"}])
        got = chief.relays(self.ws, AT, self.A)
        self.assertEqual([(g["who"], len(g["summary"])) for g in got], [("Dana", 300)])

    def test_relay_flag_and_the_deliver_once_flag(self):
        self.now_item("PLAT-7")
        self.claim("PLAT-7", self.A)
        self.follow_up("PLAT-7", 10)
        self.assertTrue(chief.relay_on(self.ws))
        chief.set_relay(self.ws, False)
        self.assertFalse(chief.status_doc(self.ws, {}, AT)["relay"]["on"])
        self.assertEqual(chief.cases(self.ws)[0]["unseen"], 1)                             # still waiting, shown as N new
        chief.relay_deliver(self.ws, self.A)
        self.assertIs(chief.cases(self.ws)[0]["deliver_now"], True)
        self.assertEqual(self.ack(self.A, chief.relays(self.ws, AT, self.A)), 1)           # delivered: the flag is used up
        self.assertIs(chief.cases(self.ws)[0]["deliver_now"], False)
        chief.set_relay(self.ws, True)
        self.assertTrue(chief.relay_on(self.ws))

    def test_done_closes_the_case_and_later_activity_is_a_new_item(self):
        uid = self.now_item("PLAT-7")["update_id"]
        self.claim("PLAT-7", self.A)
        later = AT + timedelta(days=3)                                                     # past the item's TTL: a case's item stays
        self.follow_up("PLAT-7", 10, at=later)
        self.assertEqual([u["id"] for u in hub._hub_doc(self.ws)["updates"]], [uid])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub.main(["done", uid], self.ws, {}, later), 0)
        self.assertEqual(self.rows()["PLAT-7"]["state"], "done")
        self.assertEqual(chief.cases(self.ws), [])
        a = self.follow_up("PLAT-7", 60, at=later)
        res = [r for r in hub._hub_doc(self.ws)["routed"] if r["line_id"] == a["id"]][0]
        self.assertEqual(res["decision"], "now")                                           # a new Inbox card, not a route
        self.assertEqual(chief.relays(self.ws, later, self.A), [])

    def test_status_cases_and_the_cli_verbs(self):
        self.now_item("PLAT-7")

        def run(*args, stdin=""):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = chief.main(list(args), self.ws, {}, AT)
            return code, out.getvalue(), err.getvalue()

        body = json.dumps({"key": "PLAT-7", "session_id": self.A, "title": "Fix it", "link": "https://example.com/1"})
        self.assertEqual(run("claim", "--stdin", stdin=body)[0], 0)
        code, _, err = run("claim", "--stdin", stdin=body.replace(self.A, self.B))
        self.assertEqual((code, "refusing to repoint" in err), (1, True))
        self.assertEqual(run("claim")[0], 2)
        self.follow_up("PLAT-7", 10)
        doc = chief.status_doc(self.ws, {}, AT)
        self.assertEqual(doc["relay"], {"on": True})
        self.assertEqual(doc["cases"], [{"key": "PLAT-7", "cli_session_id": self.A, "title": "Fix it", "link": "https://example.com/1",
                                         "links": [{"label": "issue", "url": "https://example.com/1"}], "also": [],
                                         "label": "PLAT-7", "unseen": 1, "last_routed_at": hub._iso(AT + timedelta(seconds=10)),
                                         "opened_at": hub._iso(AT), "deliver_now": False}])
        self.assertEqual(run("relay", "off")[1], "relay off: 1 line(s) waiting\n")
        self.assertEqual(run("relay", "deliver", "--for", self.A)[0], 0)
        waiting = json.loads(run("relays", "--for", self.A)[1])
        self.assertEqual(len(waiting), 1)
        self.assertEqual(run("relays", "--for", self.A, "--ack")[0], 2)                    # the ids come on stdin
        self.assertEqual(run("relays", "--for", self.A, "--ack", "--stdin", stdin="{}")[0], 2)
        self.assertEqual(run("relays", "--for", self.A, "--ack", "--stdin", stdin=json.dumps([waiting[0]["id"]]))[1], '{"acked": 1}\n')
        self.assertEqual(json.loads(run("relays", "--for", self.A)[1]), [])
        self.assertEqual(run("relay", "on")[1], "relay on\n")
        self.assertEqual(run("relay", "status")[1], "relay on\n")
        self.assertEqual([run("relay")[0], run("relay", "maybe")[0], run("relays")[0], run("relay", "deliver")[0]], [2, 2, 2, 2])


class TestLinks(Base):
    """Where an item came from: built from its key only when a base is known."""
    def test_no_base_no_link(self):
        bases = chief.link_bases(self.ws, {})
        self.assertEqual(bases, {"chat": None, "tracker": None, "code": None})
        for key in ("C00000001/1700000000.123456", "PLAT-7", "acme/api#12", "mail:abc", "cal:ev1", "manual:abc", "wa:1@g.us"):
            self.assertEqual(chief.item_links(key, "", [], "", bases), ("", []), key)

    def test_bases_from_config_and_stored_links(self):
        cfg = {"connectors": {"code": "github"}, "teams": [{"tracker": {"project": "PLAT", "url": "https://tracker.example.com/browse/"}}]}
        self.assertEqual(chief.link_bases(self.ws, cfg), {"chat": None, "tracker": "https://tracker.example.com", "code": "https://github.com"})
        hub.append_inbox(self.ws, [line(key="C00000002/1700000001.000100", link="https://chat.example.com/archives/C00000002/p1700000001000100"),
                                   line(key="OPS-1", link="https://jira.example.com/browse/OPS-1")])
        self.assertEqual(chief.link_bases(self.ws, {}), {"chat": "https://chat.example.com", "tracker": "https://jira.example.com", "code": None})
        self.assertEqual(chief.link_bases(self.ws, {"hub": {"connections": {"code": {"cli": "gh"}}}})["code"], "https://github.com")

    def test_source_link_shapes(self):
        b = {"chat": "https://chat.example.com", "tracker": "https://jira.example.com", "code": "https://github.com"}
        self.assertEqual(chief.source_link("C00000001/1700000000.123456", b), "https://chat.example.com/archives/C00000001/p1700000000123456")
        self.assertEqual(chief.source_link("D00000001/dm", b), "https://chat.example.com/archives/D00000001")
        self.assertEqual(chief.source_link("C00000001/17.5", b), "")          # not a message stamp: no guess
        self.assertEqual(chief.source_link("PLAT-7", b), "https://jira.example.com/browse/PLAT-7")
        self.assertEqual(chief.source_link("acme/api#12", b), "https://github.com/acme/api/pull/12")
        for key in ("mail:abc", "cal:ev1", "manual:abc", "wa:1@g.us", "watcher:chat"):
            self.assertEqual(chief.source_link(key, b), "", key)

    def test_item_links_source_first_then_also_and_text(self):
        b = {"chat": "https://chat.example.com", "tracker": "https://jira.example.com", "code": None}
        link, links = chief.item_links("C00000001/1700000000.123456", "", ["PLAT-7", "acme/api#12"],
                                       "see https://docs.example.com/page, and https://jira.example.com/browse/PLAT-7.", b)
        self.assertEqual(link, "https://chat.example.com/archives/C00000001/p1700000000123456")
        self.assertEqual(links, [{"label": "thread", "url": link}, {"label": "PLAT-7", "url": "https://jira.example.com/browse/PLAT-7"},
                                 {"label": "docs.example.com", "url": "https://docs.example.com/page"}])
        # the watcher's own link wins over a built one; mail and calendar keep it
        self.assertEqual(chief.item_links("mail:abc", "https://mail.example.com/#inbox/abc", [], "", b),
                         ("https://mail.example.com/#inbox/abc", [{"label": "mail", "url": "https://mail.example.com/#inbox/abc"}]))
        self.assertEqual(chief.item_links("PLAT-9", "javascript:alert(1)", [], "", {})[0], "")
        many = " ".join("https://example.com/%d" % n for n in range(20))
        self.assertEqual(len(chief.item_links("manual:x", "", [], many, b)[1]), chief.LINKS_MAX)

    def test_status_inbox_items_carry_link_and_links(self):
        a = line(key="C00000001/1700000000.123456", title="ping", summary="about PLAT-7", also=["PLAT-7"], link="")
        seed = line(key="PLAT-1", link="https://jira.example.com/browse/PLAT-1")
        chat = line(key="C00000009/1700000009.000001", link="https://chat.example.com/archives/C00000009/p1700000009000001")
        hub.append_inbox(self.ws, [seed, chat, a])
        hub.decide(self.ws, {}, AT, [{"line_id": a["id"], "key": a["key"], "decision": "now", "title": "ping", "why": "w"}])
        [u] = chief.status_doc(self.ws, {}, AT)["inbox"]
        self.assertEqual(u["link"], "https://chat.example.com/archives/C00000001/p1700000000123456")
        self.assertEqual(u["links"], [{"label": "thread", "url": u["link"]}, {"label": "PLAT-7", "url": "https://jira.example.com/browse/PLAT-7"}])


class TestAsk(TestCases):
    """The ask box: a manual line whose item is held for the case the pane starts."""
    main = TestLeader.main

    def decide_pending(self, at):
        ids = chief.tick(self.ws, {}, at)["judge"]
        return chief.triage_decide(self.ws, {}, at, ids, [])

    def test_hub_add_json_names_the_key(self):
        out = io.StringIO()
        self.assertEqual(hub.add_cmd(self.ws, AT, ["look", "at", "PLAT-7"], out, as_json=True), 0)
        got = json.loads(out.getvalue())
        self.assertEqual((got["ok"], got["key"], got["link"]), (True, "PLAT-7", ""))
        self.assertEqual(got["line_id"], hub.read_inbox(self.ws)[-1]["id"])

    def test_ask_holds_the_item_out_of_the_inbox_for_ten_minutes(self):
        got = chief.ask(self.ws, {}, AT, "Who owns the billing migration?")
        self.assertTrue(got["key"].startswith("manual:"))
        self.assertEqual((got["update_id"], got["owned_by"]), (None, None))
        self.decide_pending(AT + timedelta(seconds=30))                    # the leader makes it a `now` item
        self.assertEqual(len([u for u in hub._hub_doc(self.ws)["updates"] if u["state"] == "open"]), 1)
        self.assertEqual(chief.status_doc(self.ws, {}, AT + timedelta(minutes=9))["inbox"], [])
        late = chief.status_doc(self.ws, {}, AT + timedelta(minutes=10, seconds=1))   # never started: a normal manual item
        self.assertEqual([u["key"] for u in late["inbox"]], [got["key"]])

    def test_claim_before_the_leader_decides_routes_nothing_back(self):
        got = chief.ask(self.ws, {}, AT, "Chase the vendor about the invoice")
        res = chief.claim(self.ws, {}, AT, {"key": got["key"], "session_id": self.A, "title": "Chase the vendor", "link": None})
        self.assertEqual((res["ok"], res["update_id"]), (True, None))
        self.decide_pending(AT + timedelta(seconds=30))                    # a live case: stored as a route, no item
        self.assertEqual(hub._hub_doc(self.ws)["updates"], [])
        self.assertEqual(chief.relays(self.ws, AT, self.A), [])            # the manager's own words are not relayed back
        self.assertEqual(chief.status_doc(self.ws, {}, AT + timedelta(hours=1))["inbox"], [])
        self.follow_up(got["key"], 60)                                     # later activity on the key is
        self.assertEqual(len(chief.relays(self.ws, AT, self.A)), 1)

    def test_claim_after_the_leader_decided_closes_the_item(self):
        got = chief.ask(self.ws, {}, AT, "Chase the vendor about the invoice")
        self.decide_pending(AT + timedelta(seconds=30))
        chief.claim(self.ws, {}, AT + timedelta(minutes=1), {"key": got["key"], "session_id": self.A, "title": "t", "link": None})
        self.assertEqual([u for u in hub._hub_doc(self.ws)["updates"] if u["state"] != "done"], [])
        self.assertEqual(chief.status_doc(self.ws, {}, AT + timedelta(hours=1))["inbox"], [])
        self.assertEqual([c["key"] for c in chief.cases(self.ws)], [got["key"]])

    def test_ask_about_a_key_that_has_an_item_or_a_case(self):
        uid = self.now_item("PLAT-7")["update_id"]
        got = chief.ask(self.ws, {}, AT, "what about PLAT-7?")
        self.assertEqual((got["key"], got["update_id"], got["owned_by"]), ("PLAT-7", uid, None))
        self.assertEqual([u["id"] for u in chief.status_doc(self.ws, {}, AT)["inbox"]], [uid])   # its own card stays
        self.claim("PLAT-7", self.A)
        self.assertEqual(chief.ask(self.ws, {}, AT, "and PLAT-7 again")["owned_by"], self.A)

    def test_ask_cli_and_empty_text(self):
        code, out, _ = self.main("ask", "--stdin", stdin=json.dumps({"text": "Find the owner of acme/api#12"}))
        self.assertEqual((code, json.loads(out)["key"]), (0, "acme/api#12"))
        code, _, err = self.main("ask", "--stdin", stdin=json.dumps({"text": "   "}))
        self.assertEqual((code, "no text" in err), (1, True))
        self.assertEqual(self.main("ask")[0], 2)

    def test_claim_with_also_and_watch_key(self):
        self.now_item("PLAT-7")
        got = self.claim("PLAT-7", self.A, also=["C00000001/1700000000.123456", "PLAT-7"])
        self.assertEqual(got["also"], ["C00000001/1700000000.123456"])
        self.follow_up("C00000001/1700000000.123456", 10)                 # a line of the second key reaches the case
        self.assertEqual([r["key"] for r in chief.relays(self.ws, AT, self.A)], ["C00000001/1700000000.123456"])
        got = chief.watch_key(self.ws, {}, AT, "acme/api#12", self.A)
        self.assertEqual((got["key"], got["also"]), ("PLAT-7", ["C00000001/1700000000.123456", "acme/api#12"]))
        self.assertEqual(chief.watch_key(self.ws, {}, AT, "acme/api#12", self.A)["also"], got["also"])   # twice: once
        self.assertEqual(chief.cases(self.ws)[0]["also"], got["also"])
        with self.assertRaisesRegex(ValueError, "holds no live case"):
            chief.watch_key(self.ws, {}, AT, "PLAT-8", self.B)
        with self.assertRaisesRegex(ValueError, "not a hub key"):
            chief.watch_key(self.ws, {}, AT, "has space", self.A)
        self.now_item("PLAT-9")
        self.claim("PLAT-9", self.B)
        with self.assertRaisesRegex(ValueError, "refusing to repoint"):
            chief.watch_key(self.ws, {}, AT, "acme/api#12", self.B)
        with self.assertRaisesRegex(ValueError, "refusing to repoint"):
            self.claim("PLAT-9", self.B, also=["PLAT-7"])
        with self.assertRaisesRegex(ValueError, "also must be"):
            self.claim("PLAT-9", self.B, also="PLAT-1")
        code, out, _ = self.main("watch-key", "OPS-3", "--for", self.B)
        self.assertEqual((code, json.loads(out)["also"]), (0, ["OPS-3"]))
        self.assertEqual(self.main("watch-key", "OPS-4", "--for", "nope")[0], 1)
        self.assertEqual(self.main("watch-key")[0], 2)

    def test_close_case_ends_the_case_and_its_items(self):
        uid = self.now_item("PLAT-7")["update_id"]
        self.claim("PLAT-7", self.A, also=["acme/api#12"])
        code, out, _ = self.main("close-case", "acme/api#12")
        self.assertEqual((code, json.loads(out)["updates"]), (0, [uid]))
        self.assertEqual((self.rows()["PLAT-7"]["state"], chief.cases(self.ws)), ("done", []))
        self.assertEqual(self.main("close-case", "PLAT-7")[0], 1)


class TestDotsDetail(Base):
    main = TestLeader.main
    FILE = """# Dots — 2026-10-30   (window a → b)

## For you
1. Reply to the vendor — because two threads ask
   - chat: thread — https://example.com/t
2. Decide the cutoff — because the plan and the ticket disagree

## For me (on your yes)
- A1 Draft a nudge to the owner of PLAT-7 — because it is stale
- A23 Annotate 7 stale todos and intake lines (no closes) — because none moved in 14 days — todos/open.md
   - adds `stale since <date>` to each line
- A3 x

## Conflicts
- cutoff: plan says Thursday · ticket says Friday (see A23)
"""

    def write(self, rel, text):
        p = self.ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_found_in_the_newest_file_with_what_names_it(self):
        self.write("journal/dots/2026-10-29.md", "## For me\n- A23 Old action — because old\n")
        self.write("journal/dots/2026-10-30.md", self.FILE)
        got = chief.dots_detail(self.ws, "A23")
        self.assertEqual((got["id"], got["date"], got["source_file"]), ("A23", "2026-10-30", "journal/dots/2026-10-30.md"))
        self.assertEqual(got["text"], "- A23 Annotate 7 stale todos and intake lines (no closes) — because none moved in 14 days — todos/open.md\n"
                                      "   - adds `stale since <date>` to each line\n"
                                      "- cutoff: plan says Thursday · ticket says Friday (see A23)")
        self.assertEqual(got["label"], "Annotate 7 stale todos and intake lines")
        self.assertEqual(chief.dots_detail(self.ws, "A3")["label"], None)          # one word: the button keeps its label
        m = chief.dots_detail(self.ws, "M2")
        self.assertEqual((m["text"], m["label"]), ("2. Decide the cutoff — because the plan and the ticket disagree", "Decide the cutoff"))

    def test_reads_the_full_copy_first_then_the_undelivered_one(self):
        self.write(".mother/dots-unsent-2026-10-31.md", "For me\nA5 Check the release build — because it is red\n")
        self.assertEqual(chief.dots_detail(self.ws, "A5")["source_file"], ".mother/dots-unsent-2026-10-31.md")
        self.write("journal/dots/2026-10-31.md", "- A5 From the journal — x\n")
        self.write(".mother/reports/dots-2026-10-31.md", "- A5 From the full copy — x\n")
        self.assertEqual(chief.dots_detail(self.ws, "A5")["text"], "- A5 From the full copy — x")

    def test_not_found_and_capped(self):
        self.assertIsNone(chief.dots_detail(self.ws, "A1"))
        self.write("journal/dots/2026-10-30.md", self.FILE + "- A9 " + "word " * 600 + "\n")
        for bad in ("A99", "S1", "../A1", "", None, "A1 x"):
            self.assertIsNone(chief.dots_detail(self.ws, bad), bad)
        self.assertEqual(len(chief.dots_detail(self.ws, "A9")["text"]), chief.DOTS_TEXT_MAX)
        code, out, _ = self.main("dots-detail", "A99", "--json")
        self.assertEqual((code, json.loads(out)), (0, None))
        code, out, _ = self.main("dots-detail", "A1", "--json")
        self.assertEqual((code, json.loads(out)["label"]), (0, "Draft a nudge to the owner of PLAT-7"))
        self.assertEqual(self.main("dots-detail")[0], 2)


class TestSessionCards(TestTldr):
    """The refresh: what a transcript says in code, the merged card, and the floor card it fills."""
    def aged(self, p, seconds):
        t = AT.timestamp() - seconds
        os.utime(str(p), (t, t))

    def tails_at(self, cfg=None):
        return chief.session_tails(self.ws, cfg or {}, home=self.home, app_dir=self.app, at=AT)

    def store(self, rows, at=AT):
        cards = {}
        n = chief.tldr_store(self.ws, at, rows, home=self.home, app_dir=self.app, cards=cards)
        return n, cards

    def card(self, fp, **kw):
        return dict({"id": CLI_A, "fingerprint": fp, "model": "sonnet", "tldr": "Fixing it.", "chips": [], "state": "idle",
                     "progress": "", "asks": False, "question": "", "options": [], "waiting_on": None}, **kw)

    def test_waiting_line(self):
        self.assertEqual(chief.waiting_line("done\nWaiting on: the manager — pick 1 or 2"), {"state": "waiting_manager", "on": None, "what": "pick 1 or 2"})
        self.assertEqual(chief.waiting_line("Waiting on: Dana — her reply, I look again at 15:00"),
                         {"state": "waiting_other", "on": "Dana", "what": "her reply, I look again at 15:00"})
        self.assertEqual(chief.waiting_line("x\n`Waiting on: nothing — done`\n\n")["state"], "done")
        self.assertIsNone(chief.waiting_line("Waiting on: Dana — x\nand one more line"))
        self.assertIsNone(chief.waiting_line(""))

    def test_tail_rows_say_what_the_transcript_shows(self):
        self.pin()
        p = self.transcript("/work/api", CLI_A, [msg("user", "Fix it"), msg("assistant", [{"type": "text", "text": "On it.\nWaiting on: Dana — the key"}])])
        self.aged(p, 600)
        doc = self.tails_at({"chief": {"refresh_minutes": 7}})
        [s] = doc["sessions"]
        self.assertEqual(doc["refresh_minutes"], 7)
        self.assertEqual((s["running"], s["idle_for"], s["last_from"], s["open_turn"], s["error_end"], s["stuck"]),
                         (False, 600, "assistant", False, False, False))
        self.assertEqual(s["waiting"], {"state": "waiting_other", "on": "Dana", "what": "the key"})
        self.aged(p, 30)                                                    # it moved in the last 2 minutes
        self.assertTrue(self.tails_at()["sessions"][0]["running"])
        # a turn that has not ended: running while it moved in the last 30 minutes, stuck after
        p = self.transcript("/work/api", CLI_A, [msg("user", "Fix it"), msg("assistant", [{"type": "tool_use", "name": "Bash", "input": {}}])])
        self.aged(p, 600)
        s = self.tails_at()["sessions"][0]
        self.assertEqual((s["running"], s["open_turn"], s["last_from"], s["waiting"], s["stuck"]), (True, True, "assistant", None, False))
        self.aged(p, 1801)
        s = self.tails_at()["sessions"][0]
        self.assertEqual((s["running"], s["stuck"]), (False, True))
        p = self.transcript("/work/api", CLI_A, [msg("user", "go"), msg("assistant", [{"type": "text", "text": "API Error: rate limit reached"}])])
        self.aged(p, 600)
        self.assertEqual([self.tails_at()["sessions"][0][k] for k in ("error_end", "stuck")], [True, True])

    def test_case_sessions_are_refreshed_too(self):
        self.seed_sessions({"PLAT-7": {"session_id": CLI_B, "cli_session_id": CLI_B, "kind": "case", "state": "live", "title": "t"}})
        self.transcript(self.ws, CLI_B, [msg("user", "Mother case: t"), msg("assistant", "Reading the thread.")])
        [s] = self.tails_at()["sessions"]
        self.assertEqual((s["id"], s["kind"]), (CLI_B, "case"))

    def test_stored_card_fields_and_code_overrides(self):
        self.pin()
        p = self.transcript("/work/api", CLI_A, [msg("user", "Fix it"), msg("assistant", [{"type": "text", "text": "Sent.\nWaiting on: Dana — the key"}])])
        self.aged(p, 600)
        fp = self.tails_at()["sessions"][0]["fingerprint"]
        n, cards = self.store([self.card(fp, state="waiting_manager", progress="Sent the mail.\nmore " + "x" * 300, asks=True,
                                         question="Send it?", options=[{"label": "Yes", "message": "Send it"}], waiting_on="you")])
        c = cards[CLI_A]
        self.assertEqual(n, 1)
        self.assertEqual((c["state"], c["waiting_on"], c["asks"], c["question"], c["options"], c["prev_state"]),
                         ("waiting_other", "Dana", False, "", [], None))       # the `Waiting on:` line wins over the model
        self.assertEqual(len(c["progress"]), 160)
        s = self.tails_at()["sessions"][0]
        self.assertEqual((s["cached"]["state"], s["prev"]["state"], s["prev"]["at"], s["prev"]["fingerprint"]), ("waiting_other", "waiting_other", hub._iso(AT), fp))
        self.assertNotIn("at", s["cached"])
        _, cards = self.store([self.card(fp, state="bogus")])
        self.assertEqual((cards[CLI_A]["state"], cards[CLI_A]["prev_state"]), ("waiting_other", "waiting_other"))
        # no such line, and running: `working` from code; an unknown state reads idle otherwise
        p = self.transcript("/work/api", CLI_A, [msg("user", "Fix it"), msg("assistant", "Patching.")])
        self.aged(p, 10)
        fp = self.tails_at()["sessions"][0]["fingerprint"]
        self.assertEqual(self.store([self.card(fp, state="idle")])[1][CLI_A]["state"], "working")
        self.aged(p, 600)
        self.assertEqual(self.store([self.card(fp, state="bogus")])[1][CLI_A]["state"], "idle")
        # twice `working`, then nothing moves for 30 minutes: stuck
        self.aged(p, 10)
        self.store([self.card(fp)])
        self.store([self.card(fp)])
        self.aged(p, 1900)
        self.assertTrue(self.tails_at()["sessions"][0]["stuck"])

    def test_an_asleep_sessions_ask_fills_its_floor_card(self):
        import floor
        self.pin()
        p = self.transcript("/work/api", CLI_A, [msg("user", "Fix it"), msg("assistant", "Should I send it?")])
        self.aged(p, 600)
        fp = self.tails_at()["sessions"][0]["fingerprint"]
        ask = dict(state="waiting_manager", asks=True, question="Send it?", options=[{"label": "Yes", "message": "Send it"}])
        self.store([self.card(fp, **ask)])
        [c] = floor.list_cards(self.ws, AT)
        self.assertEqual((c["session_id"], c["asks"], c["question"], c["options"], c["at"]),
                         (CLI_A, True, "Send it?", [{"label": "Yes", "message": "Send it"}], hub._iso(AT)))
        # the session's own card, written after its transcript last moved, wins
        floor.write_card(self.ws, AT - timedelta(seconds=300), {"session_id": CLI_A, "asks": False, "question": "", "options": []})
        self.store([self.card(fp, **ask)])
        self.assertFalse(floor.list_cards(self.ws, AT)[0]["asks"])
        # the session moved on since its own card: the refresh's word stands, and a stale ask is cleared
        floor.write_card(self.ws, AT - timedelta(seconds=900), {"session_id": CLI_A, "asks": True, "question": "Old?", "options": []})
        self.store([self.card(fp, state="idle")])
        self.assertFalse(floor.list_cards(self.ws, AT)[0]["asks"])

class TestHabits(Base):
    """The actions log, the habits counted from it, and the quiet rule (lib/habits.py)."""
    CFG = {"agent": {"manager": "Dana Levi", "manager_short": "Dana"},
           "teams": [{"slug": "platform", "name": "Platform", "channels": [{"id": "C00000001", "name": "#platform"}]}]}
    CHAN = "C00000001"
    QUIET = dict(kind="fyi", on="other", urgency="later", actions=[{"label": "Read it", "prompt": "Read the thread"}])

    def run_cli(self, *args, stdin="", at=AT):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chief.main(list(args), self.ws, self.CFG, at)
        return code, out.getvalue(), err.getvalue()

    def log(self, at=AT, **row):
        return habits.append_actions(self.ws, at, [habits.clean_action(row, at, verbs=habits.VERBS + (habits.SHOWN,))])

    def history(self, key=None, who="Sam", ignored=4, shown=5, verb="ignore", start=0):
        """`shown` items of one bucket, `ignored` of them ignored (or closed), none engaged with."""
        for n in range(start, start + shown):
            k = key or "%s/%d.0" % (self.CHAN, 100 + n)
            self.log(verb="shown", item_id="U9%d" % n, key=k, who=who, kind="fyi")
            if n - start < ignored:
                self.log(verb=verb, item_id="U9%d" % n, key=k, who=who, kind="fyi")

    def decide(self, l, at=AT, cfg=None, **v):
        hub.append_inbox(self.ws, [l])
        cfg = self.CFG if cfg is None else cfg
        judge = chief.tick(self.ws, cfg, at, session="s-1")["judge"]
        verdicts = [] if v.get("none") else [verdict(l, **dict(self.QUIET, **v))]
        return chief.triage_decide(self.ws, cfg, at, judge, verdicts)

    def chat(self, n=1, **kw):
        d = dict(source="chat", key="%s/%d.0" % (self.CHAN, n), who="Sam <U00000009>", title="standup notes", summary="notes")
        d.update(kw)
        return line(**d)

    def test_log_shape_drops_unknown_keys_and_bad_verbs(self):
        code, out, _ = self.run_cli("act-log", "--stdin", stdin=json.dumps({
            "verb": "action", "item_id": "U4", "key": "%s/12.5" % self.CHAN, "who": "Sam <U00000009>", "kind": "act",
            "urgency": "today", "on": "team", "title": "x" * 90, "action_label": "Draft a reply", "action_index": 1,
            "summary": "a body", "why": "a body", "body": "a body", "at": "1999-01-01T00:00:00Z", "source": "mail"}))
        self.assertEqual((code, json.loads(out)), (0, {"logged": 1}))
        rows = json.loads(self.run_cli("act-log", "--json")[1])
        self.assertEqual(rows, [{"at": hub._iso(AT), "verb": "action", "item_id": "U4", "key": "%s/12.5" % self.CHAN,
                                 "source": "chat", "label": self.CHAN, "who": "Sam", "kind": "act", "urgency": "today",
                                 "on": "team", "title": "x" * 60, "action_label": "Draft a reply", "action_index": 1}])
        for bad in ({"verb": "shown"}, {"verb": "delete"}, {}, []):
            self.assertEqual(self.run_cli("act-log", "--stdin", stdin=json.dumps(bad))[0], 2, bad)
        self.assertEqual(self.run_cli("act-log", "--stdin", stdin="{no")[0], 2)
        self.assertEqual(self.run_cli("act-log")[0], 2)
        # a Today card has no hub key: its source and label are taken as given, cleaned
        self.log(verb="ask", item_id="tracker:PLAT-1", source="tracker", label="PLAT", title="t")
        self.assertEqual([(r["source"], r["label"]) for r in habits.actions(self.ws, AT)][-1], ("tracker", "PLAT"))
        self.assertEqual([habits.bucket_label(k) for k in ("PLAT-12", "acme/api#4", "D00000001/dm", "mail:abc", "wa:123@g.us", "wa:555")],
                         ["PLAT", "acme/api", "", "", "wa:123@g.us", ""])

    def test_log_prunes_by_age_and_count(self):
        self.log(at=AT - timedelta(days=91), verb="done", item_id="old")
        self.log(at=AT - timedelta(days=40), verb="done", item_id="mid")
        self.log(verb="done", item_id="new")
        self.assertEqual([r["item_id"] for r in habits.actions(self.ws, AT)], ["mid", "new"])
        self.assertEqual([r["item_id"] for r in json.loads(self.run_cli("act-log", "--json", "--days", "30")[1])], ["new"])
        with mock.patch.object(habits, "KEEP_LINES", 3):
            for n in range(5):
                self.log(verb="done", item_id="n%d" % n)
        self.assertEqual([r["item_id"] for r in habits.actions(self.ws, AT)], ["n2", "n3", "n4"])
        (chief.chief_dir(self.ws) / "actions.jsonl").write_text("{junk\n[1]\n" + json.dumps({"verb": "done", "at": "never"}) + "\n")
        self.assertEqual(habits.actions(self.ws, AT), [])                       # unreadable rows are skipped

    def test_habits_aggregation(self):
        k = "%s/1.0" % self.CHAN
        for iid, verbs in (("U1", ["shown", "ignore"]), ("U2", ["shown", "done"]), ("U3", ["shown", "snooze"]),
                           ("U4", ["shown", "open_case", "done"]), ("U5", ["shown"]), ("U6", ["shown", "snooze", "done"])):
            for v in verbs:
                self.log(verb=v, item_id=iid, key=k, who="Sam", kind="act")
        self.log(at=AT - timedelta(days=31), verb="ignore", item_id="U0", key=k, who="Sam", kind="act")   # outside 30 days
        doc = json.loads(self.run_cli("habits", "--json")[1])
        self.assertEqual((doc["version"], doc["days"], doc["on"], doc["exceptions"]), (1, 30, True, []))
        want = {"shown": 6, "engaged": 1, "closed_unread": 2, "ignored": 1, "snoozed": 1, "engage_rate": 0.167}
        got = {b["id"]: b for b in doc["buckets"]}
        self.assertEqual(sorted(got), ["chat|kind|act", "chat|label|%s" % self.CHAN, "chat|who|Sam"])
        for b in got.values():
            self.assertEqual({k2: b[k2] for k2 in want}, want, b["id"])
        self.assertEqual(habits.for_line(doc, "chat", k, "Sam <U00000009>"), {
            "this_channel": {"shown": 6, "engaged": 1, "ignored": 1, "closed_unread": 2},
            "this_person": {"shown": 6, "engaged": 1, "ignored": 1, "closed_unread": 2}})
        self.assertIsNone(habits.for_line(doc, "tracker", "PLAT-1", "Lee"))        # another bucket: nothing of this one
        self.assertEqual(json.loads(self.run_cli("habits", "--json")[1]), doc)     # deterministic

    def test_prefers_a_later_action_picked_more_than_the_first(self):
        for n, (idx, label) in enumerate([(1, "Draft reply"), (1, "draft reply"), (2, "Draft reply"), (0, "Open ticket"), (0, "Open ticket")]):
            self.log(verb="action", item_id="U%d" % n, key="PLAT-%d" % n, kind="act", action_label=label, action_index=idx)
        doc = habits.habits(self.ws, AT)
        self.assertEqual({b["id"]: b.get("prefers") for b in doc["buckets"]}, {"tracker|kind|act": "Draft reply", "tracker|label|PLAT": None})
        self.assertEqual(habits.for_line(doc, "tracker", "PLAT-50", "")["prefers"], {"act": "Draft reply"})
        self.log(verb="action", item_id="U8", key="PLAT-8", kind="act", action_label="Open ticket", action_index=0)
        self.assertIsNone(habits.habits(self.ws, AT)["buckets"][0].get("prefers"))   # 3 against 3: not more

    def test_lines_carry_only_their_own_buckets(self):
        self.history()
        self.history(key="PLAT-1", who="Lee", start=20)
        hub.append_inbox(self.ws, [self.chat(), line(key="OTHER-1", who="Kim")])
        got = {l["key"]: l["habits"] for l in chief.lines(self.ws, self.CFG)}
        self.assertEqual(got, {"%s/1.0" % self.CHAN: {"this_channel": {"shown": 5, "engaged": 0, "ignored": 4, "closed_unread": 0},
                                                      "this_person": {"shown": 5, "engaged": 0, "ignored": 4, "closed_unread": 0}},
                               "OTHER-1": None})
        habits.set_on(self.ws, False)
        self.assertEqual({l["habits"] for l in chief.lines(self.ws, self.CFG)}, {None})

    def test_quiet_rule_logs_the_line_and_lists_it(self):
        self.history()
        l = self.chat()
        r = self.decide(l)
        self.assertEqual((r["logged"], r["now"]), ([l["id"]], []))
        entry = hub._hub_doc(self.ws)["routed"][-1]
        self.assertEqual((entry["decision"], entry["why"]), ("log", "habit: chat #platform ignored 4 of 5"))
        doc = chief.status_doc(self.ws, self.CFG, AT)
        self.assertEqual((doc["inbox_count"], doc["quieted"]["count_7d"]), (0, 1))
        self.assertEqual(doc["quieted"]["recent"], [{"id": l["id"], "key": l["key"], "title": "T", "label": "#platform",
                                                     "bucket": "chat|label|%s" % self.CHAN, "at": hub._iso(AT),
                                                     "why": "habit: chat #platform ignored 4 of 5"}])
        self.assertEqual(chief.status_doc(self.ws, self.CFG, AT + timedelta(days=8))["quieted"]["count_7d"], 0)

    def test_an_ask_box_item_is_never_quieted_and_engages_its_bucket(self):
        self.history()                                                    # the channel's bucket would be quieted
        got = chief.ask(self.ws, self.CFG, AT, "look at https://chat.example.com/archives/%s/p1700000000123456" % self.CHAN)
        self.assertEqual(got["key"], "%s/1700000000.123456" % self.CHAN)
        [l] = [x for x in hub.read_inbox(self.ws) if x["id"] == got["line_id"]]
        judge = chief.tick(self.ws, self.CFG, AT, session="s-1")["judge"]
        r = chief.triage_decide(self.ws, self.CFG, AT, judge, [verdict(l, **self.QUIET)])
        self.assertEqual((r["logged"], r["now"]), ([], [l["id"]]))       # a `manual` line, whatever its key: shown, held for its case
        self.assertEqual(chief.status_doc(self.ws, self.CFG, AT)["quieted"]["count_7d"], 0)
        chief.claim(self.ws, self.CFG, AT, {"key": got["key"], "session_id": CLI_A, "title": "t", "link": None})
        self.assertEqual(chief.status_doc(self.ws, self.CFG, AT)["inbox"], [])
        # the pane logs the ask (`ask` is an engaged verb): the bucket has an engaged item, so it is quieted no more
        self.run_cli("act-log", "--stdin", stdin=json.dumps({"verb": "ask", "item_id": "ask:" + got["key"], "key": got["key"], "title": "look"}))
        b = [x for x in habits.habits(self.ws, AT)["buckets"] if x["id"] == "chat|label|%s" % self.CHAN]
        self.assertEqual([x["engaged"] for x in b], [1])
        other = self.chat(7, who="Kim <U00000010>")                      # the channel bucket alone decides
        self.assertEqual(habits.quiet(habits.habits(self.ws, AT), other, verdict(other, **self.QUIET), None, None, False), None)

    def test_quiet_rule_by_person_and_closed_unread(self):
        self.history(key="mail:thread-1", who="Sam", verb="done")       # mail has no label bucket: the person's
        l = line(source="mail", key="mail:thread-2", who="Sam <sam@example.com>")
        r = self.decide(l)
        self.assertEqual(r["logged"], [l["id"]])
        self.assertEqual(hub._hub_doc(self.ws)["routed"][-1]["why"], "habit: mail Sam ignored 4 of 5")

    def test_quiet_rule_thresholds(self):
        self.history(ignored=3)                                         # 3 of 5: not enough
        self.assertEqual(len(self.decide(self.chat(1))["now"]), 1)
        self.log(verb="ignore", item_id="U93", key="%s/103.0" % self.CHAN, who="Sam")
        l2 = self.chat(2)
        # the first chat line was shown too: 6 shown, 4 ignored, none engaged
        self.assertEqual(self.decide(l2, at=AT + timedelta(seconds=1))["logged"], [l2["id"]])
        self.log(verb="open_case", item_id="U90", key="%s/100.0" % self.CHAN, who="Sam")   # one engagement ends it
        self.assertEqual(len(self.decide(self.chat(3), at=AT + timedelta(seconds=2))["now"]), 1)

    def test_quiet_rule_needs_five_shown(self):
        self.history(ignored=4, shown=4)
        self.assertEqual(len(self.decide(self.chat())["now"]), 1)

    def test_every_exemption_keeps_the_line(self):
        cases = {
            "manual": dict(l=self.chat(kind="manual", source="manual")),
            "error": dict(l=self.chat(kind="error")),
            "decide": dict(v=dict(kind="decide")),
            "reply": dict(v=dict(kind="reply")),
            "urgent": dict(v=dict(urgency="now")),
            "no kind said": dict(v=dict(kind=None)),
            "mentioned": dict(l=self.chat(title="Dana, please look")),
            "mention by id": dict(l=self.chat(summary="<@U00000001> fyi"), cfg=dict(self.CFG, manager={"chat_dm": ["U00000001", "D00000001"]})),
            "dm": dict(l=line(source="chat", key="D00000002/dm", who="Sam"), history=dict(key="D00000002/dm")),
            "no verdict": dict(v=dict(none=True)),
        }
        for n, (name, c) in enumerate(sorted(cases.items())):
            with self.subTest(name):
                self.setUp()
                self.history(**c.get("history", {}))
                l = c.get("l") or self.chat()
                r = self.decide(l, cfg=c.get("cfg"), **c.get("v", {}))
                self.assertEqual((r["logged"], r["now"]), ([], [l["id"]]))
                self.assertEqual(chief.status_doc(self.ws, self.CFG, AT)["quieted"]["count_7d"], 0)

    def test_live_case_exceptions_and_the_switch_keep_the_line(self):
        self.history()
        self.seed_sessions({"%s/1.0" % self.CHAN: {"session_id": "s-9", "kind": "case", "state": "live", "unseen": 0}})
        r = self.decide(self.chat(1))
        self.assertEqual(r["logged"], [])                                # routed to its case, never quieted
        self.assertEqual(hub._hub_doc(self.ws)["routed"][-1]["decision"], "route")
        self.assertEqual(self.run_cli("habits", "off")[1], "habits off: 0 quieted in 7 days\n")
        self.assertEqual(chief.status_doc(self.ws, self.CFG, AT)["habits"], {"on": False})
        self.assertEqual(len(self.decide(self.chat(2), at=AT + timedelta(seconds=1))["now"]), 1)
        self.assertEqual(self.run_cli("habits", "on")[1], "habits on: 0 quieted in 7 days\n")
        self.assertEqual(self.run_cli("habits", "status")[0], 0)
        # the exceptions list: both of the line's buckets must be on it for the line to show
        self.assertEqual(self.run_cli("habits", "except", "chat|label|%s" % self.CHAN)[0], 0)
        l3 = self.chat(3)
        self.assertEqual(self.decide(l3, at=AT + timedelta(seconds=2))["logged"], [l3["id"]])
        self.assertEqual(hub._hub_doc(self.ws)["routed"][-1]["why"], "habit: chat Sam ignored 4 of 6")
        self.assertEqual(self.run_cli("habits", "except", "chat|who|Sam")[0], 0)
        self.assertEqual(habits.exceptions(self.ws), ["chat|label|%s" % self.CHAN, "chat|who|Sam"])
        self.assertEqual(len(self.decide(self.chat(4), at=AT + timedelta(seconds=3))["now"]), 1)
        self.assertEqual([self.run_cli("habits", "except", "nope")[0], self.run_cli("habits", "bogus")[0],
                          self.run_cli("habits", "show", "missing")[0]], [1, 2, 1])

    def test_a_folding_update_is_not_quieted(self):
        self.history()
        a, b = self.chat(1), self.chat(1, title="second", at="2026-10-29T09:05:00Z")
        hub.append_inbox(self.ws, [a, b])
        judge = chief.tick(self.ws, self.CFG, AT, session="s-1")["judge"]
        r = chief.triage_decide(self.ws, self.CFG, AT, judge, [verdict(x, **self.QUIET) for x in (a, b)])
        self.assertEqual((r["logged"], len(r["now"])), ([], 1))

    def test_show_puts_a_quieted_line_in_the_inbox(self):
        self.history()
        l = self.chat()
        self.decide(l, habit_note="rarely opened")
        code, out, _ = self.run_cli("habits", "show", l["id"], at=AT + timedelta(minutes=1))
        self.assertEqual((code, out), (0, "Shown in the Inbox as U1\n"))
        doc = chief.status_doc(self.ws, self.CFG, AT + timedelta(minutes=1))
        self.assertEqual(doc["quieted"], {"count_7d": 0, "recent": []})
        item = doc["inbox"][0]
        self.assertEqual((item["id"], item["key"], item["title"], item["why"], item["kind"], item["urgency"], item["label"]),
                         ("U1", l["key"], "T", "D", "fyi", "later", "#platform"))
        self.assertEqual(item["actions"], [{"label": "Read it", "prompt": "Read the thread"}])
        self.assertEqual(habits.actions(self.ws, AT + timedelta(minutes=1))[-1]["verb"], "shown")
        self.assertEqual(self.run_cli("habits", "show", l["id"])[0], 1)             # once

    def test_now_items_are_logged_as_shown_and_keep_a_habit_note(self):
        l = self.chat()
        r = self.decide(l, kind="act", habit_note="  usually\nignored " + "x" * 100)
        self.assertEqual(r["now"], [l["id"]])
        rows = habits.actions(self.ws, AT)
        self.assertEqual([(x["verb"], x["item_id"], x["label"], x["who"], x["kind"], x["title"]) for x in rows],
                         [("shown", "U1", self.CHAN, "Sam", "act", "T")])
        self.assertNotIn("summary", rows[0])
        note = chief.status_doc(self.ws, self.CFG, AT)["inbox"][0]["habit_note"]
        self.assertEqual((len(note), note.startswith("usually ignored x")), (80, True))
