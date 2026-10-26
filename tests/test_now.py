"""Tests for lib/now.py — working hours and signal thresholds.
Run: python3 -m unittest tests.test_now -v
"""
import copy
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import now  # noqa: E402

UTC = timezone.utc
with open(os.path.join(ROOT, "templates", "workspace", "config.example.json")) as _f:
    CFG = json.load(_f)
A = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
B = A + timedelta(hours=1)


def with_tick(cfg, **tick):
    c = copy.deepcopy(cfg)
    c.setdefault("routines", {}).setdefault("tick", {}).update(tick)
    return c


def with_connectors(cfg, **conns):
    c = copy.deepcopy(cfg)
    c.setdefault("connectors", {}).update(conns)
    return c


S = now.Schedule.from_config(CFG)


class WorkingHours(unittest.TestCase):
    def test_weekend_and_night_do_not_count(self):
        s = now.Schedule.from_config(CFG)
        fri = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)   # 18:00 London (BST)
        mon = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)    # 09:00 London (GMT after DST)
        self.assertAlmostEqual(now.working_hours(fri, mon, s), 4.0 + 3.0)

    def test_same_day_inside_window(self):
        a = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
        b = a + timedelta(hours=6)
        self.assertAlmostEqual(now.working_hours(a, b, S), 6.0)

    def test_reversed_range_is_zero(self):
        self.assertEqual(now.working_hours(B, A, S), 0)

    def test_schedule_defaults_and_overrides(self):
        self.assertEqual((S.start.hour, S.end.hour, S.every), (6, 22, 60))
        self.assertEqual(now.day_hours(S), 16.0)
        s = now.Schedule.from_config(with_tick(CFG, active_hours="08:00-18:30", every=30))
        self.assertEqual(s.every, 30)
        self.assertEqual(now.day_hours(s), 10.5)


class Thresholds(unittest.TestCase):
    def test_threshold_parsing_and_overrides(self):
        self.assertEqual(now.threshold_hours("review_requested", CFG, S), 4.0)
        self.assertEqual(now.threshold_hours("ticket_stalled", CFG, S), 48.0)
        cfg = with_tick(CFG, thresholds={"ticket_stalled": "2d"}, signals={"bug_unowned": False})
        self.assertEqual(now.threshold_hours("ticket_stalled", cfg, S), 32.0)
        self.assertIsNone(now.threshold_hours("bug_unowned", cfg, S))

    def test_signal_off_when_connector_absent(self):
        cfg = with_connectors(CFG, tracker="none")
        self.assertIsNone(now.threshold_hours("ticket_stalled", cfg, S))
        self.assertEqual(now.threshold_hours("decision_open", cfg, S), 16.0)

    def test_signal_tables(self):
        self.assertEqual(len(now.SIGNALS), 19)
        self.assertNotIn("default_branch_red", now.SLOW_SIGNALS)
        self.assertEqual(len(now.SLOW_SIGNALS), 10)
        self.assertEqual(now.threshold_hours("promise_due", CFG, S), 0.0)


if __name__ == "__main__":
    unittest.main()
