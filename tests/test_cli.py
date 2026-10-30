"""Tests for bin/mother. Stdlib only: python3 -m unittest tests.test_cli -v"""
import json
import shutil
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
            # The machine clock matches the template's manager.timezone unless a test says otherwise.
            "TZ": "Europe/London",
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

    # -- speak
    def fake_say(self):
        f = self.t / "say"
        f.write_text("#!/bin/sh\n"
                     "out=''; while [ $# -gt 0 ]; do case \"$1\" in -o) out=\"$2\"; shift;; esac; shift; done\n"
                     "echo \"$*\" > /dev/null; printf 'AUDIO' > \"$out\"\n"
                     "echo \"$0 $@\" >> \"" + str(self.t / "say.log") + "\"\n")
        f.chmod(0o755)
        return dict(self.env, MOTHER_SAY_BIN=str(f), HOME=str(self.t / "home"))

    def test_speak_renders_picks_voice_and_prunes(self):
        self.init()
        self.write_config()
        env = self.fake_say()
        r = subprocess.run([sys.executable, str(CLI), "speak", "--name", "brief-x"], input="Good morning, Dana.",
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout)
        self.assertEqual((o["voice"], Path(o["path"]).name, o["bytes"]), ("Samantha", "brief-x.m4a", 5))
        self.assertEqual((self.ws / ".mother/audio/brief-x.txt").read_text().strip(), "Good morning, Dana.")
        self.assertIsNone(o["shared_path"])
        cfg = json.loads((self.ws / "config.json").read_text())
        cfg["routines"]["brief"]["audio"] = {"save_to": str(self.t / "synced")}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        r = subprocess.run([sys.executable, str(CLI), "speak", "--name", "brief-y"], input="Hi.",
                           capture_output=True, text=True, env=env)
        self.assertEqual(json.loads(r.stdout)["shared_path"], str(self.t / "synced" / "brief-y.m4a"))
        self.assertTrue((self.t / "synced" / "brief-y.m4a").exists())
        r = subprocess.run([sys.executable, str(CLI), "speak", "--name", "brief-he"], input="בוקר טוב, דנה.",
                           capture_output=True, text=True, env=env)
        self.assertEqual(json.loads(r.stdout)["voice"], "Carmit")
        for i in range(20):
            subprocess.run([sys.executable, str(CLI), "speak", "--name", "b%02d" % i], input="x",
                           capture_output=True, text=True, env=env)
        self.assertEqual(len(list((self.ws / ".mother/audio").glob("*.m4a"))), 14)

    def test_speak_kokoro_engine_and_fallbacks(self):
        self.init()
        self.write_config()
        env = self.fake_say()
        cfg = json.loads((self.ws / "config.json").read_text())
        cfg["routines"]["brief"]["audio"] = {"engine": "kokoro"}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        speak = lambda text, name: json.loads(subprocess.run(
            [sys.executable, str(CLI), "speak", "--name", name], input=text, capture_output=True, text=True,
            env=env).stdout)
        o = speak("Good morning.", "k1")                                  # not installed -> say, with a note
        self.assertEqual(o["engine"], "say")
        self.assertIn("mother tts setup", o["note"])
        # a fake Kokoro install: model files + a "python" that writes a real (silent) wav
        tts = self.t / "state" / "tts"
        (tts / ".venv/bin").mkdir(parents=True)
        for f in ("kokoro-v1.0.fp16.onnx", "voices-v1.0.bin"):
            (tts / f).write_text("x")
        py = tts / ".venv/bin/python"
        py.write_text("#!" + sys.executable + "\nimport sys, wave\nw = wave.open(sys.argv[-1], 'wb')\n"
                      "w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b'\\0' * 48000)\n"
                      "w.close()\n")
        py.chmod(0o755)
        self.assertTrue(json.loads(run(["tts", "status"], env).stdout)["kokoro"])
        if not shutil.which("afconvert") and not shutil.which("ffmpeg"):
            self.skipTest("no afconvert/ffmpeg to encode the wav")
        o = speak("Good morning.", "k2")
        self.assertEqual((o["engine"], o["voice"], o["note"]), ("kokoro", "af_heart", None))
        self.assertGreater(o["bytes"], 0)
        self.assertFalse((self.ws / ".mother/audio/k2.wav").exists())     # the wav is cleaned up
        o = speak("בוקר טוב", "k3")                                       # Hebrew -> say
        self.assertEqual((o["engine"], o["voice"]), ("say", "Carmit"))
        self.assertIn("no Hebrew", o["note"])

    def test_speak_errors_are_clear(self):
        self.init()
        self.write_config()
        r = subprocess.run([sys.executable, str(CLI), "speak"], input="  ", capture_output=True, text=True,
                           env=self.fake_say())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("empty script", r.stderr)
        r = subprocess.run([sys.executable, str(CLI), "speak"], input="hi", capture_output=True, text=True,
                           env=dict(self.env, MOTHER_SAY_BIN=str(self.t / "nope")))
        self.assertNotEqual(r.returncode, 0)

    def test_brief_audio_config_validated(self):
        self.init()
        cfg = json.loads((ROOT / "templates/workspace/config.example.json").read_text())
        cfg["routines"]["brief"]["audio"] = {"voice": "Samantha", "language": "fr", "rate": 900}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        r = run(["status"], self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("routines.brief.audio", r.stdout + r.stderr)
        cfg["routines"]["brief"]["audio"] = {"engine": "elevenlabs"}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertNotEqual(run(["status"], self.env).returncode, 0)
        cfg["routines"]["brief"]["audio"] = {"language": "he", "rate": 180, "engine": "kokoro",
                                             "kokoro_voice": "bf_emma"}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(run(["status"], self.env).returncode, 0)

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

    def _tick_sync_setup(self):
        remote = self.t / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        self.init("--remote", str(remote))
        self.assertEqual(run(["sync"], self.env).returncode, 0)
        return remote

    def test_quiet_sync_skips_commit_and_push_for_tick_runs_lines_alone(self):
        remote = self._tick_sync_setup()
        head, pushed = git(self.ws, "rev-parse", "HEAD").stdout, git(remote, "rev-parse", "main").stdout
        last_sync = (self.ws / ".mother/last-sync").read_text()
        runs = self.ws / "runs.md"
        with runs.open("a") as f:
            f.write("- 2026-10-27T09:05:00Z tick ok — 1 on you (1 new), 0 neglected, 0 suggestions; couldn't check: none · slow\n")
            f.write("- 2026-10-27T10:05:00Z tick partial — 1 on you (0 new), 0 neglected, 0 suggestions; couldn't check: tracker · fast\n")
        r = run(["sync", "--quiet"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(git(self.ws, "rev-parse", "HEAD").stdout, head)
        self.assertEqual(git(remote, "rev-parse", "main").stdout, pushed)
        self.assertIn("M runs.md", git(self.ws, "status", "--porcelain").stdout)
        self.assertEqual((self.ws / ".mother/last-sync").read_text(), last_sync)
        # an explicit sync is unchanged: it commits and pushes the tick lines
        self.assertEqual(run(["sync"], self.env).returncode, 0)
        self.assertNotEqual(git(self.ws, "rev-parse", "HEAD").stdout, head)
        self.assertIn("tick ok", git(remote, "show", "main:runs.md").stdout)

    def test_quiet_sync_commits_when_anything_but_tick_lines_changed(self):
        runs = self.ws / "runs.md"

        def append(text):
            with runs.open("a") as f:
                f.write(text)
        cases = [
            ("brief line", lambda: append("- 2026-10-27T07:00:00Z brief ok — x\n")),
            ("other file", lambda: (append("- 2026-10-27T09:05:00Z tick ok — x · fast\n"),
                                    (self.ws / "memory" / "n.md").write_text("note\n"))),
            ("edited line", lambda: runs.write_text("- 2026-10-27T09:05:00Z tick ok — x · fast\n")),
        ]
        remote = self._tick_sync_setup()
        append("- 2026-10-26T07:00:00Z brief ok — seed\n")
        self.assertEqual(run(["sync"], self.env).returncode, 0)
        for name, change in cases:
            head = git(self.ws, "rev-parse", "HEAD").stdout
            change()
            self.assertEqual(run(["sync", "--quiet"], self.env).returncode, 0, name)
            self.assertNotEqual(git(self.ws, "rev-parse", "HEAD").stdout, head, name)
            self.assertEqual(git(self.ws, "status", "--porcelain").stdout.strip(), "", name)
            self.assertEqual(git(remote, "rev-parse", "main").stdout, git(self.ws, "rev-parse", "HEAD").stdout, name)

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

    def test_bypass_passes_asks_validated(self):
        self.init()
        cfg = self.write_config()
        for val, ok in ((True, True), (False, True), ("yes", False), (1, False)):
            cfg["policy"]["bypass_passes_asks"] = val
            (self.ws / "config.json").write_text(json.dumps(cfg))
            r = run(["status"], self.env)
            self.assertEqual(r.returncode, 0 if ok else 1, r.stdout)
            if not ok:
                self.assertIn("policy.bypass_passes_asks must be true or false", r.stdout)

    # -- grant (red-team fix 1: the only writer of .mother/grants.json)
    def grant(self, *args):
        return run(["grant", *args], self.env)

    def test_grant_add_writes_what_the_guard_honours(self):
        import importlib.util
        import time as _t
        self.init()
        cfg = self.write_config()
        ts = "%.6f" % (_t.time() - 60)
        r = self.grant("add", "tracker-create", "PLAT", "--by", "U00000000", "--cites", ts, "--uses", "9",
                       "--run", "digest")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads((self.ws / ".mother" / "grants.json").read_text())
        g = doc["grants"][0]
        key = Path(self.env["MOTHER_STATE_DIR"]) / "grant.key"
        self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")  # made on first use, outside the workspace
        self.assertTrue(isinstance(doc.get("sig"), str) and len(doc["sig"]) == 64)
        self.assertEqual((g["scope"], g["target"], g["uses"], g["run"], g["source"]),
                         ("tracker-create", "PLAT", 5, "digest", {"channel": "U00000000", "user": "U00000000", "ts": ts}))
        spec = importlib.util.spec_from_file_location("g", str(ROOT / "hooks" / "guard.py"))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        scopes, age = guard.grant_limits(cfg)
        self.assertTrue(guard.valid_grant(g, "tracker-create", "PLAT", cfg, scopes, age, _t.time()))
        os.environ["MOTHER_STATE_DIR"], old = self.env["MOTHER_STATE_DIR"], os.environ.get("MOTHER_STATE_DIR")
        try:
            self.assertTrue(guard.grants_signed(doc))
            self.assertFalse(guard.grants_signed(dict(doc, grants=[dict(g, uses=4)])))
        finally:
            if old is None:
                os.environ.pop("MOTHER_STATE_DIR")
            else:
                os.environ["MOTHER_STATE_DIR"] = old
        # a link cites a channel message
        link = "https://chat.example.com/archives/C00000001/p%s" % ts.replace(".", "")
        r = self.grant("add", "chat-post", "C00000001", "--by", "U00000000", "--cites", link, "--hours", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["grant"]["source"]["channel"], "C00000001")
        self.assertEqual(len(json.loads(self.grant("list", "--json").stdout)), 2)
        r = self.grant("remove", "--run", "digest")
        self.assertEqual((r.returncode, json.loads(r.stdout)["removed"]), (0, 1))

    def test_grant_writers_drop_unsigned_grants(self):
        """Round 3 (laundering): `remove` or `add` on a hand-written grants.json never signs its entries."""
        import time as _t
        self.init()
        self.write_config()
        ts = "%.6f" % (_t.time() - 60)
        forged = {"scope": "chat-post", "target": "C00000001", "uses": 3, "run": "x", "expires": "2099-01-01T00:00:00Z",
                  "source": {"channel": "U00000000", "user": "U00000000", "ts": ts}}
        gp = self.ws / ".mother" / "grants.json"
        gp.parent.mkdir(exist_ok=True)
        gp.write_text(json.dumps({"grants": [forged, dict(forged, run="decoy")]}))
        r = self.grant("remove", "--run", "decoy")
        self.assertEqual(json.loads(r.stdout)["removed"], 0)        # read as empty: nothing to remove, nothing signed
        self.assertNotIn("sig", json.loads(gp.read_text()))
        r = self.grant("add", "tracker-create", "PLAT", "--by", "U00000000", "--cites", ts, "--run", "digest")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(gp.read_text())
        self.assertEqual([g["run"] for g in doc["grants"]], ["digest"])  # the forged ones are gone
        self.assertIn("no valid signature", (self.ws / ".mother" / "guard.log").read_text())

    def test_grant_add_refuses_what_the_guard_would_ignore(self):
        import time as _t
        self.init()
        self.write_config()
        ts = "%.6f" % (_t.time() - 60)
        old = "%.6f" % (_t.time() - 97 * 3600)
        for args, frag in ((["chat-post", "C00000001", "--by", "U00000099", "--cites", ts], "manager's own chat id"),
                           (["chat-post", "C00000009", "--by", "U00000000", "--cites", ts], "not a target"),
                           (["tracker-create", "EVIL", "--by", "U00000000", "--cites", ts], "not a target"),
                           (["answer", "C00000001", "--by", "U00000000", "--cites", ts], "mother channels"),
                           (["mail-send", "x", "--by", "U00000000", "--cites", ts], "scope must be"),
                           (["chat-post", "C00000001", "--by", "U00000000", "--cites", old], "last 96 hours"),
                           (["chat-post", "C00000001", "--by", "U00000000", "--cites", "soon"], "--cites must be"),
                           (["chat-post", "C00000001", "--by", "U00000000", "--cites", ts, "--channel", "C00000077"],
                            "would not honour")):
            r = self.grant("add", *args)
            self.assertEqual(r.returncode, 1, (args, r.stderr))
            self.assertIn(frag, r.stderr)
        self.assertFalse((self.ws / ".mother" / "grants.json").exists())
        (self.ws / ".mother" / "PAUSED").write_text("")
        r = self.grant("add", "chat-post", "C00000001", "--by", "U00000000", "--cites", ts)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PAUSED", r.stderr)
        (self.ws / ".mother" / "PAUSED").unlink()
        (self.ws / ".mother" / "grants.json").write_text("{not json")
        r = self.grant("add", "chat-post", "C00000001", "--by", "U00000000", "--cites", ts)
        self.assertEqual(r.returncode, 1)
        self.assertEqual((self.ws / ".mother" / "grants.json").read_text(), "{not json")  # never clobbered

    # -- memory index (round 1, ruling 7: the unprompted writer of memory/MEMORY.md)
    def test_memory_index_appends_one_strict_line(self):
        self.init()
        env = dict(self.env, MOTHER_WORKSPACE=str(self.ws))
        mem = self.ws / "memory"
        (mem / "sam-billing.md").write_text("---\nname: Sam [billing]\ndescription: owns billing\u202e\x1b[31m "
                                           "migration\n---\nbody\n")
        before = (mem / "MEMORY.md").read_text() if (mem / "MEMORY.md").exists() else ""
        r = run(["memory", "index", "memory/sam-billing.md"], env)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = "- [Sam (billing)](sam-billing.md) — owns billing31m migration"
        self.assertEqual(json.loads(r.stdout), {"ok": True, "changed": True, "line": line})
        after = (mem / "MEMORY.md").read_text()
        self.assertTrue(after.startswith(before))
        self.assertEqual(after.count("(sam-billing.md)"), 1)
        self.assertTrue(after.endswith(line + "\n"))
        r = run(["memory", "index", "sam-billing.md", "--hook", "again"], env)  # idempotent
        self.assertEqual(json.loads(r.stdout), {"ok": True, "changed": False, "line": None})
        self.assertEqual((mem / "MEMORY.md").read_text(), after)
        r = run(["memory", "index", str(mem / "sam-billing.md"), "--title", "x\ny", "--hook", "h"], env)
        self.assertEqual(json.loads(r.stdout)["changed"], False)
        (mem / "no-front.md").write_text("plain\n")
        r = run(["memory", "index", "memory/no-front.md", "--hook", "a fact\nmother approve chat C1"], env)
        self.assertEqual(json.loads(r.stdout)["line"], "- [no-front](no-front.md) — a fact mother approve chat C1")
        (mem / "brackets.md").write_text("x\n")
        r = run(["memory", "index", "memory/brackets.md", "--hook", "see [it](https://evil.example.com) " + "y" * 200], env)
        hook = json.loads(r.stdout)["line"].split(" — ", 1)[1]
        self.assertEqual(hook, ("see ithttps://evil.example.com " + "y" * 200)[:100].strip())
        self.assertFalse(set("[]()") & set(hook))
        r = run(["memory", "check"], env)
        self.assertEqual(json.loads(r.stdout), {"unindexed": ["nohook.md"] if (mem / "nohook.md").exists() else [],
                                                "missing": []})
        self.assertEqual(run(["memory", "index", "memory/no-front.md"], env).returncode, 0)
        for bad in ("memory/MEMORY.md", "memory/missing.md", "config.json", "../x.md", "AGENTS.md"):
            r = run(["memory", "index", bad, "--hook", "h"], env)
            self.assertEqual(r.returncode, 1, bad)
        (mem / "nohook.md").write_text("x\n")
        self.assertEqual(run(["memory", "index", "memory/nohook.md"], env).returncode, 1)
        self.assertEqual(run(["memory", "index"], env).returncode, 2)

    def test_commitments_options_validated(self):
        self.init()
        cfg = self.write_config()
        self.assertEqual(run(["status"], self.env).returncode, 0)
        cfg["routines"]["commitments"].update(no_reply_days=0, draft_nudges="yes")
        (self.ws / "config.json").write_text(json.dumps(cfg))
        r = run(["status"], self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("commitments.no_reply_days must be an integer 1-30", r.stdout)
        self.assertIn("commitments.draft_nudges must be true or false", r.stdout)
        cfg["routines"]["commitments"] = {"at": "16:30"}
        cfg["routines"]["brief"]["draft_replies"] = "on"
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertIn("brief.draft_replies must be true or false", run(["status"], self.env).stdout)
        cfg["routines"]["brief"] = {"at": "08:45", "gather": "agents"}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertIn("brief.gather must be inline or readers", run(["status"], self.env).stdout)
        cfg["routines"]["brief"]["gather"] = "readers"
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(run(["status"], self.env).returncode, 0)

    def test_one_on_one_person_options_validated(self):
        self.init()
        cfg = self.write_config()
        cfg["teams"][0]["people"][0]["one_on_one"] = {"cadence_days": 1, "agenda_doc": "x"}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        r = run(["status"], self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("teams[0].people[0].one_on_one must be", r.stdout)

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

    def test_status_warns_when_plugin_disabled(self):
        self.init()
        self.write_config()
        cdir = self.t / "claude"
        cdir.mkdir()
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(cdir))
        (cdir / "settings.json").write_text(json.dumps({"enabledPlugins": {"mother@mother": False}}))
        r = run(["status"], env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("plugin:      DISABLED", r.stdout)
        (cdir / "settings.json").write_text(json.dumps({"enabledPlugins": {"mother@mother": True}}))
        self.assertIn("plugin:      enabled", run(["status"], env).stdout)

    # -- status: connector health
    def write_caps(self, caps):
        (self.ws / "capabilities.json").write_text(json.dumps(caps))

    def ent(self, status, cls=None):
        return {"status": status, "class": cls, "checked_at": "2026-10-25T06:00:00Z"}

    def test_status_health_open_and_covered(self):
        self.init()
        self.write_config()
        C, W, M = "mcp__cal_c", "mcp__cal_w", "mcp__mail_m"
        self.write_caps({"checked_at": "2026-10-25T06:00:00Z", "connectors": {
            "google": {"servers": {"read": [C, W]},
                       "health": {C: {"read": self.ent("expired", "invalidated")}, W: {"read": self.ent("ok")}}},
            "gmail": {"servers": {"read": [M]},
                      "health": {M: {"read": self.ent("error:unreachable", "unreachable")}}}}})
        r = run(["status"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("health:      1 open \u2014 mail.read", r.stdout)
        self.assertIn("(+1 covered by fallback)", r.stdout)

    def test_status_health_ok(self):
        self.init()
        self.write_config()
        M = "mcp__mail_m"
        self.write_caps({"checked_at": "2026-10-25T06:00:00Z", "connectors": {
            "gmail": {"servers": {"read": [M]}, "health": {M: {"_gateway": "x", "read": self.ent("ok")}}}}})
        self.assertIn("health:      ok\n", run(["status"], self.env).stdout)

    def test_status_health_untested_is_not_open(self):
        self.init()
        self.write_config()
        S = "mcp__chat_s"
        self.write_caps({"connectors": {"slack": {
            "servers": {"read": [S], "post": [S]},
            "health": {S: {"read": self.ent("ok"), "post": self.ent("untested")}}}}})
        self.assertIn("health:      ok\n", run(["status"], self.env).stdout)

    def test_status_health_caps_list_at_three(self):
        self.init()
        self.write_config()
        self.write_caps({"connectors": {"gmail": {
            "servers": {"read%d" % i: ["mcp__s%d" % i] for i in range(4)},
            "health": {"mcp__s%d" % i: {"read%d" % i: self.ent("denied", "denied")} for i in range(4)}}}})
        out = run(["status"], self.env).stdout
        self.assertIn("health:      4 open \u2014 ", out)
        self.assertIn("\u2026", out)

    def test_status_health_unreadable_does_not_crash(self):
        self.init()
        self.write_config()
        (self.ws / "capabilities.json").write_text("{nope")
        r = run(["status"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("health:      capabilities.json unreadable \u2014 run /mother:doctor", r.stdout)

    def test_example_config_doctor_daily(self):
        cfg = json.loads((ROOT / "templates/workspace/config.example.json").read_text())
        self.assertEqual(cfg["routines"]["doctor"], {"at": "07:00", "days": "all"})

    # -- runs (watchdog)
    def test_runs_flags_missed_and_failed_routines(self):
        self.init()
        self.write_config()
        # Sun 2026-10-25 12:00 UTC: brief/digest last due Fri, reflect Sat 21:30, doctor Mon 2026-10-19, dots Fri
        env = dict(self.env, TZ="UTC", MOTHER_NOW="2026-10-25T12:00:00Z")
        (self.ws / "runs.md").write_text(
            "- 2026-10-23T08:46:02Z brief ok — 4 sources, DM sent\n"
            "- 2026-10-23T09:01:00Z digest failed — DM post expired\n"
            "- 2026-10-24T21:31:00Z reflect partial — chat search missing_scope\n"
            "- 2026-10-19T08:31:00Z doctor ok — 5 connectors\n"
            "- 2026-10-25T07:01:00Z doctor ok — 5 connectors\n"
            "- 2026-10-19T08:00:00Z team-weekly ok — not a configured routine\n"
            "not a run line\n")
        r = run(["runs", "--json"], env)
        self.assertEqual(r.returncode, 2, r.stderr)
        state = {row["id"]: row["state"] for row in json.loads(r.stdout)}
        self.assertEqual(state, {"brief": "ok", "digest": "failed", "dots": "missed", "one-on-one": "missed", "commitments": "missed", "reflect": "partial", "doctor": "ok", "tick": "pending"})
        r = run(["status"], env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("routines:    digest failed; dots missed; one-on-one missed; commitments missed", r.stdout)
        # just after a slot, the previous slot is judged: reflect is due at 21:30 but has 2h grace
        with open(self.ws / "runs.md", "a") as f:
            f.write("- 2026-10-23T13:02Z dots ok — 3 links\n- 2026-10-23T09:30:00Z digest ok — rerun\n"
                    "- 2026-10-23T07:31:00Z one-on-one ok — 1 prepped, 0 drifting; DM sent\n"
                    "- 2026-10-23T16:31:00Z commitments ok — 2 made, 1 owed, 0 no-reply; DM sent\n")
        env["MOTHER_NOW"] = "2026-10-25T22:00:00Z"
        r = run(["runs"], env)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("all scheduled runs logged", run(["status"], env).stdout)
        # a slot from before logging began (the earliest line, 19 Oct) is not a miss; one after it is
        cfg = json.loads((self.ws / "config.json").read_text())
        cfg["routines"]["monthly-refresh"] = {"at": "10:00", "days": "monthly:1"}
        cfg["routines"]["weekly-snapshot"] = {"at": "16:00", "days": "Thu"}
        (self.ws / "config.json").write_text(json.dumps(cfg))
        rows = {row["id"]: row["state"] for row in json.loads(run(["runs", "--json"], env).stdout)}
        self.assertEqual((rows["monthly-refresh"], rows["weekly-snapshot"]), ("pending", "missed"))

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
        self.assertEqual(by_id["mother-doctor"]["cronExpression"], "0 7 * * *")
        self.assertEqual(by_id["mother-commitments"]["cronExpression"], "30 16 * * 1,2,3,4,5")
        for t in out["tasks"]:
            self.assertIn("mother:routine", t["prompt"])
            self.assertIn(str(self.ws.resolve()), t["prompt"])

    # -- tick (interval routine)
    def test_tick_config_validates(self):
        self.init()
        cfg = self.write_config()
        self.assertEqual(cfg["routines"]["tick"], {"every": 60, "active_hours": "06:00-22:00", "days": "workdays",
                                                  "notify": "none", "slow_every": 3})
        self.assertEqual(run(["status"], self.env).returncode, 0)

        def status_with(tick):
            c = json.loads((ROOT / "templates/workspace/config.example.json").read_text())
            c["routines"]["tick"] = tick
            (self.ws / "config.json").write_text(json.dumps(c))
            return run(["status"], self.env)
        good = {"every": 30, "active_hours": "07:00-19:30", "days": "all", "thresholds": {"review_requested": "2h", "invite_unanswered": 0},
                "signals": {"mail_needs_reply": False}, "notify": "dm", "slow_every": 3, "max_age_days": 0}
        self.assertEqual(status_with(good).returncode, 0)
        for bad, msg in (({"every": 45}, "routines.tick.every must be one of 30, 60, 120, 180, 240"),
                         ({"active_hours": "22:00-06:00"}, "routines.tick.active_hours must be HH:MM-HH:MM with start before end"),
                         ({"thresholds": {"nope": "2h"}}, "routines.tick.thresholds.nope is not a known signal"),
                         ({"thresholds": {"review_requested": "soon"}}, "routines.tick.thresholds.review_requested must be Nh, Nd or 0"),
                         ({"signals": {"mail_needs_reply": "yes"}}, "routines.tick.signals.mail_needs_reply must be true or false"),
                         ({"every": 60.0}, "routines.tick.every must be one of 30, 60, 120, 180, 240"),
                         ({"days": "monthly:1"}, "routines.tick.days must be workdays, all, a day, or a list of days"),
                         ({"notify": "email"}, "routines.tick.notify must be none or dm"),
                         ({"at": "09:00"}, "routines.tick.at is not a known key"),
                         ({"slow_every": 7}, "routines.tick.slow_every must be an integer 1-6"),
                         ({"max_age_days": "30"}, "routines.tick.max_age_days must be an integer 0-365")):
            r = status_with(bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn(msg, r.stdout + r.stderr)

    def test_register_tick_interval_cron_and_no_pull(self):
        self.init()
        self.write_config()
        by_id = {t["taskId"]: t for t in json.loads(run(["register"], self.env).stdout)["tasks"]}
        t = by_id["mother-tick"]
        self.assertEqual(t["title"], "Mother: tick (every 60 min, 06:00-22:00)")
        self.assertEqual(t["cronExpression"], "5 6-21 * * 1,2,3,4,5")
        self.assertIn("mother:routine", t["prompt"])
        self.assertIn("`tick`", t["prompt"])
        self.assertNotIn("git pull", t["prompt"])
        self.assertIn("git pull --rebase --autostash", by_id["mother-brief"]["prompt"])
        cfg = json.loads((self.ws / "config.json").read_text())
        cfg["routines"]["tick"]["every"] = 120
        (self.ws / "config.json").write_text(json.dumps(cfg))
        by_id = {t["taskId"]: t for t in json.loads(run(["register"], self.env).stdout)["tasks"]}
        self.assertEqual(by_id["mother-tick"]["cronExpression"], "5 6,8,10,12,14,16,18,20 * * 1,2,3,4,5")
        cfg["routines"]["tick"]["every"] = 30
        (self.ws / "config.json").write_text(json.dumps(cfg))
        by_id = {t["taskId"]: t for t in json.loads(run(["register"], self.env).stdout)["tasks"]}
        self.assertEqual(by_id["mother-tick"]["cronExpression"], "5,35 6-21 * * 1,2,3,4,5")

    def test_register_tick_converts_manager_hours_to_machine_time(self):
        self.init()
        cfg = self.write_config()
        pin = {"MOTHER_NOW": "2026-10-27T09:00:00Z"}   # London is on GMT; Kolkata is +05:30 all year

        def cron(tz, tick):
            cfg["routines"]["tick"] = dict(cfg["routines"]["tick"], **tick)
            (self.ws / "config.json").write_text(json.dumps(cfg))
            r = run(["register"], dict(self.env, TZ=tz, **pin))
            return r, (json.loads(r.stdout)["tasks"] if r.returncode == 0 else None)
        for tz, tick, want in (("UTC", {"active_hours": "06:00-22:00"}, "5 6-21 * * 1,2,3,4,5"),
                               ("Asia/Kolkata", {"active_hours": "06:00-18:00"}, "35 11-22 * * 1,2,3,4,5"),
                               ("Asia/Kolkata", {"active_hours": "06:00-18:00", "every": 30}, "5,35 11-23 * * 1,2,3,4,5"),
                               ("America/New_York", {"active_hours": "06:00-22:00", "every": 120}, "5 2,4,6,8,10,12,14,16 * * 1,2,3,4,5")):
            r, tasks = cron(tz, tick)
            self.assertEqual(r.returncode, 0, (tz, tick, r.stderr))
            self.assertEqual({t["taskId"]: t for t in tasks}["mother-tick"]["cronExpression"], want, (tz, tick))

    def test_tick_crossing_machine_midnight_fails_tick_only(self):
        self.init()
        self.write_config()                                  # 06:00-22:00 London: 11:30-03:30 on a +05:30 machine
        env = dict(self.env, TZ="Asia/Kolkata", MOTHER_NOW="2026-10-27T09:00:00Z")
        msg = "routines.tick.active_hours crosses midnight in this machine's time zone"
        r = run(["register"], env)
        self.assertEqual(r.returncode, 2)
        self.assertIn(msg, r.stderr)
        out = json.loads(r.stdout)
        ids = {t["taskId"] for t in out["tasks"]}
        self.assertNotIn("mother-tick", ids)
        self.assertIn("mother-brief", ids)                   # the other routines still register
        self.assertIn(msg, out["errors"][0])
        r = run(["status"], env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("config:      valid", r.stdout)
        self.assertIn("tick:        WARNING — " + msg, r.stdout)
        self.assertIn("tick failed", r.stdout)
        r = run(["runs", "--json"], env)
        self.assertEqual(r.returncode, 2)
        row = [x for x in json.loads(r.stdout) if x["id"] == "tick"][0]
        self.assertEqual(row["state"], "failed")
        self.assertIn(msg, row["note"])

    def test_runs_tick_reports_an_offset_change_since_register(self):
        self.init()
        self.write_config()
        env = dict(self.env, TZ="UTC")
        r = run(["register"], dict(env, MOTHER_NOW="2026-10-20T09:00:00Z"))   # London on BST: offset -60
        self.assertEqual(r.returncode, 0, r.stderr)
        tick = {t["taskId"]: t for t in json.loads(r.stdout)["tasks"]}["mother-tick"]
        self.assertEqual((tick["offset_minutes"], tick["cronExpression"]), (-60, "5 5-20 * * 1,2,3,4,5"))
        (self.ws / "runs.md").write_text("- 2026-10-21T09:05:00Z tick ok — x\n- 2026-10-27T09:05:00Z tick ok — x\n")

        def row(now):
            out = run(["runs", "--json"], dict(env, MOTHER_NOW=now))
            return [x for x in json.loads(out.stdout) if x["id"] == "tick"][0]
        self.assertEqual(row("2026-10-21T09:30:00Z")["state"], "ok")              # same offset: judged as usual
        after = row("2026-10-27T09:30:00Z")                                       # London back on GMT: offset 0
        self.assertEqual((after["state"], after["note"]), ("failed", "re-run mother register (time-zone offset changed)"))
        r = run(["register"], dict(env, MOTHER_NOW="2026-10-27T09:00:00Z"))
        self.assertEqual(row("2026-10-27T09:30:00Z")["state"], "ok")

    def test_runs_tick_judged_in_manager_timezone(self):
        self.init()
        cfg = self.write_config()                        # manager in Europe/London, GMT on these dates
        cfg["routines"]["tick"]["active_hours"] = "06:00-18:00"   # 11:30-23:30 on the machine: no midnight
        (self.ws / "config.json").write_text(json.dumps(cfg))
        env = dict(self.env, TZ="Asia/Kolkata")          # the machine is 5:30 ahead

        def row(now, lines):
            (self.ws / "runs.md").write_text(lines)
            r = run(["runs", "--json"], dict(env, MOTHER_NOW=now))
            return {x["id"]: x for x in json.loads(r.stdout)}["tick"]
        r = row("2026-10-26T10:30:00Z", "- 2026-10-26T09:05:00Z tick ok — 0 new\n")
        self.assertEqual((r["state"], r["expected"]), ("ok", "2026-10-26T10:05Z"))
        r = row("2026-10-26T10:30:00Z", "- 2026-10-26T08:05:00Z tick ok — 0 new\n")
        self.assertEqual(r["state"], "missed")
        r = row("2026-10-26T17:30:00Z", "- 2026-10-26T17:05:00Z tick ok — 0 new\n")   # 23:00 on the machine
        self.assertEqual((r["state"], r["expected"]), ("ok", "2026-10-26T17:05Z"))
        r = row("2026-10-26T05:30:00Z", "- 2026-10-23T21:05:00Z tick ok\n")          # 11:00 on the machine, 05:30 London
        self.assertEqual((r["state"], r["expected"]), ("ok", None))

    def test_runs_tick_invalid_config_does_not_crash(self):
        self.init()
        cfg = self.write_config()
        for bad in ({"active_hours": "garbage"}, {"every": 60.0}, {"days": "monthly:1"}):
            cfg["routines"]["tick"] = dict(bad)
            (self.ws / "config.json").write_text(json.dumps(cfg))
            r = run(["runs", "--json"], dict(self.env, TZ="UTC", MOTHER_NOW="2026-10-26T10:30:00Z"))
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            row = [x for x in json.loads(r.stdout) if x["id"] == "tick"][0]
            self.assertEqual((row["state"], row["note"]), ("failed", "invalid tick config — run mother status"))

    def test_runs_tick_missed_and_ok(self):
        self.init()
        self.write_config()
        env = dict(self.env, TZ="UTC")

        def tick_state(now, lines):
            (self.ws / "runs.md").write_text(lines)
            r = run(["runs", "--json"], dict(env, MOTHER_NOW=now))
            return {row["id"]: row["state"] for row in json.loads(r.stdout)}["tick"]
        # Mon 2026-10-26
        self.assertEqual(tick_state("2026-10-26T10:30:00Z", "- 2026-10-26T09:05:00Z tick ok — 0 new\n"), "ok")
        self.assertEqual(tick_state("2026-10-26T10:30:00Z", "- 2026-10-26T08:05:00Z tick ok — 0 new\n"), "missed")
        self.assertEqual(tick_state("2026-10-26T23:00:00Z", "- 2026-10-26T08:05:00Z tick ok — 0 new\n"), "ok")
        self.assertEqual(tick_state("2026-10-26T23:00:00Z", ""), "pending")
        self.assertEqual(tick_state("2026-10-26T23:00:00Z", "- 2026-10-26T21:05:00Z tick failed — x\n"), "failed")
        self.assertEqual(tick_state("2026-10-26T06:30:00Z", "- 2026-10-23T21:05:00Z tick ok\n"), "ok")
        self.assertEqual(tick_state("2026-10-25T10:30:00Z", "- 2026-10-23T08:05:00Z tick ok\n"), "ok")

    def test_routine_frontmatter_tick(self):
        text = (Path(__file__).resolve().parent.parent / "routines" / "tick.md").read_text()
        ls = text.splitlines()
        idx = [i for i, l in enumerate(ls) if l == "---"]
        fm = "\n".join(ls[idx[0] + 1:idx[1]])
        lines = {l.split(":", 1)[0]: l.split(":", 1)[1].strip() for l in fm.splitlines() if ":" in l and not l.startswith(" ")}
        self.assertEqual(lines["id"], "tick")
        self.assertEqual(lines["sync"].split("#")[0].strip(), "false")
        self.assertEqual(lines["team_scoped"].split("#")[0].strip(), "false")
        for cap in ("chat.read", "tracker.read", "mail.read", "calendar.read", "code.read"):
            self.assertIn(cap, lines["needs"])
        self.assertIn('active_hours: "06:00-22:00"', lines["default"])
        self.assertIn(".mother/now.json", lines["writes"])
        self.assertIn("runs.md", lines["writes"])
        self.assertIn("manager.chat_dm", lines["sends"])
        self.assertIn("slow_every", text)
        self.assertEqual(lines["config"].split("#")[0].strip(), "routines.tick")
        self.assertNotIn("journal/digest/", text)                      # the queue lives in intake.md
        self.assertIn("teams/<slug>/intake.md", text)
        self.assertIn("**containing** ` · slow`", text)
        self.assertIn("latest `journal/dots/` file", text)
        disp = (Path(__file__).resolve().parent.parent / "skills" / "routine" / "SKILL.md").read_text()
        self.assertIn("sync: false", disp)
        self.assertIn("skipped — outside active hours", disp)


class HubHelp(unittest.TestCase):
    def test_hub_help(self):
        env = dict(os.environ, MOTHER_STATE_DIR=tempfile.mkdtemp())
        env.pop("MOTHER_WORKSPACE", None)
        r = run(["hub", "help"], env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("install", r.stdout)


class TestWhatsappConfig(unittest.TestCase):
    # Reuse CliTest's fixtures without inheriting (and re-running) its tests.
    setUp, tearDown, init, write_config = CliTest.setUp, CliTest.tearDown, CliTest.init, CliTest.write_config

    BLOCK = {
        "enabled": True,
        "self_jid": "972500000099@s.whatsapp.net",
        "max_per_day": 20,
        "min_gap_seconds": 5,
        "read": True,
        "send": True,
    }

    def status_with(self, mutate):
        self.init()
        block = json.loads(json.dumps(self.BLOCK))
        mutate(block)
        self.write_config(whatsapp=block)
        return run(["status"], self.env)

    def test_valid_block_passes_status(self):
        r = self.status_with(lambda b: None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_example_config_ships_a_disabled_block(self):
        wa = json.loads((ROOT / "templates/workspace/config.example.json").read_text())["whatsapp"]
        self.assertIs(wa["enabled"], False)
        self.assertEqual(set(wa), {"enabled", "self_jid", "max_per_day", "min_gap_seconds", "read", "send"})
        self.init()
        self.write_config()
        self.assertEqual(run(["status"], self.env).returncode, 0)

    def test_the_earlier_designs_keys_are_accepted_and_ignored(self):
        """The manager's live config still has allow and approve_via: any value passes."""
        for legacy in ({"allow": [{"jid": "972500000001@s.whatsapp.net", "name": "Dana", "team": "platform"}],
                        "approve_via": ["slack_dm", "self_chat", "session"], "max_prompts_per_day": 10},
                       {"allow": 5, "approve_via": [], "max_prompts_per_day": "lots"}):
            r = self.status_with(lambda b: b.update(legacy))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_unknown_key_rejected(self):
        r = self.status_with(lambda b: b.update(foo=1))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.foo", r.stdout)

    def test_max_per_day_above_ceiling_rejected(self):
        r = self.status_with(lambda b: b.update(max_per_day=51))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.max_per_day", r.stdout)

    def test_bad_self_jid_rejected(self):
        r = self.status_with(lambda b: b.update(self_jid="12345@g.us"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.self_jid", r.stdout)

    def test_self_jid_required_when_enabled(self):
        r = self.status_with(lambda b: b.pop("self_jid"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.self_jid", r.stdout)

    def test_self_jid_optional_when_disabled(self):
        r = self.status_with(lambda b: (b.pop("self_jid"), b.update(enabled=False)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_other_limits_rejected(self):
        for key, val in (("max_per_day", 0), ("min_gap_seconds", -1), ("read", "yes"), ("send", 1)):
            r = self.status_with(lambda b: b.update({key: val}))
            self.assertEqual(r.returncode, 1, key)
            self.assertIn(f"whatsapp.{key}", r.stdout)


if __name__ == "__main__":
    unittest.main()


class HubConfig(unittest.TestCase):
    setUp, tearDown, init, write_config = CliTest.setUp, CliTest.tearDown, CliTest.init, CliTest.write_config

    def status_with(self, mutate):
        self.init()
        block = json.loads((ROOT / "templates/workspace/config.example.json").read_text())["hub"]
        mutate(block)
        self.write_config(hub=block)
        return run(["status"], self.env)

    def test_hub_absent_is_valid(self):
        self.init()
        cfg = json.loads((ROOT / "templates/workspace/config.example.json").read_text())
        cfg.pop("hub", None)
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(run(["status"], self.env).returncode, 0)

    def test_hub_example_block_is_valid(self):
        hub = json.loads((ROOT / "templates/workspace/config.example.json").read_text())["hub"]
        self.assertIs(hub["enabled"], False)
        r = self.status_with(lambda b: None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_hub_full_block_is_valid(self):
        def full(b):
            b.update(enabled=True, every_seconds=120, idle_every_seconds=0, model=None, max_cards_per_wake=3,
                     sweep_minutes=30, update_ttl_hours=24,
                     connections={"chat": {"command": "example-mcp", "args": ["--x"], "env": {"K": "v"}},
                                  "code": {"cli": "gh"}},
                     watch={"chat": {"channels": ["C00000001"]},
                            "mail": {"ignore_senders": ["noreply@example.com"], "ignore_labels": ["promo"]},
                            "tracker": {"projects": ["PLAT"], "extra_jql": None},
                            "calendar": {"lead_minutes": 15}, "code": {"repos": ["example/repo"]}})
        r = self.status_with(full)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_hub_unknown_key_fails(self):
        r = self.status_with(lambda b: b.update(foo=1))
        self.assertEqual(r.returncode, 1)
        self.assertIn("hub.foo", r.stdout)

    def test_hub_bad_every_fails(self):
        r = self.status_with(lambda b: b.update(every_seconds=5))
        self.assertEqual(r.returncode, 1)
        self.assertIn("hub.every_seconds", r.stdout)

    def test_hub_connection_needs_command_or_cli(self):
        r = self.status_with(lambda b: b.update(connections={"chat": {"args": []}}))
        self.assertEqual(r.returncode, 1)
        self.assertIn("hub.connections.chat", r.stdout)

    def test_hub_unknown_capability_fails(self):
        r = self.status_with(lambda b: b.update(connections={"fax": {"cli": "gh"}}))
        self.assertEqual(r.returncode, 1)
        self.assertIn("hub.connections.fax", r.stdout)

    def test_hub_whatsapp_connection_is_local_only(self):
        r = self.status_with(lambda b: b.update(connections={"whatsapp": {"local": "whatsapp"}},
                                                watch={"whatsapp": {"groups": ["15550000002-1700000000@g.us"]}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for conn in ({"command": "example-mcp"}, {"local": "other"}, {"local": "whatsapp", "command": "x"}, {}):
            r = self.status_with(lambda b: b.update(connections={"whatsapp": conn}))
            self.assertEqual(r.returncode, 1, conn)
            self.assertIn("hub.connections.whatsapp", r.stdout)
        r = self.status_with(lambda b: b.update(connections={"chat": {"local": "whatsapp"}}))
        self.assertEqual(r.returncode, 1)
        self.assertIn("hub.connections.chat", r.stdout)
        r = self.status_with(lambda b: b.update(watch={"whatsapp": {"groups": "x"}}))
        self.assertEqual(r.returncode, 1)
        self.assertIn("hub.watch.whatsapp.groups", r.stdout)

    def test_hub_cards_capped_at_3(self):
        r = self.status_with(lambda b: b.update(max_cards_per_wake=4))
        self.assertEqual(r.returncode, 1)
        self.assertIn("hub.max_cards_per_wake", r.stdout)

    def test_hub_other_ranges_fail(self):
        for key, val in (("idle_every_seconds", 86401), ("sweep_minutes", 9), ("update_ttl_hours", 169),
                         ("enabled", "yes"), ("model", 5)):
            r = self.status_with(lambda b: b.update({key: val}))
            self.assertEqual(r.returncode, 1, key)
            self.assertIn("hub." + key, r.stdout)

    def test_hub_per_source_intervals_are_valid(self):
        watch = {"chat": {"every_seconds": 30}, "mail": {"every_seconds": 3600, "idle_every_seconds": 0},
                 "tracker": {"projects": ["PLAT"], "every_seconds": 600, "idle_every_seconds": 86400},
                 "calendar": {"lead_minutes": 20, "every_seconds": 900}, "code": {"every_seconds": 600},
                 "whatsapp": {"idle_every_seconds": 1800}}
        r = self.status_with(lambda b: b.update(watch=watch))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_hub_bad_per_source_intervals_fail(self):
        for src, key, val in (("mail", "every_seconds", 29), ("mail", "every_seconds", 86401),
                              ("chat", "every_seconds", "10m"), ("code", "every_seconds", True),
                              ("tracker", "idle_every_seconds", -1), ("calendar", "idle_every_seconds", 86401),
                              ("whatsapp", "idle_every_seconds", 1.5)):
            r = self.status_with(lambda b: b.update(watch={src: {key: val}}))
            self.assertEqual(r.returncode, 1, (src, key, val))
            self.assertIn("hub.watch.%s.%s" % (src, key), r.stdout)

    def test_hub_calendar_interval_longer_than_lead_still_validates(self):
        r = self.status_with(lambda b: b.update(watch={"calendar": {"lead_minutes": 5, "every_seconds": 3600}}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_hub_watch_shape_fails(self):
        for watch in ({"chat": {"channels": "C1"}}, {"calendar": {"lead_minutes": "x"}}, {"nope": {}},
                      {"mail": {"ignore_senders": [1]}}, {"tracker": {"extra_jql": 3}}):
            r = self.status_with(lambda b: b.update(watch=watch))
            self.assertEqual(r.returncode, 1, watch)
            self.assertIn("hub.watch", r.stdout)
