"""The chief CLI's output against tests/fixtures/chief_cli.ts, the capture hooks/chief.test.tsx feeds the Chief pane mod.
This runs the real `bin/mother` in a temp HOME and workspace, so a changed shape fails here, and the mod's test of the
same file shows what the change does to the pane.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOTHER = os.path.join(ROOT, "bin", "mother")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "chief_cli.ts")
HEAD = "// Written by tests/test_cli_contract.py from the real CLI: don't edit by hand.\nexport const CLI = "
AT = "2026-11-02T09:00:00Z"
CONFIG = {"version": 1, "agent": {"name": "Agnes", "emoji": ":sparkles:", "manager": "Dana Levi", "manager_short": "Dana"},
          "manager": {"email": "dana@example.com", "timezone": "Europe/London", "workdays": ["Mon"],
                      "chat_dm": ["U00000001", "D00000001"]},
          "workspace": {"git_remote": None, "home_machine": None},
          "connectors": {"chat": "slack", "tracker": "jira", "mail": "gmail", "calendar": "google", "code": "github"},
          "teams": [{"slug": "platform", "name": "Platform", "tracker": {"project": "PLAT", "board": 1234},
                     "channels": [{"id": "C00000001", "name": "#platform", "mode": "draft"}]}],
          "policy": {"autonomy": "functional"}}

def normal(text: str) -> str:
    """What differs run to run: instants."""
    return re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", AT, text)


def capture() -> dict:
    tmp = tempfile.mkdtemp(prefix="mother-contract-")
    try:
        ws, home = os.path.join(tmp, "ws"), os.path.join(tmp, "home")
        os.makedirs(os.path.join(ws, ".mother", "chief"))
        os.makedirs(home)
        with open(os.path.join(ws, "config.json"), "w") as f:
            json.dump(CONFIG, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MOTHER_")}
        env.update(HOME=home, MOTHER_WORKSPACE=ws, MOTHER_NO_SYNC="1", MOTHER_STATE_DIR=os.path.join(home, ".mother"))
        out = {}

        def run(name, *args, stdin=None):
            p = subprocess.run([sys.executable, MOTHER] + list(args), input=stdin, capture_output=True, text=True, env=env)
            if name:
                out[name] = {"exitCode": p.returncode, "stdout": normal(p.stdout), "stderr": normal(p.stderr)}

        run("tick_nothing", "chief", "tick", "--json")
        run("status_idle", "chief", "status", "--json")
        run(None, "chief", "tick", "--json", "--as", "session-a")
        run("tick_skipped", "chief", "tick", "--json", "--as", "session-b")
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
