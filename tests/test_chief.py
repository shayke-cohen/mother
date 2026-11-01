"""Tests for lib/chief.py — worker registry, say queue, spend.
Run: python3 -m unittest tests.test_chief -v
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
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


class TestRegistry(Base):
    def test_register_worker_fields(self):
        w = chief.register_worker(self.ws, AT, "PLAT-1", "Fix the thing", also=["example-org/api#7"])
        self.assertRegex(w["session_id"], UUID4)
        self.assertEqual(w["name"], "plat-1")
        self.assertEqual(w["title"], "Fix the thing")
        self.assertEqual(w["also"], ["example-org/api#7"])
        self.assertEqual(w["kind"], "cli")
        self.assertEqual(w["state"], "live")
        self.assertEqual(w["status"], "idle")
        self.assertIs(w["held"], False)
        self.assertEqual((w["progress"], w["cost"], w["runs"], w["unseen"]), ("", 0.0, 0, 0))
        self.assertIsNone(w["link"])
        self.assertIsNone(w["last_routed_at"])
        self.assertIsNone(w["last_run_at"])
        self.assertEqual(w["opened_at"], hub._iso(AT))
        stored = hub._sessions_doc(self.ws)["sessions"]["PLAT-1"]
        self.assertEqual(stored["session_id"], w["session_id"])

    def test_name_slug_capped(self):
        w = chief.register_worker(self.ws, AT, "example-org/api#" + "9" * 40, "t")
        self.assertLessEqual(len(w["name"]), 24)
        self.assertRegex(w["name"], r"^[a-z0-9-]+$")

    def test_register_existing_returns_same_id(self):
        a = chief.register_worker(self.ws, AT, "PLAT-1", "t")
        b = chief.register_worker(self.ws, AT, "PLAT-1", "other")
        self.assertEqual(a["session_id"], b["session_id"])

    def test_register_refuses_desktop_key(self):
        self.seed_sessions({"PLAT-1": {"session_id": "local_x", "state": "live"}})
        with self.assertRaises(ValueError):
            chief.register_worker(self.ws, AT, "PLAT-1", "t")

    def test_register_replaces_dropped(self):
        self.seed_sessions({"PLAT-1": {"session_id": "old", "state": "dropped", "kind": "cli"}})
        w = chief.register_worker(self.ws, AT, "PLAT-1", "t")
        self.assertNotEqual(w["session_id"], "old")
        self.assertEqual(w["state"], "live")

    def test_workers_skips_desktop_and_dropped(self):
        self.seed_sessions({
            "D-1": {"session_id": "local_x", "state": "live"},
            "X-1": {"session_id": "y", "state": "dropped", "kind": "cli"},
        })
        chief.register_worker(self.ws, AT, "PLAT-2", "t")
        self.assertEqual(list(chief.workers(self.ws)), ["PLAT-2"])

    def test_update_worker(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        w = chief.update_worker(self.ws, "PLAT-1", status="working", progress="step 1")
        self.assertEqual((w["status"], w["progress"]), ("working", "step 1"))
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "working")
        with self.assertRaises(KeyError):
            chief.update_worker(self.ws, "NOPE-1", status="idle")


class TestHoldQueue(Base):
    def test_hold_release_and_note(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        self.assertIs(chief.hold(self.ws, "PLAT-1")["held"], True)
        self.assertIs(chief.release(self.ws, "PLAT-1", AT)["held"], False)
        q = chief.take_queue(self.ws, "PLAT-1")
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["source"], "release")
        self.assertEqual(q[0]["text"], "Shay worked in this session directly; read the latest turns before continuing.")

    def test_say_text_verbatim(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        text = '--help "x"\ny'
        chief.enqueue(self.ws, AT, "PLAT-1", text, "say")
        self.assertEqual(chief.queued(self.ws), {"PLAT-1": 1})
        got = chief.take_queue(self.ws, "PLAT-1")
        self.assertEqual(got[0]["text"], text)
        self.assertEqual(got[0]["source"], "say")
        self.assertEqual(chief.take_queue(self.ws, "PLAT-1"), [])
        self.assertEqual(chief.queued(self.ws), {})

    def test_take_queue_leaves_other_keys(self):
        chief.enqueue(self.ws, AT, "A-1", "a", "route")
        chief.enqueue(self.ws, AT, "B-1", "b", "route")
        chief.enqueue(self.ws, AT, "A-1", "a2", "say")
        self.assertEqual([e["text"] for e in chief.take_queue(self.ws, "A-1")], ["a", "a2"])
        self.assertEqual(chief.queued(self.ws), {"B-1": 1})

    def test_enqueue_rejects_bad_source(self):
        with self.assertRaises(ValueError):
            chief.enqueue(self.ws, AT, "A-1", "a", "other")

    def test_say_and_release_reset_failed(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        chief.register_worker(self.ws, AT, "PLAT-2", "t")
        chief.update_worker(self.ws, "PLAT-1", status="failed")
        chief.update_worker(self.ws, "PLAT-2", status="failed")
        chief.enqueue(self.ws, AT, "PLAT-1", "try again", "say")
        chief.release(self.ws, "PLAT-2", AT)
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "idle")
        self.assertEqual(chief.workers(self.ws)["PLAT-2"]["status"], "idle")

    def test_route_does_not_reset_failed_or_working(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        chief.update_worker(self.ws, "PLAT-1", status="failed")
        chief.enqueue(self.ws, AT, "PLAT-1", "x", "route")
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "failed")
        chief.update_worker(self.ws, "PLAT-1", status="working")
        chief.enqueue(self.ws, AT, "PLAT-1", "x", "say")
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "working")


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
        self.assertEqual(s, {"max_workers": 3, "run_minutes": 20, "daily_budget_usd": 20,
                             "chief_model": "sonnet", "worker_model": "", "tick_seconds": 30})
        s = chief.settings({"chief": {"max_workers": 5, "bogus": 1}})
        self.assertEqual(s["max_workers"], 5)
        self.assertEqual(s["run_minutes"], 20)
        self.assertNotIn("bogus", s)


# ---- the worker runner ------------------------------------------------------------------------------
FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_claude.py")


class TestRun(Base):
    def setUp(self):
        super().setUp()
        self.log = self.ws / "claude.log"
        p = mock.patch.dict(os.environ, {"MOTHER_CLAUDE_BIN": FAKE, "FAKE_CLAUDE_LOG": str(self.log)})
        p.start()
        self.addCleanup(p.stop)
        for k in ("FAKE_CLAUDE_RESULT", "FAKE_CLAUDE_SLEEP", "FAKE_CLAUDE_EXIT"):
            os.environ.pop(k, None)
        self.cfg = {}
        self.locks = []
        self.addCleanup(lambda: [f.close() for f in self.locks])

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text().splitlines()]

    def run_(self, key="PLAT-1", prompt="do it", cfg=None, **kw):
        return chief.run(self.ws, self.cfg if cfg is None else cfg, AT, key, prompt=prompt, **kw)

    def hold_lock(self, key):
        name = chief._slug(key)
        d = chief.chief_dir(self.ws) / "run"
        d.mkdir(parents=True, exist_ok=True)
        f = open(str(d / (name + ".lock")), "a")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.locks.append(f)

    def test_first_run_uses_session_id_then_resume(self):
        self.assertEqual(self.run_(title="T"), 0)
        sid = hub._sessions_doc(self.ws)["sessions"]["PLAT-1"]["session_id"]
        self.assertEqual(self.run_(), 0)
        a, b = [c["argv"] for c in self.calls()]
        self.assertEqual(a[a.index("--session-id") + 1], sid)
        self.assertNotIn("--resume", a)
        self.assertEqual(b[b.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", b)
        for flag in ("-p", "--verbose", "bypassPermissions"):
            self.assertIn(flag, a)
        self.assertIn("MOTHER worker for PLAT-1 (T)", self.calls()[0]["stdin"])

    def test_run_records_progress_cost_status(self):
        os.environ["FAKE_CLAUDE_RESULT"] = "did x\nPROGRESS: drafted reply"
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        chief.update_worker(self.ws, "PLAT-1", unseen=3)
        self.assertEqual(self.run_(), 0)
        w = chief.workers(self.ws)["PLAT-1"]
        self.assertEqual(w["progress"], "drafted reply")
        self.assertEqual(w["cost"], 0.01)
        self.assertEqual(w["status"], "idle")
        self.assertEqual(w["runs"], 1)
        self.assertEqual(w["unseen"], 0)
        self.assertEqual(w["last_run_at"], hub._iso(AT))
        tz = nowmod.Schedule.from_config(self.cfg).tz
        self.assertEqual(chief.spent_today(self.ws, AT, tz), 0.01)
        self.assertEqual(chief.running(self.ws), [])

    def test_failed_run_marks_failed(self):
        os.environ["FAKE_CLAUDE_EXIT"] = "1"
        os.environ["FAKE_CLAUDE_RESULT"] = ""
        self.assertEqual(self.run_(), 1)
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "failed")

    def test_run_refuses_desktop_key(self):
        self.seed_sessions({"PLAT-1": {"session_id": "s", "state": "live", "name": "plat-1"}})
        self.assertEqual(self.run_(), 2)
        self.assertEqual(self.calls(), [])

    def test_held_run_queues_instead(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        chief.hold(self.ws, "PLAT-1")
        self.assertEqual(self.run_(prompt="please"), 4)
        self.assertEqual(self.calls(), [])
        q = chief.take_queue(self.ws, "PLAT-1")
        self.assertEqual([(e["text"], e["source"]) for e in q], [("please", "say")])

    def test_second_concurrent_run_queues(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        self.hold_lock("PLAT-1")
        self.assertEqual(self.run_(prompt="later"), 4)
        self.assertEqual(self.calls(), [])
        self.assertEqual(chief.queued(self.ws), {"PLAT-1": 1})
        self.assertEqual(chief.running(self.ws), ["PLAT-1"])

    def test_max_workers_cap(self):
        for k in ("A-1", "A-2", "A-3"):
            chief.register_worker(self.ws, AT, k, "t")
            self.hold_lock(k)
        self.assertEqual(self.run_(key="A-4", prompt="x"), 4)
        self.assertEqual(self.calls(), [])
        self.assertEqual(chief.queued(self.ws), {"A-4": 1})
        self.assertEqual(sorted(chief.running(self.ws)), ["A-1", "A-2", "A-3"])

    def test_drain_runs_queued_text_as_next_turn(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        chief.enqueue(self.ws, AT, "PLAT-1", "first note", "say")
        chief.enqueue(self.ws, AT, "PLAT-1", "second note", "route")
        self.assertEqual(self.run_(), 0)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("[say at %s] first note" % hub._iso(AT), calls[1]["stdin"])
        self.assertIn("[route at %s] second note" % hub._iso(AT), calls[1]["stdin"])
        self.assertIn("--resume", calls[1]["argv"])
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["runs"], 2)
        self.assertEqual(chief.queued(self.ws), {})

    def test_hold_during_run_stops_drain(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "1"
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        chief.enqueue(self.ws, AT, "PLAT-1", "note", "say")
        t = threading.Timer(0.4, chief.hold, args=(self.ws, "PLAT-1"))
        t.start()
        self.addCleanup(t.join)
        self.assertEqual(self.run_(), 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(chief.queued(self.ws), {"PLAT-1": 1})

    def test_timeout_marks_failed(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "5"
        self.assertEqual(self.run_(cfg={"chief": {"run_minutes": 1 / 60}}), 1)
        w = chief.workers(self.ws)["PLAT-1"]
        self.assertEqual(w["status"], "failed")
        self.assertTrue(w["progress"].startswith("timed out"), w["progress"])

    def test_paused_and_budget_refuse(self):
        chief.pause(self.ws)
        self.assertEqual(self.run_(), 3)
        chief.resume(self.ws)
        tz = nowmod.Schedule.from_config(self.cfg).tz
        chief.add_spend(self.ws, AT, tz, 5)
        self.assertEqual(self.run_(cfg={"chief": {"daily_budget_usd": 5}}), 3)
        self.assertEqual(self.calls(), [])

    def test_corrupt_spend_refuses(self):
        d = chief.chief_dir(self.ws)
        d.mkdir(parents=True)
        (d / "spend.json").write_text("{not json")
        self.assertEqual(self.run_(), 3)
        self.assertEqual(self.calls(), [])

    def test_missing_spend_is_zero(self):
        self.assertEqual(self.run_(), 0)

    def test_worker_env_drops_chief_role(self):
        os.environ["MOTHER_CHIEF_ROLE"] = "chief"
        self.assertEqual(self.run_(), 0)
        env = self.calls()[0]["env"]
        self.assertNotIn("MOTHER_CHIEF_ROLE", env)
        self.assertEqual(env["MOTHER_CHIEF_WORKER"], "PLAT-1")
        self.assertEqual(env["MOTHER_NO_SYNC"], "1")
        self.assertEqual(env["MOTHER_WORKSPACE"], str(self.ws))

    def test_empty_prompt_and_queue_is_usage(self):
        self.assertEqual(self.run_(prompt=""), 2)
        self.assertEqual(self.calls(), [])

    def test_detach_flag_like_text_is_one_token(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            self.run_(title="--x", prompt="-- --prompt=y", detach=True)
        self.assertEqual(popen.call_args[0][0][-2:], ["--title=--x", "--prompt=-- --prompt=y"])

    def test_detach_popen_oserror_is_exit_1(self):
        with mock.patch.object(subprocess, "Popen", side_effect=OSError("nope")):
            self.assertEqual(self.run_(detach=True), 1)

    def test_status_is_working_during_run(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "1"
        seen = []
        t = threading.Timer(0.5, lambda: seen.append(chief.workers(self.ws)["PLAT-1"]["status"]))
        t.start()
        self.addCleanup(t.join)
        self.assertEqual(self.run_(), 0)
        t.join()
        self.assertEqual(seen, ["working"])
        self.assertEqual(chief.workers(self.ws)["PLAT-1"]["status"], "idle")

    def test_hold_between_check_and_lock_does_not_start_a_turn(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        real, state = chief._try_lock, {"n": 0}

        def lock_after_hold(path):
            state["n"] += 1
            if state["n"] == 1:
                chief.hold(self.ws, "PLAT-1")
            return real(path)
        with mock.patch.object(chief, "_try_lock", side_effect=lock_after_hold):
            self.assertEqual(self.run_(prompt="later"), 4)
        self.assertEqual(self.calls(), [])
        self.assertEqual(chief.queued(self.ws), {"PLAT-1": 1})

    def test_drain_stops_at_budget_and_keeps_queue(self):
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        chief.enqueue(self.ws, AT, "PLAT-1", "note", "say")
        self.assertEqual(self.run_(cfg={"chief": {"daily_budget_usd": 0.01}}), 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(chief.queued(self.ws), {"PLAT-1": 1})

    def test_worker_dropped_mid_run_is_exit_1(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "1"
        chief.register_worker(self.ws, AT, "PLAT-1", "t")
        drop = lambda: self.seed_sessions({})  # noqa: E731
        t = threading.Timer(0.4, drop)
        t.start()
        self.addCleanup(t.join)
        self.assertEqual(self.run_(), 1)
        self.assertEqual(chief.running(self.ws), [])

    def test_detach_reexecs_and_returns(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            self.assertEqual(self.run_(title="T", prompt="go", detach=True), 0)
        argv = popen.call_args[0][0]
        kw = popen.call_args[1]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], os.path.join(ROOT, "bin", "mother"))
        self.assertEqual(argv[2:], ["chief", "run", "PLAT-1", "--title=T", "--prompt=go"])
        self.assertIs(kw["start_new_session"], True)
        self.assertTrue(kw["stdout"].name.endswith(os.path.join("logs", "plat-1.log")))
        self.assertEqual(self.calls(), [])


class TestParse(unittest.TestCase):
    def test_progress_of(self):
        self.assertEqual(chief.progress_of("a\nPROGRESS: one\nmore\nPROGRESS: two"), "two")
        self.assertEqual(chief.progress_of("\n  first line\nsecond"), "first line")
        self.assertEqual(len(chief.progress_of("x" * 500)), 200)
        self.assertEqual(chief.progress_of(""), "")

    def test_parse_stream(self):
        ok = json.dumps({"type": "result", "subtype": "success", "result": "hi", "total_cost_usd": 0.5})
        self.assertEqual(chief.parse_stream("noise\n" + ok + "\n"), ("hi", 0.5, None))
        bad = json.dumps({"type": "result", "subtype": "error_max_turns", "total_cost_usd": 0.2})
        r, c, e = chief.parse_stream(bad)
        self.assertEqual((r, c), ("", 0.2))
        self.assertTrue(e)
        self.assertTrue(chief.parse_stream("")[2])


if __name__ == "__main__":
    unittest.main()
