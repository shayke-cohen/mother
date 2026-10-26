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
W = "mcp__Acme_Slack__slack"
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
                               "stamped": False, "verified": True})

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
        self.assertEqual(e["alerted_at"], iso_ago(1))
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
        self.assertEqual(health.server_short(W), "Acme_Slack__slack")
        self.assertEqual(health.server_short("mcp__486bd9bf-5fd4-4917-848c-4f2ddfdfb82e"), "486bd9bf")

    def test_health_line_format(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import health
        self.assertEqual(health.health_line(ISO_NOW, "chat", "read", W, "unreachable", None),
                         "- 2023-11-14T22:13:20Z chat.read Acme_Slack__slack unreachable \u2192 open")
        self.assertEqual(health.health_line(ISO_NOW, "chat", "read", W, "unreachable", "fallback"),
                         "- 2023-11-14T22:13:20Z chat.read Acme_Slack__slack unreachable \u2192 fallback")

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
        self.assertIn("Acme_Slack__slack dropped \u00d72 (fixed: retry 1; open 1)", out)
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

    def entry(self, status="expired", cls="invalidated", **extra):
        e = {"status": status, "class": cls, "checked_at": iso_ago(1)}
        e.update(extra)
        return e

    def conn(self, order, health):
        return {"server": order[0], "read": "ok", "servers": {"read": order},
                "health": {s: {"read": e} for s, e in health.items()}}

    def merge(self, fresh):
        f = Path(self.tmp.name) / "fresh.json"
        f.write_text(json.dumps({"checked_at": ISO_NOW, "host": "h2", "connectors": {"slack": fresh}}))
        r = self.run_cli("merge", "--file", str(f))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def set_cfg(self, tz="Asia/Jerusalem", quiet="22:00-07:00"):
        cfg = json.loads((self.ws / "config.json").read_text())
        cfg["manager"]["timezone"] = tz
        cfg["manager"]["quiet_hours"] = quiet
        (self.ws / "config.json").write_text(json.dumps(cfg))

    def at(self, h, m=0, day=14):
        """Epoch seconds for day-of-Nov-2023 h:m in Asia/Jerusalem (UTC+2)."""
        import datetime as dt
        return int((dt.datetime(2023, 11, day, tzinfo=dt.timezone.utc) + dt.timedelta(hours=h - 2, minutes=m)).timestamp())

    def alert(self, now, *args):
        self.env["MOTHER_NOW"] = str(now)
        r = self.run_cli("alert", "--kind", "chat", "--cap", "read", "--server", C, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["action"]

    def test_merge_keeps_order_and_alert(self):
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry(alerted_at=iso_ago(2))}))
        self.merge(self.conn([C, W, "N"], {W: self.entry("ok", None), C: self.entry(), "N": self.entry("ok", None)}))
        c = self.caps()
        self.assertEqual(c["servers"]["read"], [W, C, "N"])
        self.assertEqual(c["health"][C]["read"]["alerted_at"], iso_ago(2))

    def test_merge_drops_alert_when_class_changes(self):
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry(alerted_at=iso_ago(2), pending=True)}))
        self.merge(self.conn([W, C], {W: self.entry("ok", None), C: self.entry("error:unreachable", "unreachable")}))
        e = self.caps()["health"][C]["read"]
        self.assertNotIn("alerted_at", e)
        self.assertNotIn("pending", e)

    def test_merge_drops_absent_servers_and_takes_new_values(self):
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry()}))
        self.merge(self.conn([W], {W: self.entry("ok", None, checked_at=ISO_NOW)}))
        c = self.caps()
        self.assertEqual(c["servers"]["read"], [W])
        self.assertNotIn(C, c["health"])
        self.assertEqual(c["health"][W]["read"]["checked_at"], ISO_NOW)
        self.assertEqual(json.loads(self.caps_path.read_text())["host"], "h2")

    def test_merge_reports_newly_broken(self):
        self.write_caps(self.conn([W], {W: self.entry("ok", None)}))
        out = self.merge(self.conn([W], {W: self.entry()}))
        self.assertEqual(out, {"changed": ["chat.read"]})
        out = self.merge(self.conn([W], {W: self.entry()}))
        self.assertEqual(out, {"changed": []})

    def test_alert_once_per_day(self):
        self.set_cfg(quiet="01:00-02:00")
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry()}))
        t = self.at(12)
        self.assertEqual(self.alert(t), "send")
        self.assertEqual(self.alert(t, "--sent"), "sent")
        self.assertEqual(self.alert(t + 3600), "skip")
        self.assertEqual(self.alert(self.at(12, day=15)), "send")

    def test_alert_flap_same_day_no_repeat(self):
        self.set_cfg(quiet="01:00-02:00")
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry()}))
        t = self.at(12)
        self.assertEqual(self.alert(t, "--sent"), "sent")
        self.env["MOTHER_NOW"] = str(t + 600)
        self.assertEqual(self.record("read", C, "ok").returncode, 0)
        self.env["MOTHER_NOW"] = str(t + 1200)
        self.assertEqual(self.record("read", C, "expired", "--class", "invalidated").returncode, 0)
        self.assertEqual(self.alert(t + 1200), "skip")
        # a different class is a new issue
        self.assertEqual(self.record("read", C, "error:unreachable", "--class", "unreachable").returncode, 0)
        self.assertEqual(self.alert(t + 1300), "send")

    def test_alert_local_date_not_utc(self):
        self.set_cfg(quiet="03:00-04:00")
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry()}))
        # 23:30 local on the 14th and 00:30 local on the 15th share a UTC date (21:30Z / 22:30Z on the 14th)
        self.assertEqual(self.alert(self.at(23, 30), "--sent"), "sent")
        self.assertEqual(self.alert(self.at(0, 30, day=15)), "send")
        # 00:30 local and 23:30 local the same day fall on different UTC dates
        self.assertEqual(self.alert(self.at(0, 30, day=16), "--sent"), "sent")
        self.assertEqual(self.alert(self.at(23, 30, day=16)), "skip")

    def test_merge_recomputes_rollup_and_server_for_merged_order(self):
        old = self.conn([W, C], {W: self.entry("error:unreachable", "unreachable"), C: self.entry()})
        self.write_caps(old)
        fresh = self.conn([C, W], {W: self.entry("error:unreachable", "unreachable"), C: self.entry()})
        fresh["read"] = "expired"  # the doctor's rollup, computed against [C, W]
        out = self.merge(fresh)
        c = self.caps()
        self.assertEqual(c["servers"]["read"], [W, C])
        self.assertEqual(c["read"], "error:unreachable")
        self.assertEqual(c["server"], C)  # the doctor's `server`, still listed, stands (I3)
        self.assertEqual(out, {"changed": []})

    def test_alert_skips_ok(self):
        self.set_cfg()
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry("ok", None)}))
        self.assertEqual(self.alert(self.at(12)), "skip")

    def test_alert_quiet_hours_cross_midnight(self):
        self.set_cfg()
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry()}))
        self.assertEqual(self.alert(self.at(23, 30)), "pending")
        self.assertTrue(self.caps()["health"][C]["read"]["pending"])
        self.assertEqual(self.alert(self.at(23, 30), "--urgent"), "send")
        self.assertEqual(self.alert(self.at(6, 59, day=15)), "pending")
        self.env["MOTHER_NOW"] = str(self.at(7, 1, day=15))
        r = self.run_cli("pending")
        self.assertEqual(json.loads(r.stdout), [{"kind": "chat", "cap": "read", "server": C,
                                                 "status": "expired", "class": "invalidated"}])

    def test_pending_empty_during_quiet_hours(self):
        self.set_cfg()
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry(pending=True)}))
        self.env["MOTHER_NOW"] = str(self.at(2, day=15))
        self.assertEqual(json.loads(self.run_cli("pending").stdout), [])

    def test_sent_clears_pending(self):
        self.set_cfg()
        self.write_caps(self.conn([W, C], {W: self.entry("ok", None), C: self.entry(pending=True)}))
        self.assertEqual(self.alert(self.at(8), "--sent"), "sent")
        e = self.caps()["health"][C]["read"]
        self.assertNotIn("pending", e)
        self.assertIn("alerted_at", e)

    # ---- final review fixes

    def real_shape(self, **slack_over):
        """capabilities.json in the shape the doctor actually wrote (placeholder values)."""
        slack = {"server": "mcp__gw__slack", "read": "ok", "post": "ok", "search": "ok",
                 "private_history": "ok", "users": "ok", "token": "user",
                 "servers": {"search": "mcp__abcd1234", "private_history": "mcp__abcd1234", "post": "mcp__gw__slack"},
                 "notes": ["note", "note", "note", "note", "note"]}
        slack.update(slack_over)
        return {"checked_at": iso_ago(1), "host": "h1", "connectors": {
            "slack": slack,
            "jira": {"server": "mcp__gw__jira", "read": "ok", "create": "ok", "comment": "ok",
                     "transition": "untested", "sprint": "untested", "notes": ["note", "note"]},
            "gmail": {"server": "mcp__gw__gmail", "read": "ok", "draft": "ok", "send": "untested", "notes": ["note"]},
            "google": {"server": "mcp__gw__calendar", "read": "ok", "create": "untested", "notes": ["note"]},
            "github": {"tool": "gh", "read": "ok", "write": "untested", "token": "user", "notes": ["note"]}}}

    def all_caps(self):
        return json.loads(self.caps_path.read_text())

    def pick_kind(self, kind, cap, *args):
        r = self.run_cli("pick", "--kind", kind, "--cap", cap, *args)
        return r, (json.loads(r.stdout) if r.stdout.strip() else None)

    def test_c1_real_shape_pick(self):
        self.caps_path.write_text(json.dumps(self.real_shape()))
        r, out = self.pick_kind("code", "read")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(out["server"])
        self.assertEqual(out["tool"], "gh")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.pick_kind("chat", "search")[1]["server"], "mcp__abcd1234")
        self.assertEqual(self.pick_kind("chat", "read")[1]["server"], "mcp__gw__slack")
        self.assertEqual(self.pick_kind("tracker", "read")[1]["server"], "mcp__gw__jira")
        r, out = self.pick_kind("code", "write", "--target", "other")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((out["tool"], out["verified"]), ("gh", False))

    def test_c1_tool_connector_survives_record(self):
        self.caps_path.write_text(json.dumps(self.real_shape()))
        self.assertEqual(self.record("read", "mcp__gw__slack", "ok").returncode, 0)
        gh = self.all_caps()["connectors"]["github"]
        self.assertEqual({k: gh[k] for k in ("tool", "read", "write", "token", "notes")},
                         {"tool": "gh", "read": "ok", "write": "untested", "token": "user", "notes": ["note"]})
        slack = self.all_caps()["connectors"]["slack"]
        self.assertEqual(slack["notes"], ["note"] * 5)
        self.assertNotIn("notes", slack["servers"])
        self.assertEqual(self.pick_kind("code", "read")[0].returncode, 0)

    def test_c1_tool_connector_survives_merge(self):
        self.caps_path.write_text(json.dumps(self.real_shape()))
        f = Path(self.tmp.name) / "fresh.json"
        f.write_text(json.dumps(self.real_shape()))
        r = self.run_cli("merge", "--file", str(f))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"changed": []})
        gh = self.all_caps()["connectors"]["github"]
        self.assertEqual((gh["tool"], gh["read"], gh["write"], gh["token"]), ("gh", "ok", "untested", "user"))
        self.assertEqual(self.pick_kind("code", "read")[0].returncode, 0)

    def post_caps(self, w, c=None, **extra):
        order = [W] + ([C] if c else [])
        health = {W: {"post": {"status": w, "class": None if w in ("ok", "untested") else "unreachable",
                               "checked_at": iso_ago(1)}}}
        if c:
            health[C] = {"post": {"status": c, "class": None, "checked_at": iso_ago(1)}}
        slack = {"server": W, "post": w, "servers": {"post": order}, "health": health}
        slack.update(extra)
        self.write_caps(slack)

    def test_c2_untested_post_is_pickable(self):
        self.post_caps("untested")
        r, out = self.pick_kind("chat", "post", "--target", "dm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((out["server"], out["status"], out["verified"]), (W, "untested", False))
        r, out = self.pick_kind("chat", "post", "--target", "other")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_c2_untested_read_is_not_pickable(self):
        self.two_servers(w="untested", c="error:unreachable")
        self.assertEqual(self.pick()[0].returncode, 3)

    def test_c2_ok_backup_preferred_over_untested(self):
        self.post_caps("untested", "ok")
        r, out = self.pick_kind("chat", "post", "--target", "dm")
        self.assertEqual((out["server"], out["verified"], out["fallback"]), (C, True, True))

    def test_c2_failing_post_excluded(self):
        self.post_caps("error:unreachable")
        self.assertEqual(self.pick_kind("chat", "post", "--target", "dm")[0].returncode, 3)

    def test_c2_merge_ok_to_untested_not_changed(self):
        self.post_caps("ok")
        f = Path(self.tmp.name) / "fresh.json"
        f.write_text(json.dumps({"checked_at": ISO_NOW, "connectors": {"slack": {
            "server": W, "post": "untested", "servers": {"post": [W]},
            "health": {W: {"post": {"status": "untested", "class": None}}}}}}))
        r = self.run_cli("merge", "--file", str(f))
        self.assertEqual(json.loads(r.stdout), {"changed": []})
        self.assertEqual(self.pick_kind("chat", "post", "--target", "dm")[0].returncode, 0)

    def test_c2_alert_skips_untested(self):
        self.post_caps("untested")
        r = self.run_cli("alert", "--kind", "chat", "--cap", "post", "--server", W)
        self.assertEqual(json.loads(r.stdout)["action"], "skip")

    def test_i1_outage_found_by_doctor_is_logged(self):
        self.two_servers()
        down = {"status": "error:unreachable", "class": "unreachable", "checked_at": ISO_NOW}
        self.merge({"server": W, "read": "ok", "servers": {"read": [W, C]}, "health": {
            W: {"_gateway": "gw.example", "read": down}, C: {"read": {"status": "ok", "class": None}}}})
        lines = [l for l in self.health_md().splitlines() if l.startswith("- ")]
        self.assertEqual(lines, ["- %s chat.read Acme_Slack__slack unreachable → open @gw.example" % ISO_NOW])
        self.merge({"server": W, "read": "ok", "servers": {"read": [W, C]}, "health": {
            W: {"_gateway": "gw.example", "read": down}, C: {"read": {"status": "ok", "class": None}}}})
        self.rec("error:unreachable", W, "read", "unreachable")  # the ladder's re-test: same issue
        self.rec("ok", C, "read", None, "--fixed-by", "fallback")  # backup was already ok
        lines = [l for l in self.health_md().splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[1], "- %s chat.read Acme_Slack__slack unreachable → fallback @gw.example" % ISO_NOW)
        out = self.run_cli("summary").stdout.strip().splitlines()
        self.assertEqual(out, ["Acme_Slack__slack unreachable ×2 (fixed: fallback 1; open 1)"])

    def test_i1_fallback_logged_when_backup_had_no_entry(self):
        self.write_caps({"server": W, "read": "error:unreachable", "servers": {"read": [W]}, "health": {
            W: {"read": {"status": "error:unreachable", "class": "unreachable", "checked_at": iso_ago(1)}}}})
        self.rec("ok", C, "read", None, "--fixed-by", "fallback")
        self.assertTrue(self.health_md().rstrip().endswith("chat.read Acme_Slack__slack unreachable → fallback"))

    def gateway_caps(self, cls="unreachable"):
        st = "error:unreachable" if cls == "unreachable" else "expired"
        e = {"status": st, "class": cls, "checked_at": iso_ago(1)}
        self.caps_path.write_text(json.dumps({"checked_at": iso_ago(1), "connectors": {
            "slack": {"server": W, "read": st, "servers": {"read": [W]},
                      "health": {W: {"_gateway": "gw.example", "read": dict(e)}}},
            "jira": {"server": "mcp__gw__jira", "read": st, "servers": {"read": ["mcp__gw__jira"]},
                     "health": {"mcp__gw__jira": {"_gateway": "gw.example", "read": dict(e)},
                                "mcp__other": {"_gateway": "elsewhere", "read": dict(e)}}}}}))

    def galert(self, now, kind, server, *args):
        self.env["MOTHER_NOW"] = str(now)
        r = self.run_cli("alert", "--kind", kind, "--cap", "read", "--server", server, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["action"]

    def test_i2_gateway_outage_alerts_once(self):
        self.set_cfg(quiet="01:00-02:00")
        self.gateway_caps()
        t = self.at(12)
        self.assertEqual(self.galert(t, "chat", W), "send")
        self.assertEqual(self.galert(t, "chat", W, "--sent"), "sent")
        self.assertEqual(self.galert(t + 60, "tracker", "mcp__gw__jira"), "skip")
        jira = self.all_caps()["connectors"]["jira"]["health"]
        self.assertIn("alerted_at", jira["mcp__gw__jira"]["read"])
        self.assertNotIn("alerted_at", jira["mcp__other"]["read"])
        self.assertEqual(self.galert(t + 60, "tracker", "mcp__other"), "send")

    def test_i2_gateway_different_class_alerts(self):
        self.set_cfg(quiet="01:00-02:00")
        self.gateway_caps()
        t = self.at(12)
        self.assertEqual(self.galert(t, "chat", W, "--sent"), "sent")
        r = self.run_cli("record", "--kind", "tracker", "--cap", "read", "--server", "mcp__gw__jira",
                         "--status", "expired", "--class", "invalidated")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.galert(t + 60, "tracker", "mcp__gw__jira"), "send")

    def test_i3_merge_carries_old_only_connectors(self):
        old = self.real_shape(stamps=["mcp__abcd1234"])
        old["connectors"]["slack"]["health"] = {"mcp__abcd1234": {"search": {
            "status": "ok", "class": None, "checked_at": iso_ago(1), "alerted_at": iso_ago(2)}}}
        self.caps_path.write_text(json.dumps(old))
        f = Path(self.tmp.name) / "fresh.json"
        f.write_text(json.dumps({"checked_at": ISO_NOW, "connectors": {"jira": old["connectors"]["jira"]}}))
        self.assertEqual(self.run_cli("merge", "--file", str(f)).returncode, 0)
        slack = self.all_caps()["connectors"]["slack"]
        self.assertEqual(slack["stamps"], ["mcp__abcd1234"])
        self.assertEqual(slack["health"]["mcp__abcd1234"]["search"]["alerted_at"], iso_ago(2))
        self.assertIn("github", self.all_caps()["connectors"])

    def test_i3_merge_keeps_doctor_server(self):
        self.caps_path.write_text(json.dumps(self.real_shape()))
        f = Path(self.tmp.name) / "fresh.json"
        f.write_text(json.dumps(self.real_shape()))
        self.run_cli("merge", "--file", str(f))
        self.assertEqual(self.all_caps()["connectors"]["slack"]["server"], "mcp__gw__slack")
        # a doctor server no longer listed falls back to servers.read[0]
        fresh = {"server": "mcp__gone", "post": "ok", "read": "ok", "servers": {"post": [C], "read": [W]},
                 "health": {W: {"read": {"status": "ok", "class": None}}, C: {"post": {"status": "ok", "class": None}}}}
        self.merge(fresh)
        self.assertEqual(self.caps()["server"], W)
        fresh["server"] = C  # still listed (for post): the doctor's choice stands
        self.merge(fresh)
        self.assertEqual(self.caps()["server"], C)

    def test_i4_merge_fills_checked_at(self):
        self.two_servers()
        self.merge({"server": W, "read": "ok", "servers": {"read": [W]},
                    "health": {W: {"read": {"status": "ok", "class": None}}}})
        self.assertEqual(self.caps()["health"][W]["read"]["checked_at"], ISO_NOW)
        self.assertIs(self.pick("--max-age-hours", "6")[1]["fresh"], True)

    def test_i5_other_fallback_needs_known_same_identity(self):
        self.two_servers(w="error:unreachable")
        self.assertEqual(self.pick("--target", "other")[0].returncode, 3)
        self.assertEqual(self.pick("--target", "dm")[0].returncode, 0)
        self.two_servers(w="error:unreachable", token={W: "user"})
        self.assertEqual(self.pick("--target", "other")[0].returncode, 3)
        self.two_servers(w="error:unreachable", token={W: "user", C: "user"})
        r, out = self.pick("--target", "other")
        self.assertEqual((r.returncode, out["server"]), (0, C))

    def test_fallback_closes_the_backups_own_open_line(self):
        self.two_servers(w="error:unreachable", c="error:unreachable")
        self.rec("ok", C, "read", None, "--fixed-by", "fallback")
        lines = [l for l in self.health_md().splitlines() if l.startswith("- ")]
        self.assertEqual(sorted(l.split(" ", 3)[3] for l in lines),
                         ["Acme_Slack__slack unreachable → fallback", "claudeai_slack unreachable → fallback"])

    def test_merge_does_not_refresh_skipped_connectors(self):
        old = self.real_shape()
        old["checked_at"] = iso_ago(10)
        self.caps_path.write_text(json.dumps(old))
        f = Path(self.tmp.name) / "fresh.json"
        f.write_text(json.dumps({"checked_at": ISO_NOW, "connectors": {"jira": old["connectors"]["jira"]}}))
        self.assertEqual(self.run_cli("merge", "--file", str(f)).returncode, 0)
        self.assertIs(self.pick_kind("code", "read", "--max-age-hours", "6")[1]["fresh"], False)
        self.assertIs(self.pick_kind("chat", "read", "--max-age-hours", "6")[1]["fresh"], False)
        self.assertIs(self.pick_kind("tracker", "read", "--max-age-hours", "6")[1]["fresh"], True)

    def test_merge_dates_a_tool_connector_it_checked(self):
        old = self.real_shape()
        old["checked_at"] = iso_ago(10)
        self.caps_path.write_text(json.dumps(old))
        self.assertIs(self.pick_kind("code", "read", "--max-age-hours", "6")[1]["fresh"], False)  # undated
        f = Path(self.tmp.name) / "fresh.json"
        f.write_text(json.dumps({"checked_at": ISO_NOW, "connectors": {"github": old["connectors"]["github"]}}))
        self.assertEqual(self.run_cli("merge", "--file", str(f)).returncode, 0)
        self.assertIs(self.pick_kind("code", "read", "--max-age-hours", "6")[1]["fresh"], True)

    def test_merge_refuses_corrupt_file(self):
        self.caps_path.write_text("{nope")
        f = Path(self.tmp.name) / "fresh.json"
        f.write_text(json.dumps(self.real_shape()))
        r = self.run_cli("merge", "--file", str(f))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.caps_path.read_text(), "{nope")
        self.two_servers()
        before = self.caps_path.read_text()
        f.write_text("{nope")
        self.assertEqual(self.run_cli("merge", "--file", str(f)).returncode, 2)
        self.assertEqual(self.caps_path.read_text(), before)

    def test_classify_strips_urls(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import health
        status, cls = health.classify("go to https://auth.example/x?code=abc then")
        self.assertEqual(cls, "other")
        self.assertNotIn("auth.example", status)
        self.assertIn("<url>", status)


if __name__ == "__main__":
    unittest.main()
