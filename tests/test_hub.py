"""Tests for lib/hub.py — thing keys, inbox and state files.
Run: python3 -m unittest tests.test_hub -v
"""
import json
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

    def test_coalesce_keeps_latest_and_unions_also(self):
        a = line(at="2026-10-29T09:00:00Z", also=["A-1"], summary="old")
        b = line(at="2026-10-29T10:00:00Z", also=["B-2", "A-1"], summary="new")
        c = line(key="PLAT-2")
        out = hub.coalesce([b, a, c])
        self.assertEqual(len(out), 2)
        got = [x for x in out if x["key"] == "PLAT-1"][0]
        self.assertEqual(got["summary"], "new")
        self.assertEqual(got["also"], ["B-2", "A-1"])


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
                "cursor": {"n": len(cls.items)}, "partial": getattr(cls, "partial", [])}


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
        FakeAdapter.partial = []
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

    def test_sweep_for_unseen_live_sessions(self):
        with hub.locked(self.ws):
            hub.save_json(self.ws, "sessions.json", {"version": 1, "sessions": {
                "a": {"session_id": "s1", "unseen": 0, "state": "live"},
                "b": {"session_id": "s2", "unseen": 3, "state": "dropped"}}})
        self.poll()
        self.assertEqual(self.inbox(), [])
        with hub.locked(self.ws):
            hub.save_json(self.ws, "sessions.json", {"version": 1, "sessions": {
                "b": {"session_id": "s2", "unseen": 2, "state": "live"}}})
        self.poll(1)
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
        self.assertEqual(hub.build_me(self.cfg), {"email": "dana@example.com", "chat": "U00000000", "code": "danal"})

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

    def test_not_home_exits_2_and_3(self):
        self.cfg["workspace"]["home_machine"] = "elsewhere"
        with self.assertRaises(SystemExit) as cm:
            hub.watch(self.ws, self.cfg, stop=self.stop)
        self.assertEqual(cm.exception.code, 2)
        del self.cfg["workspace"]["home_machine"]
        with self.assertRaises(SystemExit) as cm:
            hub.watch(self.ws, self.cfg, stop=self.stop)
        self.assertEqual(cm.exception.code, 3)

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
        self.assertTrue(d["KeepAlive"])
        self.assertTrue(d["StandardOutPath"].endswith(".mother/hub/watcher.log"))
        self.assertTrue(any(c[0] == "bootstrap" for c in self.calls))

    def test_install_fails_on_missing_command(self):
        self.which = {"gh": "/opt/homebrew/bin/gh"}.get
        self.assertEqual(self.run_install(), 1)
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
            tools = ["mcp__x__get-issues", "mcp__x__create-issue"]
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
        self.assertIn("tracker | \u2717 | no read tool", buf.getvalue())
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
        self.assertIn("STALE", out.split("hub:")[1])


class FakeClient:
    tools = []

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
