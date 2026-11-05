"""Tests for lib/floor.py — the per-session "needs you" cards.
Run: python3 -m unittest tests.test_floor -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import floor  # noqa: E402

AT = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
SID = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"
CARD = {"session_id": SID, "asks": True, "question": "Ship it now or wait for review?",
        "options": [{"label": "Ship", "message": "Ship it now."}, {"label": "Wait", "message": "Wait for review."}],
        "waiting_on": "the manager"}


class FloorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def run_main(self, args, stdin="", at=AT):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = floor.main(args, self.ws, {}, at)
        return code, out.getvalue(), err.getvalue()

    def test_card_then_list(self):
        code, out, _ = self.run_main(["card", "--stdin"], json.dumps(CARD))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"stored": SID, "asks": True})
        stored = json.loads((self.ws / ".mother" / "floor" / (SID + ".json")).read_text())
        self.assertEqual(stored, dict(CARD, at="2026-11-01T09:00:00Z"))
        code, out, _ = self.run_main(["list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"version": 1, "cards": [stored]})

    def test_bad_session_id_exits_2_and_writes_nothing(self):
        for sid in ("../../etc/passwd", "local_a", SID.replace("1", "A"), SID.replace("1", "g"), SID + "0", "", None, 7):
            code, out, err = self.run_main(["card", "--stdin"], json.dumps(dict(CARD, session_id=sid)))
            self.assertEqual(code, 2, sid)
            self.assertEqual(out, "")
            self.assertIn("session_id", err)
        code, _, _ = self.run_main(["card", "--stdin"], "not json")
        self.assertEqual(code, 2)
        self.assertFalse((self.ws / ".mother" / "floor").exists())

    def test_unknown_verb_exits_2(self):
        self.assertEqual(self.run_main(["card"])[0], 2)
        self.assertEqual(self.run_main(["list"])[0], 2)
        self.assertEqual(self.run_main(["drop", SID])[0], 2)

    def test_write_is_atomic(self):
        floor.write_card(self.ws, AT, CARD)
        path = floor.floor_dir(self.ws) / (SID + ".json")
        before = path.read_text()
        seen = []
        real = os.replace

        def replace(src, dst):
            seen.append((Path(src).name, Path(dst).name, Path(dst).read_text()))  # the old card is whole until the rename
            real(src, dst)

        with mock.patch.object(floor.os, "replace", replace):
            floor.write_card(self.ws, AT + timedelta(minutes=1), dict(CARD, asks=False))
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].endswith(".tmp"))
        self.assertEqual(seen[0][1:], (SID + ".json", before))
        self.assertEqual(sorted(p.name for p in floor.floor_dir(self.ws).iterdir()), [SID + ".json"])  # no temp file left
        self.assertFalse(json.loads(path.read_text())["asks"])

    def test_clean_caps_and_drops_off_shape(self):
        card = floor.write_card(self.ws, AT, {
            "session_id": SID, "asks": True, "question": "a\n" + "q" * 300, "waiting_on": "w" * 200, "extra": "x",
            "options": [{"label": "L" * 40, "message": "m" * 300}, {"label": "no message"}, "text", {"label": "", "message": "x"}]
                       + [{"label": "o%d" % i, "message": "go"} for i in range(6)]})
        self.assertEqual(len(card["question"]), 200)
        self.assertNotIn("\n", card["question"])
        self.assertEqual(len(card["waiting_on"]), 80)
        self.assertEqual(len(card["options"]), 4)
        self.assertEqual((len(card["options"][0]["label"]), len(card["options"][0]["message"])), (24, 200))
        self.assertEqual(sorted(card), ["asks", "at", "options", "question", "session_id", "waiting_on"])
        cleared = floor.write_card(self.ws, AT, dict(CARD, asks=False))
        self.assertEqual((cleared["asks"], cleared["question"], cleared["options"], cleared["waiting_on"]), (False, "", [], None))
        self.assertFalse(floor.write_card(self.ws, AT, dict(CARD, asks="yes"))["asks"])   # only a real true asks
        self.assertFalse(floor.write_card(self.ws, AT, dict(CARD, question=""))["asks"])  # nothing to ask

    def test_list_prunes_after_7_days_and_skips_unreadable(self):
        floor.write_card(self.ws, AT - timedelta(days=7, minutes=1), dict(CARD, session_id=SID_B))
        floor.write_card(self.ws, AT - timedelta(days=6), CARD)
        folder = floor.floor_dir(self.ws)
        (folder / "33333333-3333-4333-8333-333333333333.json").write_text("{broken")
        (folder / "44444444-4444-4444-8444-444444444444.json").write_text(json.dumps(dict(CARD, at="2026-11-01T09:00:00Z")))  # another id inside
        cards = floor.list_cards(self.ws, AT)
        self.assertEqual([c["session_id"] for c in cards], [SID])
        self.assertFalse((folder / (SID_B + ".json")).exists())                                   # the old one is deleted
        self.assertTrue((folder / "33333333-3333-4333-8333-333333333333.json").exists())         # unreadable: left alone

    def test_list_oldest_first_and_empty_without_a_folder(self):
        self.assertEqual(floor.list_cards(self.ws, AT), [])
        floor.write_card(self.ws, AT, CARD)
        floor.write_card(self.ws, AT - timedelta(hours=1), dict(CARD, session_id=SID_B))
        self.assertEqual([c["session_id"] for c in floor.list_cards(self.ws, AT)], [SID_B, SID])


if __name__ == "__main__":
    unittest.main()
