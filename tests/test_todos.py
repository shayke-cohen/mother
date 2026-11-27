"""`mother todo close|list` in a temp workspace. Stdlib only: python3 -m unittest tests.test_todos -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "mother"

OPEN = """# Open loops

Work committed to and not yet done.

## Yours
- [ ] Send the deck to Dana `+some-project` `due:2026-12-01` `~manager@2026-11-20`
- [ ] Reply to Sam re: billing cutover date `~manager@2026-11-21`
- [ ] Reply to Sam re: hiring plan `~agent/chat@2026-11-22`

## Mine
- [ ] Read the Q2 retro deck `~agent/mail@2026-11-10`

## Blocked
_none_
"""

ARCHIVE = """# Archive

Done and expired open loops.

- [x] Old thing `done:2026-11-01` `~manager@2026-10-30`
"""

LINT = """#!/bin/sh
# minimal stand-in: every archive item line must end with an origin and carry an outcome
grep '^- \\[' todos/archive.md | grep -vE '`(done|expired):[0-9-]+` `~[^`]+@[0-9-]+`$' && exit 1
exit 0
"""


def run(args, env):
    return subprocess.run([sys.executable, str(CLI), "todo", *args], capture_output=True, text=True, env=env)


class TodoCloseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / "todos").mkdir(parents=True)
        (self.ws / "todos" / "open.md").write_text(OPEN)
        (self.ws / "todos" / "archive.md").write_text(ARCHIVE)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MOTHER_")}
        self.env.update(MOTHER_WORKSPACE=str(self.ws), MOTHER_STATE_DIR=str(Path(self.tmp.name) / "state"),
                        MOTHER_NOW="2026-11-28T09:00:00Z")

    def tearDown(self):
        self.tmp.cleanup()

    def open_text(self):
        return (self.ws / "todos" / "open.md").read_text()

    def archive_text(self):
        return (self.ws / "todos" / "archive.md").read_text()

    def test_one_match_moves_the_line_with_done_before_the_origin(self):
        p = run(["close", "send the DECK"], self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        want = "- [x] Send the deck to Dana `+some-project` `due:2026-12-01` `done:2026-11-28` `~manager@2026-11-20`"
        self.assertEqual(p.stdout.strip(), want)
        self.assertEqual(self.archive_text(), ARCHIVE + want + "\n")
        self.assertEqual(self.open_text(), OPEN.replace(
            "- [ ] Send the deck to Dana `+some-project` `due:2026-12-01` `~manager@2026-11-20`\n", ""))

    def test_origin_stays_last(self):
        run(["close", "retro deck"], self.env)
        last = self.archive_text().splitlines()[-1]
        self.assertTrue(last.endswith("`~agent/mail@2026-11-10`"), last)
        self.assertIn("`done:2026-11-28` `~agent/mail@2026-11-10`", last)

    def test_expired_and_date(self):
        p = run(["close", "retro deck", "--expired", "--date", "2026-11-24"], self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(),
                         "- [x] Read the Q2 retro deck `expired:2026-11-24` `~agent/mail@2026-11-10`")

    def test_emptied_section_gets_none_and_other_lines_untouched(self):
        run(["close", "retro deck"], self.env)
        self.assertEqual(self.open_text(), OPEN.replace(
            "- [ ] Read the Q2 retro deck `~agent/mail@2026-11-10`\n", "_none_\n"))
        self.assertTrue(self.archive_text().startswith(ARCHIVE))

    def test_zero_matches_writes_nothing_and_lists_candidates(self):
        p = run(["close", "no such item"], self.env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no item", p.stderr)
        self.assertIn("Send the deck to Dana", p.stderr)
        self.assertEqual(self.open_text(), OPEN)
        self.assertEqual(self.archive_text(), ARCHIVE)

    def test_many_matches_writes_nothing_and_lists_them(self):
        p = run(["close", "reply to sam"], self.env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("2 items", p.stderr)
        self.assertIn("billing cutover", p.stderr)
        self.assertIn("hiring plan", p.stderr)
        self.assertNotIn("Send the deck", p.stderr)
        self.assertEqual(self.open_text(), OPEN)
        self.assertEqual(self.archive_text(), ARCHIVE)

    def test_origin_is_not_a_match_target(self):
        p = run(["close", "manager@"], self.env)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.open_text(), OPEN)

    def test_malformed_line_is_refused(self):
        (self.ws / "todos" / "open.md").write_text(OPEN.replace("`~manager@2026-11-21`", "`~manager@2026-11-21` `due:2026-12-01`"))
        p = run(["close", "billing cutover"], self.env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("origin", p.stderr)
        self.assertEqual(self.archive_text(), ARCHIVE)

    def test_bad_date_and_usage(self):
        self.assertEqual(run(["close", "retro deck", "--date", "28/11/2026"], self.env).returncode, 2)
        self.assertEqual(run(["close"], self.env).returncode, 2)
        self.assertEqual(run(["close", "a", "b"], self.env).returncode, 2)
        self.assertEqual(run(["help"], self.env).returncode, 0)
        self.assertEqual(self.archive_text(), ARCHIVE)

    def test_runs_workspace_lint(self):
        (self.ws / "scripts").mkdir()
        (self.ws / "scripts" / "lint-todos.sh").write_text(LINT)
        p = run(["close", "retro deck"], self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("lint-todos.sh: ok", p.stdout)
        (self.ws / "todos" / "archive.md").write_text(ARCHIVE + "- [x] broken `~manager@2026-11-01`\n")
        p = run(["close", "send the deck"], self.env)
        self.assertEqual(p.returncode, 3)
        self.assertIn("lint-todos.sh: FAILED", p.stdout)

    def test_missing_archive_is_created_from_template(self):
        (self.ws / "todos" / "archive.md").unlink()
        p = run(["close", "retro deck"], self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(self.archive_text().startswith("# Archive"))
        self.assertTrue(self.archive_text().endswith("`done:2026-11-28` `~agent/mail@2026-11-10`\n"))

    def test_list(self):
        p = run(["list"], self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("## Yours\n- [ ] Send the deck", p.stdout)
        self.assertIn("4 items", p.stdout)
        rows = json.loads(run(["list", "--json"], self.env).stdout)
        self.assertEqual([r["section"] for r in rows], ["Yours", "Yours", "Yours", "Mine"])


if __name__ == "__main__":
    unittest.main()
