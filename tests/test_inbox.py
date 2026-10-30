"""Tests for lib/inbox.py — `mother inbox archive|read`, the panel's model-free mail triage.
Run: python3 -m unittest tests.test_inbox -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import inbox  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_mcp.py")
TOOLS = ["gw__list-emails", "gw__read-email", "gw__archive-message", "gw__mark-as-read"]
LISTED = {"messages": [{"id": "m1", "threadId": "T1"}, {"id": "m2", "threadId": "T1"}, {"id": "m3", "threadId": "T2"}]}
LINK = "https://mail.google.com/mail/u/0/#all/%s"


class Inbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / ".mother").mkdir(parents=True)
        self.log = os.path.join(self.tmp.name, "calls.log")
        self.fixture(TOOLS, {"gw__list-emails": LISTED})

    def fixture(self, tools, results, errors=None):
        path = os.path.join(self.tmp.name, "fx.json")
        with open(path, "w") as f:
            json.dump({"tools": tools, "results": results, "errors": errors or {}}, f)
        self.cfg = {"manager": {"timezone": "UTC", "email": "dana@example.com"}, "connectors": {"mail": "gmail"},
                    "hub": {"enabled": True, "connections": {"mail": {
                        "command": sys.executable, "args": [FAKE],
                        "env": {"FAKE_MCP_FIXTURE": path, "FAKE_MCP_LOG": self.log}}}}}

    def run_inbox(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", err):
            rc = inbox.main(list(args), self.ws, self.cfg, out)
        return rc, out.getvalue(), err.getvalue()

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def guard_log(self):
        p = self.ws / ".mother" / "guard.log"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_archive_takes_every_inbox_message_of_the_thread(self):
        rc, out, _ = self.run_inbox("archive", "--", LINK % "T1")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "archived mail:T1 (2 messages)")
        c = self.calls()
        self.assertEqual(c[0]["name"], "gw__list-emails")
        self.assertIn("in:inbox", c[0]["arguments"]["query"])
        self.assertEqual([(x["name"], x["arguments"]) for x in c[1:]],
                         [("gw__archive-message", {"messageId": "m1"}), ("gw__archive-message", {"messageId": "m2"})])
        rec = self.guard_log()
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["tool"], rec[0]["target"], rec[0]["decision"]),
                         ("mother inbox archive", "mail:T1", "allow"))

    def test_read_takes_the_unread_messages_and_a_key_works_like_a_link(self):
        rc, out, _ = self.run_inbox("read", "--", "mail:T2")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "marked read mail:T2 (1 message)")
        c = self.calls()
        self.assertIn("is:unread", c[0]["arguments"]["query"])
        self.assertEqual([(x["name"], x["arguments"]) for x in c[1:]], [("gw__mark-as-read", {"messageId": "m3"})])

    def test_a_thread_the_list_does_not_show_uses_its_own_id(self):
        rc, out, _ = self.run_inbox("archive", "--", "mail:T9")
        self.assertEqual(rc, 0)
        self.assertEqual([(x["name"], x["arguments"]) for x in self.calls()[1:]], [("gw__archive-message", {"messageId": "T9"})])

    def test_several_threads_in_one_call_list_once(self):
        rc, out, _ = self.run_inbox("archive", "--", "mail:T1", LINK % "T2")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["archived mail:T1 (2 messages)", "archived mail:T2 (1 message)"])
        self.assertEqual([x["name"] for x in self.calls()].count("gw__list-emails"), 1)

    def test_paused_writes_nothing(self):
        (self.ws / ".mother" / "PAUSED").write_text("")
        rc, _, err = self.run_inbox("archive", "--", "mail:T1")
        self.assertEqual(rc, 2)
        self.assertIn("paused", err)
        self.assertEqual(self.calls(), [])

    def test_the_guard_is_asked_first_and_a_no_stops_the_write(self):
        with mock.patch.object(inbox, "_guard_says", return_value="sending mail (needs a yes)"):
            rc, out, err = self.run_inbox("archive", "--", "mail:T1")
        self.assertEqual(rc, 1)
        self.assertIn("the guard would not let this through", err)
        self.assertNotIn("gw__archive-message", [x["name"] for x in self.calls()])
        self.assertEqual(self.guard_log()[0]["decision"], "refused")

    def test_the_real_guard_lets_archive_and_mark_read_through(self):
        self.assertIsNone(inbox._guard_says("gw__archive-message", {"messageId": "m1"}))
        self.assertIsNone(inbox._guard_says("gw__mark-as-read", {"messageId": "m1"}))
        self.assertIsNotNone(inbox._guard_says("gw__send-gmail-message", {"to": "a@example.com"}))

    def test_not_mail_or_no_connection_or_bad_usage_is_exit_2(self):
        self.assertEqual(self.run_inbox("archive", "--", "PLAT-2")[0], 2)
        self.assertEqual(self.run_inbox("archive")[0], 2)
        self.assertEqual(self.run_inbox("delete", "--", "mail:T1")[0], 2)
        self.cfg["hub"]["connections"] = {}
        rc, _, err = self.run_inbox("archive", "--", "mail:T1")
        self.assertEqual(rc, 2)
        self.assertIn("hub.connections.mail", err)
        self.cfg["connectors"]["mail"] = "other"
        self.assertEqual(self.run_inbox("archive", "--", "mail:T1")[0], 2)

    def test_a_missing_write_tool_or_a_failed_call_is_exit_1(self):
        self.fixture(["gw__list-emails"], {"gw__list-emails": LISTED})
        rc, _, err = self.run_inbox("archive", "--", "mail:T1")
        self.assertEqual(rc, 1)
        self.assertIn("archive-message", err)
        self.fixture(TOOLS, {"gw__list-emails": LISTED}, errors={"gw__archive-message": "403 forbidden"})
        rc, out, err = self.run_inbox("archive", "--", "mail:T1", "mail:T2")
        self.assertEqual(rc, 1)
        self.assertIn("couldn't archive mail:T1", err)


if __name__ == "__main__":
    unittest.main()
