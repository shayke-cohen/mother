"""Tests for lib/hub.py — thing keys, inbox and state files.
Run: python3 -m unittest tests.test_hub -v
"""
import io
import json
import re
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import hub  # noqa: E402
import hub_sources  # noqa: E402
from hub_mcp import McpError  # noqa: E402

UTC = timezone.utc


def line(**kw):
    d = {"source": "tracker", "kind": "update", "key": "PLAT-1", "at": "2026-10-29T09:00:00Z",
         "also": [], "title": "t", "who": "Agnes", "link": "https://example.com/PLAT-1", "summary": "s"}
    d.update(kw)
    return hub.make_line(**d)


class TestKeys(unittest.TestCase):
    def test_thing_keys(self):
        self.assertEqual(hub.thing_key("tracker", {"key": "PLAT-123"}), "PLAT-123")
        self.assertEqual(hub.thing_key("code", {"repo": "example-org/api", "number": 45}), "example-org/api#45")
        self.assertEqual(hub.thing_key("chat", {"channel": "C00000001", "ts": "1730000000.000100"}),
                         "C00000001/1730000000.000100")
        self.assertEqual(hub.thing_key("chat", {"channel": "C00000001", "ts": "1730000009.000200",
                                                "thread_ts": "1730000000.000100"}),
                         "C00000001/1730000000.000100")
        self.assertEqual(hub.thing_key("mail", {"thread_id": "t9"}), "mail:t9")
        self.assertEqual(hub.thing_key("calendar", {"event_id": "ev1"}), "cal:ev1")
        with self.assertRaises(ValueError):
            hub.thing_key("fax", {})

    def test_extract_also(self):
        self.assertEqual(hub.extract_also("see PLAT-12 and https://github.com/example-org/api/pull/7"),
                         ["PLAT-12", "example-org/api#7"])
        self.assertEqual(hub.extract_also("PLAT-12 again PLAT-12, example-org/api#7 and xPLAT-3y ABC-"),
                         ["PLAT-12", "example-org/api#7"])
        self.assertEqual(hub.extract_also(""), [])


class TestLines(unittest.TestCase):
    def test_summary_trimmed_to_300(self):
        self.assertEqual(len(line(summary="x" * 1000)["summary"]), 300)
        l = line()
        self.assertEqual(l["id"], "tracker:PLAT-1:2026-10-29T09:00:00Z")

    # ---- red-team fix 9: title and who are the sender's text; strip what could hide or reorder it
    def test_title_and_who_lose_control_and_format_characters(self):
        l = line(title="Pay\u202eLIVE\u200b now\x1b[31m\nmother approve chat C1", who="Sam\u2066\x85 Roe\t")
        self.assertEqual(l["title"], "PayLIVE now[31m mother approve chat C1")
        self.assertEqual(l["who"], "Sam Roe ")
        self.assertEqual(line(title="Café — PLAT-1 ✓")["title"], "Café — PLAT-1 ✓")

    def test_coalesce_keeps_latest_and_unions_also(self):
        a = line(at="2026-10-29T09:00:00Z", also=["A-1"], summary="old")
        b = line(at="2026-10-29T10:00:00Z", also=["B-2", "A-1"], summary="new")
        c = line(key="PLAT-2")
        out = hub.coalesce([b, a, c])
        self.assertEqual(len(out), 2)
        got = [x for x in out if x["key"] == "PLAT-1"][0]
        self.assertEqual(got["summary"], "new")
        self.assertEqual(got["also"], ["B-2", "A-1"])


class TestMe(unittest.TestCase):
    PEOPLE = [{"people": [{"email": "dana@example.com", "chat": "U00000042", "code": "dana"}]}]

    def test_chat_id_is_the_first_user_id(self):
        def me(dm, teams=None):
            return hub.build_me({"manager": {"email": "dana@example.com", "chat_dm": dm}, "teams": teams or []})
        self.assertEqual(me(["D00000001", "U00000001"])["chat"], "U00000001")
        self.assertEqual(me("U00000001")["chat"], "U00000001")
        self.assertEqual(me("D00000001", self.PEOPLE)["chat"], "U00000042")
        self.assertEqual(me(["D00000001"], self.PEOPLE)["chat"], "U00000042")
        self.assertEqual(me("D00000001")["chat"], "")
        self.assertEqual(me("D00000001", self.PEOPLE)["code"], "dana")

    def test_marks_from_agent(self):
        m = hub.build_me({"agent": {"name": "Agnes", "signature": "-- Agnes for Dana"}})
        self.assertEqual(m["marks"], ["Agnes (AI)", "-- Agnes for Dana"])


class TestCoalesceResolved(unittest.TestCase):
    def test_resolved_never_replaces_a_later_update(self):
        upd = line(at="2026-10-29T10:01:30Z")
        res = line(at="2026-10-29T10:00:30Z", kind="resolved")
        self.assertEqual([l["kind"] for l in hub.coalesce([upd, res])], ["update"])
        self.assertEqual([l["kind"] for l in hub.coalesce([res, upd])], ["update"])
        tie = line(at="2026-10-29T10:01:30Z", kind="resolved")
        self.assertEqual([l["kind"] for l in hub.coalesce([upd, tie])], ["update"])
        later = line(at="2026-10-29T10:02:00Z", kind="resolved")
        self.assertEqual([l["kind"] for l in hub.coalesce([upd, later])], ["resolved"])


class TestFiles(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.ws = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_save_json_is_atomic(self):
        self.assertEqual(hub.load_json(self.ws, "cursors.json", {"x": 1}), {"x": 1})
        with hub.locked(self.ws):
            hub.save_json(self.ws, "cursors.json", {"a": 1})
        self.assertEqual(hub.load_json(self.ws, "cursors.json", {}), {"a": 1})
        self.assertEqual([p for p in hub.hub_dir(self.ws).iterdir() if p.name.endswith(".tmp")], [])
        # a concurrent writer waits for the lock
        order = []
        with hub.locked(self.ws):
            def other():
                with hub.locked(self.ws):
                    order.append("other")
            th = threading.Thread(target=other)
            th.start()
            time.sleep(0.3)
            order.append("first")
        th.join(5)
        self.assertEqual(order, ["first", "other"])

    def test_append_inbox(self):
        hub.append_inbox(self.ws, [line(), line(key="PLAT-2")])
        hub.append_inbox(self.ws, [line(key="PLAT-3")])
        rows = [json.loads(l) for l in (hub.hub_dir(self.ws) / "inbox.jsonl").read_text().splitlines()]
        self.assertEqual([r["key"] for r in rows], ["PLAT-1", "PLAT-2", "PLAT-3"])

    def test_rotate_keeps_seven_days(self):
        d = hub.hub_dir(self.ws)
        hub.append_inbox(self.ws, [line()])
        for day in ("2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-28"):
            (d / ("inbox-%s.jsonl" % day)).write_text("{}\n")
        hub.rotate_inbox(self.ws, datetime(2026, 10, 29, 6, 0, tzinfo=UTC))
        names = sorted(p.name for p in d.glob("inbox*.jsonl"))
        self.assertEqual(names, ["inbox-2026-10-23.jsonl", "inbox-2026-10-28.jsonl", "inbox-2026-10-29.jsonl"])
        self.assertFalse((d / "inbox.jsonl").exists())
        # appending after rotation starts a fresh inbox; rotating again merges into the day's file
        hub.append_inbox(self.ws, [line(key="PLAT-9")])
        hub.rotate_inbox(self.ws, datetime(2026, 10, 29, 23, 0, tzinfo=UTC))
        self.assertEqual(len((d / "inbox-2026-10-29.jsonl").read_text().splitlines()), 2)

    def test_append_waits_for_the_lock(self):
        order = []
        with hub.locked(self.ws):
            th = threading.Thread(target=lambda: (hub.append_inbox(self.ws, [line()]), order.append("appended")))
            th.start()
            time.sleep(0.3)
            self.assertFalse((hub.hub_dir(self.ws) / "inbox.jsonl").exists())
            order.append("released")
        th.join(5)
        self.assertEqual(order, ["released", "appended"])
        self.assertEqual(len((hub.hub_dir(self.ws) / "inbox.jsonl").read_text().splitlines()), 1)

    def test_rotate_waits_for_the_lock_and_keeps_every_line(self):
        d = hub.hub_dir(self.ws)
        hub.append_inbox(self.ws, [line(key="PLAT-1")])
        (d / "inbox-2026-10-29.jsonl").write_text(json.dumps(line(key="PLAT-0")) + "\n")
        at = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
        with hub.locked(self.ws):
            th = threading.Thread(target=hub.rotate_inbox, args=(self.ws, at))
            th.start()
            time.sleep(0.3)
            self.assertTrue((d / "inbox.jsonl").exists())  # rotation has not started
        th.join(5)
        hub.append_inbox(self.ws, [line(key="PLAT-LATE")])
        keys = []
        for p in d.glob("inbox*.jsonl"):
            keys += [json.loads(l)["key"] for l in p.read_text().splitlines()]
        self.assertEqual(sorted(keys), ["PLAT-0", "PLAT-1", "PLAT-LATE"])
        self.assertEqual(list(d.glob("*.rotating")), [])

    def test_stranded_rotating_file_is_folded(self):
        d = hub.hub_dir(self.ws)
        d.mkdir(parents=True)
        (d / "inbox.jsonl.123.rotating").write_text(json.dumps(line(key="PLAT-S")) + "\n")
        hub.rotate_inbox(self.ws, datetime(2026, 10, 29, 6, 0, tzinfo=UTC))
        self.assertEqual(list(d.glob("*.rotating")), [])
        self.assertIn("PLAT-S", (d / "inbox-2026-10-29.jsonl").read_text())

    def test_rotate_empty_or_missing_inbox_makes_no_dated_file(self):
        d = hub.hub_dir(self.ws)
        at = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
        hub.rotate_inbox(self.ws, at)
        self.assertEqual(list(d.glob("inbox-*.jsonl")), [])
        (d / "inbox.jsonl").write_text("")
        hub.rotate_inbox(self.ws, at)
        self.assertEqual(list(d.glob("inbox-*.jsonl")), [])
        self.assertFalse((d / "inbox.jsonl").exists())


class FakeAdapter:
    """Stands in for the tracker adapter: the first poll returns a cursor and no lines (as a real one does)."""
    items = []
    fail = None
    calls = 0

    @classmethod
    def poll(cls, client, cursor, cfg, me, at):
        cls.calls += 1
        if cls.fail:
            raise McpError(cls.fail, "server went away")
        if cursor is None:
            return {"lines": [], "cursor": {"n": 0}, "partial": []}
        new = cls.items[cursor["n"]:]
        return {"lines": [line(key=k, at="2026-10-29T09:%02d:00Z" % i) for i, k in enumerate(new)],
                "cursor": {"n": len(cls.items)}, "partial": getattr(cls, "partial", []),
                "notes": getattr(cls, "notes", [])}


class Watcher(unittest.TestCase):
    AT = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)  # a Thursday, inside active hours (Europe/London, 06-22)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = {"version": 1, "manager": {"email": "dana@example.com", "timezone": "Europe/London",
                                              "workdays": ["Mon", "Tue", "Wed", "Thu", "Fri"], "chat_dm": "U00000000"},
                    "connectors": {"tracker": "jira"},
                    "hub": {"enabled": True, "every_seconds": 120, "idle_every_seconds": 600, "sweep_minutes": 30,
                            "connections": {"tracker": {"command": "example-server", "args": [], "env": {}}},
                            "watch": {"tracker": {"projects": ["PLAT"]}}}}
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        FakeAdapter.items, FakeAdapter.fail, FakeAdapter.calls = [], None, 0
        FakeAdapter.partial, FakeAdapter.notes = [], []
        saved = dict(hub_sources.ADAPTERS)
        hub_sources.ADAPTERS["tracker:jira"] = FakeAdapter
        self.addCleanup(lambda: (hub_sources.ADAPTERS.clear(), hub_sources.ADAPTERS.update(saved)))
        self.clients = {"tracker": object()}

    def at(self, minutes=0):
        return self.AT + timedelta(minutes=minutes)

    def poll(self, minutes=0, clients=None):
        return hub.poll_once(self.ws, self.cfg, self.at(minutes), self.clients if clients is None else clients)

    def inbox(self):
        p = hub.hub_dir(self.ws) / "inbox.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_first_poll_sets_cursor_and_appends_nothing(self):
        beat = self.poll()
        self.assertEqual(hub.load_json(self.ws, "cursors.json", {})["tracker"], {"n": 0})
        self.assertEqual(self.inbox(), [])
        self.assertEqual(beat["sources"], {"tracker": {"status": "ok"}})
        self.assertEqual(beat["errors"], [])
        self.assertEqual(hub.load_json(self.ws, "watcher.json", {}), beat)
        self.assertEqual((beat["at"], beat["every_seconds"], beat["active"]), ("2026-10-29T10:00:00Z", 120, True))

    def test_new_items_appended_once_across_two_polls(self):
        self.poll()
        FakeAdapter.items = ["PLAT-1", "PLAT-2"]
        self.poll(2)
        self.poll(4)
        self.assertEqual([l["key"] for l in self.inbox()], ["PLAT-1", "PLAT-2"])

    def test_failing_source_one_error_line_and_cursor_holds(self):
        self.poll()
        FakeAdapter.fail = "unreachable"
        for m in (2, 4, 6):
            beat = self.poll(m)
        errs = [l for l in self.inbox() if l["kind"] == "error"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["source"], "tracker")
        self.assertEqual(hub.load_json(self.ws, "cursors.json", {})["tracker"], {"n": 0})
        ent = beat["sources"]["tracker"]
        self.assertEqual(ent["status"], "couldnt_check")
        self.assertEqual(ent["since"], "2026-10-29T10:02:00Z")
        self.assertIn("went away", ent["reason"])
        self.assertEqual(len(beat["errors"]), 1)
        self.assertFalse((self.ws / "capabilities.json").exists())  # the watcher never touches health
        # recovery: items found while it was down come through once the source answers again
        FakeAdapter.fail = None
        FakeAdapter.items = ["PLAT-9"]
        beat = self.poll(8)
        self.assertEqual(beat["sources"]["tracker"], {"status": "ok"})
        self.assertEqual([l["key"] for l in self.inbox() if l["kind"] == "update"], ["PLAT-9"])
        self.assertFalse((self.ws / "capabilities.json").exists())
        self.assertEqual(hub.load_json(self.ws, "cursors.json", {})["_errored"], [])
        FakeAdapter.fail = "unreachable"  # a new failure after recovery is a new error line
        self.poll(10)
        self.assertEqual(len([l for l in self.inbox() if l["kind"] == "error"]), 2)

    def test_forbidden_tool_stops_the_watcher(self):
        from hub_mcp import ForbiddenTool

        def boom(*a):
            raise ForbiddenTool("nope")
        orig = FakeAdapter.__dict__["poll"]
        FakeAdapter.poll = staticmethod(boom)
        self.addCleanup(setattr, FakeAdapter, "poll", orig)
        with self.assertRaises(ForbiddenTool):
            self.poll()

    def test_partial_source_is_reported_with_reasons(self):
        self.poll()
        FakeAdapter.items = ["PLAT-1"]
        FakeAdapter.partial = ["search failed"]
        beat = self.poll(2)
        self.assertEqual(beat["sources"]["tracker"]["status"], "partial")
        self.assertEqual(beat["sources"]["tracker"]["reason"], "search failed")
        self.assertEqual(hub.load_json(self.ws, "cursors.json", {})["tracker"], {"n": 1})
        self.assertEqual([l["kind"] for l in self.inbox()], ["update"])

    def test_notes_are_recorded_without_changing_the_status(self):
        self.poll()
        FakeAdapter.notes = ["C00000001: lookback truncated"]
        beat = self.poll(2)
        self.assertEqual(beat["sources"]["tracker"], {"status": "ok", "notes": ["C00000001: lookback truncated"]})
        self.assertEqual(beat["errors"], [])
        saved = json.loads((self.ws / ".mother" / "hub" / "watcher.json").read_text())
        self.assertEqual(saved["sources"]["tracker"]["notes"], ["C00000001: lookback truncated"])
        buf = io.StringIO()
        hub.status(self.ws, self.AT + timedelta(minutes=2), out=buf)
        self.assertIn("tracker: ok\n    note: C00000001: lookback truncated", buf.getvalue())
        FakeAdapter.notes = []
        self.assertNotIn("notes", self.poll(4)["sources"]["tracker"])

    def test_client_that_would_not_start_is_one_error(self):
        bad = {"tracker": McpError("dropped", "server exited")}
        for m in (0, 2):
            beat = self.poll(m, clients=bad)
        self.assertEqual(beat["sources"]["tracker"]["status"], "couldnt_check")
        self.assertEqual(len([l for l in self.inbox() if l["kind"] == "error"]), 1)

    def test_missing_connection_is_couldnt_check(self):
        del self.cfg["hub"]["connections"]["tracker"]
        beat = self.poll(clients={})
        self.assertEqual(beat["sources"]["tracker"]["status"], "couldnt_check")
        self.assertEqual(beat["sources"]["tracker"]["reason"], "no connection")
        self.assertEqual(FakeAdapter.calls, 0)

    def test_no_tracker_projects_is_a_config_gap_not_a_failure(self):
        self.cfg["hub"]["watch"] = {}
        hub_sources.ADAPTERS["tracker:jira"] = hub_sources.TrackerJira
        beat = self.poll()
        self.assertEqual(beat["sources"]["tracker"]["status"], "couldnt_check")
        self.assertEqual(beat["sources"]["tracker"]["reason"], "no tracker projects configured")
        self.assertEqual([l for l in self.inbox() if l["kind"] == "error"], [])

    def test_tracker_projects_are_watch_plus_team_projects_deduped(self):
        self.cfg["teams"] = [{"slug": "a", "tracker": {"project": "OPS"}}, {"slug": "b", "tracker": {"project": "PLAT"}}]
        self.assertEqual(hub._watch_cfg(self.cfg, self.cfg["hub"], "tracker")["projects"], ["PLAT", "OPS"])

    def test_sweep_only_when_pending(self):
        self.poll()
        self.poll(31)
        self.assertEqual([l for l in self.inbox() if l["kind"] == "sweep"], [])
        # a missing or odd hub.json / sessions.json is nothing pending
        (hub.hub_dir(self.ws) / "hub.json").write_text("not json")
        (hub.hub_dir(self.ws) / "sessions.json").write_text("[]")
        self.poll(32)
        self.assertEqual(self.inbox(), [])
        with hub.locked(self.ws):
            hub.save_json(self.ws, "hub.json", {"version": 1, "pending": [{"key": "PLAT-1"}]})
        self.poll(33)
        self.assertEqual([l["kind"] for l in self.inbox()], ["sweep"])
        self.poll(40)  # inside sweep_minutes: no second sweep
        self.assertEqual(len(self.inbox()), 1)
        self.poll(64)
        self.assertEqual(len(self.inbox()), 2)

    def test_last_append_at_marks_lines_the_hub_must_decide(self):
        beat = self.poll()
        self.assertNotIn("last_append_at", beat)
        FakeAdapter.items = ["PLAT-1"]
        self.assertEqual(self.poll(2)["last_append_at"], "2026-10-29T10:02:00Z")
        self.assertEqual(self.poll(4)["last_append_at"], "2026-10-29T10:02:00Z")  # nothing new: kept
        real = FakeAdapter.poll
        FakeAdapter.poll = classmethod(lambda cls, *a: {"lines": [line(key="PLAT-1", kind="resolved")],
                                                        "cursor": {"n": 1}, "partial": []})
        self.addCleanup(setattr, FakeAdapter, "poll", real)
        self.assertEqual(self.poll(6)["last_append_at"], "2026-10-29T10:02:00Z")  # resolved never wakes the hub

    def test_pass_that_only_resolves_a_key_does_not_mark_the_hub_behind(self):
        FakeAdapter.items = ["PLAT-1"]
        self.poll()
        self.assertEqual(self.poll(2)["last_append_at"], "2026-10-29T10:02:00Z")
        real = FakeAdapter.poll
        self.addCleanup(setattr, FakeAdapter, "poll", real)
        lines = []
        FakeAdapter.poll = classmethod(lambda cls, *a: {"lines": list(lines), "cursor": {"n": 1}, "partial": []})
        # an update and its resolution in one pass: tail prints nothing for the key, so the hub must not look behind
        lines[:] = [line(key="PLAT-2", at="2026-10-29T10:00:00Z"),
                    line(key="PLAT-2", kind="resolved", at="2026-10-29T10:01:00Z")]
        self.assertEqual(self.poll(4)["last_append_at"], "2026-10-29T10:02:00Z")
        # one key resolved, another updated: the update is printed, so it moves
        lines[:] = [line(key="PLAT-3", kind="resolved", at="2026-10-29T10:05:00Z"),
                    line(key="PLAT-4", at="2026-10-29T10:05:00Z")]
        self.assertEqual(self.poll(6)["last_append_at"], "2026-10-29T10:06:00Z")
        # a resolution that does not win (same `at` as the update) leaves the update standing: it moves
        lines[:] = [line(key="PLAT-5", at="2026-10-29T10:07:00Z"),
                    line(key="PLAT-5", kind="resolved", at="2026-10-29T10:07:00Z")]
        self.assertEqual(self.poll(8)["last_append_at"], "2026-10-29T10:08:00Z")

    def test_sweep_for_unseen_live_sessions(self):
        with hub.locked(self.ws):
            hub.save_json(self.ws, "sessions.json", {"version": 1, "sessions": {
                "a": {"session_id": "s1", "unseen": 0, "state": "live"},
                "b": {"session_id": "s2", "unseen": 3, "state": "dropped"}}})
        self.poll()
        self.assertEqual(self.inbox(), [])
        with hub.locked(self.ws):
            hub.save_json(self.ws, "sessions.json", {"version": 1, "sessions": {
                "b": {"session_id": "s2", "unseen": 2, "state": "live", "last_routed_at": "2026-10-27T09:00:00Z"}}})
        self.poll(1)  # unseen news routed more than update_ttl_hours working hours ago no longer sweeps
        self.assertEqual(self.inbox(), [])
        with hub.locked(self.ws):
            hub.save_json(self.ws, "sessions.json", {"version": 1, "sessions": {
                "b": {"session_id": "s2", "unseen": 2, "state": "live", "last_routed_at": "2026-10-29T09:50:00Z"}}})
        self.poll(2)
        self.assertEqual([l["kind"] for l in self.inbox()], ["sweep"])

    def test_interval_inside_and_outside_active_hours(self):
        self.assertEqual(hub.interval(self.cfg, self.AT), (True, 120))
        night = datetime(2026, 10, 29, 23, 0, tzinfo=UTC)
        self.assertEqual(hub.interval(self.cfg, night), (False, 600))
        self.cfg["hub"]["idle_every_seconds"] = 0  # sleep until the window opens: Fri 06:00 London = 06:00 UTC
        self.assertEqual(hub.interval(self.cfg, night), (False, 7 * 3600))
        saturday = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
        self.assertFalse(hub.interval(self.cfg, saturday)[0])
        beat = hub.poll_once(self.ws, self.cfg, night, self.clients, poll=False)
        self.assertEqual((beat["active"], beat["every_seconds"]), (False, 7 * 3600))
        self.assertEqual(FakeAdapter.calls, 0)

    def test_me_comes_from_manager_and_matching_person(self):
        self.cfg["teams"] = [{"people": [{"name": "Dana Levi", "email": "dana@example.com", "code": "danal"}]}]
        self.assertEqual(hub.build_me(self.cfg), {"email": "dana@example.com", "chat": "U00000000", "code": "danal",
                                                  "tracker_account": "", "marks": []})

    def test_home_status(self):
        os.environ["MOTHER_HOSTNAME"] = "laptop"
        self.addCleanup(os.environ.pop, "MOTHER_HOSTNAME", None)
        self.assertEqual(hub.home_status(self.cfg), 3)
        self.cfg["workspace"] = {"home_machine": "other"}
        self.assertEqual(hub.home_status(self.cfg), 2)
        self.cfg["workspace"] = {"home_machine": "laptop"}
        self.assertEqual(hub.home_status(self.cfg), 0)


    def test_new_connection_that_fails_to_start_still_gets_its_error_line(self):
        conns = self.cfg["hub"]["connections"]
        saved = dict(conns)
        conns.clear()
        self.poll(clients={})  # couldnt_check: no connection, no error line
        self.assertEqual(self.inbox(), [])
        conns.update(saved)
        bad = {"tracker": McpError("dropped", "server exited")}
        self.poll(2, clients=bad)
        self.poll(4, clients=bad)
        self.assertEqual(len([l for l in self.inbox() if l["kind"] == "error"]), 1)

    def test_recovery_after_missing_connection_writes_nothing_to_health(self):
        del self.cfg["hub"]["connections"]["tracker"]
        self.poll(clients={})
        self.cfg["hub"]["connections"]["tracker"] = {"command": "example-server", "args": [], "env": {}}
        self.poll(2)
        self.assertFalse((self.ws / "capabilities.json").exists())


class Loop(unittest.TestCase):
    """watch(): driven with an injected clock, sleep, stop flag and client factory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = {"version": 1, "manager": {"email": "dana@example.com", "timezone": "UTC",
                                              "workdays": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
                    "connectors": {"tracker": "jira"}, "workspace": {"home_machine": "laptop"},
                    "hub": {"enabled": True, "every_seconds": 120, "idle_every_seconds": 600,
                            "connections": {"tracker": {"command": "example-server", "args": [], "env": {}}}}}
        os.environ["MOTHER_HOSTNAME"] = "laptop"
        self.addCleanup(os.environ.pop, "MOTHER_HOSTNAME", None)
        FakeAdapter.items, FakeAdapter.fail, FakeAdapter.calls, FakeAdapter.partial = [], None, 0, []
        saved = dict(hub_sources.ADAPTERS)
        hub_sources.ADAPTERS["tracker:jira"] = FakeAdapter
        self.addCleanup(lambda: (hub_sources.ADAPTERS.clear(), hub_sources.ADAPTERS.update(saved)))
        self.now = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        self.slept = []
        self.stop = threading.Event()
        self.made = []

    def sleep(self, n):
        self.slept.append(n)
        self.now += timedelta(seconds=n)
        if self.after and self.after(self):
            self.stop.set()

    def run_watch(self, after, factory=None):
        self.after = after
        hub.watch(self.ws, self.cfg, clock=lambda: self.now, sleep=self.sleep,
                  factory=factory or self.factory, stop=self.stop)

    def factory(self, cfg, have):
        out = dict(have)
        if "tracker" not in out:
            c = FakeClient()
            self.made.append(c)
            out["tracker"] = c
        return out

    def quiet_watch(self):
        import io
        from unittest import mock
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            hub.watch(self.ws, self.cfg, clock=lambda: self.now, sleep=self.sleep, factory=self.factory,
                      stop=self.stop)  # returns: the process exits 0, so launchd does not respawn it
        return err.getvalue()

    def test_not_home_or_not_enabled_returns_with_one_line(self):
        self.after = None
        self.cfg["workspace"]["home_machine"] = "elsewhere"
        self.assertIn("not the home machine", self.quiet_watch())
        del self.cfg["workspace"]["home_machine"]
        self.assertIn("not the home machine", self.quiet_watch())
        self.cfg["workspace"]["home_machine"] = "laptop"
        for off in (False, None, "yes"):
            self.cfg["hub"]["enabled"] = off
            self.assertIn("hub.enabled", self.quiet_watch())
        del self.cfg["hub"]["enabled"]
        self.assertIn("hub.enabled", self.quiet_watch())
        self.assertEqual(self.made, [])
        self.assertFalse((hub.hub_dir(self.ws) / "watcher.json").exists())

    def test_bad_config_numbers_return_with_one_line(self):
        self.after = None
        for k, v in (("every_seconds", "soon"), ("every_seconds", 0), ("idle_every_seconds", -1),
                     ("sweep_minutes", [1]), ("update_ttl_hours", "x")):
            cfg = json.loads(json.dumps(self.cfg))
            cfg["hub"][k] = v
            self.cfg, saved = cfg, self.cfg
            self.assertIn("hub.%s" % k, self.quiet_watch())
            self.cfg = saved
        self.assertEqual(self.made, [])

    def test_rotation_failure_is_logged_and_the_loop_goes_on(self):
        import io
        from unittest import mock
        self.now = datetime(2026, 10, 29, 23, 57, tzinfo=UTC)
        self.cfg["hub"]["idle_every_seconds"] = 120
        with mock.patch.object(hub, "rotate_inbox", side_effect=OSError("disk full")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.run_watch(lambda t: sum(t.slept) >= 480)
        self.assertIn("disk full", err.getvalue())
        self.assertGreaterEqual(FakeAdapter.calls, 3)

    def test_watcher_log_rotates_at_1mb(self):
        d = hub.hub_dir(self.ws)
        d.mkdir(parents=True, exist_ok=True)
        log = d / "watcher.log"
        log.write_text("x" * (hub.LOG_MAX + 10))
        hub.cap_log(self.ws)
        self.assertEqual(log.stat().st_size, 0)
        self.assertEqual((d / "watcher.log.1").stat().st_size, hub.LOG_MAX + 10)
        log.write_text("small")
        hub.cap_log(self.ws)
        self.assertEqual(log.read_text(), "small")

    def test_polls_every_interval_then_returns_on_stop(self):
        self.run_watch(lambda t: sum(t.slept) >= 240)
        self.assertEqual(FakeAdapter.calls, 2)
        self.assertEqual(len(self.made), 1)
        self.assertTrue(self.made[0].closed)

    def test_sigterm_returns(self):
        import signal

        def after(t):
            os.kill(os.getpid(), signal.SIGTERM)
            return False
        before = signal.getsignal(signal.SIGTERM)
        self.run_watch(after)  # returns instead of dying: the handler only sets the flag
        self.assertEqual(FakeAdapter.calls, 1)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_inbox_rotates_when_the_local_day_changes(self):
        self.now = datetime(2026, 10, 29, 21, 59, tzinfo=UTC)
        hub.append_inbox(self.ws, [line(key="PLAT-OLD")])
        self.run_watch(lambda t: (hub.hub_dir(t.ws) / "inbox-2026-10-29.jsonl").exists())
        d = hub.hub_dir(self.ws)
        self.assertIn("PLAT-OLD", (d / "inbox-2026-10-29.jsonl").read_text())
        self.assertFalse((d / "inbox.jsonl").exists())

    def test_failed_client_is_closed_and_rebuilt(self):
        FakeAdapter.fail = "dropped"
        self.run_watch(lambda t: sum(t.slept) >= 240)
        self.assertEqual(len(self.made), 2)
        self.assertTrue(all(c.closed for c in self.made))
        self.assertEqual(len([l for l in hub.load_json(self.ws, "cursors.json", {})["_errored"]]), 1)

    def test_forbidden_tool_propagates_out_of_watch(self):
        from hub_mcp import ForbiddenTool
        orig = FakeAdapter.__dict__["poll"]

        def boom(*a):
            raise ForbiddenTool("nope")
        FakeAdapter.poll = staticmethod(boom)
        self.addCleanup(setattr, FakeAdapter, "poll", orig)
        with self.assertRaises(ForbiddenTool):
            self.run_watch(None)
        self.assertTrue(self.made[0].closed)  # cleanup still ran


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.plist = Path(self.tmp.name) / "LaunchAgents" / "com.mother.hub.plist"
        self.cfg = {"version": 1, "manager": {"email": "dana@example.com", "timezone": "UTC",
                                              "workdays": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
                    "connectors": {"tracker": "jira", "code": "github"}, "workspace": {"home_machine": "laptop"},
                    "hub": {"enabled": True, "connections": {
                        "tracker": {"command": "example-server", "args": [], "env": {}},
                        "code": {"cli": "gh", "command": "ignored"}}}}
        os.environ["MOTHER_HOSTNAME"] = "laptop"
        self.addCleanup(os.environ.pop, "MOTHER_HOSTNAME", None)
        self.calls = []
        self.which = {"example-server": "/opt/x/bin/example-server", "gh": "/opt/homebrew/bin/gh"}.get

    def ctl(self, *a):
        self.calls.append(a)
        return 0, ""

    def run_install(self):
        return hub.install(self.ws, self.cfg, plist_path=self.plist, ctl=self.ctl, which=self.which,
                           python="/usr/bin/python3", mother="/plug/bin/mother")

    def test_plist_has_absolute_paths_and_path_env(self):
        import plistlib
        self.assertEqual(self.run_install(), 0)
        d = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(d["Label"], "com.mother.hub")
        self.assertEqual(d["ProgramArguments"], ["/usr/bin/python3", "/plug/bin/mother", "hub", "watch"])
        env = d["EnvironmentVariables"]
        self.assertEqual(env["MOTHER_WORKSPACE"], str(self.ws))
        self.assertEqual(env["PATH"], "/opt/x/bin:/opt/homebrew/bin:/usr/bin:/bin")
        self.assertEqual(d["KeepAlive"], {"SuccessfulExit": False})  # a clean exit (off home, disabled) stays down
        self.assertEqual(d["ThrottleInterval"], 60)
        self.assertTrue(d["StandardOutPath"].endswith(".mother/hub/watcher.log"))
        self.assertTrue(any(c[0] == "bootstrap" for c in self.calls))

    def test_install_fails_on_missing_command(self):
        self.which = {"gh": "/opt/homebrew/bin/gh"}.get
        self.assertEqual(self.run_install(), 1)
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.calls, [])

    def test_install_refuses_when_not_enabled(self):
        import io
        from unittest import mock
        for off in (False, None):
            self.cfg["hub"]["enabled"] = off
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(self.run_install(), 1)
            self.assertIn("hub.enabled", err.getvalue())
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.calls, [])

    def test_install_refuses_off_home(self):
        self.cfg["workspace"]["home_machine"] = "elsewhere"
        self.assertEqual(self.run_install(), 2)
        self.assertFalse(self.plist.exists())

    def test_uninstall_boots_out_and_removes(self):
        self.run_install()
        self.calls.clear()
        self.assertEqual(hub.uninstall(plist_path=self.plist, ctl=self.ctl), 0)
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.calls[0][0], "bootout")

    def test_check_table(self):
        import io
        import hub_mcp

        class C:
            tools = ["mcp__x__get-issues", "mcp__x__get-issue-changelog", "mcp__x__list-projects", "mcp__x__create-issue"]
            closed = False

            def close(self):
                self.closed = True
        c = C()
        buf = io.StringIO()
        rc = hub.check(self.cfg, factory=lambda cfg, have: {"tracker": c}, gh_user=lambda: None,
                       which=self.which, out=buf)
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("tracker | ok |", buf.getvalue())
        self.assertIn("code | ok |", buf.getvalue())
        self.assertTrue(c.closed)
        c.tools = ["mcp__x__create-issue"]
        buf = io.StringIO()
        self.assertEqual(hub.check(self.cfg, factory=lambda cfg, have: {"tracker": c}, gh_user=lambda: None,
                                   which=self.which, out=buf), 1)
        self.assertIn("tracker | \u2717 | missing read tools: get-issue-changelog, get-issues, list-projects", buf.getvalue())
        buf = io.StringIO()

        def bad():
            raise RuntimeError("not logged in")
        hub.check(self.cfg, factory=lambda cfg, have: {"tracker": hub_mcp.McpError("other", "boom")}, gh_user=bad,
                  which=self.which, out=buf)
        self.assertIn("tracker | \u2717 | other: boom", buf.getvalue())
        self.assertIn("code | \u2717 | not logged in", buf.getvalue())


class Status(unittest.TestCase):
    AT = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    def run_status(self):
        import io
        buf = io.StringIO()
        return hub.status(self.ws, self.AT, out=buf), buf.getvalue()

    def beat(self, secs_ago, active=True):
        hub.save_json(self.ws, "watcher.json", {"at": hub_sources.iso(self.AT - timedelta(seconds=secs_ago)),
                                                "every_seconds": 120, "active": active,
                                                "sources": {"tracker": {"status": "ok"}}, "errors": []})

    def test_status_stale_exit_2(self):
        self.beat(300)
        rc, out = self.run_status()
        self.assertEqual(rc, 2)
        self.assertIn("STALE", out)

    def test_missing_watcher_exit_2(self):
        self.assertEqual(self.run_status()[0], 2)

    def test_fresh_watcher_without_hub_is_ok_and_says_never_woke(self):
        self.beat(60)
        rc, out = self.run_status()
        self.assertEqual(rc, 0)
        self.assertIn("hub: never woke", out)

    def test_old_beat_outside_active_hours_is_not_stale(self):
        self.beat(5000, active=False)
        self.assertEqual(self.run_status()[0], 0)

    def test_stale_hub_wake_is_shown_but_exit_0(self):
        self.beat(60)
        hub.save_json(self.ws, "hub.json", {"last_wake": hub_sources.iso(self.AT - timedelta(seconds=900))})
        rc, out = self.run_status()
        self.assertEqual(rc, 0)
        self.assertIn("hub: last wake", out)
        self.assertNotIn("STALE", out.split("hub:")[1])  # an old wake with nothing new since is a quiet day
        w = hub.load_json(self.ws, "watcher.json", {})
        w["last_append_at"] = hub_sources.iso(self.AT - timedelta(seconds=600))
        hub.save_json(self.ws, "watcher.json", w)
        rc, out = self.run_status()
        self.assertEqual(rc, 0)
        self.assertIn("STALE", out.split("hub:")[1])
        w["last_append_at"] = hub_sources.iso(self.AT - timedelta(seconds=100))  # appended recently: not yet
        hub.save_json(self.ws, "watcher.json", w)
        self.assertNotIn("STALE", self.run_status()[1].split("hub:")[1])


class FakeClient:
    tools = []

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True



class Verbs(unittest.TestCase):
    AT = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)  # a Thursday

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = {"manager": {"timezone": "UTC"}, "hub": {"update_ttl_hours": 24}}

    def tail_lines(self, **kw):
        import io
        buf = io.StringIO()
        hub.tail(self.ws, out=buf, err=kw.pop("err", io.StringIO()), **kw)
        return [json.loads(x) for x in buf.getvalue().splitlines()]

    def add(self, *lines):
        hub.append_inbox(self.ws, list(lines))

    def decide(self, *items, **kw):
        # tests name lines like "x:1"; make sure each exists in the inbox (decide refuses unknown ids)
        known = {l["id"] for l in hub.read_inbox(self.ws)}
        for it in items:
            if it["line_id"] not in known:
                known.add(it["line_id"])
                hub.append_inbox(self.ws, [dict(line(key=it["key"]), id=it["line_id"])])
        return hub.decide(self.ws, self.cfg, kw.get("at", self.AT), list(items))

    def raise_card(self, key, at):
        """`session add` needs a raised card (or an existing session) for its key: stand one in, as the hub would."""
        if key in hub.load_json(self.ws, "sessions.json", {}).get("sessions", {}):
            return
        h = hub.load_json(self.ws, "hub.json", {})
        pend = h.setdefault("pending", [])
        hit = next((p for p in pend if p.get("key") == key), None)
        if hit is None:
            hit = {"key": key, "title": key, "proposed_at": hub._iso(at)}
            pend.append(hit)
        if not hit.get("raised_at"):
            hit["raised_at"] = hub._iso(at)
        hub.save_json(self.ws, "hub.json", h)

    def hub_doc(self):
        return hub.load_json(self.ws, "hub.json", {})

    def session(self, *args, **kw):
        import io
        if args[:1] == ("add",) and not kw.get("raw"):
            self.raise_card(args[1], kw.get("at", self.AT))
        buf = io.StringIO()
        rc = hub.session_cmd(self.ws, self.cfg, kw.get("at", self.AT), list(args), False, buf)
        return rc, buf.getvalue()

    def pending(self, *args, **kw):
        import io
        buf = io.StringIO()
        rc = hub.pending_cmd(self.ws, self.cfg, kw.get("at", self.AT), list(args), False, buf)
        return rc, buf.getvalue()

    def test_pending_raised_stamps_name_and_time(self):
        self.decide({"line_id": "x:1", "key": "PLAT-2", "decision": "propose", "title": "P"})
        self.assertEqual(self.hub_doc()["pending"][0]["name"], "")
        later = datetime(2026, 10, 29, 10, 30, tzinfo=UTC)
        rc, _ = self.pending("raised", "PLAT-2", "--name", "plat-2", at=later)
        self.assertEqual(rc, 0)
        p = self.hub_doc()["pending"][0]
        self.assertEqual((p["name"], p["raised_at"]), ("plat-2", "2026-10-29T10:30:00Z"))
        self.assertEqual(p["proposed_at"], "2026-10-29T10:00:00Z")

    def test_pending_raised_unknown_key_exits_1(self):
        rc, _ = self.pending("raised", "PLAT-404", "--name", "x")
        self.assertEqual(rc, 1)
        self.assertEqual(self.pending("raised", "PLAT-404")[0], 2)

    def test_tail_collapses_by_key_and_skips_resolved(self):
        self.add(line(key="PLAT-1", at="2026-10-29T09:00:00Z", title="old"),
                 line(key="PLAT-1", at="2026-10-29T09:05:00Z", title="new", also=["PLAT-9"]),
                 line(key="PLAT-2", at="2026-10-29T09:01:00Z"),
                 line(key="PLAT-2", at="2026-10-29T09:06:00Z", kind="resolved"),
                 line(source="hub", key="sweep", kind="sweep", at="2026-10-29T09:07:00Z"),
                 line(source="hub", key="watcher:chat", kind="error", at="2026-10-29T09:08:00Z"))
        hub.save_json(self.ws, "hub.json", {"version": 1, "pending": [{"key": "PLAT-2"}, {"key": "PLAT-1"}],
                                           "updates": [{"id": "U1", "key": "PLAT-2", "state": "open"}]})
        out = self.tail_lines()
        self.assertEqual([(l["kind"], l["key"]) for l in out],
                         [("update", "PLAT-1"), ("sweep", "sweep"), ("error", "watcher:chat")])
        self.assertEqual(out[0]["title"], "new")
        self.assertEqual(out[0]["also"], ["PLAT-9"])
        h = self.hub_doc()
        self.assertEqual([p["key"] for p in h["pending"]], ["PLAT-1"])
        self.assertEqual(h["updates"], [])
        self.assertNotIn("inbox_cursor", h)  # tail never moves the cursor

    def test_decide_advances_cursor_and_logs(self):
        a = line(key="PLAT-1", at="2026-10-29T09:00:00Z")
        b = line(key="PLAT-2", at="2026-10-29T09:01:00Z")
        self.add(a, b)
        res = self.decide({"line_id": a["id"], "key": "PLAT-1", "decision": "log"})
        self.assertEqual(res[0]["decision"], "log")
        h = self.hub_doc()
        self.assertEqual(h["inbox_cursor"], a["id"])
        self.assertEqual(h["last_wake"], "2026-10-29T10:00:00Z")
        self.assertEqual(h["routed"][0]["decision"], "log")
        self.assertEqual([l["key"] for l in self.tail_lines()], ["PLAT-2"])
        self.decide({"line_id": b["id"], "key": "PLAT-2", "decision": "error"})
        self.assertEqual(self.tail_lines(), [])
        # an older line id never moves the cursor back
        self.decide({"line_id": a["id"], "key": "PLAT-1", "decision": "log"})
        self.assertEqual(self.hub_doc()["inbox_cursor"], b["id"])

    def test_routed_keeps_last_50(self):
        for i in range(55):
            self.decide({"line_id": "x:%d" % i, "key": "K-%d" % i, "decision": "log"})
        r = self.hub_doc()["routed"]
        self.assertEqual(len(r), 50)
        self.assertEqual(r[-1]["key"], "K-54")

    def test_now_and_propose_write_updates_and_pending(self):
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "now", "title": "T", "why": "w",
                     "link": "https://example.com/1"},
                    {"line_id": "x:2", "key": "PLAT-2", "decision": "propose", "title": "P", "read": "r",
                     "next_step": "n", "name": "plat-2"})
        h = self.hub_doc()
        self.assertEqual(h["updates"][0]["id"], "U1")
        self.assertEqual(h["updates"][0]["state"], "open")
        self.assertEqual(h["updates"][0]["why"], "w")
        self.assertEqual(h["pending"][0]["key"], "PLAT-2")
        self.assertEqual(h["pending"][0]["proposed_at"], "2026-10-29T10:00:00Z")
        self.assertIsNone(h["pending"][0]["raised_at"])
        self.assertEqual(h["pending"][0]["name"], "plat-2")
        self.decide({"line_id": "x:3", "key": "PLAT-1", "decision": "now", "title": "T2"})
        self.assertEqual(len(self.hub_doc()["updates"]), 1)

    def test_route_increments_unseen(self):
        self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t",
                     "--link", "claude://session/S1")
        res = self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "route"})
        self.assertEqual(res[0]["session_id"], "S1")
        s = hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]
        self.assertEqual(s["unseen"], 1)
        self.assertEqual(s["last_routed_at"], "2026-10-29T10:00:00Z")
        self.assertEqual(s["link"], "claude://session/S1")
        self.assertEqual(self.hub_doc()["routed"][0]["session_id"], "S1")
        self.session("seen", "PLAT-1")
        self.assertEqual(hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]["unseen"], 0)

    def test_route_to_dropped_session_becomes_proposal(self):
        self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t")
        self.session("drop", "PLAT-1")
        res = self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "route", "title": "T"},
                          {"line_id": "x:2", "key": "PLAT-7", "decision": "route"})
        for r in res:
            self.assertEqual(r["decision"], "propose")
            self.assertIn("proposal", r["note"])
        h = self.hub_doc()
        self.assertEqual({p["key"] for p in h["pending"]}, {"PLAT-1", "PLAT-7"})
        self.assertEqual(h["routed"][0]["decision"], "propose")
        self.assertEqual(hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]["unseen"], 0)

    def test_also_key_finds_session(self):
        self.session("add", "PLAT-12", "--id", "S12", "--name", "n", "--title", "t", "--also", "o/r#4")
        l = line(source="chat", key="C00000001/1700.1", also=["PLAT-12"])
        self.add(l)
        res = self.decide({"line_id": l["id"], "key": l["key"], "decision": "route"})
        self.assertEqual(res[0]["session_id"], "S12")
        self.assertEqual(hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-12"]["unseen"], 1)
        res = self.decide({"line_id": "x:9", "key": "o/r#4", "decision": "route"})
        self.assertEqual(res[0]["session_id"], "S12")
        rc, out = self.session("for", "o/r#4")
        self.assertEqual((rc, out.strip()), (0, "S12"))
        self.assertEqual(self.session("for", "PLAT-99")[0], 1)

    def test_session_add_clears_pending(self):
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "propose"},
                    {"line_id": "x:2", "key": "PLAT-2", "decision": "propose"},
                    {"line_id": "x:3", "key": "PLAT-1", "decision": "now"})
        self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t")
        h = self.hub_doc()
        self.assertEqual([p["key"] for p in h["pending"]], ["PLAT-2"])
        self.assertEqual(h["updates"], [])  # its key gained a session
        s = hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]
        self.assertEqual((s["state"], s["unseen"], s["link"], s["also"]), ("live", 0, None, []))

    def test_updates_expire_after_ttl_working_hours(self):
        self.cfg["hub"]["update_ttl_hours"] = 8
        # Thursday 09:00 UTC; default hours 06:00-22:00 Mon-Fri
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "now", "title": "t"},
                    at=datetime(2026, 10, 29, 9, 0, tzinfo=UTC))
        # Thursday 16:00 = 7 working hours: still there
        self.decide({"line_id": "x:2", "key": "PLAT-2", "decision": "log"}, at=datetime(2026, 10, 29, 16, 0, tzinfo=UTC))
        self.assertEqual(len(self.hub_doc()["updates"]), 1)
        # Saturday evening: hours Thu 16-22 (6) + Fri (16) > 8 -> gone; a weekend alone would not count
        self.decide({"line_id": "x:3", "key": "PLAT-3", "decision": "log"}, at=datetime(2026, 10, 31, 20, 0, tzinfo=UTC))
        self.assertEqual(self.hub_doc()["updates"], [])

    def test_updates_survive_a_weekend(self):
        self.cfg["hub"]["update_ttl_hours"] = 8
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "now", "title": "t"},
                    at=datetime(2026, 10, 30, 20, 0, tzinfo=UTC))  # Friday 20:00, 2 working hours left in day
        self.decide({"line_id": "x:2", "key": "PLAT-2", "decision": "log"}, at=datetime(2026, 11, 1, 20, 0, tzinfo=UTC))
        self.assertEqual(len(self.hub_doc()["updates"]), 1)

    def test_update_done_and_snooze(self):
        import io
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "now", "title": "a"},
                    {"line_id": "x:2", "key": "PLAT-2", "decision": "now", "title": "b"})
        out = io.StringIO()
        self.assertEqual(hub.update_cmd(self.ws, self.cfg, self.AT, "done", ["U1"], False, out), 0)
        self.assertEqual(hub.update_cmd(self.ws, self.cfg, self.AT, "snooze", ["U2", "1h"], False, out), 0)
        ups = {u["id"]: u for u in self.hub_doc()["updates"]}
        self.assertEqual(ups["U1"]["state"], "done")
        self.assertEqual(ups["U2"]["state"], "snoozed")
        self.assertEqual(ups["U2"]["snooze_until"], "2026-10-29T11:00:00Z")
        self.assertEqual(hub.update_cmd(self.ws, self.cfg, self.AT, "done", ["U9"], False, out), 1)
        self.assertEqual(hub.update_cmd(self.ws, self.cfg, self.AT, "snooze", ["U2", "soon"], False, out), 2)
        # once the snooze ends the update is open again
        self.decide({"line_id": "x:3", "key": "PLAT-3", "decision": "log"}, at=self.AT + timedelta(hours=2))
        self.assertEqual({u["id"]: u["state"] for u in self.hub_doc()["updates"]}["U2"], "open")

    def test_tail_reads_rotated_files_from_cursor(self):
        a = line(key="PLAT-1", at="2026-10-28T09:00:00Z")
        b = line(key="PLAT-2", at="2026-10-28T09:01:00Z")
        self.add(a, b)
        hub.rotate_inbox(self.ws, datetime(2026, 10, 28, 23, 0, tzinfo=UTC))
        c = line(key="PLAT-3", at="2026-10-29T09:00:00Z")
        self.add(c)
        hub.save_json(self.ws, "hub.json", {"version": 1, "inbox_cursor": a["id"]})
        self.assertEqual([l["key"] for l in self.tail_lines()], ["PLAT-2", "PLAT-3"])

    def test_tail_pruned_cursor_starts_from_oldest_and_says_so(self):
        import io
        self.add(line(key="PLAT-1"))
        hub.save_json(self.ws, "hub.json", {"version": 1, "inbox_cursor": "tracker:GONE:2020-01-01T00:00:00Z"})
        err = io.StringIO()
        self.assertEqual([l["key"] for l in self.tail_lines(err=err)], ["PLAT-1"])
        self.assertIn("no longer in the inbox", err.getvalue())

    def test_tail_follow_survives_rotation(self):
        import io

        class Out(io.StringIO):
            pass
        out = Out()
        self.add(line(key="PLAT-1", at="2026-10-29T09:00:00Z"))
        step = {"n": 0}

        def sleep(_):
            step["n"] += 1
            if step["n"] == 1:
                self.add(line(key="PLAT-2", at="2026-10-29T09:01:00Z"))
            elif step["n"] == 2:
                hub.rotate_inbox(self.ws, datetime(2026, 10, 29, 23, 0, tzinfo=UTC))
                self.add(line(key="PLAT-3", at="2026-10-30T09:00:00Z"))

        hub.tail(self.ws, follow=True, out=out, err=io.StringIO(), sleep=sleep, stop=lambda: step["n"] >= 3)
        keys = [json.loads(x)["key"] for x in out.getvalue().splitlines()]
        self.assertEqual(keys, ["PLAT-1", "PLAT-2", "PLAT-3"])

    def test_unknown_line_id_is_refused(self):
        import io
        from unittest import mock
        a = line(key="PLAT-1")
        self.add(a)
        self.decide({"line_id": a["id"], "key": "PLAT-1", "decision": "log"})
        before = self.hub_doc()
        with self.assertRaises(ValueError):
            hub.decide(self.ws, self.cfg, self.AT, [{"line_id": a["id"], "key": "PLAT-1", "decision": "log"},
                                                    {"line_id": "nope", "key": "K", "decision": "log"}])
        self.assertEqual(self.hub_doc(), before)
        body = json.dumps({"line_id": "nope", "key": "K", "decision": "log"})
        with mock.patch("sys.stdin", io.StringIO(body)), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(hub.main(["decide"], self.ws, self.cfg, self.AT), 2)
        self.assertEqual(self.hub_doc(), before)

    def test_out_of_order_batch_keeps_inbox_order(self):
        a = line(key="PLAT-1", at="2026-10-29T09:00:00Z")
        b = line(key="PLAT-2", at="2026-10-29T09:01:00Z")
        c = line(key="PLAT-3", at="2026-10-29T09:02:00Z")
        self.add(a, b, c)
        res = self.decide({"line_id": c["id"], "key": "PLAT-3", "decision": "log"},
                          {"line_id": a["id"], "key": "PLAT-1", "decision": "log"},
                          {"line_id": b["id"], "key": "PLAT-2", "decision": "log"})
        self.assertEqual([r["key"] for r in res], ["PLAT-1", "PLAT-2", "PLAT-3"])
        h = self.hub_doc()
        self.assertEqual([r["key"] for r in h["routed"]], ["PLAT-1", "PLAT-2", "PLAT-3"])
        self.assertEqual(h["inbox_cursor"], c["id"])
        res = self.decide({"line_id": a["id"], "key": "PLAT-1", "decision": "log"})
        self.assertTrue(res[0]["already_decided"])
        h = self.hub_doc()
        self.assertEqual(h["inbox_cursor"], c["id"])
        self.assertEqual(h["routed"][-1]["key"], "PLAT-3")

    def test_now_with_live_session_becomes_route(self):
        self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t")
        res = self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "now", "title": "T"})
        self.assertEqual(res[0]["decision"], "route")
        self.assertEqual(res[0]["session_id"], "S1")
        self.assertIn("route", res[0]["note"])
        self.assertNotIn("update_id", res[0])
        h = self.hub_doc()
        self.assertEqual(h["updates"], [])
        self.assertEqual(h["routed"][0]["decision"], "route")
        s = hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]
        self.assertEqual((s["unseen"], s["last_routed_at"]), (1, "2026-10-29T10:00:00Z"))

    def test_tail_exits_quietly_on_interrupt_or_broken_pipe(self):
        from unittest import mock
        for exc in (KeyboardInterrupt, BrokenPipeError):
            with mock.patch.object(hub, "tail", side_effect=exc), \
                    mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
                self.assertEqual(hub.main(["tail", "--follow"], self.ws, self.cfg, self.AT), 0)

    def test_main_decide_json_and_bad_input(self):
        import io
        from unittest import mock
        l1 = line(key="PLAT-1")
        self.add(l1)
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"line_id": l1["id"], "key": "PLAT-1", "decision": "log"}))), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as so:
            self.assertEqual(hub.main(["decide", "--json"], self.ws, self.cfg, self.AT), 0)
        self.assertEqual(json.loads(so.getvalue())[0]["decision"], "log")
        with mock.patch("sys.stdin", io.StringIO('[{"line_id":"a","key":"k","decision":"bogus"}]')), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(hub.main(["decide"], self.ws, self.cfg, self.AT), 2)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as so:
            self.assertEqual(hub.main(["show", "--json"], self.ws, self.cfg, self.AT), 0)
        self.assertIn("pending", json.loads(so.getvalue())["hub"])

    def test_also_given_as_a_string_is_one_key(self):
        import io
        from unittest import mock
        l = line(key="C00000001/4.0")
        self.add(l)
        body = json.dumps({"line_id": l["id"], "key": l["key"], "decision": "propose", "also": "PLAT-12"})
        with mock.patch("sys.stdin", io.StringIO(body)), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(hub.main(["decide"], self.ws, self.cfg, self.AT), 0)
        self.assertEqual(self.hub_doc()["pending"][0]["also"], ["PLAT-12"])
        bad = json.dumps({"line_id": l["id"], "key": l["key"], "decision": "log", "also": [1]})
        with mock.patch("sys.stdin", io.StringIO(bad)), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(hub.main(["decide"], self.ws, self.cfg, self.AT), 2)

    def test_cursor_moves_past_the_whole_coalesced_group(self):
        # inbox order is not `at` order: the printed (latest-at) PLAT-1 line sits before an older PLAT-1 line
        a = line(key="PLAT-1", at="2026-10-29T09:05:00Z", title="newest")
        b = line(key="PLAT-2", at="2026-10-29T09:01:00Z")
        c = line(key="PLAT-1", at="2026-10-29T09:00:00Z", title="older, appended later")
        self.add(a, b, c)
        out = self.tail_lines()
        self.assertEqual([(l["key"], l["title"]) for l in out], [("PLAT-1", "newest"), ("PLAT-2", "t")])
        self.decide(*[{"line_id": l["id"], "key": l["key"], "decision": "log"} for l in out])
        self.assertEqual(self.hub_doc()["inbox_cursor"], c["id"])
        self.assertEqual(self.tail_lines(), [])

    # ---- C2: pending and unseen do not wait forever
    def test_pending_expires_after_ttl_working_hours(self):
        self.cfg["hub"]["update_ttl_hours"] = 8
        thu9 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "propose"},
                    {"line_id": "x:2", "key": "PLAT-2", "decision": "propose"}, at=thu9)
        self.pending("raised", "PLAT-2", "--name", "plat-2", at=datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
        # Thursday 21:00: PLAT-1 (proposed 09:00) is 12 working hours old; PLAT-2 (raised 15:00) is 6
        self.decide({"line_id": "x:3", "key": "PLAT-3", "decision": "log"}, at=datetime(2026, 10, 29, 21, 0, tzinfo=UTC))
        self.assertEqual([p["key"] for p in self.hub_doc()["pending"]], ["PLAT-2"])

    def test_pending_drop(self):
        import io
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "propose"})
        self.pending("raised", "PLAT-1", "--name", "plat-1", "--task", "task_9")
        self.decide({"line_id": "x:2", "key": "PLAT-1", "decision": "propose"})  # a newer line keeps the card id
        self.assertEqual(self.hub_doc()["pending"][0]["task_id"], "task_9")
        buf = io.StringIO()
        self.assertEqual(hub.pending_cmd(self.ws, self.cfg, self.AT, ["drop", "PLAT-1"], True, buf), 0)
        self.assertEqual(json.loads(buf.getvalue())["task_id"], "task_9")
        self.decide({"line_id": "x:3", "key": "PLAT-1", "decision": "propose"})
        self.assertEqual(self.pending("drop", "PLAT-1")[0], 0)
        self.assertEqual(self.hub_doc()["pending"], [])
        self.assertEqual(self.pending("drop", "PLAT-1")[0], 1)
        self.assertEqual(self.pending("drop")[0], 2)

    def test_pending_work_ignores_old_unseen(self):
        self.cfg["hub"]["update_ttl_hours"] = 8
        self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t")
        thu9 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "route"}, at=thu9)
        self.assertTrue(hub.pending_work(self.ws, self.cfg, thu9 + timedelta(hours=1)))
        self.assertFalse(hub.pending_work(self.ws, self.cfg, thu9 + timedelta(hours=12)))
        self.session("seen", "PLAT-1")
        self.assertFalse(hub.pending_work(self.ws, self.cfg, thu9 + timedelta(hours=1)))
        self.decide({"line_id": "x:2", "key": "PLAT-2", "decision": "propose"}, at=thu9)
        self.assertTrue(hub.pending_work(self.ws, self.cfg, thu9 + timedelta(hours=1)))

    # ---- I2: decide is idempotent per line
    def test_decide_twice_is_already_decided_without_side_effects(self):
        self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t")
        a = line(key="PLAT-1")
        self.add(a)
        self.decide({"line_id": a["id"], "key": "PLAT-1", "decision": "route"})
        before = (self.hub_doc(), hub.load_json(self.ws, "sessions.json", {}))
        res = self.decide({"line_id": a["id"], "key": "PLAT-1", "decision": "route"},
                          at=self.AT + timedelta(minutes=5))
        self.assertEqual(res, [{"line_id": a["id"], "key": "PLAT-1", "already_decided": True}])
        self.assertEqual((self.hub_doc(), hub.load_json(self.ws, "sessions.json", {})), before)
        b = line(key="PLAT-1", at="2026-10-29T09:30:00Z")
        self.add(b)
        res = self.decide({"line_id": b["id"], "key": "PLAT-1", "decision": "route"},
                          {"line_id": b["id"], "key": "PLAT-1", "decision": "route"})
        self.assertEqual([r.get("already_decided", False) for r in res], [False, True])
        self.assertEqual(hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]["unseen"], 2)

    # ---- I3: a thread routed through `also` stays with its session
    def test_route_through_also_keeps_the_thread(self):
        self.session("add", "PLAT-1", "--id", "S1", "--name", "plat-1", "--title", "t")
        l1 = hub.make_line("chat", "update", "C00000001/1.0", "2026-10-29T10:00:00Z", summary="see PLAT-1",
                           also=["PLAT-1", "o/r#4"])
        l2 = hub.make_line("chat", "update", "C00000001/1.0", "2026-10-29T10:05:00Z", summary="any news?")
        self.add(l1)
        self.assertEqual(self.decide({"line_id": l1["id"], "key": l1["key"], "decision": "route"})[0]["session_id"], "S1")
        s = hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]
        self.assertEqual(s["also"], ["C00000001/1.0", "o/r#4"])
        self.add(l2)
        res = self.decide({"line_id": l2["id"], "key": l2["key"], "decision": "route"})
        self.assertEqual((res[0]["decision"], res[0].get("session_id")), ("route", "S1"))

    def test_session_also_capped_at_50(self):
        self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t",
                     "--also", ",".join("K-%d" % i for i in range(50)))
        l = line(source="chat", key="C00000001/2.0", also=["PLAT-1"])
        self.add(l)
        self.decide({"line_id": l["id"], "key": l["key"], "decision": "route"})
        also = hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]["also"]
        self.assertEqual(len(also), 50)
        self.assertEqual((also[0], also[-1]), ("K-1", "C00000001/2.0"))

    def test_pending_keeps_also(self):
        l = line(source="chat", key="C00000001/3.0", also=["PLAT-7"])
        self.add(l)
        self.decide({"line_id": l["id"], "key": l["key"], "decision": "propose"})
        self.assertEqual(self.hub_doc()["pending"][0]["also"], ["PLAT-7"])

    # ---- I8: what arrived between a card and its start is caught up
    def test_session_add_reports_catch_up_after_raise(self):
        import io
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "propose", "read": "r1", "next_step": "n1"})
        self.pending("raised", "PLAT-1", "--name", "plat-1", at=self.AT + timedelta(minutes=1))
        self.decide({"line_id": "x:2", "key": "PLAT-1", "decision": "route", "read": "r2", "next_step": "n2"},
                    at=self.AT + timedelta(minutes=10))
        p = self.hub_doc()["pending"][0]
        self.assertEqual((p["last_update_at"], p["raised_at"], p["read"]),
                         ("2026-10-29T10:10:00Z", "2026-10-29T10:01:00Z", "r2"))
        buf = io.StringIO()
        hub.session_cmd(self.ws, self.cfg, self.AT + timedelta(minutes=20),
                        ["add", "PLAT-1", "--id", "S1", "--name", "plat-1", "--title", "t"], True, buf)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["catch_up"], {"key": "PLAT-1", "read": "r2", "next_step": "n2",
                                           "link": "https://example.com/PLAT-1", "at": "2026-10-29T10:10:00Z"})
        # no update since the raise: no catch-up
        self.decide({"line_id": "x:3", "key": "PLAT-2", "decision": "propose"})
        self.pending("raised", "PLAT-2", "--name", "plat-2", at=self.AT + timedelta(minutes=1))
        buf = io.StringIO()
        hub.session_cmd(self.ws, self.cfg, self.AT + timedelta(minutes=20),
                        ["add", "PLAT-2", "--id", "S2", "--name", "plat-2", "--title", "t"], True, buf)
        self.assertNotIn("catch_up", json.loads(buf.getvalue()))

    # ---- red-team fix 5: `session add` only for a raised card or a session the hub already follows
    def test_session_add_needs_a_raised_card_or_an_existing_session(self):
        rc, _ = self.session("add", "EVIL-1", "--id", "local_main", "--name", "main", "--title", "x", raw=True)
        self.assertEqual(rc, 1)
        self.assertNotIn("EVIL-1", hub.load_json(self.ws, "sessions.json", {}).get("sessions", {}))
        self.decide({"line_id": "x:1", "key": "PLAT-1", "decision": "propose"})
        rc, _ = self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t", raw=True)
        self.assertEqual(rc, 1)  # proposed, but no card was raised
        self.pending("raised", "PLAT-1", "--name", "plat-1")
        rc, _ = self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t", raw=True)
        self.assertEqual(rc, 0)
        rc, _ = self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t2", raw=True)
        self.assertEqual(rc, 0)  # already in sessions.json

    # ---- round 1, finding 4: a live key is not repointed to another session
    def test_session_add_does_not_repoint_a_live_key(self):
        self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t")
        rc, _ = self.session("add", "PLAT-1", "--id", "S-OTHER", "--name", "n", "--title", "t", raw=True)
        self.assertEqual(rc, 1)
        self.assertEqual(hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]["session_id"], "S1")
        rc, _ = self.session("add", "PLAT-1", "--id", "S1", "--name", "n", "--title", "t2", raw=True)
        self.assertEqual(rc, 0)  # the same id: a refresh
        # a newly started card for the key (pending, raised) may take it over
        h = hub.load_json(self.ws, "hub.json", {})
        h.setdefault("pending", []).append({"key": "PLAT-1", "title": "t", "proposed_at": "2026-10-29T10:00:00Z",
                                            "raised_at": "2026-10-29T10:01:00Z"})
        hub.save_json(self.ws, "hub.json", h)
        rc, _ = self.session("add", "PLAT-1", "--id", "S2", "--name", "n", "--title", "t", raw=True)
        self.assertEqual(rc, 0)
        self.assertEqual(hub.load_json(self.ws, "sessions.json", {})["sessions"]["PLAT-1"]["session_id"], "S2")

    # ---- red-team smaller item: decide refuses a line id whose line has another key
    def test_decide_refuses_a_line_id_with_another_key(self):
        l = line(key="PLAT-1")
        self.add(l)
        with self.assertRaises(ValueError):
            hub.decide(self.ws, self.cfg, self.AT, [{"line_id": l["id"], "key": "PLAT-9", "decision": "route"}])
        self.assertEqual(self.hub_doc().get("routed", []), [])
        import io
        import unittest.mock as um
        with um.patch("sys.stdin", io.StringIO(json.dumps([{"line_id": l["id"], "key": "PLAT-9",
                                                             "decision": "log"}]))), \
                um.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(hub.main(["decide"], self.ws, self.cfg, self.AT), 2)
        self.assertIn("different key", err.getvalue())


class Direct(unittest.TestCase):
    """The manager's watch list (follow / mute) and `manual` lines."""
    AT = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = {"manager": {"timezone": "UTC", "email": "dana@example.com", "chat_dm": "U00000000"},
                    "connectors": {"tracker": "jira"},
                    "hub": {"enabled": True, "connections": {"tracker": {"command": "x", "args": [], "env": {}}},
                            "watch": {"chat": {"channels": ["C00000001"]}, "tracker": {"projects": ["PLAT"]}}}}

    def run_verb(self, *args):
        import io
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            rc = hub.main(list(args), self.ws, self.cfg, self.AT)
        finally:
            sys.stdout = real
        return rc, out.getvalue()

    def inbox(self):
        return hub.read_inbox(self.ws)

    def test_add_link_appends_manual_line_with_key(self):
        rc, _ = self.run_verb("add", "https://example.com/browse/PLAT-7")
        self.assertEqual(rc, 0)
        (l,) = self.inbox()
        self.assertEqual((l["kind"], l["source"], l["key"]), ("manual", "manual", "PLAT-7"))
        self.assertEqual(l["link"], "https://example.com/browse/PLAT-7")
        self.run_verb("add", "https://example.com/archives/C00000001/p1700000000123456")
        self.assertEqual(self.inbox()[-1]["key"], "C00000001/1700000000.123456")
        self.run_verb("add", "look at this", "--key", "mine")
        self.assertEqual(self.inbox()[-1]["key"], "mine")

    def test_add_key_takes_only_the_hub_token_charset(self):
        for bad in ("PLAT 1", "it's", '"x"', "a;b", "x?y", "#1"):
            rc, _ = self.run_verb("add", "a note", "--key", bad)
            self.assertEqual(rc, 2, bad)
        self.assertEqual(self.inbox(), [])
        for good in ("PLAT-1", "acme/web#12", "C00000001/1700000000.000100", "manual:abc"):
            self.assertEqual(self.run_verb("add", "a note", "--key", good)[0], 0, good)

    def test_unfollow_of_a_configured_project_says_it_is_still_watched(self):
        self.cfg["teams"] = [{"slug": "web", "tracker": {"project": "WEB"}}]
        for x in ("PLAT", "WEB", "C00000001"):
            rc, out = self.run_verb("unfollow", x)
            self.assertEqual(rc, 0)
            self.assertIn("still watched from config.json", out, x)
        self.run_verb("follow", "OPS")
        self.assertNotIn("still watched", self.run_verb("unfollow", "OPS")[1])

    def test_watching_includes_team_projects(self):
        self.cfg["teams"] = [{"slug": "web", "tracker": {"project": "WEB"}}]
        self.run_verb("follow", "OPS")
        self.assertIn("tracker projects: PLAT, OPS, WEB", self.run_verb("watching")[1])
        doc = json.loads(self.run_verb("watching", "--json")[1])
        self.assertEqual(doc["watch"]["tracker"]["projects"], ["PLAT", "OPS", "WEB"])

    def test_add_text_uses_manual_hash_key(self):
        self.run_verb("add", "ask Agnes about the launch")
        self.run_verb("add", "ask Agnes about the launch")
        a, b = self.inbox()
        self.assertTrue(a["key"].startswith("manual:"))
        self.assertEqual(a["key"], b["key"])
        self.assertEqual(self.run_verb("add")[0], 2)

    def test_manual_line_never_collapsed_or_resolved(self):
        m1 = line(source="manual", kind="manual", key="PLAT-1", at="2026-10-29T09:00:00Z")
        m2 = line(source="manual", kind="manual", key="PLAT-1", at="2026-10-29T09:00:00Z", summary="again")
        res = line(kind="resolved", key="PLAT-1", at="2026-10-29T09:30:00Z")
        upd = line(kind="update", key="PLAT-1", at="2026-10-29T09:10:00Z")
        self.assertEqual(len(hub.coalesce([m1, upd, m2, res])), 3)
        self.assertEqual(sum(1 for l in hub.coalesce([m1, m2]) if l["kind"] == "manual"), 2)
        hub.append_inbox(self.ws, [m1, upd, res])
        h = {"pending": [], "updates": [{"id": "U1", "key": "PLAT-1", "state": "open"}], "routed": []}
        hub.save_json(self.ws, "hub.json", h)
        out = hub.digest(self.ws, hub.read_inbox(self.ws))
        self.assertEqual([l["kind"] for l in out], ["manual"])
        self.assertEqual(len(hub.load_json(self.ws, "hub.json", {})["updates"]), 1)  # not resolved away
        self.assertTrue(hub._must_decide([m1, res]))

    def test_follow_classifies_targets(self):
        self.run_verb("follow", "--channel", "C00000002")
        self.run_verb("follow", "--channel", "G00000003")
        for x, part in (("WEB", "projects"), ("C00000002", "channels"), ("G00000003", "channels"),
                        ("C00000002/1700000000.000100", "threads"), ("example/repo", "repos")):
            self.assertEqual(self.run_verb("follow", x)[0], 0, x)
            self.assertIn(x, hub.load_watch(self.ws)["follow"][part])
        self.assertEqual(self.run_verb("follow", "no good")[0], 2)
        self.assertEqual(self.run_verb("follow", "plat-1")[0], 2)
        self.run_verb("follow", "WEB")
        self.assertEqual(hub.load_watch(self.ws)["follow"]["projects"], ["WEB"])

    def test_double_dash_ends_flags_for_add_follow_and_mute(self):
        self.assertEqual(self.run_verb("follow", "--", "WEB")[0], 0)
        self.assertIn("WEB", hub.load_watch(self.ws)["follow"]["projects"])
        self.assertEqual(self.run_verb("follow", "--project", "--", "CORE")[0], 0)
        self.assertEqual(self.run_verb("mute", "--", "PLAT-9")[0], 0)
        self.assertIn("PLAT-9", hub.load_watch(self.ws)["mute"]["keys"])
        self.assertEqual(self.run_verb("unmute", "--", "PLAT-9")[0], 0)
        self.assertEqual(self.run_verb("add", "--", "https://example.com/browse/PLAT-7")[0], 0)
        (l,) = self.inbox()
        self.assertEqual((l["key"], l["link"]), ("PLAT-7", "https://example.com/browse/PLAT-7"))
        self.assertEqual(self.run_verb("follow", "--")[0], 2)

    def test_follow_bare_token_is_project_unless_known_or_forced(self):
        for x in ("CORE2024", "GROWTH1", "C00000099"):
            self.run_verb("follow", x)
        f = hub.load_watch(self.ws)["follow"]
        self.assertEqual(f["projects"], ["CORE2024", "GROWTH1", "C00000099"])
        self.run_verb("follow", "--channel", "C00000098")
        self.run_verb("follow", "C00000001")  # already a configured channel
        f = hub.load_watch(self.ws)["follow"]
        self.assertEqual((f["channels"], f["projects"]), (["C00000098", "C00000001"], ["CORE2024", "GROWTH1", "C00000099"]))
        self.assertEqual(self.run_verb("follow", "--repo", "WEB")[0], 2)

    def test_mute_email_is_exact(self):
        mute = {"people": ["bob@example.com"]}
        self.assertTrue(hub.is_muted(line(who="Bob <bob@example.com>"), mute))
        self.assertTrue(hub.is_muted(line(who="BOB@example.com"), mute))
        self.assertFalse(hub.is_muted(line(who="Rob <rbob@example.com>"), mute))
        self.assertFalse(hub.is_muted(line(who="bob@example.com.evil.net"), mute))

    def test_add_strips_control_chars_and_bad_links(self):
        self.run_verb("add", "see https://example.com/x\x1b[31m\x07 \u202ehi", "--key", "k\x1by")
        (l,) = self.inbox()
        for f in ("summary", "title", "link", "key"):
            self.assertFalse(any(c in l[f] for c in "\x1b\x07\u202e"), f)
        self.assertEqual(l["key"], "ky")
        self.assertRegex(l["link"], r"^https?://\S+$")

    def test_identical_adds_in_one_second_both_reach_tail(self):
        self.run_verb("add", "same thing")
        self.run_verb("add", "same thing")
        ids = [l["id"] for l in self.inbox()]
        self.assertEqual(len(set(ids)), 2)
        import io
        buf = io.StringIO()
        hub.tail(self.ws, out=buf, err=io.StringIO())
        self.assertEqual(len(buf.getvalue().splitlines()), 2)

    def test_unfollow_thread_stops_polling(self):
        import types
        t = "C00000001/1700000000.000100"
        client = types.SimpleNamespace(calls=[], call=None)

        def call(name, args):
            client.calls.append((name, dict(args)))
            return {"messages": []}
        client.call = call
        cur = {"ts": "%.6f" % (self.AT.timestamp() - 60), "threads": {}}
        cfg = {"follow": [t]}
        res = hub_sources.ChatSlack.poll(client, cur, cfg, {}, self.AT)
        self.assertIn(t, res["cursor"]["threads"])
        res = hub_sources.ChatSlack.poll(client, res["cursor"], {"follow": []}, {}, self.AT)
        self.assertNotIn(t, res["cursor"]["threads"])

    def test_unfollow_removes(self):
        self.run_verb("follow", "WEB")
        w = hub.load_watch(self.ws)
        w["auto"]["projects"] = [{"key": "WEB", "from": "PLAT-1", "at": "x"}]
        hub.save_watch(self.ws, w)
        self.assertEqual(self.run_verb("unfollow", "WEB")[0], 0)
        w = hub.load_watch(self.ws)
        self.assertEqual((w["follow"]["projects"], w["auto"]["projects"]), ([], []))
        self.assertEqual(self.run_verb("unfollow", "WEB")[0], 0)  # absent: no change, no error

    def test_mute_classifies_and_unmutes(self):
        for x in ("PLAT-9", "U00000009", "agnes@example.com", "C00000009"):
            self.run_verb("mute", x)
        m = hub.load_watch(self.ws)["mute"]
        self.assertEqual((m["keys"], m["people"], m["channels"]),
                         (["PLAT-9"], ["U00000009", "agnes@example.com"], ["C00000009"]))
        self.run_verb("unmute", "PLAT-9")
        self.assertEqual(hub.load_watch(self.ws)["mute"]["keys"], [])

    def test_mute_drops_watcher_lines_not_manual(self):
        class A:
            SOURCE = "tracker"
            READ_TOOLS = set()
            seen = None

            @classmethod
            def poll(cls, client, cursor, cfg, me, at):
                cls.seen = cfg
                if cursor is None:
                    return {"lines": [], "cursor": {"n": 1}, "partial": []}
                return {"lines": [line(key="PLAT-1"), line(key="PLAT-2", also=["PLAT-8"]),
                                  line(key="PLAT-3", who="U00000009"), line(key="PLAT-4", who="Agnes <a@example.com>"),
                                  line(source="chat", key="C00000009/1.0", who="U1"),
                                  line(source="manual", kind="manual", key="PLAT-1")], "cursor": {"n": 2},
                        "partial": []}
        saved = dict(hub_sources.ADAPTERS)
        hub_sources.ADAPTERS["tracker:jira"] = A
        self.addCleanup(lambda: (hub_sources.ADAPTERS.clear(), hub_sources.ADAPTERS.update(saved)))
        for x in ("PLAT-1", "PLAT-8", "U00000009", "a@example.com", "C00000009"):
            self.run_verb("mute", x)
        self.run_verb("follow", "WEB")
        clients = {"tracker": object()}
        hub.poll_once(self.ws, self.cfg, self.AT, clients)
        hub.poll_once(self.ws, self.cfg, self.AT + timedelta(minutes=2), clients)
        self.assertEqual([(l["source"], l["key"]) for l in self.inbox()], [("manual", "PLAT-1")])
        self.assertEqual(set(A.seen["projects"]), {"PLAT", "WEB"})

    def poll_with(self, adapter, minutes=0):
        saved = dict(hub_sources.ADAPTERS)
        hub_sources.ADAPTERS["tracker:jira"] = adapter
        self.addCleanup(lambda: (hub_sources.ADAPTERS.clear(), hub_sources.ADAPTERS.update(saved)))
        return hub.poll_once(self.ws, self.cfg, self.AT + timedelta(minutes=minutes), {"tracker": object()})

    def adapter(self, keys, drop=None, known="all"):
        if known == "all":  # every key-shaped token names a real project
            known = sorted({x.split("-")[0] for k, a in keys for x in [k] + list(a) if re.match(r"^[A-Z][A-Z0-9]+-", x)})

        class A:
            READ_TOOLS = set()
            seen = []

            @classmethod
            def poll(cls, client, cursor, cfg, me, at):
                cls.seen.append(cfg)
                if cursor is None:
                    return {"lines": [], "cursor": {"n": 1}, "partial": [], "known_projects": known}
                out = {"lines": [line(key=k, also=a) for k, a in keys], "cursor": {"n": 2}, "partial": [],
                       "known_projects": known}
                if drop:
                    out["drop_auto"] = drop
                return out
        return A

    def test_mention_of_unwatched_project_auto_follows(self):
        A = self.adapter([("PLAT-1", ["WEB-4"]), ("C00000001/1.0", ["OPS-2", "plat-9"]), ("MUTED-1", []),
                          ("GONE-1", [])])
        self.run_verb("mute", "MUTED")
        self.run_verb("unfollow", "GONE")
        self.poll_with(A)
        self.poll_with(A, 2)
        w = hub.load_watch(self.ws)
        self.assertEqual([a["key"] for a in w["auto"]["projects"]], ["WEB", "OPS"])
        self.assertEqual(w["auto"]["projects"][0]["from"], "WEB-4")
        self.assertTrue(w["auto"]["projects"][0]["at"].startswith("2026-10-29"))
        self.poll_with(A, 4)  # now polled, and not added twice
        self.assertEqual(set(A.seen[-1]["projects"]), {"PLAT", "WEB", "OPS"})
        self.assertEqual(len(hub.load_watch(self.ws)["auto"]["projects"]), 2)
        self.run_verb("follow", "GONE")  # follow clears the unfollow record
        self.assertEqual(hub.load_watch(self.ws)["unfollowed"], [])

    def test_auto_follow_takes_only_real_projects(self):
        A = self.adapter([("C00000001/1.0", ["UTF-8", "ISO-8601", "GPT-4", "WEB-4"])], known=["PLAT", "WEB"])
        self.poll_with(A)
        self.poll_with(A, 2)
        self.assertEqual([a["key"] for a in hub.load_watch(self.ws)["auto"]["projects"]], ["WEB"])

    def test_auto_follow_takes_nothing_when_projects_could_not_be_read(self):
        A = self.adapter([("C00000001/1.0", ["WEB-4"])], known=None)
        self.poll_with(A)
        self.poll_with(A, 2)
        self.assertEqual(hub.load_watch(self.ws)["auto"]["projects"], [])

    def test_auto_projects_capped_at_20(self):
        keys = [("P%02d-1" % n, []) for n in range(25)]
        A = self.adapter(keys)
        self.poll_with(A)
        self.poll_with(A, 2)
        got = [a["key"] for a in hub.load_watch(self.ws)["auto"]["projects"]]
        self.assertEqual(len(got), 20)
        self.assertEqual(got[-1], "P24")
        self.assertNotIn("P00", got)

    def test_auto_project_dropped_after_two_failures(self):
        hub.save_json(self.ws, "watch.json", {"auto": {"projects": [{"key": "WEB", "from": "WEB-1", "at": "x"}]}})
        A = self.adapter([], drop=["WEB"])
        self.poll_with(A)
        self.poll_with(A, 2)
        w = hub.load_watch(self.ws)
        self.assertEqual(w["auto"]["projects"], [])
        self.assertEqual(w["unfollowed"], ["WEB"])
        self.run_verb("unfollow", "PLAT")
        self.assertIn("PLAT", hub.load_watch(self.ws)["unfollowed"])

    def test_watching_merges_config_and_file(self):
        self.run_verb("follow", "WEB")
        self.run_verb("follow", "--channel", "C00000002")
        self.run_verb("follow", "C00000002/1700000000.000100")
        self.run_verb("follow", "example/repo")
        self.run_verb("mute", "PLAT-9")
        rc, out = self.run_verb("watching", "--json")
        doc = json.loads(out)["watch"]
        self.assertEqual(doc["chat"]["channels"], ["C00000001", "C00000002"])
        self.assertEqual(doc["chat"]["follow"], ["C00000002/1700000000.000100"])
        self.assertEqual(doc["tracker"]["projects"], ["PLAT", "WEB"])
        self.assertEqual(doc["code"]["repos"], ["example/repo"])
        self.assertEqual(doc["mute"]["keys"], ["PLAT-9"])
        text = self.run_verb("watching")[1]
        self.assertIn("tracker projects: PLAT, WEB", text)
        self.assertEqual(self.cfg["hub"]["watch"]["tracker"]["projects"], ["PLAT"])  # config untouched


SELF_JID = "15550000009@s.whatsapp.net"
DM_JID = "15550000001@s.whatsapp.net"
GROUP_JID = "15550000002-1700000000@g.us"


class WaReader:
    """A fake local reader; the watcher only ever calls `messages` and `ping` on it."""

    def __init__(self, items=(), error=None):
        self.items, self.error, self.pings = list(items), error, 0

    def head(self):
        if self.error:
            raise self.error
        return max([i["pk"] for i in self.items] or [0])

    def messages_after(self, pk, limit):
        if self.error:
            raise self.error
        return [i for i in self.items if i["pk"] > pk][:limit]

    def ping(self):
        self.pings += 1
        if self.error:
            raise self.error


def wa_item(text, at, chat=DM_JID, sid="S1", from_me=False, sender="Dana"):
    return {"pk": int(re.sub(r"\D", "", sid) or 0) + 100 * (1 if from_me else 0), "chat": chat, "chat_name": "Dana", "from_me": from_me, "sender": "me" if from_me else sender,
            "text": text, "at": at, "link": "whatsapp://%s/%s" % (chat, sid)}


class WhatsAppWatcher(unittest.TestCase):
    """The `whatsapp` capability through poll_once, check, smoke and the watch verbs."""
    AT = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)

    def setUp(self):
        import whatsapp
        self.wa = whatsapp
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        saved = os.environ.get("MOTHER_HOSTNAME")
        os.environ["MOTHER_HOSTNAME"] = "examplehost"
        self.addCleanup(lambda: os.environ.pop("MOTHER_HOSTNAME", None) if saved is None
                        else os.environ.__setitem__("MOTHER_HOSTNAME", saved))
        self.cfg = {"version": 1, "manager": {"email": "dana@example.com", "timezone": "UTC", "chat_dm": "U00000000"},
                    "workspace": {"home_machine": "examplehost"},
                    "connectors": {"whatsapp": "whatsapp"},
                    "whatsapp": {"enabled": True, "read": True, "send": False, "self_jid": SELF_JID},
                    "hub": {"enabled": True, "connections": {"whatsapp": {"local": "whatsapp"}},
                            "watch": {"whatsapp": {"groups": [GROUP_JID]}}}}

    def poll(self, reader, minutes=0):
        return hub.poll_once(self.ws, self.cfg, self.AT + timedelta(minutes=minutes), {"whatsapp": reader})

    def inbox(self):
        return hub.read_inbox(self.ws)

    def test_dm_arrives_and_own_reply_resolves(self):
        r = WaReader()
        self.poll(r)
        r.items = [wa_item("lunch?", "2026-10-29T10:00:30Z")]
        beat = self.poll(r, 1)
        self.assertEqual(beat["sources"]["whatsapp"], {"status": "ok"})
        (l,) = self.inbox()
        self.assertEqual((l["source"], l["kind"], l["key"]), ("whatsapp", "update", "wa:" + DM_JID))
        r.items = [wa_item("lunch?", "2026-10-29T10:00:30Z"),
                   wa_item("sure", "2026-10-29T10:01:30Z", sid="S2", from_me=True)]
        self.poll(r, 3)
        self.assertEqual([x["kind"] for x in self.inbox()], ["update", "resolved"])

    def test_disabled_read_off_and_off_home_are_couldnt_check_without_an_error_line(self):
        base = self.cfg
        for edit, word in ((lambda c: c["whatsapp"].update(enabled=False), "whatsapp.enabled"),
                           (lambda c: c["whatsapp"].update(read=False), "whatsapp.read"),
                           (lambda c: c["workspace"].update(home_machine="elsewhere"), "home machine")):
            cfg = json.loads(json.dumps(base))
            edit(cfg)
            self.cfg = cfg
            beat = self.poll(WaReader([wa_item("hi", "2026-10-29T10:00:30Z")]))
            ent = beat["sources"]["whatsapp"]
            self.assertEqual(ent["status"], "couldnt_check")
            self.assertIn(word, ent["reason"])
        self.assertEqual(self.inbox(), [])

    def test_unreadable_database_is_one_error_line(self):
        r = WaReader()
        self.poll(r)  # the first poll reads nothing
        r.error = self.wa.ReaderError("denied")
        for m in (1, 3):
            beat = self.poll(r, m)
        ent = beat["sources"]["whatsapp"]
        self.assertEqual(ent["status"], "couldnt_check")
        self.assertIn("Full Disk Access", ent["reason"])
        (e,) = self.inbox()
        self.assertEqual((e["kind"], e["key"]), ("error", "watcher:whatsapp"))

    def test_mute_by_jid_and_by_key(self):
        l = line(source="whatsapp", key="wa:" + DM_JID, who="Dana <%s>" % DM_JID)
        self.assertTrue(hub.is_muted(l, {"keys": ["wa:" + DM_JID]}))
        self.assertTrue(hub.is_muted(l, {"keys": [DM_JID]}))
        self.assertTrue(hub.is_muted(l, {"people": [DM_JID]}))
        self.assertFalse(hub.is_muted(l, {"keys": ["wa:" + GROUP_JID, "15550000001@g.us"]}))
        self.assertEqual(hub.classify_mute(DM_JID), ("keys", "wa:" + DM_JID))
        self.assertEqual(hub.classify_mute(GROUP_JID), ("keys", "wa:" + GROUP_JID))
        r = WaReader()
        self.poll(r)
        hub.save_json(self.ws, "watch.json", {"mute": {"keys": [DM_JID]}})
        r.items = [wa_item("lunch?", "2026-10-29T10:00:30Z")]
        self.poll(r, 1)
        self.assertEqual(self.inbox(), [])

    def run_verb(self, *args):
        import io
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            rc = hub.main(list(args), self.ws, self.cfg, self.AT)
        finally:
            sys.stdout = real
        return rc, out.getvalue()

    def test_follow_a_whatsapp_jid(self):
        other = "15550000003-1700000001@g.us"
        self.assertEqual(hub.classify_follow(other), ("whatsapp", other))
        self.assertEqual(hub.classify_follow(DM_JID), ("whatsapp", DM_JID))
        self.assertEqual(hub.classify_follow(other, "whatsapp"), ("whatsapp", other))
        self.assertIsNone(hub.classify_follow("not a jid", "whatsapp"))
        self.assertEqual(self.run_verb("follow", "--whatsapp", other)[0], 0)
        self.assertEqual(self.run_verb("follow", DM_JID)[0], 0)
        self.assertEqual(hub.load_watch(self.ws)["follow"]["whatsapp"], [other, DM_JID])
        merged = hub.merged_watch(self.cfg, hub.load_watch(self.ws))
        self.assertEqual(merged["whatsapp"]["groups"], [GROUP_JID, other, DM_JID])
        self.assertIn("whatsapp groups: %s" % GROUP_JID, self.run_verb("watching")[1])
        rc, out = self.run_verb("unfollow", other)
        self.assertEqual(rc, 0)
        self.assertNotIn("still watched", out)
        self.assertIn("still watched from config.json", self.run_verb("unfollow", GROUP_JID)[1])
        self.assertEqual(self.run_verb("follow", "--whatsapp", "WEB")[0], 2)

    def test_a_followed_group_is_read_whole(self):
        other = "15550000003-1700000001@g.us"
        self.run_verb("follow", other)
        r = WaReader()
        self.poll(r)
        r.items = [wa_item("anyone free?", "2026-10-29T10:00:30Z", chat=other, sender="Agnes")]
        self.poll(r, 1)
        (l,) = self.inbox()
        self.assertEqual(l["key"], "wa:" + other)

    def test_build_clients_gives_a_local_reader_with_no_send_path(self):
        got = hub.build_clients(self.cfg, {})
        self.assertIsInstance(got["whatsapp"], hub_sources.LocalWhatsApp)
        self.assertFalse(any(hasattr(got["whatsapp"], n) for n in ("send", "call", "bridge_call", "tools")))

    def test_check_reads_the_database_and_prints_no_content(self):
        import io
        r = WaReader([wa_item("secret plans", "2026-10-29T10:00:30Z")])
        buf = io.StringIO()
        rc = hub.check(self.cfg, factory=lambda cfg, have: {"whatsapp": r}, out=buf)
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(r.pings, 1)
        self.assertRegex(buf.getvalue(), r"^whatsapp \| ok \| ")
        self.assertNotIn("secret", buf.getvalue())
        r.error = self.wa.ReaderError("denied")
        buf = io.StringIO()
        self.assertEqual(hub.check(self.cfg, factory=lambda cfg, have: {"whatsapp": r}, out=buf), 1)
        self.assertIn("whatsapp | ✗ | ", buf.getvalue())
        self.assertIn("Full Disk Access", buf.getvalue())
        self.cfg["whatsapp"]["read"] = False  # off in config: no read at all
        r.error, r.pings = None, 0
        buf = io.StringIO()
        self.assertEqual(hub.check(self.cfg, factory=lambda cfg, have: {"whatsapp": r}, out=buf), 1)
        self.assertEqual(r.pings, 0)
        self.assertIn("whatsapp.read", buf.getvalue())

    def test_smoke_prints_counts_only(self):
        import io
        r = WaReader([wa_item("secret plans", "2026-10-29T10:00:30Z")])
        buf = io.StringIO()
        rc = hub.smoke(self.cfg, self.AT, buf, factory=lambda cfg, have: {"whatsapp": r}, sleep=lambda s: None)
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("whatsapp | ok | ", buf.getvalue())
        self.assertNotIn("secret", buf.getvalue())
        self.assertNotIn(DM_JID, buf.getvalue())

    def test_mute_wa_key_as_the_panel_and_the_hub_skill_send_it(self):
        for jid, who in ((DM_JID, "Dana"), (GROUP_JID, "Agnes")):
            kind, value = hub.classify_mute("wa:" + jid)
            self.assertEqual((kind, value), ("keys", "wa:" + jid))
            l = line(source="whatsapp", key="wa:" + jid, who=who)
            self.assertTrue(hub.is_muted(l, {kind: [value]}))
        self.assertEqual(hub.classify_mute("wa:nonsense")[0], "keys")  # not a JID: still just a key
        self.assertEqual(self.run_verb("mute", "--", "wa:" + DM_JID)[0], 0)
        self.assertEqual(hub.load_watch(self.ws)["mute"]["keys"], ["wa:" + DM_JID])
        self.assertEqual(hub.load_watch(self.ws)["mute"]["people"], [])

    def test_mute_a_hidden_id_chat_as_the_panel_sends_it(self):
        lid = "987654321012345@lid"
        self.assertEqual(hub.classify_mute(lid), ("keys", "wa:" + lid))
        self.assertEqual(hub.classify_mute("wa:" + lid), ("keys", "wa:" + lid))
        self.assertEqual(hub.classify_mute("wa:anything"), ("keys", "wa:anything"))
        self.assertEqual(hub.classify_follow(lid), ("whatsapp", lid))
        l = line(source="whatsapp", key="wa:" + lid, who="a chat")
        self.assertTrue(hub.is_muted(l, {"keys": ["wa:" + lid]}))
        self.assertEqual(self.run_verb("mute", "--", "wa:" + lid)[0], 0)
        self.assertEqual(hub.load_watch(self.ws)["mute"], {"keys": ["wa:" + lid], "people": [], "channels": []})

    def test_whatsapp_text_never_auto_follows_a_project(self):
        r = WaReader()
        self.poll(r)
        r.items = [wa_item("please see PLAT-7 today", "2026-10-29T10:00:30Z")]
        self.cfg["connectors"]["tracker"] = "jira"
        saved = dict(hub_sources.ADAPTERS)
        self.addCleanup(lambda: (hub_sources.ADAPTERS.clear(), hub_sources.ADAPTERS.update(saved)))
        real = hub.auto_follow
        seen = []
        hub.auto_follow = lambda ws, lines, at, watched=(), known=None: seen.append(lines) or real(
            ws, lines, at, watched, ["PLAT"])
        self.addCleanup(lambda: setattr(hub, "auto_follow", real))
        self.poll(r, 1)
        self.assertEqual(hub.load_watch(self.ws)["auto"]["projects"], [])
        self.assertEqual(real(self.ws, self.inbox(), self.AT, (), ["PLAT"]), [])
        self.assertEqual(hub.load_watch(self.ws)["auto"]["projects"], [])

    def test_the_local_connection_alone_enables_the_source(self):
        del self.cfg["connectors"]
        r = WaReader()
        self.poll(r)
        r.items = [wa_item("lunch?", "2026-10-29T10:00:30Z")]
        beat = self.poll(r, 1)
        self.assertEqual(beat["sources"]["whatsapp"], {"status": "ok"})
        self.assertEqual(len(self.inbox()), 1)
        self.assertIsInstance(hub.build_clients(self.cfg, {})["whatsapp"], hub_sources.LocalWhatsApp)
        import io
        buf = io.StringIO()
        self.assertEqual(hub.check(self.cfg, factory=lambda cfg, have: {"whatsapp": r}, out=buf), 0, buf.getvalue())

    def test_a_local_connection_needs_no_program_on_the_path(self):
        found, missing = hub.resolve_programs(self.cfg, which=lambda n: None)
        self.assertEqual((found, missing), ({}, []))


if __name__ == "__main__":
    unittest.main()
