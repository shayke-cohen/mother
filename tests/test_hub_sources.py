"""Tests for lib/hub_mcp.py and lib/hub_sources.py — read-only client and source adapters.
Fixtures are invented example.com data. Run: python3 -m unittest tests.test_hub_sources -v
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import health  # noqa: E402
import hub_mcp  # noqa: E402
import hub_sources as hs  # noqa: E402
import whatsapp  # noqa: E402
from hub_mcp import McpError as McpErrorAlias  # noqa: E402

_spec = importlib.util.spec_from_file_location("guard", os.path.join(ROOT, "hooks", "guard.py"))
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_mcp.py")
UTC = timezone.utc
AT = datetime(2026, 10, 29, 10, 0, 0, tzinfo=UTC)
NOW = AT.timestamp()
ME = {"email": "dana@example.com", "chat": "U00000000", "code": "danalevi"}


class Fake:
    """In-memory stand-in for an MCP client: handlers map a tool suffix to a function(args) -> result."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.tools = ["gw__" + k for k in handlers]
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.handlers[name.rsplit("__", 1)[-1]](arguments)


def ro(handlers, suffixes):
    f = Fake(handlers)
    return hub_mcp.ReadOnlyClient(f, hub_mcp.resolve_tools(f.tools, suffixes), suffixes), f


class TestClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fx = os.path.join(self.tmp, "fx.json")
        self.log = os.path.join(self.tmp, "calls.jsonl")

    def client(self, fixture, **env):
        Path(self.fx).write_text(json.dumps(fixture))
        e = {"FAKE_MCP_FIXTURE": self.fx, "FAKE_MCP_LOG": self.log}
        e.update(env)
        c = hub_mcp.Client(sys.executable, [FAKE], e, timeout=10)
        self.addCleanup(c.close)
        return c

    def logged(self):
        return [json.loads(l) for l in Path(self.log).read_text().splitlines()] if os.path.exists(self.log) else []

    def test_client_lists_tools_before_call(self):
        c = self.client({"tools": ["gw__get_x", "gw__post_message"], "results": {"gw__get_x": {"a": 1}}})
        c.start()
        self.assertEqual(c.tools, ["gw__get_x", "gw__post_message"])
        self.assertEqual(c.call("gw__get_x", {"q": 1}), {"a": 1})

    def test_error_result_maps_to_kind(self):
        c = self.client({"tools": ["gw__get_x"], "errors": {"gw__get_x": "connect ECONNREFUSED 127.0.0.1"}})
        c.start()
        with self.assertRaises(hub_mcp.McpError) as cm:
            c.call("gw__get_x", {})
        self.assertEqual(cm.exception.kind, "unreachable")
        for msg, kind in (("token expired, sign in", "invalidated"), ("weird failure", "other")):
            self.assertEqual(health.classify(msg)[1], kind)
            self.assertIn(kind, health.CLASSES)
        self.assertIn(cm.exception.kind, health.CLASSES)

    def test_timeout_is_unreachable_and_client_recovers(self):
        c = self.client({"tools": ["gw__get_x", "gw__slow"], "results": {"gw__get_x": {"ok": 1}},
                         "sleep": {"gw__slow": 5}})
        c.timeout = 1
        c.start()
        with self.assertRaises(hub_mcp.McpError) as cm:
            c.call("gw__slow", {})
        self.assertEqual(cm.exception.kind, "unreachable")
        self.assertIn(cm.exception.kind, health.CLASSES)
        self.assertEqual(c.call("gw__get_x", {}), {"ok": 1})  # respawned lazily

    def test_restart_is_per_call_not_per_lifetime(self):
        c = self.client({"tools": ["gw__get_x"], "results": {"gw__get_x": {"ok": 1}}}, FAKE_MCP_DIE_AFTER="1")
        c.start()
        for _ in range(5):  # every second call finds a dead server; each recovers
            self.assertEqual(c.call("gw__get_x", {}), {"ok": 1})

    def test_restarts_once_on_eof(self):
        c = self.client({"tools": ["gw__get_x"], "results": {"gw__get_x": {"ok": 1}}}, FAKE_MCP_DIE_AFTER="1")
        c.start()
        self.assertEqual(c.call("gw__get_x", {}), {"ok": 1})
        self.assertEqual(c.call("gw__get_x", {}), {"ok": 1})  # server exited; one restart serves it

    def test_readonly_refuses_write_tool_without_sending(self):
        c = self.client({"tools": ["gw__get_x", "gw__post_message"], "results": {}})
        c.start()
        r = hub_mcp.ReadOnlyClient(c, hub_mcp.resolve_tools(c.tools, {"get_x"}))
        with self.assertRaises(hub_mcp.ForbiddenTool):
            r.call("gw__post_message", {"text": "hi"})
        with self.assertRaises(hub_mcp.ForbiddenTool):
            r.call("post_message", {"text": "hi"})
        self.assertEqual(self.logged(), [])
        r.call("get_x", {})  # suffix resolves against the allowed full names
        self.assertEqual(len(self.logged()), 1)


class TestReadOnlyMissingTools(unittest.TestCase):
    def test_missing_or_ambiguous_allowed_tool_is_a_source_error_not_forbidden(self):
        f = Fake({"get_channel_history": lambda a: {}})
        r = hub_mcp.ReadOnlyClient(f, hub_mcp.resolve_tools(f.tools, hs.ChatSlack.READ_TOOLS), hs.ChatSlack.READ_TOOLS)
        with self.assertRaises(hub_mcp.McpError) as cm:
            r.call("search-messages", {})
        self.assertIn("search-messages", cm.exception.message)
        with self.assertRaises(hub_mcp.ForbiddenTool):
            r.call("post_message", {})
        with self.assertRaises(hub_mcp.ForbiddenTool):
            r.call("gw__post_message", {})
        f.tools = ["a__get_channel_history", "b__get_channel_history"]
        r = hub_mcp.ReadOnlyClient(f, hub_mcp.resolve_tools(f.tools, hs.ChatSlack.READ_TOOLS), hs.ChatSlack.READ_TOOLS)
        with self.assertRaises(hub_mcp.McpError):
            r.call("get_channel_history", {})
        self.assertEqual(f.calls, [])

    def test_chat_server_without_search_is_partial(self):
        f = Fake({"get_channel_history": lambda a: {"messages": []}, "get_thread_replies": lambda a: {"messages": []}})
        r = hub_mcp.ReadOnlyClient(f, hub_mcp.resolve_tools(f.tools, hs.ChatSlack.READ_TOOLS), hs.ChatSlack.READ_TOOLS)
        out = hs.ChatSlack.poll(r, {"ts": "%.6f" % (NOW - 120), "threads": {}}, {"channels": ["C00000001"]}, ME, AT)
        self.assertTrue(any("search-messages" in p or "other" in p for p in out["partial"]), out["partial"])


class TestAdapterShape(unittest.TestCase):
    def test_every_adapter_read_tools_are_reads(self):
        self.assertEqual(set(hs.ADAPTERS), {"chat:slack", "mail:gmail", "tracker:jira", "calendar:google",
                                            "code:github", "whatsapp:whatsapp"})
        for key, a in hs.ADAPTERS.items():
            if key.startswith("whatsapp:"):
                self.assertEqual(a.READ_TOOLS, set())  # no tools at all: it reads the local database
                continue
            self.assertTrue(a.READ_TOOLS, key)
            for t in a.READ_TOOLS:
                lead = t.replace("-", "_").split("_")[0]
                self.assertTrue(lead in guard.READ_VERBS or lead in ("search", "list", "get"), (key, t))

    def test_first_poll_returns_cursor_only(self):
        for key, a in hs.ADAPTERS.items():
            cap = key.split(":")[0]
            if cap == "whatsapp":
                continue  # TestWhatsApp.test_first_poll_returns_cursor_only
            handlers = {t: (lambda args: {}) for t in a.READ_TOOLS}
            client = hs.GhClient(runner=lambda p: {}) if cap == "code" else ro(handlers, a.READ_TOOLS)[0]
            out = a.poll(client, None, {}, ME, AT)
            self.assertEqual(out["lines"], [], key)
            self.assertIsInstance(out["cursor"], dict, key)


def msg(ts, user="U00000009", text="hello", **kw):
    d = {"ts": "%.6f" % ts, "user": user, "text": text}
    d.update(kw)
    return d


class TestChat(unittest.TestCase):
    def poll(self, history=None, replies=None, search=None, cursor=None, cfg=None, lookback_error=None,
             reply_errors=()):
        cur = cursor or {"ts": "%.6f" % (NOW - 600), "threads": {}}

        def hist(a):  # honours oldest/latest/limit like the real tool
            if "latest" in a and lookback_error:
                raise lookback_error
            got = [m for m in (history or {}).get(a["channel_id"], [])
                   if float(m["ts"]) > float(a.get("oldest", 0)) and float(m["ts"]) <= float(a.get("latest", 1e18))]
            return {"ok": True, "messages": sorted(got, key=lambda m: float(m["ts"]))[:a.get("limit", 100)]}

        def rep(a):
            if a["thread_ts"] in reply_errors:
                raise hub_mcp.McpError("server_error", "boom")
            return {"ok": True, "messages": (replies or {}).get(a["thread_ts"], [])}

        def srch(a):
            if isinstance(search, Exception):
                raise search
            return {"ok": True, "messages": {"matches": (search or {}).get(a["searchText"], [])}}

        client, f = ro({"get_channel_history": hist, "get_thread_replies": rep, "search-messages": srch},
                       hs.ChatSlack.READ_TOOLS)
        return hs.ChatSlack.poll(client, cur, cfg or {"channels": ["C00000001"]}, ME, AT), f

    def test_chat_prefilter(self):
        search = {"<@U00000000>": [{"ts": "%.6f" % (NOW - 60), "user": "U00000005", "text": "ping PLAT-12 pls",
                                    "channel": {"id": "C00000077"}}],
                  "is:dm": [{"ts": "%.6f" % (NOW - 30), "user": "U00000006", "text": "dm",
                             "channel": {"id": "D00000001"}}]}
        out, f = self.poll(history={"C00000001": [msg(NOW - 100, text="watched msg")]}, search=search)
        keys = sorted(l["key"] for l in out["lines"])
        self.assertEqual(len(keys), 3)
        self.assertIn("C00000001/%.6f" % (NOW - 100), keys)
        self.assertTrue(any(k.startswith("C00000077/") for k in keys))
        self.assertTrue(any(k.startswith("D00000001/") for k in keys))
        mention = [l for l in out["lines"] if l["key"].startswith("C00000077")][0]
        self.assertEqual(mention["also"], ["PLAT-12"])
        # an unwatched channel is never read
        self.assertEqual({a["channel_id"] for n, a in f.calls if n.endswith("get_channel_history")}, {"C00000001"})
        self.assertEqual(len([1 for n, a in f.calls if n.endswith("get_channel_history")]), 2)  # pass + lookback

    def test_dm_is_one_thing_and_manager_reply_resolves_it(self):
        search = {"is:dm": [{"ts": "%.6f" % (NOW - 90), "user": "U00000006", "text": "dm", "channel": {"id": "D00000001"}},
                            {"ts": "%.6f" % (NOW - 30), "user": "U00000000", "text": "answered",
                             "channel": {"id": "D00000001"}}]}
        out, _ = self.poll(search=search)
        self.assertEqual([(l["key"], l["kind"]) for l in out["lines"]], [("D00000001/dm", "resolved")])

    def test_search_overlap_and_dedupe(self):
        # cursor is NOW-600; the 10-minute overlap reaches back to NOW-1200
        late = {"ts": "%.6f" % (NOW - 700), "user": "U00000005", "text": "late indexed", "channel": {"id": "C00000077"}}
        old = {"ts": "%.6f" % (NOW - 2000), "user": "U00000005", "text": "old", "channel": {"id": "C00000078"}}
        search = {"<@U00000000>": [late, old]}
        out, f = self.poll(search=search)
        self.assertEqual([l["key"] for l in out["lines"]], ["C00000077/%.6f" % (NOW - 700)])
        sent = [a for n, a in f.calls if n.endswith("search-messages")][0]
        self.assertEqual(sent["after"], "2026-10-28")  # the day before the cursor's date
        again, _ = self.poll(search=search, cursor=out["cursor"])
        self.assertEqual(again["lines"], [])  # the seen-set dedupes the overlap

    def test_history_emitted_message_is_not_re_emitted_by_search(self):
        m = msg(NOW - 100, text="hey <@U00000000>")
        hit = {"ts": m["ts"], "user": m["user"], "text": m["text"], "channel": {"id": "C00000001"}}
        out, _ = self.poll(history={"C00000001": [m]}, search={"<@U00000000>": [hit]})
        self.assertEqual(len(out["lines"]), 1)  # history and search in the same poll: one line
        again, _ = self.poll(search={"<@U00000000>": [hit]}, cursor=out["cursor"])
        self.assertEqual(again["lines"], [])  # next poll's overlap window finds it again: skipped

    def test_thread_the_manager_posted_in_yields_replies(self):
        parent = NOW - 3000
        pkey = "C00000001/%.6f" % parent
        cur = {"ts": "%.6f" % (NOW - 600), "threads": {pkey: parent}}
        out, _ = self.poll(replies={"%.6f" % parent: [msg(parent, user="U00000000"),
                                                       msg(NOW - 120, text="reply", thread_ts="%.6f" % parent)]},
                           cursor=cur)
        self.assertEqual([(l["key"], l["kind"]) for l in out["lines"]], [(pkey, "update")])

    def _parent(self, ts, latest, n=1, **kw):
        return msg(ts, reply_count=n, latest_reply="%.6f" % latest, **kw)

    def test_new_reply_in_unfollowed_thread_is_read(self):
        parent = NOW - 3000  # older than the cursor: found by the lookback read
        p = self._parent(parent, NOW - 120)
        r = msg(NOW - 120, text="reply", thread_ts="%.6f" % parent)
        out, f = self.poll(history={"C00000001": [p]}, replies={"%.6f" % parent: [p, r]})
        self.assertEqual([(l["key"], l["kind"]) for l in out["lines"]], [("C00000001/%.6f" % parent, "update")])
        main, back = [a for n, a in f.calls if n.endswith("get_channel_history")]
        self.assertNotIn("latest", main)
        self.assertEqual(float(back["latest"]), NOW - 600)  # the cursor
        self.assertAlmostEqual(float(back["oldest"]), AT.timestamp() - 7 * 86400, places=3)
        self.assertNotIn("C00000001/%.6f" % parent, out["cursor"]["threads"])  # not added to the followed set
        mine = msg(NOW - 60, user="U00000000", text="ok", thread_ts="%.6f" % parent)
        out, _ = self.poll(history={"C00000001": [p]}, replies={"%.6f" % parent: [p, r, mine]})
        self.assertIn("resolved", [l["kind"] for l in out["lines"]])

    def test_old_threads_not_reread(self):
        parent = NOW - 3000
        p = self._parent(parent, NOW - 2000)
        out, f = self.poll(history={"C00000001": [p]}, replies={"%.6f" % parent: [p]})
        self.assertEqual(out["lines"], [])
        self.assertEqual([n for n, a in f.calls if n.endswith("get_thread_replies")], [])
        p = self._parent(parent, NOW - 120)
        r = msg(NOW - 120, thread_ts="%.6f" % parent)
        out, _ = self.poll(history={"C00000001": [p]}, replies={"%.6f" % parent: [p, r]})
        again, f = self.poll(history={"C00000001": [p]}, replies={"%.6f" % parent: [p, r]}, cursor=out["cursor"])
        self.assertEqual(again["lines"], [])  # the seen-set dedupes a thread read again within the overlap

    def test_thread_cap_marks_partial(self):
        hist, reps = [], {}
        for i in range(12):
            parent = NOW - 3000 - i
            hist.append(self._parent(parent, NOW - 100 - i))
            reps["%.6f" % parent] = [msg(NOW - 100 - i, thread_ts="%.6f" % parent)]
        out, f = self.poll(history={"C00000001": hist}, replies=reps)
        self.assertEqual(len([1 for n, a in f.calls if n.endswith("get_thread_replies")]), 10)
        self.assertEqual(len(out["lines"]), 10)
        self.assertIn("more than 10 active threads in C00000001", out["partial"])
        newest = {"C00000001/%.6f" % (NOW - 3000 - i) for i in range(10)}
        self.assertEqual({l["key"] for l in out["lines"]}, newest)  # most recent latest_reply first

    def test_lookback_truncated_is_a_note_not_partial(self):
        page = [msg(NOW - 5000 - i) for i in range(101)]
        out, _ = self.poll(history={"C00000001": page})
        self.assertEqual(out["notes"], ["C00000001: lookback truncated"])
        self.assertNotIn("C00000001: lookback truncated", out["partial"])

    def test_lookback_failure_is_partial_and_pass_goes_on(self):
        out, _ = self.poll(history={"C00000001": [msg(NOW - 100, text="new")]},
                           lookback_error=hub_mcp.McpError("server_error", "boom"))
        self.assertEqual(len(out["lines"]), 1)
        self.assertEqual(out["partial"], ["C00000001: lookback server_error"])

    def test_unreadable_thread_is_partial_others_still_read(self):
        hist, reps = [], {}
        for i in range(2):
            parent = NOW - 3000 - i
            hist.append(self._parent(parent, NOW - 100 - i))
            reps["%.6f" % parent] = [msg(NOW - 100 - i, thread_ts="%.6f" % parent)]
        bad = "%.6f" % (NOW - 3000)
        out, _ = self.poll(history={"C00000001": hist}, replies=reps, reply_errors=(bad,))
        self.assertEqual(len(out["lines"]), 1)
        self.assertEqual(out["partial"], ["C00000001/%s: replies unreadable" % bad])

    def test_followed_thread_always_read(self):
        parent = NOW - 3000
        pkey = "C00000001/%.6f" % parent
        r = msg(NOW - 120, thread_ts="%.6f" % parent)
        out, _ = self.poll(replies={"%.6f" % parent: [msg(parent), r]},
                           cfg={"channels": ["C00000001"], "follow": [pkey]})
        self.assertEqual([l["key"] for l in out["lines"]], [pkey])

    def test_own_activity_is_resolved_not_candidate(self):
        out, _ = self.poll(history={"C00000001": [msg(NOW - 100, user="U00000000", text="on it")]})
        self.assertEqual([l["kind"] for l in out["lines"]], ["resolved"])
        self.assertIn("C00000001/%.6f" % (NOW - 100), out["cursor"]["threads"])  # later replies are followed

    def test_search_scope_error_makes_source_partial_not_failed(self):
        out, _ = self.poll(history={"C00000001": [msg(NOW - 100)]},
                           search=hub_mcp.McpError("missing_scope", "search:read"))
        self.assertEqual(len(out["lines"]), 1)
        self.assertEqual(len(out["partial"]), 2)

    def test_two_messages_in_one_second_keep_distinct_ids(self):
        out, _ = self.poll(history={"C00000001": [msg(NOW - 100.5, text="a", thread_ts="%.6f" % (NOW - 900)),
                                                  msg(NOW - 100.2, text="b", thread_ts="%.6f" % (NOW - 900))]})
        lines = out["lines"]
        self.assertEqual(len(lines), 1)  # one thread: coalesced to the later one
        self.assertEqual(lines[0]["id"], "chat:C00000001/%.6f:%.6f" % (NOW - 900, NOW - 100.2))
        a = hub_line_ids = [hs.hub.make_line("chat", "update", "C/1", "2026-10-29T10:00:00Z", uid=t)["id"]
                            for t in ("1761732000.000100", "1761732000.000900")]
        self.assertEqual(len(set(hub_line_ids)), 2, a)

    def test_full_history_page_is_partial(self):
        page = [msg(NOW - 500 + i) for i in range(101)]
        out, _ = self.poll(history={"C00000001": page})
        self.assertTrue(any("more than 100" in p for p in out["partial"]), out["partial"])

    def test_own_signed_posts_do_not_wake_the_hub(self):
        me = dict(ME, marks=["Agnes (AI)"])
        signed = msg(NOW - 100, user="U00000000", text=":sparkles: *Agnes (AI)* · on behalf of Dana\nshipped")
        bot = msg(NOW - 90, user="", text=":sparkles: *Agnes (AI)* · on behalf of Dana\nhi", bot_id="B1")
        human = msg(NOW - 80, user="U00000009", text="what did Agnes (AI) mean?")
        client, _ = ro({"get_channel_history": lambda a: {"messages": [signed, bot, human]},
                        "get_thread_replies": lambda a: {"messages": []},
                        "search-messages": lambda a: {"messages": {"matches": []}}}, hs.ChatSlack.READ_TOOLS)
        out = hs.ChatSlack.poll(client, {"ts": "%.6f" % (NOW - 600), "threads": {}}, {"channels": ["C00000001"]},
                                me, AT)
        self.assertEqual([(l["kind"], l["summary"]) for l in out["lines"]], [("update", "what did Agnes (AI) mean?")])

    def test_summary_is_trimmed(self):
        out, _ = self.poll(history={"C00000001": [msg(NOW - 100, text="x" * 900)]})
        self.assertEqual(len(out["lines"][0]["summary"]), 300)


def mail_msg(i, sender="Sam <sam@example.com>", subj="Question", labels=("INBOX",), thread=None):
    return {"id": i, "threadId": thread or "t" + i, "labelIds": list(labels), "snippet": "snip",
            "internalDate": str(int((NOW - 60) * 1000)),
            "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subj}]}}


class TestMail(unittest.TestCase):
    def test_mail_prefilter_ignores_noise_senders(self):
        msgs = {"a": mail_msg("a", subj="Re PLAT-9 rollout"),
                "b": mail_msg("b", sender="Bot <noreply@example.com>"),
                "c": mail_msg("c", labels=("INBOX", "Newsletters")),
                "d": mail_msg("d", sender="Dana <dana@example.com>")}

        def lst(a):
            if a["query"].startswith("from:"):
                return {"messages": [{"id": "s1", "threadId": "tsent"}]}
            return {"messages": [{"id": k, "threadId": "t" + k} for k in msgs]}

        sent = mail_msg("s1", sender="Dana <dana@example.com>", thread="tsent")
        client, f = ro({"list-emails": lst, "read-email": lambda a: msgs.get(a["messageId"]) or sent},
                       hs.MailGmail.READ_TOOLS)
        cfg = {"ignore_senders": ["noreply@"], "ignore_labels": ["Newsletters"]}
        out = hs.MailGmail.poll(client, {"since": int(NOW - 600), "seen": []}, cfg, ME, AT)
        by = {l["key"]: l for l in out["lines"]}
        self.assertEqual(set(by), {"mail:ta", "mail:tsent"})
        self.assertEqual(by["mail:ta"]["kind"], "update")
        self.assertEqual(by["mail:ta"]["also"], ["PLAT-9"])
        self.assertEqual(by["mail:tsent"]["kind"], "resolved")
        self.assertIn("-label", f.calls[0][1]["query"])
        out2 = hs.MailGmail.poll(client, out["cursor"], cfg, ME, AT)  # already-seen ids are not re-read
        self.assertEqual([l["key"] for l in out2["lines"]], ["mail:tsent"])
        self.assertEqual(by["mail:ta"]["link"], "https://mail.google.com/mail/u/0/#all/ta")

    def test_sent_reply_does_not_swallow_newer_mail(self):
        # the manager replied at 09:58:30; a new message arrived in the same thread at 09:59:30, same poll
        def at_ms(sec):
            return str(int((NOW - sec) * 1000))
        inbound = dict(mail_msg("m1", thread="T1"), internalDate=at_ms(30))
        sent = dict(mail_msg("s1", sender="Dana <dana@example.com>", thread="T1"), internalDate=at_ms(90))

        def lst(a):
            return {"messages": [{"id": "s1", "threadId": "T1"}] if a["query"].startswith("from:")
                    else [{"id": "m1", "threadId": "T1"}]}
        client, _ = ro({"list-emails": lst, "read-email": lambda a: {"m1": inbound, "s1": sent}[a["messageId"]]},
                       hs.MailGmail.READ_TOOLS)
        out = hs.MailGmail.poll(client, {"since": int(NOW - 600), "seen": []}, {}, ME, AT)
        self.assertEqual([(l["kind"], l["at"]) for l in out["lines"]], [("update", hs.ts_iso(NOW - 30))])
        # the reply after the newest inbound message does resolve it
        sent["internalDate"] = at_ms(10)
        out = hs.MailGmail.poll(client, {"since": int(NOW - 600), "seen": []}, {}, ME, AT)
        self.assertEqual([(l["kind"], l["at"]) for l in out["lines"]], [("resolved", hs.ts_iso(NOW - 10))])

    def test_full_mail_page_is_partial(self):
        msgs = {"m%d" % i: mail_msg("m%d" % i) for i in range(hs.MAX_MAIL + 1)}

        def lst(a):
            return {"messages": [] if a["query"].startswith("from:") else [{"id": k, "threadId": "t" + k} for k in msgs]}
        client, _ = ro({"list-emails": lst, "read-email": lambda a: msgs[a["messageId"]]}, hs.MailGmail.READ_TOOLS)
        out = hs.MailGmail.poll(client, {"since": int(NOW - 600), "seen": []}, {}, ME, AT)
        self.assertEqual(out["partial"], ["more than %d new messages" % hs.MAX_MAIL])
        self.assertEqual(len(out["lines"]), hs.MAX_MAIL)


def issue(key, status="To Do", assignee="acc1", prio="Medium", ncomments=0, labels=(), created="2026-01-01T00:00:00.000+0000",
          last_author="sam@example.com"):
    comments = [{"author": {"emailAddress": last_author, "displayName": "Sam"}}] * ncomments
    return {"key": key, "self": "https://jira.example.com/rest/api/3/issue/10001",
            "fields": {"summary": "Thing " + key, "status": {"name": status}, "assignee": {"accountId": assignee},
                       "priority": {"name": prio}, "created": created, "updated": "2026-10-29T09:58:00.000+0000",
                       "labels": list(labels), "comment": {"total": ncomments, "comments": comments}}}


class TestTracker(unittest.TestCase):
    def run_poll(self, issues, fp, cfg=None, handler=None):
        def get_issues(a):  # the real read tool requires projectKey
            if not a.get("projectKey"):
                raise hub_mcp.McpError("other", "Invalid arguments for `get-issues`: Missing required parameter `projectKey`.")
            return handler(a) if handler else {"issues": issues}
        client, f = ro({"get-issues": get_issues, "get-issue-changelog": lambda a: {"values": []}},
                       hs.TrackerJira.READ_TOOLS)
        cur = {"since": "2026-10-29T09:50:00Z", "fp": fp}
        return hs.TrackerJira.poll(client, cur, cfg or {"projects": ["PLAT"], "extra_jql": "labels != x"}, ME, AT), f

    def test_polls_once_per_project_with_project_key(self):
        def h(a):
            return {"issues": [issue(a["projectKey"] + "-1", status="Done")]}
        out, f = self.run_poll([], {"PLAT-1": dict(self.BASE), "OPS-1": dict(self.BASE)},
                               cfg={"projects": ["PLAT", "OPS", "PLAT"]}, handler=h)
        self.assertEqual([c[1]["projectKey"] for c in f.calls if c[0].endswith("get-issues")], ["PLAT", "OPS"])
        self.assertIn("project = OPS", f.calls[1][1]["jql"])
        self.assertEqual(sorted(l["key"] for l in out["lines"]), ["OPS-1", "PLAT-1"])
        self.assertEqual(out["partial"], [])

    def test_one_failing_project_is_partial_others_advance(self):
        def h(a):
            if a["projectKey"] == "BAD":
                raise hub_mcp.McpError("other", "no such project")
            return {"issues": [issue("OPS-1", status="Done")]}
        out, f = self.run_poll([], {"OPS-1": dict(self.BASE), "BAD-9": dict(self.BASE)},
                               cfg={"projects": ["BAD", "OPS"]}, handler=h)
        self.assertEqual([l["key"] for l in out["lines"]], ["OPS-1"])
        self.assertEqual(len(out["partial"]), 1)
        self.assertIn("BAD", out["partial"][0])
        self.assertEqual(out["cursor"]["fp"]["OPS-1"]["status"], "Done")
        self.assertIn("BAD-9", out["cursor"]["fp"])  # the failed project's fingerprints are kept

    def changelog_poll(self, handler, me=None, issues=None):
        def get_issues(a):
            return {"issues": issues or [issue("PLAT-1", status="Done")]}
        client, f = ro({"get-issues": get_issues, "get-issue-changelog": handler}, hs.TrackerJira.READ_TOOLS)
        cur = {"since": "2026-10-29T09:50:00Z", "fp": {"PLAT-1": dict(self.BASE)}}
        return hs.TrackerJira.poll(client, cur, {"projects": ["PLAT"]}, me or ME, AT), f

    @staticmethod
    def history(created, author, field="status"):
        return {"created": created, "author": author, "items": [{"field": field}]}

    def test_own_status_change_is_resolved(self):
        mine = {"accountId": "acc9", "emailAddress": "dana@example.com"}
        theirs = {"accountId": "acc2", "emailAddress": "sam@example.com"}
        h = lambda a: {"total": 3, "values": [self.history("2026-10-29T09:41:00.000+0000", theirs),
                                  self.history("2026-10-29T09:55:00.000+0000", mine),
                                  self.history("2026-10-29T09:56:00.000+0000", theirs, "labels")]}
        out, f = self.changelog_poll(h)
        self.assertEqual([(l["key"], l["kind"]) for l in out["lines"]], [("PLAT-1", "resolved")])
        self.assertEqual([c[1]["issueKey"] for c in f.calls if c[0].endswith("changelog")], ["PLAT-1"] * 2)
        # by account id alone
        h2 = lambda a: {"total": 1, "values": [self.history("2026-10-29T09:55:00.000+0000", {"accountId": "acc9"})]}
        out, _ = self.changelog_poll(h2, me=dict(ME, email="", tracker_account="acc9"))
        self.assertEqual(out["lines"][0]["kind"], "resolved")

    def paged(self, entries, total=True):
        calls = []

        def h(a):
            calls.append(a)
            start, n = a.get("startAt", 0), a["maxResults"]
            out = {"values": entries[start:start + n], "isLast": start + n >= len(entries)}
            if total:
                out["total"] = len(entries)
            return out
        return h, calls

    def test_latest_change_read_from_last_page(self):
        mine = {"accountId": "acc9", "emailAddress": "dana@example.com"}
        theirs = {"accountId": "acc2", "emailAddress": "sam@example.com"}
        ents = [self.history("2026-10-0%dT09:00:00.000+0000" % (1 + n // 40), theirs, "labels") for n in range(118)]
        ents += [self.history("2026-10-29T09:41:00.000+0000", theirs), self.history("2026-10-29T09:55:00.000+0000", mine)]
        h, calls = self.paged(ents)
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "resolved")
        self.assertEqual([(c["startAt"], c["maxResults"]) for c in calls], [(0, 1), (70, 50)])
        # an old own edit on page 1, the latest change is someone else's
        ents2 = [self.history("2026-10-01T09:00:00.000+0000", mine)] + ents[:118] + \
            [self.history("2026-10-29T09:55:00.000+0000", theirs)]
        h, _ = self.paged(ents2)
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "update")

    def test_no_total_pages_until_last_capped(self):
        mine = {"accountId": "acc9", "emailAddress": "dana@example.com"}
        ents = [self.history("2026-10-01T09:00:00.000+0000", {"accountId": "x"}, "labels")] * 130
        ents.append(self.history("2026-10-29T09:55:00.000+0000", mine))
        h, calls = self.paged(ents, total=False)
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "resolved")
        self.assertEqual(len(calls), 3)

    def test_capped_or_unterminated_history_stays_update(self):
        mine = {"accountId": "acc9", "emailAddress": "dana@example.com"}
        old_own = self.history("2026-10-01T09:00:00.000+0000", mine)
        filler = self.history("2026-10-02T09:00:00.000+0000", {"accountId": "x"}, "labels")
        h, calls = self.paged([old_own] + [filler] * 600, total=False)  # more than 5 pages
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "update")
        self.assertEqual(len(calls), 6)
        base, _ = self.paged([old_own], total=False)  # no isLast in the answer
        h2 = lambda a: {k: v for k, v in base(a).items() if k != "isLast"}
        out, _ = self.changelog_poll(h2)
        self.assertEqual(out["lines"][0]["kind"], "update")

    def test_offsets_are_normalised(self):
        mine = {"accountId": "acc9", "emailAddress": "dana@example.com"}
        theirs = {"accountId": "acc2", "emailAddress": "sam@example.com"}
        # 12:30+0300 is 09:30Z, before the cursor (09:50Z) even though it sorts later than 09:55+0000 as text
        h = lambda a: {"total": 2, "values": [self.history("2026-10-29T09:55:00.000+0000", mine),
                                              self.history("2026-10-29T12:30:00.000+0300", theirs)]}
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "resolved")
        self.assertEqual(hs.parse_iso("2026-10-29T12:30:00.000+0300"), hs.parse_iso("2026-10-29T09:30:00Z"))

    def test_any_others_change_since_the_cursor_is_update(self):
        mine = {"accountId": "acc9", "emailAddress": "dana@example.com"}
        theirs = {"accountId": "acc2", "emailAddress": "sam@example.com"}
        # the manager changed it last, but someone else changed a watched field after the cursor too
        h = lambda a: {"total": 2, "values": [self.history("2026-10-29T09:52:00.000+0000", theirs, "assignee"),
                                              self.history("2026-10-29T09:55:00.000+0000", mine)]}
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "update")
        # only the manager's changes since the cursor (two of them): resolved
        h = lambda a: {"total": 2, "values": [self.history("2026-10-29T09:52:00.000+0000", mine, "priority"),
                                              self.history("2026-10-29T09:55:00.000+0000", mine)]}
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "resolved")
        # no watched change since the cursor at all: nothing to attribute, so it stays an update
        h = lambda a: {"total": 1, "values": [self.history("2026-10-29T09:40:00.000+0000", mine)]}
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "update")

    def test_others_change_is_update(self):
        mine = {"accountId": "acc9", "emailAddress": "dana@example.com"}
        theirs = {"accountId": "acc2", "emailAddress": "sam@example.com"}
        h = lambda a: {"total": 2, "values": [self.history("2026-10-29T09:51:00.000+0000", mine),
                                  self.history("2026-10-29T09:55:00.000+0000", theirs)]}
        out, _ = self.changelog_poll(h)
        self.assertEqual(out["lines"][0]["kind"], "update")

    def test_changelog_failure_falls_back_to_update(self):
        def boom(a):
            raise hub_mcp.McpError("other", "nope")
        out, _ = self.changelog_poll(boom)
        self.assertEqual([l["kind"] for l in out["lines"]], ["update"])
        out, _ = self.changelog_poll(lambda a: "garbage")
        self.assertEqual([l["kind"] for l in out["lines"]], ["update"])

    def test_no_changelog_call_for_label_only_or_unchanged(self):
        client, f = ro({"get-issues": lambda a: {"issues": [issue("PLAT-1", labels=["x"])]},
                        "get-issue-changelog": lambda a: {}}, hs.TrackerJira.READ_TOOLS)
        hs.TrackerJira.poll(client, {"since": "2026-10-29T09:50:00Z", "fp": {"PLAT-1": dict(self.BASE)}},
                            {"projects": ["PLAT"]}, ME, AT)
        self.assertEqual([c for c in f.calls if c[0].endswith("changelog")], [])

    def test_auto_project_failure_counter_and_drop(self):
        broken = []

        def h(a):
            if a["projectKey"] in broken:
                raise hub_mcp.McpError("other", "no such project")
            return {"issues": []}
        client, _ = ro({"get-issues": h}, hs.TrackerJira.READ_TOOLS)
        cfg = {"projects": ["WEB", "PLAT"], "auto": ["WEB"]}
        cur = {"since": "2026-10-29T09:50:00Z", "fp": {}}
        out = hs.TrackerJira.poll(client, cur, cfg, ME, AT)  # read once: from now on its failures count
        self.assertEqual(out["cursor"]["auto_read"], ["WEB"])
        broken.append("WEB")
        out = hs.TrackerJira.poll(client, out["cursor"], cfg, ME, AT)
        self.assertEqual(out["cursor"]["fails"], {"WEB": 1})
        self.assertNotIn("drop_auto", out)
        out = hs.TrackerJira.poll(client, out["cursor"], cfg, ME, AT)
        self.assertEqual(out["drop_auto"], ["WEB"])
        self.assertEqual([(l["kind"], l["key"]) for l in out["lines"]], [("error", "watcher:tracker:WEB")])
        self.assertEqual(out["cursor"]["fails"], {})
        # a configured project never counts
        out = hs.TrackerJira.poll(client, {"since": cur["since"], "fp": {}}, {"projects": ["WEB"]}, ME, AT)
        self.assertEqual(out["cursor"]["fails"], {})

    def test_auto_project_never_read_is_dropped_silently(self):
        def h(a):
            if a["projectKey"] == "WEB":
                raise hub_mcp.McpError("other", "no such project")
            return {"issues": []}
        client, _ = ro({"get-issues": h}, hs.TrackerJira.READ_TOOLS)
        out = hs.TrackerJira.poll(client, {"since": "2026-10-29T09:50:00Z", "fp": {}},
                                  {"projects": ["WEB", "PLAT"], "auto": ["WEB"]}, ME, AT)
        self.assertEqual(out["drop_auto"], ["WEB"])
        self.assertEqual((out["lines"], out["partial"]), ([], []))
        self.assertEqual(out["cursor"]["fails"], {})

    def projects_poll(self, handler, cursor=None, at=AT):
        client, f = ro({"get-issues": lambda a: {"issues": []}, "list-projects": handler}, hs.TrackerJira.READ_TOOLS)
        cur = cursor if cursor is not None else {"since": "2026-10-29T09:50:00Z", "fp": {}}
        return hs.TrackerJira.poll(client, cur, {"projects": ["PLAT"]}, ME, at), f

    def test_real_project_keys_are_read_paged_and_cached_for_a_day(self):
        def h(a):
            start = a["startAt"]
            vals = [{"key": "P%03d" % n} for n in range(start, min(start + a["maxResults"], 150))]
            return {"values": vals, "total": 150, "isLast": start + a["maxResults"] >= 150}
        out, f = self.projects_poll(h)
        self.assertEqual(len(out["known_projects"]), 150)
        self.assertEqual([c[1]["startAt"] for c in f.calls if c[0].endswith("list-projects")], [0, 100])
        self.assertEqual(out["cursor"]["projects"]["at"], "2026-10-29T10:00:00Z")
        later, f2 = self.projects_poll(h, out["cursor"], AT + timedelta(hours=23))
        self.assertEqual(len(later["known_projects"]), 150)
        self.assertEqual([c for c in f2.calls if c[0].endswith("list-projects")], [])  # from the cache
        nextday, f3 = self.projects_poll(lambda a: [{"key": "PLAT"}], out["cursor"], AT + timedelta(hours=25))
        self.assertEqual(nextday["known_projects"], ["PLAT"])
        self.assertEqual(len([c for c in f3.calls if c[0].endswith("list-projects")]), 1)

    def test_failed_project_list_gives_none_and_keeps_the_old_cache(self):
        def boom(a):
            raise hub_mcp.McpError("other", "nope")
        old = {"since": "2026-10-29T09:50:00Z", "fp": {}, "projects": {"at": "2026-10-27T10:00:00Z", "keys": ["PLAT"]}}
        out, _ = self.projects_poll(boom, old)
        self.assertIsNone(out["known_projects"])
        self.assertEqual(out["cursor"]["projects"], old["projects"])
        self.assertEqual(out["partial"], [])
        out, _ = self.projects_poll(lambda a: "garbage")
        self.assertIsNone(out["known_projects"])
        # a server without the tool: none, and the source still polls
        out, _ = TestTracker.run_poll(self, [], {})
        self.assertIsNone(out["known_projects"])

    def test_no_projects_is_a_config_gap(self):
        self.assertEqual(hs.TrackerJira.gap({"projects": []}), "no tracker projects configured")
        self.assertIsNone(hs.TrackerJira.gap({"projects": ["PLAT"]}))

    def test_chat_search_scope_error_is_named(self):
        def search(a):
            raise hub_mcp.McpError("other", "An API error occurred: missing_scope")
        client, _ = ro({"get_channel_history": lambda a: {"messages": []}, "get_thread_replies": lambda a: {},
                        "search-messages": search}, hs.ChatSlack.READ_TOOLS)
        out = hs.ChatSlack.poll(client, {"ts": "%.6f" % (NOW - 600), "threads": {}}, {"channels": []}, ME, AT)
        self.assertEqual(out["partial"], ["mentions: missing scope", "DMs: missing scope"])

    BASE = {"status": "To Do", "assignee": "acc1", "priority": "Medium", "comments": 0}

    def test_tracker_change_fields(self):
        cases = [("status", issue("PLAT-1", status="Done")), ("assignee", issue("PLAT-1", assignee="acc2")),
                 ("priority", issue("PLAT-1", prio="High")), ("comment", issue("PLAT-1", ncomments=1))]
        for name, i in cases:
            out, _ = self.run_poll([i], {"PLAT-1": dict(self.BASE)})
            self.assertEqual([l["key"] for l in out["lines"]], ["PLAT-1"], name)
            self.assertEqual(out["lines"][0]["link"], "https://jira.example.com/browse/PLAT-1")
        out, _ = self.run_poll([issue("PLAT-1", labels=["new-label"])], {"PLAT-1": dict(self.BASE)})
        self.assertEqual(out["lines"], [])  # label-only change

    def test_full_tracker_page_is_partial(self):
        out, _ = self.run_poll([issue("PLAT-%d" % n) for n in range(51)], {})
        self.assertEqual(out["partial"], ["more than 50 updated issues"])
        self.assertEqual(len(out["lines"]), 50)

    def test_jql_covers_scope_and_extra(self):
        _, f = self.run_poll([], {})
        jql = f.calls[0][1]["jql"]
        for part in ("assignee = currentUser()", "reporter = currentUser()", "watcher = currentUser()",
                     "project = PLAT", "labels != x", "updated >= -"):
            self.assertIn(part, jql)

    def test_own_comment_is_resolved(self):
        out, _ = self.run_poll([issue("PLAT-1", ncomments=1, last_author="dana@example.com")],
                               {"PLAT-1": dict(self.BASE)})
        self.assertEqual([l["kind"] for l in out["lines"]], ["resolved"])

    def test_unseen_issue_touched_since_cursor_is_candidate(self):
        # newly assigned to the manager: old issue, first sight, updated after the cursor
        out, _ = self.run_poll([issue("PLAT-2")], {})
        self.assertEqual([l["key"] for l in out["lines"]], ["PLAT-2"])
        stale = issue("PLAT-4")
        stale["fields"]["updated"] = "2026-10-29T09:00:00.000+0000"
        out, _ = self.run_poll([stale], {})
        self.assertEqual(out["lines"], [])
        self.assertIn("PLAT-4", out["cursor"]["fp"])

    def test_fingerprints_pruned_by_least_recently_updated(self):
        fp = {"K-%d" % n: dict(self.BASE, u="2026-10-%02dT00:00:00.000+0000" % (n % 28 + 1)) for n in range(600)}
        fp["OLDEST-INSERTED"] = dict(self.BASE, u="2026-10-29T09:59:00.000+0000")
        out, _ = self.run_poll([], dict(list({"OLDEST-INSERTED": fp.pop("OLDEST-INSERTED")}.items()) + list(fp.items())))
        self.assertEqual(len(out["cursor"]["fp"]), 500)
        self.assertIn("OLDEST-INSERTED", out["cursor"]["fp"])  # first inserted, but recently updated


def event(eid, start_min=None, updated="2026-10-29T09:58:00.000Z", resp="needsAction", organizer_self=False,
          status="confirmed"):
    ev = {"id": eid, "status": status, "summary": "Sync", "updated": updated, "htmlLink": "https://cal.example.com/" + eid,
          "organizer": {"email": "sam@example.com", "self": organizer_self},
          "attendees": [{"email": "dana@example.com", "self": True, "responseStatus": resp}]}
    if start_min is not None:
        ev["start"] = {"dateTime": datetime.fromtimestamp(NOW + start_min * 60, UTC).isoformat()}
    return ev


class TestCalendar(unittest.TestCase):
    def poll(self, events, cursor=None, cfg=None):
        client, _ = ro({"list-events": lambda a: {"items": events}}, hs.CalendarGoogle.READ_TOOLS)
        cur = cursor or {"since": "2026-10-29T09:50:00Z", "resp": {}, "notified": []}
        return hs.CalendarGoogle.poll(client, cur, cfg or {"lead_minutes": 10}, ME, AT)

    def test_calendar_new_invite_and_starting_soon(self):
        out = self.poll([event("ev1", start_min=300), event("ev2", start_min=5, updated="2026-10-01T00:00:00Z",
                                                          resp="accepted"),
                         event("ev3", start_min=60, updated="2026-10-01T00:00:00Z")])
        by = {l["key"]: l["summary"] for l in out["lines"]}
        self.assertEqual(by["cal:ev1"], "new invite")
        self.assertTrue(by["cal:ev2"].startswith("starts at"))
        self.assertNotIn("cal:ev3", by)
        again = self.poll([event("ev2", start_min=5, updated="2026-10-01T00:00:00Z", resp="accepted")],
                          cursor=out["cursor"])
        self.assertEqual(again["lines"], [])  # starting-soon fires once per event start

    def test_expanded_recurring_instance_starts_soon(self):
        inst = event("ev9_20261029T100500Z", start_min=5, updated="2026-01-01T00:00:00Z", resp="accepted")
        inst["recurringEventId"] = "ev9"
        out = self.poll([inst])
        self.assertEqual([l["key"] for l in out["lines"]], ["cal:ev9_20261029T100500Z"])

    def test_first_sight_already_answered_is_resolved(self):
        out = self.poll([event("ev5", start_min=300, resp="accepted")])
        self.assertEqual([(l["key"], l["kind"]) for l in out["lines"]], [("cal:ev5", "resolved")])

    def test_cancellation_and_own_response(self):
        out = self.poll([event("ev1", start_min=300, status="cancelled")],
                        cursor={"since": "2026-10-29T09:50:00Z", "resp": {"ev1": "accepted"}, "notified": []})
        self.assertEqual(out["lines"][0]["summary"], "cancelled")
        out = self.poll([event("ev2", start_min=300, resp="accepted")],
                        cursor={"since": "2026-10-29T09:50:00Z", "resp": {"ev2": "needsAction"}, "notified": []})
        self.assertEqual([l["kind"] for l in out["lines"]], ["resolved"])


class TestCode(unittest.TestCase):
    def runner(self, table):
        self.paths = []

        def run(path):
            self.paths.append(path)
            for k, v in table.items():
                if path.startswith(k):
                    return v
            return {}
        return hs.GhClient(runner=run)

    def test_review_requested_and_comments_and_ci(self):
        pr = {"number": 45, "title": "Fix", "updated_at": "2026-10-29T09:58:00Z", "user": {"login": "samroe"},
              "repository_url": "https://api.example.com/repos/example-org/platform-api",
              "html_url": "https://github.example.com/example-org/platform-api/pull/45"}
        mine = dict(pr, number=46)
        c = self.runner({
            "search/issues?per_page=30&sort=updated&q=is:pr+is:open+review-requested": {"items": [pr]},
            "search/issues?per_page=30&sort=updated&q=is:pr+is:open+author": {"items": [mine]},
            "repos/example-org/platform-api/issues/46/comments": [
                {"created_at": "2026-10-29T09:57:00Z", "user": {"login": "samroe"}, "body": "nit"},
                {"created_at": "2026-10-29T09:58:00Z", "user": {"login": "danalevi"}, "body": "fixed"}],
            "repos/example-org/platform-api/pulls/46/comments": [],
            "repos/example-org/platform-api/commits/main/check-runs": {"check_runs": [
                {"name": "build", "conclusion": "failure", "head_sha": "abc"}]},
            "repos/example-org/platform-api": {"default_branch": "main"}})
        cur = {"since": "2026-10-29T09:50:00Z", "ci": {}, "branch": {}}
        out = hs.CodeGithub.poll(c, cur, {"repos": ["example-org/platform-api"]}, ME, AT)
        kinds = {(l["key"], l["kind"]) for l in out["lines"]}
        self.assertIn(("example-org/platform-api#45", "update"), kinds)
        keys = {l["key"] for l in out["lines"]}
        self.assertEqual(keys, {"example-org/platform-api#45", "example-org/platform-api#46",
                                "example-org/platform-api#ci-main"})
        # same red sha is not reported twice
        again = hs.CodeGithub.poll(c, out["cursor"], {"repos": ["example-org/platform-api"]}, ME, AT)
        self.assertNotIn("example-org/platform-api#ci-main", {l["key"] for l in again["lines"]})

    def test_left_review_requested_set_is_resolved(self):
        pr = {"number": 45, "title": "Fix", "updated_at": "2026-10-29T09:00:00Z", "user": {"login": "samroe"},
              "repository_url": "https://api.example.com/repos/example-org/platform-api"}
        c = self.runner({"search/issues?per_page=30&sort=updated&q=is:pr+is:open+review-requested": {"items": []}})
        cur = {"since": "2026-10-29T09:50:00Z", "ci": {}, "branch": {}, "rr": {"example-org/platform-api#45": "Fix"}}
        out = hs.CodeGithub.poll(c, cur, {}, ME, AT)
        self.assertEqual([(l["key"], l["kind"]) for l in out["lines"]], [("example-org/platform-api#45", "resolved")])
        self.assertEqual(out["cursor"]["rr"], {})
        c = self.runner({"search/issues?per_page=30&sort=updated&q=is:pr+is:open+review-requested": {"items": [pr]}})
        out = hs.CodeGithub.poll(c, None, {}, ME, AT)
        self.assertEqual(out["cursor"]["rr"], {"example-org/platform-api#45": "Fix"})

    def test_ci_green_needs_every_run_completed(self):
        def poll(runs):
            c = self.runner({"repos/example-org/platform-api/commits/main/check-runs": {"check_runs": runs},
                             "repos/example-org/platform-api": {"default_branch": "main"}})
            cur = {"since": "2026-10-29T09:50:00Z", "ci": {"example-org/platform-api": "abc"}, "branch": {}}
            return hs.CodeGithub.poll(c, cur, {"repos": ["example-org/platform-api"]}, {"email": "x"}, AT)
        pending = poll([{"name": "a", "status": "completed", "conclusion": "success", "head_sha": "def"},
                        {"name": "b", "status": "in_progress", "conclusion": None, "head_sha": "def"}])
        self.assertEqual(pending["lines"], [])
        done = poll([{"name": "a", "status": "completed", "conclusion": "success", "head_sha": "def"}])
        self.assertEqual([l["kind"] for l in done["lines"]], ["resolved"])

    def test_gh_client_allow_list(self):
        c = hs.GhClient(runner=lambda p: {})
        for bad in ("user/emails", "repos/a/b/issues/1/comments -X POST", "repos/../admin", "orgs/x"):
            with self.assertRaises(hub_mcp.ForbiddenTool, msg=bad):
                c.call("get", {"path": bad})
        with self.assertRaises(hub_mcp.ForbiddenTool):
            c.call("post", {"path": "repos/a/b"})
        self.assertEqual(c.call("get", {"path": "repos/a/b"}), {})


if __name__ == "__main__":
    unittest.main()


SELF = "15550000009@s.whatsapp.net"
DANA = "15550000001@s.whatsapp.net"
TEAM = "15550000002-1700000000@g.us"
OPS = "15550000003-1700000001@g.us"


class FakeReader:
    """Stands in for hub_sources.LocalWhatsApp: `messages` returns whatever it was given, oldest first."""

    def __init__(self, items=(), error=None):
        self.items = list(items)
        self.error = error
        self.calls = []

    def messages(self, since_iso, limit):
        self.calls.append((since_iso, limit))
        if self.error:
            raise self.error
        return list(self.items)


def wa(text, at, chat=DANA, sid="S1", sender="Dana", from_me=False, name="Dana"):
    return {"chat": chat, "chat_name": name, "from_me": from_me, "sender": "me" if from_me else sender,
            "text": text, "at": at, "link": "whatsapp://%s/%s" % (chat, sid)}


class TestWhatsApp(unittest.TestCase):
    CFG = {"_local": {"enabled": True, "read": True, "home": True, "self_jid": SELF}, "groups": [TEAM]}
    CURSOR = {"ts": "2026-10-29T09:55:00Z", "seen": [], "open": {}}

    def poll(self, items=(), cursor=CURSOR, cfg=None, reader=None, me=None):
        reader = reader or FakeReader(items)
        self.reader = reader
        return hs.WhatsApp.poll(reader, cursor, cfg or self.CFG, me or ME, AT)

    def test_first_poll_returns_cursor_only(self):
        out = self.poll([wa("hi", "2026-10-29T09:59:00Z")], cursor=None)
        self.assertEqual(out["lines"], [])
        self.assertEqual(out["cursor"], {"ts": "2026-10-29T10:00:00Z", "seen": [], "open": {}})
        self.assertEqual(self.reader.calls, [])

    def test_dm_is_a_candidate(self):
        out = self.poll([wa("can you look at PLAT-7 today?", "2026-10-29T09:58:00Z", sid="S9")])
        (l,) = out["lines"]
        self.assertEqual((l["source"], l["kind"], l["key"]), ("whatsapp", "update", "wa:" + DANA))
        self.assertEqual(l["who"], "Dana <%s>" % DANA)
        self.assertEqual(l["link"], "whatsapp://send?phone=15550000001")
        self.assertEqual(l["also"], ["PLAT-7"])
        self.assertEqual(l["id"], "whatsapp:wa:%s:S9" % DANA)
        self.assertEqual(l["at"], "2026-10-29T09:58:00Z")

    def test_one_line_per_chat_and_text_is_capped_and_cleaned(self):
        out = self.poll([wa("first", "2026-10-29T09:57:00Z", sid="A"),
                         wa("second‮\nline", "2026-10-29T09:58:00Z", sid="B")])
        (l,) = out["lines"]
        self.assertEqual(l["summary"], "second line")
        out = self.poll([wa("x" * 500 + "\x1b", "2026-10-29T09:57:00Z")])
        self.assertLessEqual(len(out["lines"][0]["summary"]), 300)
        self.assertLessEqual(len(out["lines"][0]["title"]), 80)
        self.assertNotIn("\x1b", out["lines"][0]["summary"])

    def test_group_needs_a_mention_unless_listed(self):
        quiet = wa("lunch?", "2026-10-29T09:58:00Z", chat=OPS, sender="Agnes", name="Ops", sid="Q")
        self.assertEqual(self.poll([quiet])["lines"], [])
        mention = wa("@15550000009 can you approve?", "2026-10-29T09:58:30Z", chat=OPS, sender="Agnes", name="Ops",
                     sid="M")
        (l,) = self.poll([quiet, mention])["lines"]
        self.assertEqual((l["key"], l["who"], l["link"]), ("wa:" + OPS, "Agnes", ""))
        self.assertTrue(l["title"].startswith("Ops: "))
        near = wa("@155500000090 not me", "2026-10-29T09:58:30Z", chat=OPS, name="Ops", sid="N")
        self.assertEqual(self.poll([near])["lines"], [])

    def test_listed_or_followed_group_is_read_whole(self):
        team = wa("standup moved", "2026-10-29T09:58:00Z", chat=TEAM, sender="Agnes", name="Team")
        (l,) = self.poll([team])["lines"]
        self.assertEqual(l["key"], "wa:" + TEAM)
        cfg = dict(self.CFG, groups=[OPS])  # followed groups arrive in the same list
        self.assertEqual(self.poll([team], cfg=cfg)["lines"], [])
        quiet = wa("lunch?", "2026-10-29T09:58:00Z", chat=OPS, name="Ops")
        self.assertEqual(len(self.poll([quiet], cfg=cfg)["lines"]), 1)

    def test_own_reply_resolves(self):
        out = self.poll([wa("ping", "2026-10-29T09:57:00Z", sid="A"),
                         wa("on it", "2026-10-29T09:58:00Z", sid="B", from_me=True)])
        (l,) = out["lines"]
        self.assertEqual((l["kind"], l["key"]), ("resolved", "wa:" + DANA))
        self.assertEqual(out["cursor"]["open"], {})
        out = self.poll([wa("ping", "2026-10-29T09:57:00Z", sid="A")])
        self.assertEqual(out["cursor"]["open"], {"wa:" + DANA: "2026-10-29T09:57:00Z"})
        out = self.poll([wa("on it", "2026-10-29T09:58:00Z", sid="B", from_me=True)], cursor=out["cursor"])
        self.assertEqual([x["kind"] for x in out["lines"]], ["resolved"])

    def test_own_message_in_a_quiet_group_is_nothing(self):
        out = self.poll([wa("hi all", "2026-10-29T09:58:00Z", chat=OPS, from_me=True, name="Ops")])
        self.assertEqual(out["lines"], [])

    def test_own_reply_after_a_mention_resolves_the_group(self):
        m = wa("@15550000009 ok?", "2026-10-29T09:57:00Z", chat=OPS, name="Ops", sid="M")
        r = wa("yes", "2026-10-29T09:58:00Z", chat=OPS, name="Ops", sid="R", from_me=True)
        self.assertEqual([x["kind"] for x in self.poll([m, r])["lines"]], ["resolved"])

    def test_the_agents_own_signed_message_is_neither(self):
        me = dict(ME, marks=["Mother (AI)"])
        out = self.poll([wa("ping", "2026-10-29T09:57:00Z", sid="A"),
                         wa("sent\n\n*Mother (AI)* on behalf of Dana", "2026-10-29T09:58:00Z", sid="B",
                            from_me=True)], me=me)
        self.assertEqual([x["kind"] for x in out["lines"]], ["update"])

    def test_cursor_overlap_and_dedupe(self):
        first = self.poll([wa("hello", "2026-10-29T09:58:00Z", sid="S1")])
        self.assertEqual(len(first["lines"]), 1)
        self.assertEqual(self.reader.calls, [("2026-10-29T09:53:00Z", 201)])  # cursor ts less 2 minutes
        self.assertEqual(first["cursor"]["ts"], "2026-10-29T10:00:00Z")
        again = self.poll([wa("hello", "2026-10-29T09:58:00Z", sid="S1"),
                           wa("and more", "2026-10-29T09:59:00Z", sid="S2")], cursor=first["cursor"])
        self.assertEqual([l["id"] for l in again["lines"]], ["whatsapp:wa:%s:S2" % DANA])
        self.assertEqual(self.reader.calls, [("2026-10-29T09:58:00Z", 201)])

    def test_seen_set_is_bounded(self):
        seen = []
        for n in range(5):
            items = [wa("m", "2026-10-29T09:58:00Z", sid="P%dS%d" % (n, i), chat=OPS, name="Ops") for i in range(150)]
            out = self.poll(items, cursor=dict(self.CURSOR, seen=seen))
            seen = out["cursor"]["seen"]
        self.assertEqual(len(seen), 500)
        self.assertIn("whatsapp://%s/P4S149" % OPS, seen)

    def test_more_than_the_limit_is_partial(self):
        items = [wa("m", "2026-10-29T09:58:00Z", sid="S%d" % i) for i in range(201)]
        out = self.poll(items)
        self.assertEqual(out["partial"], ["more than 200 WhatsApp messages since the last pass"])

    def test_unreadable_database_raises_with_the_reason(self):
        with self.assertRaises(McpErrorAlias) as cm:
            self.poll(reader=FakeReader(error=whatsapp.ReaderError("denied")))
        self.assertIn("Full Disk Access", cm.exception.message)
        with self.assertRaises(McpErrorAlias) as cm:
            self.poll(reader=FakeReader(error=whatsapp.ReaderError("locked")))
        self.assertIn("locked", cm.exception.message)

    def test_gap_reasons(self):
        def gap(**kw):
            local = dict(self.CFG["_local"], **kw)
            return hs.WhatsApp.gap(dict(self.CFG, _local=local))
        self.assertIsNone(gap())
        self.assertIn("whatsapp.enabled", gap(enabled=False))
        self.assertIn("whatsapp.read", gap(read=False))
        self.assertIn("home machine", gap(home=False))
        self.assertIn("self_jid", gap(self_jid=""))
        self.assertIn("whatsapp", hs.WhatsApp.gap({}))  # fails closed

    def test_local_reader_has_no_send_path_and_never_touches_the_bridge(self):
        import inspect
        public = {n for n in dir(hs.LocalWhatsApp()) if not n.startswith("_")}
        self.assertEqual(public, {"messages", "ping"})
        src = inspect.getsource(hs)
        for word in ("bridge_call", "cmd_send", "_exchange", "_start_bridge", "install_bridge"):
            self.assertNotIn(word, src)

    def test_local_reader_reads_through_whatsapp_read_functions(self):
        calls = []
        saved = (whatsapp.read_messages, whatsapp.recent_chats)
        self.addCleanup(lambda: (setattr(whatsapp, "read_messages", saved[0]),
                                 setattr(whatsapp, "recent_chats", saved[1])))
        whatsapp.read_messages = lambda chat=None, since_iso=None, limit=100: (
            calls.append((chat, since_iso, limit)) or {"status": "ok", "items": [{"x": 1}]})
        whatsapp.recent_chats = lambda limit=30, query=None: calls.append(("chats", limit)) or []
        r = hs.LocalWhatsApp()
        self.assertEqual(r.messages("2026-10-29T09:00:00Z", 5), [{"x": 1}])
        r.ping()
        self.assertEqual(calls, [(None, "2026-10-29T09:00:00Z", 5), ("chats", 1)])
        whatsapp.read_messages = lambda *a, **kw: {"status": "couldnt_check", "reason": "denied", "items": []}
        with self.assertRaises(whatsapp.ReaderError):
            r.messages("2026-10-29T09:00:00Z", 5)
