"""Tests for lib/now.py — working hours and signal thresholds.
Run: python3 -m unittest tests.test_now -v
"""
import copy
import glob
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
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

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)   # Tue 12:00 London (GMT)
H = timedelta(hours=1)


def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_ago(n):
    return iso(NOW - timedelta(hours=n))


def days_ago(n):
    return iso(NOW - timedelta(days=n))


def cand(signal, since=None, link="https://example.com/pr/1", actions=None, kind="pr", **kw):
    c = {"kind": kind, "signal": signal, "title": "T " + signal, "why": "w", "link": link, "also": [],
         "team": "platform", "since": since or hours_ago(10), "actions": actions or []}
    c.update(kw)
    return c


def inp(*cands, checked=None, coverage=None, **kw):
    cov = {k: ({"status": v} if isinstance(v, str) else v) for k, v in (coverage or {}).items()}
    d = {"coverage": cov, "checked_signals": checked or [], "routines": [], "candidates": list(cands)}
    d.update(kw)
    return d


class Merge(unittest.TestCase):
    def test_below_threshold_is_dropped_above_is_kept(self):
        out = now.merge(inp(cand("review_requested", since=hours_ago(3))), None, {}, CFG, NOW)
        self.assertEqual(out["items"], [])
        out = now.merge(inp(cand("review_requested", since=hours_ago(5))), None, {}, CFG, NOW)
        self.assertEqual(out["items"][0]["state"], "new")
        self.assertEqual(out["items"][0]["section"], "on_you")
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["timezone"], "Europe/London")
        self.assertEqual(out["generated_at"], iso(NOW))
        self.assertEqual(out["items"][0]["id"], "pr:https://example.com/pr/1:review_requested")
        self.assertEqual(out["counts"], {"on_you": 1, "neglected": 0, "suggestion": 0})

    def test_off_signal_is_dropped(self):
        cfg = with_connectors(CFG, tracker="none")
        out = now.merge(inp(cand("ticket_stalled", since=days_ago(7))), None, {}, cfg, NOW)
        self.assertEqual(out["items"], [])

    def test_second_tick_is_open_and_keeps_first_seen(self):
        c = cand("review_requested")
        first = now.merge(inp(c), None, {}, CFG, NOW)
        second = now.merge(inp(c), first, {}, CFG, NOW + H)
        it = second["items"][0]
        self.assertEqual(it["state"], "open")
        self.assertEqual(it["first_seen"], iso(NOW))
        self.assertEqual(it["last_seen"], iso(NOW + H))

    def test_suggestion_section_and_actions(self):
        acts = [{"label": "Do it", "prompt": "x"}]
        out = now.merge(inp(cand("suggestion_waiting", kind="sug", actions=acts)), None, {}, CFG, NOW)
        self.assertEqual(out["items"][0]["section"], "suggestion")
        out = now.merge(inp(cand("suggestion_waiting", kind="sug")), None, {}, CFG, NOW)
        self.assertEqual(out["items"][0]["section"], "on_you")

    def test_action_ids_are_stable(self):
        acts = [{"label": "Nudge", "prompt": "x"}]
        c = cand("review_requested", actions=acts)
        first = now.merge(inp(c), None, {}, CFG, NOW)
        self.assertEqual(first["items"][0]["actions"],
                         [{"id": "S1", "label": "Nudge", "prompt": "mother now do S1"}])
        self.assertEqual(first["next_action"], 2)
        other = cand("mail_needs_reply", link="m1", kind="mail", actions=acts)
        second = now.merge(inp(c, other), first, {}, CFG, NOW + H)
        by = {i["signal"]: i for i in second["items"]}
        self.assertEqual(by["review_requested"]["actions"][0]["id"], "S1")
        self.assertEqual(by["mail_needs_reply"]["actions"][0]["id"], "S2")
        self.assertEqual(second["next_action"], 3)
        third = now.merge(inp(c), second, {}, CFG, NOW + 2 * H)
        self.assertEqual(third["next_action"], 3)

    def test_slow_signal_not_checked_stays_open(self):
        first = now.merge(inp(cand("ticket_stalled", since=days_ago(7))), None, {}, CFG, NOW)
        second = now.merge(inp(checked=["review_requested"], coverage={"tracker": "ok"}), first, {}, CFG, NOW + H)
        self.assertEqual(len(second["items"]), 1)
        self.assertNotIn("unverified_since", second["items"][0])

    def test_checked_and_gone_is_closed(self):
        first = now.merge(inp(cand("ticket_stalled", since=days_ago(7))), None, {}, CFG, NOW)
        second = now.merge(inp(checked=["ticket_stalled"], coverage={"tracker": "ok"}), first, {}, CFG, NOW + H)
        self.assertEqual(second["items"], [])

    def test_couldnt_check_keeps_item_unverified(self):
        first = now.merge(inp(cand("ticket_stalled", since=days_ago(7))), None, {}, CFG, NOW)
        cov = {"tracker": "couldnt_check"}
        second = now.merge(inp(checked=["ticket_stalled"], coverage=cov), first, {}, CFG, NOW + H)
        self.assertEqual(second["items"][0]["unverified_since"], iso(NOW + H))
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["coverage"]["tracker"]["since"], iso(NOW + H))
        third = now.merge(inp(checked=["ticket_stalled"], coverage=cov), second, {}, CFG, NOW + 2 * H)
        self.assertEqual(third["items"][0]["unverified_since"], iso(NOW + H))
        self.assertEqual(third["coverage"]["tracker"]["since"], iso(NOW + H))
        back = now.merge(inp(cand("ticket_stalled", since=days_ago(7)), coverage={"tracker": "ok"}),
                         third, {}, CFG, NOW + 3 * H)
        self.assertNotIn("unverified_since", back["items"][0])

    def test_absent_connector_has_no_coverage_row(self):
        cfg = with_connectors(CFG, tracker="none")
        cov = {"tracker": {"status": "couldnt_check", "reason": "x"}, "chat": "ok", "workspace": "ok"}
        out = now.merge(inp(coverage=cov), None, {}, cfg, NOW)
        self.assertEqual(sorted(out["coverage"]), ["chat", "workspace"])
        self.assertEqual(out["status"], "ok")

    def test_suggestion_lapses_after_three_working_days(self):
        acts = [{"label": "Do", "prompt": "x"}]
        c = cand("suggestion_waiting", kind="sug", actions=acts)
        first = now.merge(inp(c), None, {}, CFG, NOW)
        keep = now.merge(inp(c), first, {}, CFG, NOW + timedelta(hours=47))
        self.assertEqual(len(keep["items"]), 1)
        gone = now.merge(inp(c), keep, {}, CFG, NOW + timedelta(hours=72))   # Fri 12:00 = 48 working hours
        self.assertEqual(gone["items"], [])

    def test_quiet_flag_follows_manager_quiet_hours(self):
        self.assertFalse(now.merge(inp(), None, {}, CFG, NOW)["quiet"])
        self.assertTrue(now.merge(inp(), None, {}, CFG, NOW + timedelta(hours=8))["quiet"])   # 20:00
        self.assertTrue(now.merge(inp(), None, {}, CFG, NOW - timedelta(hours=5))["quiet"])   # 07:00

    def test_next_at_skips_night_and_weekend(self):
        fri = datetime(2026, 10, 23, 20, 30, tzinfo=UTC)   # 21:30 London (BST)
        out = now.merge(inp(), None, {}, CFG, fri)
        self.assertEqual(out["next_at"], iso(datetime(2026, 10, 26, 6, 5, tzinfo=UTC)))   # Mon 06:05 GMT
        self.assertEqual(out["stale_after"], iso(datetime(2026, 10, 26, 7, 5, tzinfo=UTC)))
        out = now.merge(inp(), None, {}, CFG, NOW)
        self.assertEqual(out["next_at"], iso(datetime(2026, 10, 27, 12, 5, tzinfo=UTC)))

    def test_items_sorted_by_section_then_oldest(self):
        out = now.merge(inp(
            cand("ticket_stalled", link="a", since=days_ago(7)),
            cand("review_requested", link="b", since=hours_ago(6)),
            cand("review_requested", link="c", since=hours_ago(9)),
        ), None, {}, CFG, NOW)
        self.assertEqual([i["link"] for i in out["items"]], ["c", "b", "a"])

    def test_write_is_atomic_and_load_rejects_bad_files(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self.assertIsNone(now.load_now(ws))
            doc = now.merge(inp(cand("review_requested")), None, {}, CFG, NOW)
            now.write_now(ws, doc)
            self.assertEqual(glob.glob(os.path.join(d, ".mother", "*.tmp")), [])
            self.assertEqual(now.load_now(ws), doc)
            f = ws / ".mother" / "now.json"
            for bad in ("", "{", '{"version": 2}', "[]"):
                f.write_text(bad)
                self.assertIsNone(now.load_now(ws))


if __name__ == "__main__":
    unittest.main()
