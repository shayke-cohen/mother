"""End-to-end scenarios for the hub: a stand-in chat server (tests/fixtures/fake_mcp.py over stdio) and a stand-in
tracker adapter -> poll_once -> inbox -> tail -> a scripted decider (stands in for the hub model) -> decide / session /
pending verbs -> the files the Now panel reads (hub.json, sessions.json, watcher.json, watch.json).
Also `mother hub smoke`. Fixtures are invented example.com data.
Run: python3 -m unittest tests.test_hub_e2e -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import hub  # noqa: E402
import hub_sources  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_mcp.py")
UTC = timezone.utc
GW = "gw__"
CHAT_TOOLS = ("get_channel_history", "get_thread_replies", "search-messages")
CH = "C00000001"
ME = "U00000000"


class FakeTracker:
    """Stands in for the tracker adapter; records the watch config of every pass."""
    items = []
    cfgs = []
    known = ["PLAT", "WEB"]  # the tracker's real projects

    @classmethod
    def poll(cls, client, cursor, cfg, me, at):
        cls.cfgs.append(dict(cfg))
        if cursor is None:
            return {"lines": [], "cursor": {"n": 0}, "partial": [], "known_projects": cls.known}
        new = cls.items[cursor["n"]:]
        return {"lines": [hub.make_line("tracker", "update", k, hub._iso(at), title=k, who="Agnes",
                                        link="https://example.com/browse/" + k, summary="changed")
                          for k in new], "cursor": {"n": len(cls.items)}, "partial": [], "known_projects": cls.known}


def conn(fixture, log):
    return {"command": sys.executable, "args": [FAKE],
            "env": {"FAKE_MCP_FIXTURE": fixture, "FAKE_MCP_LOG": log}}


class E2E(unittest.TestCase):
    AT = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"MOTHER_STATE_DIR": os.path.join(self.tmp.name, "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MOTHER_WORKSPACE", None)
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.fx = os.path.join(self.tmp.name, "fx.json")
        self.log = os.path.join(self.tmp.name, "calls.jsonl")
        self.cfg = {"version": 1,
                    "manager": {"email": "dana@example.com", "timezone": "UTC", "chat_dm": ME,
                                "workdays": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
                    "connectors": {"chat": "slack", "tracker": "jira"},
                    "hub": {"enabled": True, "every_seconds": 120, "sweep_minutes": 240,
                            "connections": {"chat": conn(self.fx, self.log),
                                            "tracker": {"command": "example-server", "args": [], "env": {}}},
                            "watch": {"chat": {"channels": [CH]}, "tracker": {"projects": ["PLAT"]}}}}
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        FakeTracker.items, FakeTracker.cfgs = [], []
        saved = dict(hub_sources.ADAPTERS)
        hub_sources.ADAPTERS["tracker:jira"] = FakeTracker
        self.addCleanup(lambda: (hub_sources.ADAPTERS.clear(), hub_sources.ADAPTERS.update(saved)))
        self.clients = {}
        self.addCleanup(self.close_clients)
        self.write_chat()

    def close_clients(self):
        for c in self.clients.values():
            if not isinstance(c, Exception) and hasattr(c, "_client"):
                hub._close(c)
        self.clients = {}

    # ----------------------------------------------------------------------------------------- the stand-ins
    def at(self, minutes=0):
        return self.AT + timedelta(minutes=minutes)

    def ts(self, minutes, secs=0):
        return "%.6f" % (self.at(minutes).timestamp() + secs)

    def msg(self, minutes, text, user="U00000009", secs=0, **kw):
        d = {"ts": self.ts(minutes, secs), "user": user, "text": text}
        d.update(kw)
        return d

    def write_chat(self, history=None, search=None):
        Path(self.fx).write_text(json.dumps({
            "tools": [GW + t for t in CHAT_TOOLS],
            "results": {GW + "get_channel_history": {"ok": True, "messages": history or []},
                        GW + "get_thread_replies": {"ok": True, "messages": []},
                        GW + "search-messages": {"ok": True, "messages": {"matches": search or []}}}}))

    def poll(self, minutes, history=None, search=None):
        """One watcher pass at `minutes` after AT, against a fresh chat server that serves `history`."""
        self.write_chat(history, search)
        self.close_clients()
        chat_only = {"connectors": self.cfg["connectors"],
                     "hub": {"connections": {"chat": self.cfg["hub"]["connections"]["chat"]}}}
        self.clients = hub.build_clients(chat_only, {})
        self.clients["tracker"] = object()
        return hub.poll_once(self.ws, self.cfg, self.at(minutes), self.clients)

    # ------------------------------------------------------------------------------- the hub's side of the wire
    def tail(self):
        buf = io.StringIO()
        hub.tail(self.ws, out=buf)
        return [json.loads(l) for l in buf.getvalue().splitlines()]

    def decider(self, lines):
        """The scripted hub: housekeeping is logged, a line that asks for a decision is an update for the manager,
        anything for a key that has a live session is routed to it, the rest is proposed as a new card."""
        sess = hub._sessions_doc(self.ws)
        items = []
        for l in lines:
            if l["kind"] in ("sweep", "error"):
                dec = "log"
            elif "decision" in l["summary"].lower():
                dec = "now"
            elif hub.find_session(sess, [l["key"]] + l["also"]):
                dec = "route"
            else:
                dec = "propose"
            items.append({"line_id": l["id"], "key": l["key"], "decision": dec, "also": l["also"]})
        return items

    def wake(self, minutes):
        """What one hub wake does: tail, decide what it printed. Returns (lines printed, decide results)."""
        lines = self.tail()
        res = hub.decide(self.ws, self.cfg, self.at(minutes), self.decider(lines)) if lines else []
        return lines, res

    def verb(self, minutes, *args):
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            rc = hub.main(list(args), self.ws, self.cfg, self.at(minutes))
        finally:
            sys.stdout = real
        self.assertEqual(rc, 0, out.getvalue())
        return out.getvalue()

    def card(self, key, minutes):
        self.verb(minutes, "pending", "raised", key, "--name", key.lower())

    def start_session(self, key, sid, minutes):
        self.verb(minutes, "session", "add", key, "--id", sid, "--name", key.lower(), "--title", key)

    def open_session_for(self, key, sid, minutes):
        """A tracker update the hub proposes, raises as a card and starts a session for."""
        FakeTracker.items.append(key)
        self.poll(minutes)
        _, res = self.wake(minutes)
        self.assertEqual([r["decision"] for r in res], ["propose"])
        self.card(key, minutes)
        self.start_session(key, sid, minutes)

    # ------------------------------------------------------------------------------------- what the panel reads
    def hub_json(self):
        return hub.load_json(self.ws, "hub.json", {})

    def sessions(self):
        return hub.load_json(self.ws, "sessions.json", {}).get("sessions", {})

    def inbox(self):
        return [l for l in hub.read_inbox(self.ws) if l["source"] != "hub"]

    # ------------------------------------------------------------------------------------------- scenarios
    def test_thread_about_an_issue_lands_in_the_issue_session(self):
        self.poll(0)
        self.open_session_for("PLAT-1", "S1", 2)
        self.assertEqual(self.sessions()["PLAT-1"]["state"], "live")
        self.assertEqual(self.hub_json()["pending"], [])

        parent = self.msg(4, "blocked on PLAT-1 until the vendor answers")
        self.poll(4, history=[parent])
        lines, res = self.wake(4)
        (line,) = [l for l in lines if l["source"] == "chat"]
        self.assertEqual((line["key"], line["also"]), ("%s/%s" % (CH, parent["ts"]), ["PLAT-1"]))
        (r,) = [r for r in res if r["key"] == line["key"]]
        self.assertEqual((r["decision"], r["session_id"]), ("route", "S1"))
        self.assertEqual(self.sessions()["PLAT-1"]["unseen"], 1)
        self.assertIn(line["key"], self.sessions()["PLAT-1"]["also"])

        # a later reply in that thread no longer names the issue, and still finds the session
        reply = self.msg(6, "any news?", thread_ts=parent["ts"])
        self.poll(6, history=[reply])
        lines, res = self.wake(6)
        (r,) = [r for r in res if r["key"] == line["key"]]
        self.assertEqual((r["decision"], r["session_id"]), ("route", "S1"))
        self.assertEqual(self.sessions()["PLAT-1"]["unseen"], 2)

        beat = hub.load_json(self.ws, "watcher.json", {})
        self.assertEqual(beat["sources"], {"chat": {"status": "ok"}, "tracker": {"status": "ok"}})
        self.assertEqual(beat["errors"], [])
        self.assertTrue(beat["last_append_at"])
        self.assertTrue(self.hub_json()["last_wake"])

    def test_the_managers_reply_resolves_the_update(self):
        self.poll(0)
        ask = self.msg(2, "need a decision on the rollout date")
        self.poll(2, history=[ask])
        lines, res = self.wake(2)
        key = "%s/%s" % (CH, ask["ts"])
        self.assertEqual([r["decision"] for r in res], ["now"])
        self.assertEqual([(u["key"], u["state"]) for u in self.hub_json()["updates"]], [(key, "open")])

        answer = self.msg(4, "go with Thursday", user=ME, thread_ts=ask["ts"])
        self.poll(4, history=[answer])
        self.assertIn((key, "resolved"), [(l["key"], l["kind"]) for l in self.inbox()])
        self.assertEqual([l for l in self.tail() if l["key"] == key], [])  # nothing for the hub to do
        self.assertEqual(self.hub_json()["updates"], [])  # the panel stops showing it

    def test_hub_down_then_catch_up_covers_each_line_once(self):
        self.poll(0)
        for i, minutes in enumerate((2, 4, 6)):
            self.poll(minutes, history=[self.msg(minutes, "thing %d needs a look" % i)])
        self.assertEqual(self.hub_json().get("routed", []), [])  # nobody decided anything while the hub was down

        lines, res = self.wake(8)
        chat = [l for l in lines if l["source"] == "chat"]
        self.assertEqual(len(chat), 3)
        routed = self.hub_json()["routed"]
        self.assertEqual(len(routed), len(lines))
        self.assertEqual(self.hub_json()["inbox_cursor"], routed[-1]["line_id"])
        self.assertEqual(len(self.hub_json()["pending"]), 3)
        self.assertEqual(self.tail(), [])  # the cursor moved: the next wake sees nothing

        again = hub.decide(self.ws, self.cfg, self.at(9), self.decider(lines))  # a replayed backlog
        self.assertTrue(all(r.get("already_decided") for r in again))
        self.assertEqual(len(self.hub_json()["routed"]), len(lines))

    def test_four_cards_in_one_wake_three_raised_one_waits(self):
        self.poll(0)
        FakeTracker.items.extend(["PLAT-1", "PLAT-2", "PLAT-3", "PLAT-4"])
        self.poll(2)
        lines, res = self.wake(2)
        self.assertEqual([r["decision"] for r in res], ["propose"] * 4)
        for k in ("PLAT-1", "PLAT-2", "PLAT-3"):  # max_cards_per_wake is 3
            self.card(k, 2)
        pend = {p["key"]: p for p in self.hub_json()["pending"]}
        self.assertEqual(sorted(pend), ["PLAT-1", "PLAT-2", "PLAT-3", "PLAT-4"])
        self.assertEqual([k for k in sorted(pend) if pend[k]["raised_at"]], ["PLAT-1", "PLAT-2", "PLAT-3"])
        self.assertIsNone(pend["PLAT-4"]["raised_at"])

        self.card("PLAT-4", 30)  # raised on a later wake
        self.assertTrue(all(p["raised_at"] for p in self.hub_json()["pending"]))
        for i, k in enumerate(("PLAT-1", "PLAT-2", "PLAT-3", "PLAT-4")):
            self.start_session(k, "S%d" % (i + 1), 31)
        self.assertEqual(self.hub_json()["pending"], [])
        self.assertEqual(sorted(k for k, s in self.sessions().items() if s["state"] == "live"),
                         ["PLAT-1", "PLAT-2", "PLAT-3", "PLAT-4"])

    def test_a_muted_channel_stays_quiet(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["hub"]["watch"]["chat"]["channels"] = [CH, "C00000002"]
        self.cfg = cfg
        self.verb(0, "mute", "C00000002")
        self.poll(0)
        self.poll(2, history=[self.msg(2, "launch notes are ready")])
        keys = [l["key"] for l in self.inbox() if l["source"] == "chat"]
        self.assertTrue(keys)
        self.assertTrue(all(k.startswith(CH + "/") for k in keys), keys)
        asked = {json.loads(l)["arguments"].get("channel_id") for l in Path(self.log).read_text().splitlines()
                 if json.loads(l)["name"].endswith("get_channel_history")}
        self.assertEqual(asked, {CH, "C00000002"})  # still read; the lines are dropped before the inbox

    def test_a_mapped_dm_and_the_self_dm_inbox_reach_the_hub(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["hub"]["watch"]["chat"]["channels"] = []
        cfg["teams"] = [{"slug": "web", "people": [{"name": "Agnes", "chat": "U00000001"}]}]
        self.cfg = cfg
        self.verb(0, "dms", "set", "U00000001=D00000001")
        self.verb(0, "dms", "inbox", "D00000009")
        self.poll(0)
        # the stand-in server answers every history read with the same page
        page = [self.msg(2, "remember to call the venue", user=ME),
                self.msg(2, "can you look at PLAT-1?", user="U00000001", secs=1)]
        self.poll(2, history=page)
        got = sorted((l["kind"], l["key"]) for l in self.tail())
        self.assertIn(("update", "D00000001/dm"), got)
        manual = [k for kind, k in got if kind == "manual"]
        self.assertEqual(len(manual), 1)
        self.assertTrue(manual[0].startswith("manual:"))
        asked = {json.loads(l)["arguments"].get("channel_id") for l in Path(self.log).read_text().splitlines()
                 if json.loads(l)["name"].endswith("get_channel_history")}
        self.assertEqual(asked, {"D00000001", "D00000009"})

    def test_add_of_a_link_routes_to_its_session(self):
        self.poll(0)
        self.open_session_for("PLAT-1", "S1", 2)
        self.verb(4, "add", "https://example.com/browse/PLAT-1")
        lines, res = self.wake(4)
        (line,) = [l for l in lines if l["kind"] == "manual"]
        self.assertEqual(line["key"], "PLAT-1")
        (r,) = [r for r in res if r["key"] == "PLAT-1"]
        self.assertEqual((r["decision"], r["session_id"]), ("route", "S1"))
        self.assertEqual(self.sessions()["PLAT-1"]["unseen"], 1)

    def test_a_session_the_manager_asks_for_is_linked_to_its_key(self):
        """The hub skill's steps: add a manual line, decide propose with the slug, stamp it raised (the session was
        started `user_asked` with that name), and the sweep's `session add` adopts it; later lines route to it."""
        self.poll(0)
        self.verb(2, "add", "https://example.com/browse/PLAT-7 needs a look", "--key", "PLAT-7")
        (line,) = [l for l in self.tail() if l["key"] == "PLAT-7"]
        res = hub.decide(self.ws, self.cfg, self.at(2), [{"line_id": line["id"], "key": "PLAT-7",
                                                          "decision": "propose", "name": "plat-7", "title": "Look"}])
        self.assertEqual(res[0]["decision"], "propose")
        self.verb(2, "pending", "raised", "PLAT-7", "--name", "plat-7")
        self.assertTrue(self.hub_json()["pending"][0]["raised_at"])
        self.verb(3, "session", "add", "PLAT-7", "--id", "S7", "--name", "plat-7", "--title", "Look")
        self.assertEqual(self.hub_json()["pending"], [])
        FakeTracker.items.append("PLAT-7")
        self.poll(4)
        _, res = self.wake(4)
        (r,) = [r for r in res if r["key"] == "PLAT-7"]
        self.assertEqual((r["decision"], r["session_id"]), ("route", "S7"))

    def test_a_mentioned_unwatched_project_is_followed_and_polled_next_pass(self):
        self.poll(0)
        self.poll(2, history=[self.msg(2, "WEB-5 is broken again; UTF-8 and ISO-8601 are not projects")])
        auto = hub.load_watch(self.ws)["auto"]["projects"]
        self.assertEqual([a["key"] for a in auto], ["WEB"])
        self.assertEqual(FakeTracker.cfgs[-1]["projects"], ["PLAT"])  # not in the pass that found it
        self.poll(4)
        self.assertEqual(FakeTracker.cfgs[-1]["projects"], ["PLAT", "WEB"])
        self.assertEqual(FakeTracker.cfgs[-1]["auto"], ["WEB"])

    def test_nothing_the_watcher_calls_can_write(self):
        self.poll(0)
        self.poll(2, history=[self.msg(2, "hello")])
        called = {json.loads(l)["name"].rsplit("__", 1)[-1] for l in Path(self.log).read_text().splitlines()}
        self.assertTrue(called <= set(CHAT_TOOLS), called)


class Smoke(unittest.TestCase):
    AT = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
    SECRET = "payroll spreadsheet for Agnes"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"MOTHER_STATE_DIR": os.path.join(self.tmp.name, "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MOTHER_WORKSPACE", None)
        self.fx = os.path.join(self.tmp.name, "fx.json")
        self.log = os.path.join(self.tmp.name, "calls.jsonl")
        self.write(tools=CHAT_TOOLS)
        self.cfg = {"version": 1, "manager": {"email": "dana@example.com", "timezone": "UTC", "chat_dm": ME},
                    "connectors": {"chat": "slack"},
                    "hub": {"enabled": True, "connections": {"chat": conn(self.fx, self.log)},
                            "watch": {"chat": {"channels": [CH]}}}}
        # a workspace that looks real: smoke must leave its hub state alone
        self.real = Path(self.tmp.name) / "real-ws"
        self.real_hub = self.real / ".mother" / "hub"
        self.real_hub.mkdir(parents=True)
        for name in ("inbox.jsonl", "cursors.json", "watch.json", "watcher.json"):
            (self.real_hub / name).write_text("sentinel %s\n" % name)
        (self.real / "config.json").write_text(json.dumps(self.cfg))

    def write(self, tools, errors=None):
        news = {"ts": "%.6f" % (self.AT.timestamp() + 5), "user": "U00000009", "text": self.SECRET}
        Path(self.fx).write_text(json.dumps({
            "tools": [GW + t for t in tools], "errors": {GW + k: v for k, v in (errors or {}).items()},
            "results": {GW + "get_channel_history": {"ok": True, "messages": [news]},
                        GW + "get_thread_replies": {"ok": True, "messages": []},
                        GW + "search-messages": {"ok": True, "messages": {"matches": []}}}}))

    def run_smoke(self):
        out, naps = io.StringIO(), []
        rc = hub.smoke(self.cfg, self.AT, out, sleep=naps.append)
        return rc, out.getvalue(), naps

    def snapshot(self):
        return {p.name: p.read_text() for p in sorted(self.real_hub.iterdir())}

    def test_smoke_uses_a_throwaway_state_dir(self):
        before = self.snapshot()
        made = []
        real_mkdtemp = tempfile.mkdtemp
        with mock.patch("tempfile.mkdtemp", side_effect=lambda *a, **k: made.append(real_mkdtemp(*a, **k)) or made[-1]):
            rc, out, naps = self.run_smoke()
        self.assertEqual(rc, 0, out)
        self.assertEqual(naps, [20])
        self.assertEqual(self.snapshot(), before)  # the real inbox, cursors and watch list are untouched
        self.assertEqual(sorted(os.listdir(self.real)), [".mother", "config.json"])
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))  # the throwaway copy is gone
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "state", "workspace")))

    def test_smoke_prints_no_content(self):
        rc, out, _ = self.run_smoke()
        self.assertEqual(rc, 0, out)
        self.assertNotIn(self.SECRET, out)
        self.assertNotIn("Agnes", out)
        self.assertNotIn("U00000009", out)
        rows = [l.split(" | ") for l in out.splitlines() if l.startswith("chat | ") and len(l.split(" | ")) == 4]
        self.assertEqual(rows, [["chat", "ok", "1", "-"]])  # one new message in the second pass

    def test_smoke_exit_codes(self):
        self.write(tools=CHAT_TOOLS, errors={"search-messages": "weird failure"})  # partial is still readable
        rc, out, _ = self.run_smoke()
        self.assertEqual(rc, 0, out)
        self.assertIn("chat | partial |", out)

        self.write(tools=("get_channel_history",))  # the server lacks read tools: check fails
        rc, out, naps = self.run_smoke()
        self.assertEqual(rc, 2, out)
        self.assertEqual(naps, [])  # no polling pass after a failed check

        self.write(tools=CHAT_TOOLS, errors={"get_channel_history": "connect ECONNREFUSED"})  # passes can't read
        rc, out, _ = self.run_smoke()
        self.assertEqual(rc, 2, out)
        self.assertIn("chat | couldnt_check |", out)

        self.cfg["hub"]["connections"] = {}
        rc, out, _ = self.run_smoke()
        self.assertEqual(rc, 2)

    def test_smoke_verb_is_wired(self):
        buf = io.StringIO()
        with mock.patch.object(hub, "smoke", return_value=2) as sm, mock.patch("sys.stdout", buf):
            rc = hub.main(["smoke"], self.real, self.cfg, self.AT)
        self.assertEqual(rc, 2)
        sm.assert_called_once_with(self.cfg, self.AT)


if __name__ == "__main__":
    unittest.main()
