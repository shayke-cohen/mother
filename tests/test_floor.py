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
        self.assertEqual(stored, dict(CARD, at="2026-11-01T09:00:00Z", ask_id=floor.ask_id(CARD["question"], "2026-11-01T09:00:00Z")))
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
        self.assertEqual(sorted(card), ["ask_id", "asks", "at", "options", "question", "session_id", "waiting_on"])
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

    def test_ask_id_is_the_question_at_its_time(self):
        first = floor.write_card(self.ws, AT, CARD)
        self.assertRegex(first["ask_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(floor.list_cards(self.ws, AT)[0]["ask_id"], first["ask_id"])
        # the same question written again is the same question: a refresh doesn't restart its age or lift a Hold
        again = floor.write_card(self.ws, AT + timedelta(hours=1), CARD)
        self.assertEqual((again["ask_id"], again["at"]), (first["ask_id"], first["at"]))
        # another question is another id
        other = floor.write_card(self.ws, AT + timedelta(hours=2), dict(CARD, question="Merge or wait?"))
        self.assertNotEqual(other["ask_id"], first["ask_id"])
        # asked afresh after an answer cleared it: another id, though the words match
        cleared = floor.write_card(self.ws, AT + timedelta(hours=3), dict(CARD, asks=False))
        self.assertNotIn("ask_id", cleared)
        afresh = floor.write_card(self.ws, AT + timedelta(hours=4), CARD)
        self.assertNotEqual(afresh["ask_id"], first["ask_id"])
        self.assertEqual(afresh["at"], "2026-11-01T13:00:00Z")

    def test_a_stored_ask_id_is_never_believed(self):
        floor.write_card(self.ws, AT, CARD)
        path = floor.floor_dir(self.ws) / (SID + ".json")
        path.write_text(json.dumps(dict(json.loads(path.read_text()), ask_id="forged")))
        self.assertNotEqual(floor.list_cards(self.ws, AT)[0]["ask_id"], "forged")

    def test_new_fields_are_kept_cleaned_and_only_when_they_say_something(self):
        card = floor.write_card(self.ws, AT, dict(CARD, recommendation="Ship it\nnow " + "r" * 300, why_manager="w" * 200, recap="Tests pass.\n" + "x" * 300,
                                                  source="wrap_up", repo="o/r", branch="feat/x", repo_url="https://github.com/o/r"))
        self.assertTrue(card["recommendation"].startswith("Ship it now "))
        self.assertEqual((len(card["recommendation"]), len(card["why_manager"]), len(card["recap"])), (200, 100, 200))
        self.assertEqual((card["source"], card["repo"], card["branch"], card["repo_url"]), ("wrap_up", "o/r", "feat/x", "https://github.com/o/r"))
        listed = floor.list_cards(self.ws, AT)[0]
        self.assertEqual({k: listed[k] for k in ("recommendation", "source", "repo_url")}, {k: card[k] for k in ("recommendation", "source", "repo_url")})
        bare = floor.write_card(self.ws, AT, dict(CARD, session_id=SID_B, source="anything", repo_url="javascript:alert(1)", recommendation=7))
        self.assertEqual([k for k in ("source", "repo_url", "recommendation", "why_manager", "recap", "repo", "branch") if k in bare], [])
        # a card with no ask keeps its recap, and no recommendation
        done = floor.write_card(self.ws, AT, {"session_id": SID, "asks": False, "recap": "Merged.", "recommendation": "x", "source": "wrap_up"})
        self.assertEqual((done["recap"], done["source"], "recommendation" in done, "ask_id" in done), ("Merged.", "wrap_up", False, False))

    def checkout(self, remote, head="ref: refs/heads/feat/x\n"):
        repo = self.ws / "co"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text(head)
        (repo / ".git" / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = %s\n\tfetch = x\n[branch "m"]\n\tremote = origin\n' % remote)
        (repo / "src").mkdir()
        return repo

    def test_repo_and_branch_come_from_the_checkouts_files(self):
        repo = self.checkout("git@github.com:o/r.git")
        card = floor.write_card(self.ws, AT, dict(CARD, cwd=str(repo / "src")))
        self.assertEqual((card["repo"], card["branch"], card["repo_url"]), ("o/r", "feat/x", "https://github.com/o/r"))
        self.assertNotIn("cwd", card)
        self.assertNotIn("cwd", json.loads((floor.floor_dir(self.ws) / (SID + ".json")).read_text()))

    def test_repo_facts_keeps_no_credential_and_guesses_no_link(self):
        got = floor.repo_facts(str(self.checkout("https://user:hunter2@github.com/o/r")))
        self.assertEqual(got, {"branch": "feat/x", "repo": "o/r", "repo_url": "https://github.com/o/r"})
        self.assertNotIn("hunter2", json.dumps(got))

    def test_repo_facts_elsewhere(self):
        self.assertEqual(floor.repo_facts(str(self.checkout("https://gitlab.example.com/o/r.git", head="abc123\n"))), {"repo": "o/r"})  # no PR link shape; detached: no branch
        self.assertEqual(floor.repo_facts(str(self.ws / "co2")), {})
        self.assertEqual(floor.repo_facts("relative/path"), {})
        self.assertEqual(floor.repo_facts(None), {})

    def test_repo_facts_in_a_worktree(self):
        main = self.checkout("git@github.com:o/r.git", head="ref: refs/heads/main\n")
        wt = self.ws / "wt"
        wt.mkdir()
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/side\n")
        (gitdir / "commondir").write_text("../..\n")
        (wt / ".git").write_text("gitdir: %s\n" % gitdir)
        self.assertEqual(floor.repo_facts(str(wt)), {"branch": "side", "repo": "o/r", "repo_url": "https://github.com/o/r"})


class WaitingTest(unittest.TestCase):
    """The card's `waiting` (CONTRACT §5g "Waiting on"): parsed from the final message, `since` kept while it repeats."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def card(self, at, text=None, **more):
        doc = {"session_id": SID, "asks": False, "question": "", "options": [], "waiting_on": None, **more}
        if text is not None:
            doc["waiting_text"] = text
        return floor.write_card(self.ws, at, doc)

    def listed(self, at):
        return {c["session_id"]: c for c in floor.list_cards(self.ws, at)}

    def test_parses_the_last_line_in_each_form(self):
        self.assertEqual(self.card(AT, "done the fix.\nWaiting on: Dana — the API key")["waiting"],
                         {"kind": "other", "who": "Dana", "what": "the API key", "since": floor._iso(AT)})
        self.assertEqual(self.card(AT, "Waiting on: the manager — pick a name")["waiting"]["kind"], "manager")
        self.assertEqual(self.card(AT, "Waiting on: nothing — done")["waiting"]["kind"], "nothing")
        self.assertEqual(floor.waiting_line("Waiting on: Dana — x\nmore text"), None)   # only the last line counts

    def test_since_is_kept_while_the_same_wait_repeats(self):
        self.card(AT, "Waiting on: Dana — the API key")
        later = AT + timedelta(hours=5)
        self.assertEqual(self.card(later, "ok\nWaiting on: dana — the API key")["waiting"]["since"], floor._iso(AT))
        self.card(later + timedelta(minutes=1))                         # the manager typed: no text, the wait stays
        self.assertEqual(self.listed(later)[SID]["waiting"]["since"], floor._iso(AT))
        self.card(later, "turn with no line")                            # a turn that forgot the line keeps it too
        self.assertEqual(self.listed(later)[SID]["waiting"]["since"], floor._iso(AT))
        moved = self.card(later, "Waiting on: Dana — the signed contract")["waiting"]   # what changed: a new wait
        self.assertEqual(moved["since"], floor._iso(later))
        self.assertEqual(self.card(later, "Waiting on: Sam — the signed contract")["waiting"]["since"], floor._iso(later))

    def test_a_caller_cannot_set_the_wait_or_its_marks(self):
        got = self.card(AT, None, waiting={"kind": "other", "who": "X", "what": "y", "since": "2020-01-01T00:00:00Z"},
                        done_kept=floor._iso(AT))
        self.assertNotIn("waiting", got)
        self.assertNotIn("done_kept", got)

    def test_check_due_after_24_hours_and_once_a_day(self):
        self.card(AT, "Waiting on: Dana — the API key")
        self.assertFalse(self.listed(AT + timedelta(hours=23))[SID]["waiting"]["check_due"])
        day = AT + timedelta(hours=25)
        self.assertTrue(self.listed(day)[SID]["waiting"]["check_due"])
        self.assertEqual(floor.mark(self.ws, day, {"session_id": SID, "mark": "checked"})["mark"], "checked")
        self.assertFalse(self.listed(day)[SID]["waiting"]["check_due"])
        with self.assertRaisesRegex(ValueError, "last 24 hours"):
            floor.mark(self.ws, day + timedelta(hours=2), {"session_id": SID, "mark": "checked"})
        self.assertTrue(self.listed(day + timedelta(hours=24))[SID]["waiting"]["check_due"])
        self.card(day, "Waiting on: the manager — a yes")                 # waiting on the manager is never re-checked
        self.assertFalse(self.listed(day + timedelta(days=3))[SID]["waiting"]["check_due"])

    def test_marks_keep_the_cards_time_and_reset_with_a_new_wait(self):
        written = self.card(AT, "Waiting on: Dana — the API key")["at"]
        floor.mark(self.ws, AT + timedelta(days=3), {"session_id": SID, "mark": "keep"})
        floor.mark(self.ws, AT + timedelta(days=3), {"session_id": SID, "mark": "drop"})
        c = self.listed(AT + timedelta(days=3))[SID]
        self.assertEqual((c["at"], c["waiting"]["snooze_until"], c["waiting"]["dropped"]),
                         (written, floor._iso(AT + timedelta(days=6)), True))
        self.card(AT + timedelta(days=4), "Waiting on: Dana — the API key")   # the same wait: marks stay
        self.assertTrue(self.listed(AT + timedelta(days=4))[SID]["waiting"]["dropped"])
        self.card(AT + timedelta(days=4), "Waiting on: Dana — a review")      # a new one: none
        self.assertNotIn("dropped", self.listed(AT + timedelta(days=4))[SID]["waiting"])

    def test_done_keep_lasts_until_the_session_moves(self):
        c = self.card(AT, "Waiting on: nothing — done")
        floor.mark(self.ws, AT + timedelta(minutes=11), {"session_id": SID, "mark": "done-keep"})
        self.assertEqual(self.listed(AT + timedelta(minutes=11))[SID]["done_kept"], c["at"])
        self.card(AT + timedelta(minutes=20))                             # it moved: the mark is gone
        self.assertNotIn("done_kept", self.listed(AT + timedelta(minutes=20))[SID])

    def test_mark_refusals_and_cli(self):
        for doc, why in (({"session_id": "x", "mark": "keep"}, "36-character"), ({"session_id": SID, "mark": "nope"}, "mark must"),
                         ({"session_id": SID, "mark": "keep"}, "no card")):
            with self.assertRaisesRegex(ValueError, why):
                floor.mark(self.ws, AT, doc)
        self.card(AT)
        with self.assertRaisesRegex(ValueError, "waits on nothing"):
            floor.mark(self.ws, AT, {"session_id": SID, "mark": "keep"})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": SID, "mark": "done-keep"}))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(floor.main(["mark", "--stdin"], self.ws, {}, AT), 0)
        self.assertEqual(json.loads(out.getvalue())["mark"], "done-keep")

    def test_note_waiting_from_a_transcript(self):
        floor.note_waiting(self.ws, AT, SID, {"state": "waiting_other", "on": "Dana", "what": "the key"})
        c = self.listed(AT)[SID]
        self.assertEqual((c["asks"], c["waiting"]["who"], c["waiting"]["since"]), (False, "Dana", floor._iso(AT)))
        floor.note_waiting(self.ws, AT + timedelta(hours=1), SID, {"state": "waiting_other", "on": "Dana", "what": "the key"})
        self.assertEqual(self.listed(AT)[SID]["waiting"]["since"], floor._iso(AT))
        self.assertIsNone(floor.note_waiting(self.ws, AT, "bad", {"state": "done", "on": None, "what": ""}))


if __name__ == "__main__":
    unittest.main()
