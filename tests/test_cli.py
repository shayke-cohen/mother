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
        self.assertEqual(state, {"brief": "ok", "digest": "failed", "dots": "missed", "one-on-one": "missed", "commitments": "missed", "reflect": "partial", "doctor": "ok"})
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


class TestWhatsappConfig(unittest.TestCase):
    # Reuse CliTest's fixtures without inheriting (and re-running) its tests.
    setUp, tearDown, init, write_config = CliTest.setUp, CliTest.tearDown, CliTest.init, CliTest.write_config

    BLOCK = {
        "enabled": True,
        "self_jid": "972500000099@s.whatsapp.net",
        "allow": [
            {"jid": "972500000001@s.whatsapp.net", "name": "Dana", "team": "platform"},
            {"jid": "120363000000000001@g.us", "name": "PLAT team", "team": "platform", "group": True},
        ],
        "approve_via": ["slack_dm", "self_chat", "session"],
        "max_per_day": 20,
        "min_gap_seconds": 5,
        "max_prompts_per_day": 10,
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
        self.init()
        self.write_config()
        self.assertEqual(run(["status"], self.env).returncode, 0)

    def test_unknown_key_rejected(self):
        r = self.status_with(lambda b: b.update(foo=1))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.foo", r.stdout)

    def test_empty_approve_via_rejected(self):
        r = self.status_with(lambda b: b.update(approve_via=[]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.approve_via", r.stdout)

    def test_unknown_approve_via_rejected(self):
        r = self.status_with(lambda b: b.update(approve_via=["sms"]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.approve_via", r.stdout)

    def test_max_per_day_above_ceiling_rejected(self):
        r = self.status_with(lambda b: b.update(max_per_day=51))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.max_per_day", r.stdout)

    def test_bad_jid_rejected(self):
        r = self.status_with(lambda b: b["allow"][0].update(jid="12345"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("whatsapp.allow[0].jid", r.stdout)

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
        for key, val in (("max_per_day", 0), ("min_gap_seconds", -1), ("max_prompts_per_day", 0), ("read", "yes")):
            r = self.status_with(lambda b: b.update({key: val}))
            self.assertEqual(r.returncode, 1, key)
            self.assertIn(f"whatsapp.{key}", r.stdout)

    def test_allow_not_a_list_rejected_without_crash(self):
        for bad in (5, {}):
            r = self.status_with(lambda b: b.update(allow=bad))
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("whatsapp.allow must be a list", r.stdout)
            self.assertNotIn("Traceback", r.stderr)

    def test_allow_entry_shape_rejected(self):
        r = self.status_with(lambda b: b["allow"][1].update(extra=1))
        self.assertIn("whatsapp.allow[1].extra", r.stdout)
        r = self.status_with(lambda b: b["allow"][0].pop("name"))
        self.assertIn("whatsapp.allow[0].name", r.stdout)


if __name__ == "__main__":
    unittest.main()
