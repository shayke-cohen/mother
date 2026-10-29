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
from datetime import datetime, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import hub  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
