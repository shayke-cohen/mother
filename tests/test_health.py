"""Tests for lib/health.py via `mother health`. Stdlib only: python3 -m unittest tests.test_health -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "mother"
W = "mcp__Willow_Slack__slack"
C = "mcp__claudeai_slack"
NOW = 1_700_000_000
ISO_NOW = "2023-11-14T22:13:20Z"


def iso_ago(hours):
    import datetime as dt
    return dt.datetime.fromtimestamp(NOW - hours * 3600, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        cfg = json.loads((ROOT / "templates/workspace/config.example.json").read_text())
        cfg["connectors"]["chat"] = "slack"
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.env = dict(os.environ)
        self.env.update({"MOTHER_WORKSPACE": str(self.ws), "MOTHER_STATE_DIR": str(Path(self.tmp.name) / "state"),
                         "MOTHER_NOW": str(NOW)})
        self.caps_path = self.ws / "capabilities.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "health", *args], capture_output=True, text=True, env=self.env)

    def write_caps(self, slack):
        self.caps_path.write_text(json.dumps({"checked_at": iso_ago(1), "connectors": {"slack": slack}}))

    def caps(self):
        return json.loads(self.caps_path.read_text())["connectors"]["slack"]

    def two_servers(self, w="ok", c="ok", **extra):
        def ent(s):
            return {"status": s, "class": None if s == "ok" else "unreachable", "checked_at": iso_ago(1)}
        slack = {"server": W, "read": "ok", "servers": {"read": [W, C]},
                 "health": {W: {"read": ent(w)}, C: {"read": ent(c)}}}
        slack.update(extra)
        self.write_caps(slack)

    def pick(self, *args):
        r = self.run_cli("pick", "--kind", "chat", "--cap", "read", *args)
        return r, (json.loads(r.stdout) if r.stdout.strip() else None)

    def test_pick_prefers_first_ok(self):
        self.two_servers()
        r, out = self.pick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["server"], W)
        self.assertIs(out["fallback"], False)

    def test_pick_falls_back_when_preferred_down(self):
        self.two_servers(w="error:unreachable")
        r, out = self.pick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["server"], C)
        self.assertIs(out["fallback"], True)

    def test_pick_other_target_skips_stamping(self):
        self.two_servers(w="error:unreachable", stamps=[C])
        r, out = self.pick("--target", "other")
        self.assertEqual(r.returncode, 3)
        self.assertIsNone(out["server"])

    def test_pick_dm_target_allows_stamping(self):
        self.two_servers(w="error:unreachable", stamps=[C])
        r, out = self.pick("--target", "dm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["server"], C)
        self.assertIs(out["stamped"], True)

    def test_pick_other_target_skips_different_token(self):
        self.two_servers(w="error:unreachable", token={W: "user", C: "bot"})
        r, out = self.pick("--target", "other")
        self.assertEqual(r.returncode, 3)

    def test_pick_fresh_flag(self):
        self.two_servers()
        caps = json.loads(self.caps_path.read_text())
        caps["connectors"]["slack"]["health"][W]["read"]["checked_at"] = iso_ago(7)
        self.caps_path.write_text(json.dumps(caps))
        self.assertIs(self.pick("--max-age-hours", "6")[1]["fresh"], False)
        caps["connectors"]["slack"]["health"][W]["read"]["checked_at"] = iso_ago(5)
        self.caps_path.write_text(json.dumps(caps))
        self.assertIs(self.pick("--max-age-hours", "6")[1]["fresh"], True)

    def test_pick_legacy_file(self):
        self.caps_path.write_text(json.dumps({"checked_at": iso_ago(2), "connectors": {"slack": {
            "server": W, "read": "ok", "search": "ok", "post": "ok",
            "servers": {"search": "mcp__X"}}}}))
        r, out = self.pick()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["server"], W)
        r = self.run_cli("pick", "--kind", "chat", "--cap", "search")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["server"], "mcp__X")

    def test_pick_legacy_string_token(self):
        self.caps_path.write_text(json.dumps({"checked_at": iso_ago(2), "connectors": {"slack": {
            "server": W, "read": "ok", "token": "user", "servers": {"read": "mcp__X"}}}}))
        r, out = self.pick("--target", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out, {"server": "mcp__X", "status": "ok", "fresh": True, "fallback": False,
                               "stamped": False})

    def test_pick_legacy_top_level_servers(self):
        self.caps_path.write_text(json.dumps({"checked_at": iso_ago(2), "servers": {"chat.search": "mcp__X"},
                                              "connectors": {"slack": {"server": W, "read": "ok", "search": "ok"}}}))
        r = self.run_cli("pick", "--kind", "chat", "--cap", "search")
        self.assertEqual(json.loads(r.stdout)["server"], "mcp__X")

    def record(self, cap, server, status, *extra):
        return self.run_cli("record", "--kind", "chat", "--cap", cap, "--server", server, "--status", status, *extra)

    def test_record_updates_rollup_and_keeps_order(self):
        self.two_servers()
        r = self.record("read", W, "error:unreachable", "--class", "unreachable")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"previous": "ok", "rollup": "ok"})
        s = self.caps()
        self.assertEqual(s["servers"]["read"], [W, C])
        self.assertEqual(s["health"][W]["read"]["status"], "error:unreachable")
        self.assertEqual(s["health"][W]["read"]["class"], "unreachable")
        self.assertEqual(s["health"][W]["read"]["checked_at"], ISO_NOW)
        self.assertEqual(s["read"], "ok")

    def test_record_new_server_appended_and_rollup_first_status(self):
        self.two_servers(w="error:unreachable", c="error:unreachable")
        r = self.record("read", "mcp__new", "denied", "--class", "denied")
        self.assertIsNone(json.loads(r.stdout)["previous"])
        s = self.caps()
        self.assertEqual(s["servers"]["read"], [W, C, "mcp__new"])
        self.assertEqual(s["read"], "error:unreachable")

    def test_record_keeps_alert_when_class_unchanged(self):
        self.two_servers(w="error:unreachable")
        caps = json.loads(self.caps_path.read_text())
        caps["connectors"]["slack"]["health"][W]["read"].update(alerted_at=iso_ago(1), pending=True)
        self.caps_path.write_text(json.dumps(caps))
        self.record("read", W, "error:unreachable", "--class", "unreachable")
        e = self.caps()["health"][W]["read"]
        self.assertEqual(e["alerted_at"], iso_ago(1))
        self.assertTrue(e["pending"])
        self.record("read", W, "expired", "--class", "invalidated")
        e = self.caps()["health"][W]["read"]
        self.assertNotIn("alerted_at", e)

    def test_record_clears_alert_on_ok(self):
        self.two_servers(w="error:unreachable")
        caps = json.loads(self.caps_path.read_text())
        caps["connectors"]["slack"]["health"][W]["read"].update(alerted_at=iso_ago(1), pending=True)
        self.caps_path.write_text(json.dumps(caps))
        self.record("read", W, "ok")
        e = self.caps()["health"][W]["read"]
        self.assertEqual(e["status"], "ok")
        self.assertNotIn("alerted_at", e)
        self.assertNotIn("pending", e)

    def test_record_rejects_bad_arguments(self):
        self.two_servers()
        self.assertEqual(self.record("read", W, "weird").returncode, 2)
        self.assertEqual(self.record("read", W, "ok", "--class", "nope").returncode, 2)
        self.assertEqual(self.record("read", W, "ok", "--fixed-by", "magic").returncode, 2)
        self.assertEqual(self.run_cli("record", "--kind", "nokind", "--cap", "read", "--server", W,
                                      "--status", "ok").returncode, 2)

    def test_record_refuses_corrupt_file(self):
        self.caps_path.write_text("{not json")
        r = self.record("read", W, "ok")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.caps_path.read_text(), "{not json")

    def test_record_concurrent_writes_both_land(self):
        self.two_servers()
        caps = [f"cap{i}" for i in range(8)]
        with ThreadPoolExecutor(8) as ex:
            rs = list(ex.map(lambda c: self.record(c, W, "ok"), caps))
        for r in rs:
            self.assertEqual(r.returncode, 0, r.stderr)
        h = self.caps()["health"][W]
        for c in caps:
            self.assertIn(c, h)

    # ---- Task 2: classify, health.md, summary

    def health_md(self):
        return (self.ws / "health.md").read_text()

    def rec(self, status, server=W, cap="read", cls=None, *extra, now=None):
        env = dict(self.env)
        if now:
            env["MOTHER_NOW"] = str(now)
        args = ["record", "--kind", "chat", "--cap", cap, "--server", server, "--status", status]
        if cls:
            args += ["--class", cls]
        r = subprocess.run([sys.executable, str(CLI), "health", *args, *extra], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_classify_table(self):
        rows = [
            ("The user's connection to this connector was invalidated. The user needs to reconnect it from connector settings.",
             ("expired", "invalidated")),
            ("missing_scope: search:read", ("missing_scope:search:read", "missing_scope")),
            ("Insufficient scope: channels:history", ("missing_scope:channels:history", "missing_scope")),
            ("fetch failed: ECONNREFUSED 10.0.0.1:443", ("error:unreachable", "unreachable")),
            ("403 Forbidden", ("denied", "denied")),
            ("not allowed", ("denied", "denied")),
            ("HTTP 401", ("expired", "invalidated")),
            ("MCP error: Connection closed", ("error:dropped", "dropped")),
            ("weird 1234567890abcdef thing", ("error:weird # thing", "other")),
        ]
        sys.path.insert(0, str(ROOT / "lib"))
        import health
        for text, want in rows:
            with self.subTest(text=text):
                self.assertEqual(health.classify(text), want)
        r = self.run_cli("classify", "--error", "403 Forbidden")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"status": "denied", "class": "denied"})

    def test_server_short(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import health
        self.assertEqual(health.server_short(W), "Willow_Slack__slack")
        self.assertEqual(health.server_short("mcp__486bd9bf-5fd4-4917-848c-4f2ddfdfb82e"), "486bd9bf")

    def test_health_line_format(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import health
        self.assertEqual(health.health_line(ISO_NOW, "chat", "read", W, "unreachable", None),
                         "- 2023-11-14T22:13:20Z chat.read Willow_Slack__slack unreachable \u2192 open")
        self.assertEqual(health.health_line(ISO_NOW, "chat", "read", W, "unreachable", "fallback"),
                         "- 2023-11-14T22:13:20Z chat.read Willow_Slack__slack unreachable \u2192 fallback")

    def test_record_logs_open_then_fixed_once(self):
        self.rec("error:unreachable", W, "read", "unreachable")
        self.rec("error:unreachable", W, "read", "unreachable")
        self.rec("ok", W, "read", None, "--fixed-by", "fallback")
        self.rec("ok", W, "read", None, "--fixed-by", "fallback")
        lines = [l for l in self.health_md().splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].endswith("unreachable \u2192 open"), lines[0])
        self.assertTrue(lines[1].endswith("unreachable \u2192 fallback"), lines[1])
        self.assertTrue(self.health_md().startswith(
            "One line per connector failure or fix, appended by `mother health record`.\n"))

    def test_gateway_stored_under_server_not_capability(self):
        self.rec("error:unreachable", W, "read", "unreachable", "--gateway", "mcp-s.example")
        h = self.caps()["health"][W]
        self.assertEqual(h["_gateway"], "mcp-s.example")
        self.assertNotIn("gateway", h["read"])
        self.assertTrue(self.health_md().rstrip().endswith("@mcp-s.example"))

    def test_summary_folds_gateway(self):
        for s in (W, C):
            self.rec("error:unreachable", s, "read", "unreachable", "--gateway", "mcp-s.example")
        r = self.run_cli("summary")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("gateway mcp-s.example unreachable \u00d72"), lines[0])

    def test_summary_counts_and_window(self):
        self.rec("error:dropped", W, "read", "dropped", now=NOW - 20 * 86400)
        self.rec("error:dropped", W, "send", "dropped")
        self.rec("ok", W, "send", None, "--fixed-by", "retry")
        self.rec("error:unreachable", C, "read", "unreachable")
        out = self.run_cli("summary").stdout.strip().splitlines()
        self.assertIn("Willow_Slack__slack dropped \u00d72 (fixed: retry 1; open 1)", out)
        self.assertIn("claudeai_slack unreachable \u00d71 (open 1)", out)
        self.assertEqual(len(out), 2)

    def test_summary_empty(self):
        r = self.run_cli("summary")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_health_md_has_no_ids(self):
        sid = "mcp__486bd9bf-5fd4-4917-848c-4f2ddfdfb82e"
        self.rec("error:unreachable", sid, "read", "unreachable")
        self.rec("ok", sid, "read", None, "--fixed-by", "you")
        md = self.health_md()
        self.assertIn("486bd9bf", md)
        self.assertNotIn("486bd9bf-5fd4-4917-848c-4f2ddfdfb82e", md)


if __name__ == "__main__":
    unittest.main()
