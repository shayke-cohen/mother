"""Tests for lib/items.py — the evidence check on a reader sub-agent's reply.
Run: python3 -m unittest tests.test_items -v
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import items  # noqa: E402

MOTHER = os.path.join(ROOT, "bin", "mother")


def reply(source="mail", status="ok", its=None, **cov):
    c = {"status": status, "reason": cov.pop("reason", "")}
    c.update(cov)
    return json.dumps({"source": source, "coverage": c, "items": its if its is not None else []})


def it(link, title="Alex asked about the rollout date", **kw):
    d = {"kind": "thread", "title": title, "who": "Alex", "when": "2026-10-25T08:00:00Z", "link": link}
    d.update(kw)
    return d


class TestCheck(unittest.TestCase):
    def test_uncited_items_are_dropped(self):
        code, r = items.check(reply(its=[it("https://mail.example.com/m/1"), it(""), it("see the thread"),
                                         {"title": "no link at all"}]))
        self.assertEqual(code, 0)
        self.assertEqual([x["link"] for x in r["items"]], ["https://mail.example.com/m/1"])
        self.assertEqual(r["dropped"], 3)
        self.assertIsNone(r["warning"])

    def test_link_kinds(self):
        for good in ("https://x.example.com/a?b=1", "PLAT-123", "acme/web#412", "todos/open.md",
                     "teams/platform/intake.md"):
            self.assertTrue(items.is_link(good), good)
        for bad in ("", "plat-1", "the doc", "/etc/passwd", "../x.md", None, 12):
            self.assertFalse(items.is_link(bad), bad)

    def test_couldnt_check_is_kept_and_warned(self):
        code, r = items.check(reply(status="couldnt_check", reason="mail.read failed: 401"))
        self.assertEqual(code, 0)
        self.assertEqual(r["coverage"]["status"], "couldnt_check")
        self.assertIn("couldn't check — mail.read failed: 401", r["warning"])

    def test_partial_is_warned(self):
        code, r = items.check(reply(status="partial", reason="opened 8 of 22", opened=8, unopened=14,
                                    its=[it("https://mail.example.com/m/1")]))
        self.assertEqual(r["coverage"]["opened"], 8)
        self.assertIn("partial — opened 8 of 22", r["warning"])

    def test_all_uncited_is_not_quiet(self):
        code, r = items.check(reply(its=[it("somewhere"), it("")]))
        self.assertEqual(code, 0)
        self.assertEqual(r["coverage"]["status"], "couldnt_check")
        self.assertIn("uncited", r["warning"])

    def test_noise_count_with_a_query_is_a_count_not_a_failure(self):
        # The live tracker reader found nothing recent and reported only a stale-backlog tally cited by its JQL.
        jql = "project = MOBPUB AND assignee = currentUser() AND statusCategory != Done AND updated < -14d"
        code, r = items.check(reply(source="tracker", its=[it(jql, title="16 assigned issues stale 14+ days",
                                                               state="noise")]))
        self.assertEqual((code, r["coverage"]["status"], r["warning"]), (0, "ok", None))
        self.assertEqual(r["counts"], [{"title": "16 assigned issues stale 14+ days", "query": jql}])
        self.assertEqual((r["items"], r["dropped"]), ([], 0))

    def test_a_finding_is_not_a_count(self):
        code, r = items.check(reply(its=[it("see the search", state="needs-reply")]))
        self.assertEqual(r["coverage"]["status"], "couldnt_check")

    def test_quiet_ok_source_has_no_warning(self):
        code, r = items.check(reply(its=[]))
        self.assertEqual((code, r["coverage"]["status"], r["warning"]), (0, "ok", None))

    def test_cap_and_dedupe(self):
        its = [it("https://x.example.com/%d" % i) for i in range(30)] + [it("https://x.example.com/0")]
        code, r = items.check(reply(its=its), max_items=25)
        self.assertEqual(len(r["items"]), 25)
        self.assertEqual(r["truncated"], 5)

    def test_prose_and_fences_are_tolerated(self):
        raw = "Here is what I found.\n```json\n%s\n```\nDone." % reply(its=[it("PLAT-9")], source="tracker")
        code, r = items.check(raw)
        self.assertEqual((code, r["source"], r["items"][0]["link"]), (0, "tracker", "PLAT-9"))

    def test_malformed_is_couldnt_check_with_exit_2(self):
        for raw in ("", "I couldn't reach the server.", '{"items": []}', '{"source": "mail", "items": []}',
                    '{"source": "mail", "coverage": {"status": "fine"}, "items": []}',
                    '{"source": "mail", "coverage": {"status": "ok"}, "items": "none"}'):
            code, r = items.check(raw)
            self.assertEqual(code, 2, raw)
            self.assertEqual(r["coverage"]["status"], "couldnt_check")
            self.assertEqual(r["items"], [])

    def test_long_notes_are_trimmed(self):
        code, r = items.check(reply(its=[it("PLAT-1", note="x" * 500)]))
        self.assertEqual(len(r["items"][0]["note"]), items.NOTE_MAX)


class TestLinks(unittest.TestCase):
    def test_links_in_markdown(self):
        md = ("- Review: acme/web#412 — tidy\n- Tracker: PLAT-12 In Progress — "
              "https://acme.atlassian.net/browse/PLAT-12.\n- Chat: Sam — https://acme.slack.com/archives/C1/p2")
        self.assertEqual(items.links_in(md), ["https://acme.atlassian.net/browse/PLAT-12",
                                              "https://acme.slack.com/archives/C1/p2", "acme/web#412", "PLAT-12"])

    def test_short_and_bare_pr_refs(self):
        md = ("- Review (10): wix-private/himalaya#4745, #4744; app-smith-agent#712, #709\n"
              "- Plugin PR #2 is blocked\n- Slack #platform-support stays out")
        self.assertEqual(items.links_in(md), ["wix-private/himalaya#4745", "himalaya#4744", "app-smith-agent#712",
                                              "app-smith-agent#709"])

    def test_canon(self):
        for v in ("https://github.com/acme/web/pull/412", "acme/web#412", "web#412", "Web#412"):
            self.assertEqual(items.canon(v), "web#412", v)
        self.assertEqual(items.canon("https://x.example.com/a/"), "https://x.example.com/a")

    def test_source_of(self):
        self.assertEqual(items.source_of("https://acme.slack.com/archives/C1/p2"), "chat")
        self.assertEqual(items.source_of("https://github.com/acme/web/pull/412"), "code")
        self.assertEqual(items.source_of("acme/web#412"), "code")
        self.assertEqual(items.source_of("web#412"), "code")
        self.assertEqual(items.source_of("PLAT-12"), "tracker")
        self.assertEqual(items.source_of("https://mail.google.com/mail/u/0/#inbox/abc"), "mail")
        self.assertEqual(items.source_of("https://calendar.google.com/event?eid=1"), "calendar")
        self.assertIsNone(items.source_of("https://example.com/doc"))


class TestCli(unittest.TestCase):
    def run_cli(self, stdin, *args):
        return subprocess.run([sys.executable, MOTHER, "items", "check"] + list(args), input=stdin,
                              capture_output=True, text=True)

    def test_stdin(self):
        p = self.run_cli(reply(its=[it("PLAT-1"), it("nope")], source="tracker"))
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout)
        self.assertEqual((r["source"], r["dropped"]), ("tracker", 1))

    def test_malformed_exits_2(self):
        p = self.run_cli("the server timed out")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["coverage"]["status"], "couldnt_check")


if __name__ == "__main__":
    unittest.main()
