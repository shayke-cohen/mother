"""Tests for lib/now.py — working hours and signal thresholds.
Run: python3 -m unittest tests.test_now -v
"""
import copy
import fcntl
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
                         [{"id": "S1", "label": "Nudge", "prompt": "mother now do S1", "task": "x"}])
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

    def test_lapsed_suggestion_stays_lapsed(self):
        c = cand("suggestion_waiting", kind="sug", since=iso(NOW), actions=[{"label": "Do", "prompt": "x"}])
        later = NOW + timedelta(hours=72)    # 48 working hours after since
        out = now.merge(inp(c), None, {}, CFG, later)
        self.assertEqual(out["items"], [])
        out = now.merge(inp(c), out, {}, CFG, later + H)
        self.assertEqual(out["items"], [])

    def test_item_dropped_when_signal_or_connector_turned_off(self):
        first = now.merge(inp(cand("ticket_stalled", since=days_ago(7))), None, {}, CFG, NOW)
        self.assertEqual(len(first["items"]), 1)
        off = with_tick(CFG, signals={"ticket_stalled": False})
        self.assertEqual(now.merge(inp(), first, {}, off, NOW + H)["items"], [])
        none = with_connectors(CFG, tracker="none")
        self.assertEqual(now.merge(inp(), first, {}, none, NOW + H)["items"], [])

    def test_malformed_input_is_skipped_and_counted(self):
        bad = cand("review_requested")
        del bad["link"]
        bad_action = cand("mail_needs_reply", kind="mail", link="m", actions=[{"prompt": "x"}])
        good = cand("review_requested", link="ok")
        out = now.merge(inp(bad, good, bad_action, coverage={"chat": {}}), None, {}, CFG, NOW)
        self.assertEqual(sorted(i["link"] for i in out["items"]), ["m", "ok"])
        self.assertEqual(out["dropped"], 2)   # bad candidate + bad action
        self.assertEqual(out["coverage"]["chat"]["status"], "couldnt_check")
        self.assertEqual(now.merge(inp(good), None, {}, CFG, NOW)["dropped"], 0)

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


M = timedelta(minutes=1)
MORNING = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
ID = "pr:https://example.com/pr/1:review_requested"


def t(title, item_id=None):
    return {"title": title, "why": "w " + title, "link": "https://example.com/" + title, "item_id": item_id}


def titles(doc):
    return [x["title"] for x in doc["top"]]


class ManagerState(unittest.TestCase):
    def first(self):
        return now.merge(inp(cand("review_requested", since=hours_ago(6))), None, {}, CFG, NOW)

    def test_done_hides_and_new_request_resurfaces(self):
        first = self.first()
        st = {"items": {ID: {"state": "done", "at": iso(NOW), "until": None}}}
        old = cand("review_requested", since=hours_ago(6))
        self.assertEqual(now.merge(inp(old), first, st, CFG, NOW + H)["items"], [])
        self.assertIn(ID, st["items"])
        out = now.merge(inp(cand("review_requested", since=iso(NOW + 30 * M))), first, st, CFG, NOW + 6 * H)
        self.assertEqual(out["items"][0]["state"], "new")
        self.assertNotIn(ID, st["items"])

    def test_done_also_hides_a_kept_item(self):
        first = self.first()
        st = {"items": {ID: {"state": "done", "at": iso(NOW), "until": None}}}
        self.assertEqual(now.merge(inp(), first, st, CFG, NOW + H)["items"], [])

    def test_snooze_hides_until_then_returns(self):
        first = self.first()
        c = cand("review_requested", since=hours_ago(6))
        st = {"items": {ID: {"state": "snoozed", "until": iso(NOW + 2 * H), "at": iso(NOW)}}}
        self.assertEqual(now.merge(inp(c), first, st, CFG, NOW + H)["items"], [])
        out = now.merge(inp(c), first, st, CFG, NOW + 2 * H)
        self.assertEqual((out["items"][0]["state"], out["items"][0]["id"]), ("open", ID))
        self.assertEqual(st["items"], {})

    def test_snooze_expiring_on_a_kept_item(self):
        first = self.first()
        st = {"items": {ID: {"state": "snoozed", "until": iso(NOW + H), "at": iso(NOW)}}}
        out = now.merge(inp(), first, st, CFG, NOW + 2 * H)
        self.assertEqual(out["items"][0]["state"], "open")
        self.assertEqual(st["items"], {})

    def test_snooze_words(self):
        fri = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)          # 15:00 London (BST)
        mon = datetime(2026, 10, 26, 6, 0, tzinfo=S.tz)
        self.assertEqual(now.snooze_until("1h", fri, S), fri + H)
        self.assertEqual(now.snooze_until("tomorrow", fri, S), mon)
        wed = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
        self.assertEqual(now.snooze_until("tomorrow", wed, S), datetime(2026, 10, 29, 6, 0, tzinfo=S.tz))
        self.assertEqual(now.snooze_until("next-week", wed, S), datetime(2026, 11, 2, 6, 0, tzinfo=S.tz))
        self.assertEqual(now.snooze_until("next-week", fri, S), mon)
        sun_mon = now.Schedule.from_config(
            dict(CFG, manager=dict(CFG["manager"], workdays=["Tue", "Wed"])))
        self.assertEqual(now.snooze_until("next-week", wed, sun_mon), datetime(2026, 11, 3, 6, 0, tzinfo=S.tz))
        with self.assertRaises(ValueError):
            now.snooze_until("soon", fri, S)

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self.assertEqual(now.load_state(ws), {"items": {}, "top": None})
            st = {"items": {ID: {"state": "done", "until": None, "at": iso(NOW)}},
                  "top": {"date": "2026-10-27", "set_by": "manager", "entries": [t("A")]}}
            now.save_state(ws, st)
            self.assertEqual(now.load_state(ws), st)
            self.assertEqual(glob.glob(os.path.join(d, ".mother", "*.tmp")), [])
            (ws / ".mother" / "now-state.json").write_text("{")
            self.assertEqual(now.load_state(ws), {"items": {}, "top": None})

    def test_apply_edit_hides_without_a_tick(self):
        first = self.first()
        st = {"items": {ID: {"state": "snoozed", "until": iso(NOW + H), "at": iso(NOW)}}}
        out = now.apply_edit(first, st)
        self.assertEqual((out["items"], out["counts"]["on_you"]), ([], 0))
        self.assertEqual(len(first["items"]), 1)                     # input untouched

    def test_apply_edit_applies_manager_top(self):
        a = now.merge(inp(top=[t("A")], swap_hint="x"), None, {}, CFG, MORNING)
        st = {"items": {}, "top": {"date": "2026-10-27", "set_by": "manager", "entries": [t("Z")]}}
        out = now.apply_edit(a, st)
        self.assertEqual((titles(out), out["top"][0]["set_by"], out["swap_hint"]), (["Z"], "manager", None))

    def test_top_proposed_once_then_fixed(self):
        a = now.merge(inp(top=[t("A"), t("B"), t("C"), t("D")]), None, {}, CFG, MORNING)
        self.assertEqual(titles(a), ["A", "B", "C"])
        self.assertEqual([x["rank"] for x in a["top"]], [1, 2, 3])
        self.assertEqual({x["set_by"] for x in a["top"]}, {"agent"})
        self.assertEqual(a["top_date"], "2026-10-27")
        b = now.merge(inp(top=[t("X")]), a, {}, CFG, MORNING + H)
        self.assertEqual(titles(b), ["A", "B", "C"])

    def test_empty_first_proposal_can_be_filled_later(self):
        a = now.merge(inp(), None, {}, CFG, MORNING)
        self.assertEqual(a["top"], [])
        b = now.merge(inp(top=[t("A")]), a, {}, CFG, MORNING + H)
        self.assertEqual(titles(b), ["A"])

    def test_top_resets_next_day(self):
        a = now.merge(inp(top=[t("A")]), None, {}, CFG, MORNING)
        b = now.merge(inp(top=[t("N")]), a, {}, CFG, MORNING + 24 * H)
        self.assertEqual((titles(b), b["top_date"]), (["N"], "2026-10-28"))

    def test_today_is_the_managers_local_date(self):
        cfg = copy.deepcopy(CFG)
        cfg["manager"]["timezone"] = "Asia/Tokyo"
        late = datetime(2026, 10, 27, 16, 0, tzinfo=UTC)             # 01:00 Oct 28 in Tokyo
        a = now.merge(inp(top=[t("A")]), None, {}, cfg, late)
        self.assertEqual(a["top_date"], "2026-10-28")

    def test_manager_top_wins_and_has_no_swap_hint(self):
        st = {"items": {}, "top": {"date": "2026-10-27", "set_by": "manager", "entries": [t("M1"), t("M2")]}}
        a = now.merge(inp(top=[t("A")], swap_hint="consider X"), None, st, CFG, MORNING)
        self.assertEqual(titles(a), ["M1", "M2"])
        self.assertEqual({x["set_by"] for x in a["top"]}, {"manager"})
        self.assertIsNone(a["swap_hint"])
        b = now.merge(inp(top=[t("A")], swap_hint="y"), a, st, CFG, MORNING + H)
        self.assertEqual((titles(b), b["swap_hint"]), (["M1", "M2"], None))
        stale = {"items": {}, "top": {"date": "2026-10-26", "set_by": "manager", "entries": [t("OLD")]}}
        c = now.merge(inp(top=[t("A")]), None, stale, CFG, MORNING)
        self.assertEqual(titles(c), ["A"])

    def test_swap_hint_kept_only_for_agent_top(self):
        a = now.merge(inp(top=[t("A")], swap_hint="consider X"), None, {}, CFG, MORNING)
        self.assertEqual(a["swap_hint"], "consider X")
        b = now.merge(inp(swap_hint="consider Y"), a, {}, CFG, MORNING + H)
        self.assertEqual((titles(b), b["swap_hint"]), (["A"], "consider Y"))
        c = now.merge(inp(), b, {}, CFG, MORNING + 2 * H)
        self.assertIsNone(c["swap_hint"])

    def test_top_entry_done_when_item_closes(self):
        c = cand("ticket_stalled", since=days_ago(7), link="tk")
        tid = "pr:tk:ticket_stalled"
        a = now.merge(inp(c, top=[t("A", tid), t("B")]), None, {}, CFG, MORNING)
        self.assertEqual([x["state"] for x in a["top"]], ["open", "open"])
        b = now.merge(inp(checked=["ticket_stalled"], coverage={"tracker": "ok"}), a, {}, CFG, MORNING + H)
        self.assertEqual([x["state"] for x in b["top"]], ["done", "open"])
        self.assertEqual(titles(b), ["A", "B"])                      # done stays listed
        again = now.merge(inp(c), b, {}, CFG, MORNING + 2 * H)
        self.assertEqual(again["top"][0]["state"], "done")           # sticky

    def test_top_entry_done_when_marked_done_but_not_when_unknown_or_snoozed(self):
        a = now.merge(inp(cand("review_requested", since=hours_ago(6)), top=[t("A", ID), t("B", "nope")]),
                      None, {}, CFG, NOW)
        self.assertEqual([x["state"] for x in a["top"]], ["open", "open"])
        st = {"items": {ID: {"state": "done", "at": iso(NOW), "until": None}}}
        b = now.merge(inp(), a, st, CFG, NOW + H)
        self.assertEqual([x["state"] for x in b["top"]], ["done", "open"])
        sn = {"items": {ID: {"state": "snoozed", "at": iso(NOW), "until": iso(NOW + 3 * H)}}}
        c = now.merge(inp(), a, sn, CFG, NOW + H)
        self.assertEqual(c["top"][0]["state"], "open")

    def test_kept_suggestion_lapses_from_since_not_first_seen(self):
        c = cand("suggestion_waiting", kind="sug", since=hours_ago(30), actions=[{"label": "Do", "prompt": "x"}])
        first = now.merge(inp(c), None, {}, CFG, NOW)              # 22 working hours old: kept
        self.assertEqual(len(first["items"]), 1)
        later = NOW + timedelta(hours=34)                            # Wed 22:00 = 48 from since, 26 from first_seen
        self.assertEqual(now.merge(inp(), first, {}, CFG, later)["items"], [])

    def test_manager_top_keeps_done_across_ticks_and_edits(self):
        c = cand("ticket_stalled", since=days_ago(7), link="tk")
        tid = "pr:tk:ticket_stalled"
        st = {"items": {}, "top": {"date": "2026-10-27", "set_by": "manager",
                                   "entries": [t("A", tid), t("B"), t("C", "nope")]}}
        a = now.merge(inp(c), None, st, CFG, MORNING)
        self.assertEqual([x["state"] for x in a["top"]], ["open"] * 3)
        b = now.merge(inp(checked=["ticket_stalled"], coverage={"tracker": "ok"}), a, st, CFG, MORNING + H)
        self.assertEqual([x["state"] for x in b["top"]], ["done", "open", "open"])
        c3 = now.merge(inp(), b, st, CFG, MORNING + 2 * H)
        self.assertEqual([x["state"] for x in c3["top"]], ["done", "open", "open"])
        st["items"]["x"] = {"state": "snoozed", "until": iso(MORNING + 5 * H), "at": iso(MORNING)}
        e = now.apply_edit(c3, st)
        self.assertEqual([x["state"] for x in e["top"]], ["done", "open", "open"])

    def test_lock_is_exclusive_and_save_works_inside(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            with now.locked(ws):
                with open(str(ws / ".mother" / "now.lock"), "a") as other:
                    with self.assertRaises(OSError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                now.save_state(ws, {"items": {}, "top": None})
            with open(str(ws / ".mother" / "now.lock"), "a") as other:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)      # released after the block
            self.assertEqual(now.load_state(ws), {"items": {}, "top": None})

    def test_apply_edit_does_not_carry_done_from_yesterday(self):
        yday = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)
        a = now.merge(inp(top=[t("A", "x")]), None, {"items": {"x": {"state": "done", "at": iso(yday)}}}, CFG, yday)
        self.assertEqual(a["top"][0]["state"], "done")
        st = {"items": {}, "top": {"date": "2026-10-27", "set_by": "manager", "entries": [t("A", "x")]}}
        e = now.apply_edit(a, st)
        self.assertEqual((e["top_date"], e["top"][0]["state"]), ("2026-10-27", "open"))


class TopSetAtAndHint(unittest.TestCase):
    def test_agent_proposal_gets_set_at_and_keeps_it(self):
        a = now.merge(inp(top=[t("A")]), None, {}, CFG, MORNING)
        self.assertEqual(a["top"][0]["set_at"], iso(MORNING))
        b = now.merge(inp(top=[t("X")]), a, {}, CFG, MORNING + H)
        self.assertEqual(b["top"][0]["set_at"], iso(MORNING))
        self.assertEqual(b["generated_at"], iso(MORNING + H))

    def test_late_filled_proposal_is_stamped_when_proposed(self):
        a = now.merge(inp(), None, {}, CFG, MORNING)
        b = now.merge(inp(top=[t("A")]), a, {}, CFG, MORNING + H)
        self.assertEqual(b["top"][0]["set_at"], iso(MORNING + H))

    def test_manager_edit_records_at_and_carries_it(self):
        st = {"items": {}, "top": {"date": "2026-10-27", "set_by": "manager", "at": iso(MORNING + H), "entries": [t("Z")]}}
        a = now.merge(inp(top=[t("A")]), None, st, CFG, MORNING + 2 * H)
        self.assertEqual((a["top"][0]["set_by"], a["top"][0]["set_at"]), ("manager", iso(MORNING + H)))
        base = now.merge(inp(top=[t("A")]), None, {}, CFG, MORNING)
        out = now.apply_edit(base, st)
        self.assertEqual(out["top"][0]["set_at"], iso(MORNING + H))

    def test_cli_top_edit_writes_at(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            now.write_now(ws, now.merge(inp(top=[t("A")]), None, {}, CFG, MORNING))
            with redirect_stdout(io.StringIO()):
                now.main(["top", "done", "1"], ws, CFG, MORNING + H)
            st = now.load_state(ws)
            self.assertEqual(st["top"]["at"], iso(MORNING + H))
            self.assertEqual(now.load_now(ws)["top"][0]["set_at"], iso(MORNING + H))

    def _hint_run(self, hint, *cands):
        return now.merge(inp(*cands, top=[t("A")], swap_hint=hint), None, {}, CFG, NOW)["swap_hint"]

    def test_hint_item_id_kept_when_it_names_an_item(self):
        c = cand("review_requested", since=hours_ago(5))
        iid = now.merge(inp(c), None, {}, CFG, NOW)["items"][0]["id"]
        h = self._hint_run({"title": "T", "why": "w", "link": "https://nowhere.example.com", "item_id": iid}, c)
        self.assertEqual(h["item_id"], iid)

    def test_hint_item_id_resolved_by_unique_link(self):
        c = cand("review_requested", since=hours_ago(5))
        out = now.merge(inp(c), None, {}, CFG, NOW)["items"][0]
        h = self._hint_run({"title": "T", "why": "w", "link": out["link"], "item_id": "bogus"}, c)
        self.assertEqual(h["item_id"], out["id"])

    def test_hint_item_id_dropped_when_unknown_or_link_ambiguous(self):
        c = cand("review_requested", since=hours_ago(5))
        h = self._hint_run({"title": "T", "why": "w", "link": "https://nowhere.example.com", "item_id": "bogus"}, c)
        self.assertEqual(h, {"title": "T", "why": "w", "link": "https://nowhere.example.com"})
        c2 = cand("unanswered_mention", since=hours_ago(5))
        both = now.merge(inp(c, c2), None, {}, CFG, NOW)["items"]
        self.assertEqual(len(both), 2)
        self.assertEqual(both[0]["link"], both[1]["link"])
        h = self._hint_run({"title": "T", "why": "w", "link": both[0]["link"], "item_id": "bogus"}, c, c2)
        self.assertNotIn("item_id", h)

    def test_hint_without_item_id_is_untouched(self):
        h = {"title": "T", "why": "w", "link": "https://example.com/x"}
        self.assertEqual(self._hint_run(h), h)


import subprocess  # noqa: E402
import shutil  # noqa: E402
import tempfile as _tf  # noqa: E402

CLI = os.path.join(ROOT, "bin", "mother")
NOW_PIN = "2026-10-27T09:00:00Z"


class NowCli(unittest.TestCase):
    def setUp(self):
        self._tmp = _tf.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"
        self.ws.mkdir()
        cfg = copy.deepcopy(CFG)
        cfg.setdefault("connectors", {}).update({"code": "github", "chat": "slack", "tracker": "jira"})
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.env = dict(os.environ, MOTHER_WORKSPACE=str(self.ws), MOTHER_STATE_DIR=str(Path(self._tmp.name) / "st"),
                        MOTHER_NOW=NOW_PIN)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, CLI, "now", *args], capture_output=True, text=True,
                              env=self.env, input=stdin)

    def merge(self, *cands, **kw):
        r = self.run_cli("merge", stdin=json.dumps(inp(*cands, **kw)))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def doc(self):
        r = self.run_cli("show", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def seed(self):
        return self.merge(cand("review_requested", since="2026-10-26T07:00:00Z", link="https://example.com/pr/1",
                               actions=[{"label": "Review it"}]),
                          cand("pr_no_reviewer", since="2026-10-20T07:00:00Z", link="https://example.com/pr/2"),
                          checked=["review_requested"], coverage={"code": "ok", "chat": "couldnt_check"})

    def test_merge_writes_file_and_prints_note(self):
        r = self.seed()
        self.assertEqual(r.stdout.strip(), "1 on you (1 new), 1 neglected, 0 suggestions; couldn't check: chat")
        self.assertTrue((self.ws / ".mother" / "now.json").is_file())
        self.assertEqual(len(self.doc()["items"]), 2)

    def test_merge_note_none(self):
        r = self.merge(coverage={"code": "ok"})
        self.assertTrue(r.stdout.strip().endswith("couldn't check: none"))

    def test_merge_malformed_exits_2_and_keeps_file(self):
        self.seed()
        p = self.ws / ".mother" / "now.json"
        before = p.read_bytes()
        for bad in ("not json", "[1]"):
            r = self.run_cli("merge", stdin=bad)
            self.assertEqual(r.returncode, 2, bad)
        self.assertEqual(p.read_bytes(), before)

    def test_show_without_file(self):
        r = self.run_cli("show")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Tick hasn't run on this machine yet.", r.stdout)

    def test_show_text_lists_sections(self):
        self.seed()
        out = self.run_cli("show").stdout
        self.assertIn("T review_requested", out)
        self.assertLess(out.index("T review_requested"), out.index("T pr_no_reviewer"))

    def test_done_hides_item_and_survives_merge(self):
        self.seed()
        iid = self.doc()["items"][0]["id"]
        self.assertEqual(self.run_cli("done", iid).returncode, 0)
        self.assertNotIn(iid, [i["id"] for i in self.doc()["items"]])
        self.assertEqual(self.run_cli("done", "nope").returncode, 2)

    def test_snooze(self):
        self.seed()
        iid = self.doc()["items"][0]["id"]
        self.assertEqual(self.run_cli("snooze", iid, "tomorrow").returncode, 0)
        self.assertNotIn(iid, [i["id"] for i in self.doc()["items"]])
        st = json.loads((self.ws / ".mother" / "now-state.json").read_text())
        self.assertEqual(st["items"][iid]["state"], "snoozed")

    def test_snooze_bogus_exits_2(self):
        self.seed()
        iid = self.doc()["items"][0]["id"]
        self.assertEqual(self.run_cli("snooze", "bogus", "1h").returncode, 2)
        self.assertEqual(self.run_cli("snooze", iid, "someday").returncode, 2)

    def test_top_set_item_and_free_text(self):
        self.seed()
        iid = self.doc()["items"][0]["id"]
        r = self.run_cli("top", "set", iid, "Write the Q4 plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        top = self.doc()["top"]
        self.assertEqual([e["set_by"] for e in top], ["manager", "manager"])
        self.assertEqual((top[0]["title"], top[0]["item_id"]), ("T review_requested", iid))
        self.assertEqual((top[1]["title"], top[1]["item_id"]), ("Write the Q4 plan", None))
        self.assertEqual(self.run_cli("top", "set").returncode, 2)
        self.assertEqual(self.run_cli("top", "set", "a", "b", "c", "d").returncode, 2)

    def test_top_swap_and_append_and_done(self):
        self.seed()
        iid = self.doc()["items"][0]["id"]
        other = self.doc()["items"][1]["id"]
        self.run_cli("top", "set", "Plan")
        self.assertEqual(self.run_cli("top", "swap", "3", iid).returncode, 0)
        top = self.doc()["top"]
        self.assertEqual([e["title"] for e in top], ["Plan", "T review_requested"])
        self.assertEqual(self.run_cli("top", "swap", "1", other).returncode, 0)
        self.assertEqual(self.doc()["top"][0]["item_id"], other)
        self.assertEqual(self.run_cli("top", "swap", "1", "nope").returncode, 2)
        self.assertEqual(self.run_cli("top", "swap", "4", iid).returncode, 2)
        self.assertEqual(self.run_cli("top", "done", "2").returncode, 0)
        self.assertEqual([e["state"] for e in self.doc()["top"]], ["open", "done"])
        self.assertEqual(self.run_cli("top", "done", "3").returncode, 2)

    def test_do_prints_item_and_action(self):
        self.seed()
        r = self.run_cli("do", "S1")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["action"]["id"], "S1")
        self.assertIn("task", out["action"])
        self.assertEqual(out["item"]["signal"], "review_requested")

    def test_do_unknown_exits_2(self):
        self.seed()
        self.assertEqual(self.run_cli("do", "S9").returncode, 2)

    def test_do_lapsed_suggestion_exits_2(self):
        self.merge(cand("suggestion_waiting", since="2026-10-27T07:00:00Z", kind="todo",
                        link="https://example.com/s", actions=[{"label": "Do"}]))
        self.assertEqual(self.run_cli("do", "S1").returncode, 0)
        self.env["MOTHER_NOW"] = "2026-11-03T09:00:00Z"
        self.assertEqual(self.run_cli("do", "S1").returncode, 2)

    def test_unknown_verb(self):
        self.assertEqual(self.run_cli("bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
