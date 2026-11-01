"""Tests for lib/chief.py — worker registry, say queue, spend.
Run: python3 -m unittest tests.test_chief -v
"""
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
