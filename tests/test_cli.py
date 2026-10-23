"""Tests for bin/mother. Stdlib only: python3 -m unittest tests.test_cli -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "mother"


def run(args, env, cwd=None):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, env=env, cwd=cwd)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.env = dict(os.environ)
        self.env.pop("MOTHER_WORKSPACE", None)
        self.env.update({
            "MOTHER_STATE_DIR": str(self.t / "state"),
            "MOTHER_HOSTNAME": "laptop-a",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        })
        self.ws = self.t / "ws"

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, *extra):
        r = run(["init", str(self.ws), *extra], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def write_config(self, **over):
        cfg = json.loads((ROOT / "templates/workspace/config.example.json").read_text())
        cfg.update(over)
        (self.ws / "config.json").write_text(json.dumps(cfg))
        return cfg

    # -- link
    def test_link_symlinks_cli_and_refuses_to_clobber_a_file(self):
        env = dict(self.env, HOME=str(self.t / "home"))
        bindir = self.t / "bin"
        r = run(["link", str(bindir)], env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((bindir / "mother").resolve(), CLI.resolve())
        self.assertEqual(run(["link", str(bindir)], env).returncode, 0)  # re-link is idempotent
        (bindir / "mother").unlink()
        (bindir / "mother").write_text("not ours")
        r = run(["link", str(bindir)], env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a symlink", r.stderr)
        self.assertEqual((bindir / "mother").read_text(), "not ours")

    # -- init / pointer
    def test_init_creates_layout_and_pointer(self):
        self.init()
        for p in ["AGENTS.md", "CLAUDE.md", "config.json", "memory/MEMORY.md", "todos/open.md",
                  "projects/PROJECTS.md", "teams/TEAMS.md", "runs.md", ".mother", ".git"]:
            self.assertTrue((self.ws / p).exists(), p)
        self.assertFalse((self.ws / "config.example.json").exists())
        self.assertEqual((self.t / "state/workspace").read_text().strip(), str(self.ws.resolve()))
        self.assertEqual(run(["path"], self.env).stdout.strip(), str(self.ws.resolve()))

    def test_init_does_not_overwrite_existing_files(self):
        self.ws.mkdir()
        (self.ws / "AGENTS.md").write_text("mine")
        self.init()
        self.assertEqual((self.ws / "AGENTS.md").read_text(), "mine")

    def test_init_adopting_repo_extends_its_gitignore(self):
        self.ws.mkdir()
        (self.ws / "AGENTS.md").write_text("existing agent")
        (self.ws / ".gitignore").write_text("node_modules/\n")
        self.init()
        gi = (self.ws / ".gitignore").read_text()
        self.assertIn("node_modules/", gi)
        self.assertIn(".mother/", gi)
        self.init()  # idempotent: no duplicate lines
        self.assertEqual((self.ws / ".gitignore").read_text().count(".mother/"), 1)
        run(["sync"], self.env)
        self.assertNotIn(".mother", git(self.ws, "ls-files").stdout)

    def test_init_refuses_unrelated_nonempty_dir(self):
        self.ws.mkdir()
        (self.ws / "random.txt").write_text("x")
        r = run(["init", str(self.ws)], self.env)
        self.assertNotEqual(r.returncode, 0)

    def test_no_workspace_is_a_clear_error(self):
        r = run(["status"], self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mother init", r.stderr)

    # -- scan
    def fake_token(self):
        return "gh" + "p_" + "A" * 30  # built at runtime so this file never matches the scanner

    def test_scan_flags_token_and_passes_clean(self):
        self.init()
        bad = self.ws / "memory" / "leak.md"
        bad.write_text(f"token {self.fake_token()}\n")
        r = run(["scan", str(bad)], self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SECRET-SCAN", r.stderr)
        good = self.ws / "memory" / "ok.md"
        good.write_text("nothing to see\n")
        self.assertEqual(run(["scan", str(good)], self.env).returncode, 0)

    # -- sync
    def test_sync_local_only_commits(self):
        self.init()
        r = run(["sync"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("chore(sync)", git(self.ws, "log", "--oneline", "-1").stdout)
        self.assertTrue((self.ws / ".mother/last-sync").is_file())
        self.assertNotIn(".mother", git(self.ws, "ls-files").stdout)

    def test_sync_pushes_to_remote_and_second_machine_receives(self):
        remote = self.t / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        self.init("--remote", str(remote))
        self.assertEqual(run(["sync"], self.env).returncode, 0)
        self.assertIn("chore(sync)", git(remote, "log", "--oneline", "-1").stdout)
        # machine B joins, edits, syncs; machine A edits, syncs → rebase, both edits land
        env_b = dict(self.env, MOTHER_STATE_DIR=str(self.t / "state-b"), MOTHER_HOSTNAME="laptop-b")
        ws_b = self.t / "ws-b"
        self.assertEqual(run(["join", str(remote), str(ws_b)], env_b).returncode, 0)
        (ws_b / "memory" / "b.md").write_text("from b\n")
        self.assertEqual(run(["sync"], env_b).returncode, 0)
        (self.ws / "memory" / "a.md").write_text("from a\n")
        self.assertEqual(run(["sync"], self.env).returncode, 0)
        files = git(remote, "ls-tree", "-r", "--name-only", "main").stdout
        self.assertIn("memory/a.md", files)
        self.assertIn("memory/b.md", files)

    def test_sync_quarantines_secret_and_commits_nothing(self):
        self.init()
        run(["sync"], self.env)
        before = git(self.ws, "rev-parse", "HEAD").stdout
        (self.ws / "memory" / "leak.md").write_text(f"x {self.fake_token()}\n")
        r = run(["sync"], self.env)
        self.assertEqual(r.returncode, 0)  # hooks must never fail a turn
        self.assertTrue((self.ws / "QUARANTINE.md").exists())
        self.assertEqual(git(self.ws, "rev-parse", "HEAD").stdout, before)
        # still blocked on the next run, even after the leak is gone, until QUARANTINE.md is deleted
        (self.ws / "memory" / "leak.md").unlink()
        run(["sync"], self.env)
        self.assertEqual(git(self.ws, "rev-parse", "HEAD").stdout, before)

    def test_sync_without_workspace_is_silent_success(self):
        r = run(["sync", "--quiet"], self.env)
        self.assertEqual(r.returncode, 0)

    # -- home
    def test_home_set_check_move(self):
        self.init()
        self.write_config()
        self.assertEqual(run(["home", "check"], self.env).returncode, 3)
        self.assertEqual(run(["home", "set"], self.env).returncode, 0)
        self.assertEqual(run(["home", "check"], self.env).returncode, 0)
        env_b = dict(self.env, MOTHER_HOSTNAME="laptop-b")
        self.assertEqual(run(["home", "check"], env_b).returncode, 2)
        self.assertNotEqual(run(["home", "set", "laptop-b"], self.env).returncode, 0)  # set won't silently move
        self.assertEqual(run(["home", "move", "laptop-b"], self.env).returncode, 0)
        self.assertEqual(run(["home", "check"], env_b).returncode, 0)

    # -- status / validate
    def test_status_valid_example(self):
        self.init()
        self.write_config()
        r = run(["status"], self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("config:      valid", r.stdout)

    def test_chat_dm_may_be_a_list(self):
        self.init()
        cfg = self.write_config()
        cfg["manager"]["chat_dm"] = ["U00000000", "D00000000"]
        cfg["manager"]["chat_user"] = "U00000000"
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(run(["status"], self.env).returncode, 0)
        cfg["manager"]["chat_dm"] = []
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(run(["status"], self.env).returncode, 1)

    def test_status_rejects_bad_config(self):
        self.init()
        cfg = self.write_config(bogus=1)
        cfg["teams"][0]["channels"].append({"id": "C9", "mode": "auto", "external": True})
        cfg["routines"]["brief"]["at"] = "9am"
        (self.ws / "config.json").write_text(json.dumps(cfg))
        r = run(["status"], self.env)
        self.assertEqual(r.returncode, 1)
        for frag in ("unknown top-level key 'bogus'", "external channels are capped", "brief.at must be HH:MM"):
            self.assertIn(frag, r.stdout)

    def test_monthly_routine_and_bad_days(self):
        self.init()
        cfg = self.write_config()
        cfg["routines"]["monthly-refresh"] = {"at": "10:00", "days": "monthly:1"}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        out = json.loads(run(["register"], self.env).stdout)
        self.assertIn("0 10 1 * *", [t["cronExpression"] for t in out["tasks"]])
        cfg["routines"]["monthly-refresh"]["days"] = "monthly:31"
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(run(["status"], self.env).returncode, 1)

    # -- register
    def test_register_crons_and_pointers(self):
        self.init()
        self.write_config()
        out = json.loads(run(["register"], self.env).stdout)
        by_id = {t["taskId"]: t for t in out["tasks"]}
        self.assertEqual(by_id["mother-brief"]["cronExpression"], "45 8 * * 1,2,3,4,5")
        self.assertEqual(by_id["mother-reflect"]["cronExpression"], "30 21 * * *")
        self.assertEqual(by_id["mother-meeting-prep-platform"]["cronExpression"], "0 7 * * 1")
        self.assertEqual(by_id["mother-meeting-recap-platform"]["cronExpression"], "0 12 * * 1")
        self.assertEqual(by_id["mother-doctor"]["cronExpression"], "30 8 * * 1")
        for t in out["tasks"]:
            self.assertIn("mother:routine", t["prompt"])
            self.assertIn(str(self.ws.resolve()), t["prompt"])


if __name__ == "__main__":
    unittest.main()
