"""The chief CLI's output against tests/fixtures/chief_cli.ts, the capture hooks/chief.test.tsx feeds the Chief pane mod.
This runs the real `bin/mother` in a temp HOME and workspace, so a changed shape fails here, and the mod's test of the
same file shows what the change does to the pane. No worker runs and nothing reaches Slack.
Run: python3 -m unittest tests.test_cli_contract -v
Refresh the file after a deliberate change: python3 tests/test_cli_contract.py --write
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOTHER = os.path.join(ROOT, "bin", "mother")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "chief_cli.ts")
HEAD = "// Written by tests/test_cli_contract.py from the real CLI: don't edit by hand.\nexport const CLI = "
SIG = ":sparkles: *Agnes (AI)* · on behalf of Dana"
POST = "mcp__chat__slack__post_message"
CREATE = "mcp__tracker__jira__create-issue"
MAIL = "mcp__mail__gmail__send-gmail-message"
AT, UUID, POST_TS = "2026-11-02T09:00:00Z", "00000000-0000-4000-8000-000000000001", "1762077600.000100"
CONFIG = {"version": 1, "agent": {"name": "Agnes", "emoji": ":sparkles:", "manager": "Dana Levi", "manager_short": "Dana"},
          "manager": {"email": "dana@example.com", "timezone": "Europe/London", "workdays": ["Mon"],
                      "chat_dm": ["U00000001", "D00000001"]},
          "workspace": {"git_remote": None, "home_machine": None},
          "connectors": {"chat": "slack", "tracker": "jira", "mail": "gmail", "calendar": "google", "code": "github"},
          "teams": [], "policy": {"autonomy": "functional"}}


def normal(text: str) -> str:
    """What differs run to run: instants and the worker's session id."""
    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", AT, text)
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", UUID, text)


def capture() -> dict:
    tmp = tempfile.mkdtemp(prefix="mother-contract-")
    try:
        ws, home = os.path.join(tmp, "ws"), os.path.join(tmp, "home")
        os.makedirs(os.path.join(ws, ".mother", "chief"))
        os.makedirs(home)
        with open(os.path.join(ws, "config.json"), "w") as f:
            json.dump(CONFIG, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MOTHER_")}
        env.update(HOME=home, MOTHER_WORKSPACE=ws, MOTHER_NO_SYNC="1", MOTHER_STATE_DIR=os.path.join(home, ".mother"),
                   MOTHER_CLAUDE_BIN=os.path.join(ROOT, "tests", "fixtures", "fake_claude.py"),
                   FAKE_CLAUDE_LOG=os.path.join(tmp, "fake.log"))
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import chief
        chief.register_worker(Path(ws), datetime.now(timezone.utc), "PLAT-1", "Plat one")
        out = {}
        subprocess.run([sys.executable, MOTHER, "hub", "dms", "inbox", "D00000001"], capture_output=True, check=True, env=env)

        def run(name, *args, stdin=None):
            p = subprocess.run([sys.executable, MOTHER] + list(args), input=stdin, capture_output=True, text=True, env=env)
            out[name] = {"exitCode": p.returncode, "stdout": normal(p.stdout), "stderr": normal(p.stderr)}

        def file_it(name, tool, inp, *extra):
            run(name, "decide", "propose", "PLAT-1", "--tool", tool, "--recommend", "send", *extra, "--stdin",
                stdin=json.dumps(inp))

        run("tick_nothing", "chief", "tick", "--json")
        run("batch_nothing", "decide", "batch", "--json")
        file_it("propose_unsigned", POST, {"channel_id": "C00000001", "text": "hi"})
        file_it("propose_reply", POST, {"channel_id": "C00000001", "thread_ts": "1700000000.000100",
                                        "text": "We ship Tuesday.\n" + SIG}, "--why", "they <asked> @x")
        file_it("propose_create", CREATE, {"projectKey": "PLAT", "issueType": "Bug", "summary": "s", "description": "d"},
                "--urgent")
        file_it("propose_mail", MAIL, {"to": ["a@example.com"], "subject": "x", "body": "y"})
        run("tick_due", "chief", "tick", "--json")
        run("status_pending", "chief", "status", "--json")
        run("sent_before_batch", "decide", "sent", "--ids", "D1,D2,D3", "--ts", POST_TS)
        run("batch_due", "decide", "batch", "--json")
        run("sent_ok", "decide", "sent", "--ids", "D1,D2,D3", "--ts", POST_TS)
        run("sent_again", "decide", "sent", "--ids", "D1,D2,D3", "--ts", POST_TS)
        run("sent_no_ids", "decide", "sent", "--ids", "", "--ts", POST_TS)
        run("edit_several_fields", "decide", "answer", "2", "edit", "--via", "pane", "--", "new words")
        run("answer_ok", "decide", "answer", "1", "ok", "--via", "pane")
        run("answer_again", "decide", "answer", "1", "ok", "--via", "pane")
        run("answer_no", "decide", "answer", "2", "no", "--via", "pane")
        run("answer_usage", "decide", "answer", "3", "edit", "--via", "pane")
        run("post_log", "chief", "post-log", "--stdin", stdin=json.dumps(
            {"tool": "slack__post_message", "target": "D00000001", "outcome": "attempt", "reason": ""}))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_fixture() -> dict:
    with open(FIXTURE, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith(HEAD):
        raise ValueError("%s doesn't start as this module writes it" % FIXTURE)
    return json.loads(text[len(HEAD):])


def write_fixture(doc: dict) -> None:
    with open(FIXTURE, "w", encoding="utf-8") as f:
        f.write(HEAD + json.dumps(doc, indent=1, ensure_ascii=False) + "\n")


class CliContract(unittest.TestCase):
    maxDiff = None

    def test_the_real_cli_prints_what_the_mods_fixture_holds(self):
        got, want = capture(), read_fixture()
        self.assertEqual(sorted(got), sorted(want))
        for name in want:
            self.assertEqual(got[name], want[name], "%s changed: if that's meant, run python3 "
                             "tests/test_cli_contract.py --write and read what hooks/chief.test.tsx says" % name)

    def test_the_fixture_is_safe_to_publish(self):
        with open(FIXTURE, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(os.path.expanduser("~"), text)
        self.assertNotIn("/tmp", text)


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        write_fixture(capture())
    else:
        unittest.main()
