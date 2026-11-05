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
        self.assertEqual(s, {"daily_budget_usd": 20, "tldr_model": "sonnet", "triage_model": "sonnet"})
        self.assertEqual(chief.settings({"chief": {"triage_model": "gpt"}})["triage_model"], "sonnet")
        self.assertEqual(chief.settings({"chief": {"triage_model": "opus"}})["triage_model"], "opus")
        s = chief.settings({"chief": {"daily_budget_usd": 5, "max_workers": 5, "batch_minutes": 30, "bogus": 1}})
        self.assertEqual(s, {"daily_budget_usd": 5, "tldr_model": "sonnet", "triage_model": "sonnet"})


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
                                                "session"]))

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


def verdict(l, is_work=True, needs_attention=True, title="T", tldr="D"):
    return {"line_id": l["id"], "key": l["key"], "is_work": is_work, "needs_attention": needs_attention,
            "title": title, "tldr": tldr}


class TestLeader(Base):
    """`tick --as <session>`: one desktop session leads, and code decides the judge lines."""
    inbox = TestTick.inbox

    def main(self, *args, at=AT, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin or "")):
            code = chief.main(list(args), self.ws, {}, at)
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
                                    "inbox_count"})
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
                self.assertEqual(json.loads(out.getvalue()), {"stored": 1})
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
                mock.patch.object(chief.portrait_doc, "__defaults__", (self.ws / "missing.png",)):
            self.assertEqual(self.run_verb(), (1, None))


if __name__ == "__main__":
    unittest.main()
