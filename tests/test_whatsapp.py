"""Tests for lib/whatsapp.py: chats, read, send, the bridge client, link and setup. Stdlib only:
python3 -m unittest tests.test_whatsapp -v

Every test builds a fixture database with the real table and column names; the real WhatsApp database is
never opened (MOTHER_WHATSAPP_DB points at the fixture), and the bridge is a fake on a temp socket."""
import datetime as _dt
import io as _io
import json
import os
import socket as _socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "mother"
sys.path.insert(0, str(ROOT / "lib"))
import whatsapp  # noqa: E402

SELF = "972500000099@s.whatsapp.net"
DM = "972500000001@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
OTHER = "972500000002@s.whatsapp.net"
COLD = "972500000005@s.whatsapp.net"
LEVI = "972500000006@s.whatsapp.net"
LID = "987654321012345@lid"
CORE0 = 978307200  # 2001-01-01 in unix seconds


def fixture_db(path, chats, messages, drop=(), lasts=None):
    """chats: [(pk, jid, name, sessiontype)]; messages: [dict]. `drop` omits columns from ZWAMESSAGE; `lasts`
    overrides ZLASTMESSAGEDATE per chat pk (a pinned chat's is far in the future)."""
    cols = ["Z_PK INTEGER PRIMARY KEY", "ZCHATSESSION INTEGER", "ZISFROMME INTEGER", "ZFROMJID TEXT",
            "ZTOJID TEXT", "ZTEXT TEXT", "ZMESSAGEDATE REAL", "ZSTANZAID TEXT", "ZMESSAGETYPE INTEGER",
            "ZPARENTMESSAGE INTEGER", "ZGROUPMEMBER INTEGER"]
    cols = [c for c in cols if c.split()[0] not in drop]
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, "
               "ZSESSIONTYPE INTEGER, ZGROUPINFO INTEGER, ZLASTMESSAGEDATE REAL)")
    db.execute("CREATE TABLE ZWAMESSAGE (%s)" % ", ".join(cols))
    db.execute("CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT, ZCONTACTNAME TEXT, "
               "ZFIRSTNAME TEXT, ZCHATSESSION INTEGER)")
    for pk, jid, name, st in chats:
        last = (lasts or {}).get(pk, max([m["date"] for m in messages if m["chat"] == pk] or [0]))
        db.execute("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?,?,?)", (pk, jid, name, st, None, last))
    for i, m in enumerate(messages, 1):
        row = {"Z_PK": i, "ZCHATSESSION": m["chat"], "ZISFROMME": m.get("me", 0), "ZFROMJID": m.get("from"),
               "ZTOJID": None, "ZTEXT": m.get("text"), "ZMESSAGEDATE": m["date"], "ZSTANZAID": m["stanza"],
               "ZMESSAGETYPE": m.get("type", 0), "ZPARENTMESSAGE": None, "ZGROUPMEMBER": m.get("member")}
        keys = [k for k in row if k not in drop]
        db.execute("INSERT INTO ZWAMESSAGE (%s) VALUES (%s)" % (",".join(keys), ",".join("?" * len(keys))),
                   [row[k] for k in keys])
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (1, ?, 'Dana', NULL, 2)", (OTHER,))
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (2, ?, NULL, 'Agnes', 2)", ("972500000003@s.whatsapp.net",))
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (3, ?, NULL, NULL, 2)", ("972500000004@s.whatsapp.net",))
    db.commit()
    db.close()


def core(iso):
    return whatsapp.to_core(iso)


CHATS = [(1, DM, "Dana", 0), (2, GROUP, "PLAT team", 1), (3, OTHER, "Sam Roe", 0), (4, SELF, "You", 0),
         (5, COLD, "Agnes", 0), (6, LID, "Hidden One", 0), (7, LEVI, "Dana Roe", 0),
         (8, "status@broadcast", "Status", 0)]
MSGS = [
    {"chat": 1, "text": "hello from dana", "date": core("2026-10-26T08:00:00Z"), "stanza": "A1", "from": DM},
    {"chat": 1, "text": "I replied", "date": core("2026-10-26T08:05:00Z"), "stanza": "A2", "me": 1},
    {"chat": 1, "text": None, "date": core("2026-10-26T08:10:00Z"), "stanza": "A3", "from": DM, "type": 1},
    {"chat": 1, "text": "old news", "date": core("2026-10-20T08:00:00Z"), "stanza": "A0", "from": DM},
    {"chat": 2, "text": "ship it", "date": core("2026-10-26T09:00:00Z"), "stanza": "G1", "member": 1},
    {"chat": 3, "text": "a stranger writes", "date": core("2026-10-26T09:30:00Z"), "stanza": "X1", "from": OTHER},
    {"chat": 4, "text": "note to self", "date": core("2026-10-26T10:05:00Z"), "stanza": "S2", "me": 1},
    {"chat": 6, "text": "from a hidden id", "date": core("2026-10-25T10:00:00Z"), "stanza": "L1"},
    {"chat": 7, "text": "levi here", "date": core("2026-10-24T10:00:00Z"), "stanza": "V1", "from": LEVI},
    {"chat": 8, "text": "a status update", "date": core("2026-10-26T11:00:00Z"), "stanza": "B1"},
]
SINCE = "2026-10-25T00:00:00Z"
PINNED = core("7026-01-01T00:00:00Z")  # how WhatsApp stores a pinned chat's date


def make_cfg(home="laptop-a"):
    return {"workspace": {"home_machine": home},
            "agent": {"name": "Agnes", "emoji": ":sparkles:", "manager": "Dana Roe", "manager_short": "Dana"},
            "whatsapp": {"enabled": True, "self_jid": SELF, "read": True, "send": True}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.db = self.t / "ChatStorage.sqlite"
        self.env = dict(os.environ)
        self.env.pop("MOTHER_WORKSPACE", None)
        self.env.update({"MOTHER_STATE_DIR": str(self.t / "state"), "MOTHER_HOSTNAME": "laptop-a",
                         "MOTHER_WHATSAPP_DB": str(self.db)})
        patcher = mock.patch.dict(os.environ, {k: self.env[k] for k in
                                               ("MOTHER_STATE_DIR", "MOTHER_HOSTNAME", "MOTHER_WHATSAPP_DB")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def build(self, **kw):
        fixture_db(self.db, CHATS, MSGS, **kw)


class SpyConn:
    """Wraps a real connection; records SQL; can inject failures on execute."""
    def __init__(self, real, fail=None, log=None):
        self.real, self.fail, self.log = real, list(fail or []), log if log is not None else []

    def execute(self, sql, params=()):
        self.log.append((sql, tuple(params)))
        if self.fail:
            raise self.fail.pop(0)
        return self.real.execute(sql, params)

    def close(self):
        self.real.close()


def cli(env, *args):
    return subprocess.run([sys.executable, str(CLI), "whatsapp", *args], capture_output=True, text=True, env=env)


class TestChats(Base):
    def test_recent_chats_names_types_dates_and_jids_never_a_message(self):
        self.build()
        got = whatsapp.recent_chats()
        self.assertEqual([c["jid"] for c in got], [SELF, OTHER, GROUP, DM, LID, LEVI, COLD])  # newest first
        out = [whatsapp.chat_out(c) for c in got]
        self.assertEqual(out[3], {"name": "Dana", "type": "dm", "last": "2026-10-26T08:10:00Z", "jid": DM})
        self.assertEqual(out[2]["type"], "group")
        self.assertEqual(out[4], {"name": "Hidden One", "type": "dm", "last": "2026-10-25T10:00:00Z", "jid": LID,
                                  "hidden_id": True})
        self.assertIsNone(out[-1]["last"])  # no messages
        blob = json.dumps(out)
        for text in ("hello from dana", "ship it", "note to self", "a status update"):
            self.assertNotIn(text, blob)
        self.assertNotIn("broadcast", blob)

    def test_pinned_chats_show_pinned_and_come_first(self):
        self.build(lasts={7: PINNED, 3: PINNED + 100})
        got = [whatsapp.chat_out(c) for c in whatsapp.recent_chats()]
        self.assertEqual([c["name"] for c in got[:2]], ["Sam Roe", "Dana Roe"])
        self.assertEqual({c["last"] for c in got[:2]}, {"pinned"})
        self.assertNotIn("7026", json.dumps(got))
        self.assertEqual(got[2]["jid"], SELF)

    def test_query_and_limit(self):
        self.build()
        self.assertEqual([c["name"] for c in whatsapp.recent_chats(query="dana")], ["Dana", "Dana Roe"])
        self.assertEqual([c["jid"] for c in whatsapp.recent_chats(query="000002")], [OTHER])
        self.assertEqual(whatsapp.recent_chats(query="100%"), [])  # LIKE wildcards are literal
        self.assertEqual(len(whatsapp.recent_chats(limit=2)), 2)

    def test_chats_never_touch_messages(self):
        self.build()
        log = []
        real = sqlite3.connect
        with mock.patch.object(sqlite3, "connect", lambda *a, **k: SpyConn(real(*a, **k), log=log)):
            whatsapp.recent_chats(query="x")
        self.assertTrue(log)
        for sql, _ in log:
            self.assertNotIn("ZWAMESSAGE", sql)

    def test_cli_chats(self):
        self.build()
        ws = self.t / "ws"
        ws.mkdir()
        (ws / "config.json").write_text(json.dumps(make_cfg()))
        env = dict(self.env, MOTHER_WORKSPACE=str(ws))
        r = cli(env, "chats", "--query", "Dana", "--limit", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c["name"] for c in json.loads(r.stdout)], ["Dana", "Dana Roe"])
        self.assertEqual(cli(env, "chats", "--limit", "x").returncode, 1)
        self.assertEqual(cli(env, "chats", "--bogus").returncode, 1)


class TestResolve(Base):
    def setUp(self):
        Base.setUp(self)
        self.build()

    def who(self, w):
        return whatsapp.resolve_chat(make_cfg(), w)

    def test_a_name_a_number_a_jid_or_me(self):
        self.assertEqual(self.who("Dana")["jid"], DM)          # exact: "Dana Roe" is another chat
        self.assertEqual(self.who("  dana roe ")["jid"], LEVI)
        self.assertEqual(self.who("+972 50-000-0001")["jid"], DM)
        self.assertEqual(self.who(GROUP)["jid"], GROUP)
        me = self.who("me")
        self.assertEqual((me["jid"], me["self"]), (SELF, True))
        self.assertTrue(self.who(SELF)["self"])
        self.assertFalse(self.who("Dana")["self"])
        self.assertTrue(self.who("Hidden One").get("hidden_id"))

    def test_ambiguous_unknown_and_broadcast(self):
        with self.assertRaises(whatsapp.ChatError) as cm:
            self.who("Dan")  # no partial matches: the candidates are listed
        self.assertIn("no WhatsApp chat is named 'Dan'", str(cm.exception))
        self.assertIn(LEVI, str(cm.exception))
        with self.assertRaises(whatsapp.ChatError):
            self.who("Sam")
        db = sqlite3.connect(str(self.db))
        for pk in range(10, 17):
            db.execute("INSERT INTO ZWACHATSESSION VALUES (?, ?, 'PLAT team', 1, NULL, 1)",
                       (pk, "1203630000000000%02d@g.us" % pk))
        db.commit()
        db.close()
        with self.assertRaises(whatsapp.ChatError) as cm:
            self.who("plat team")
        self.assertIn("8 chats are named 'plat team'", str(cm.exception))
        self.assertEqual(str(cm.exception).count("@g.us"), 5)  # up to 5 listed
        for w in ("Nobody", "Status", "+972 50 000 0077", "123@g.us", ""):
            with self.assertRaises(whatsapp.ChatError):
                self.who(w)
        cfg = make_cfg()
        del cfg["whatsapp"]["self_jid"]
        with self.assertRaises(whatsapp.ChatError):
            whatsapp.resolve_chat(cfg, "me")


class TestExactName(Base):
    def test_an_exact_name_resolves_however_many_chats_contain_it_and_in_any_script(self):
        self.build()
        db = sqlite3.connect(str(self.db))
        for pk in range(100, 400):  # 300 newer chats whose names contain "Dan"
            db.execute("INSERT INTO ZWACHATSESSION VALUES (?, ?, ?, 0, NULL, ?)",
                       (pk, "9725%08d@s.whatsapp.net" % pk, "Dan %d" % pk, core("2026-10-27T00:00:00Z")))
        db.execute("INSERT INTO ZWACHATSESSION VALUES (500, '972500000500@s.whatsapp.net', 'Dan', 0, NULL, 1)")
        db.execute("INSERT INTO ZWACHATSESSION VALUES (501, '972500000501@s.whatsapp.net', 'Éva', 0, NULL, 1)")
        db.execute("INSERT INTO ZWACHATSESSION VALUES (502, '972500000502@s.whatsapp.net', 'דנה', 0, NULL, 1)")
        db.commit()
        db.close()
        cfg = make_cfg()
        self.assertEqual(whatsapp.resolve_chat(cfg, "dan")["jid"], "972500000500@s.whatsapp.net")
        self.assertEqual(whatsapp.resolve_chat(cfg, "éva")["jid"], "972500000501@s.whatsapp.net")
        self.assertEqual(whatsapp.resolve_chat(cfg, " דנה ")["jid"], "972500000502@s.whatsapp.net")
        with self.assertRaises(whatsapp.ChatError) as cm:
            whatsapp.resolve_chat(cfg, "Da")
        self.assertLessEqual(str(cm.exception).count("@s.whatsapp.net"), 5)


class TestReader(Base):
    def test_one_chat_by_name_oldest_first(self):
        self.build()
        out = whatsapp.read_messages(self.chat("Dana"), SINCE)
        self.assertEqual(out["status"], "ok")
        self.assertEqual([i["text"] for i in out["items"]], ["hello from dana", "I replied", "[media]"])
        first = out["items"][0]
        self.assertEqual(first, {"chat": DM, "chat_name": "Dana", "from_me": False, "sender": "Dana",
                                 "text": "hello from dana", "at": "2026-10-26T08:00:00Z",
                                 "link": "whatsapp://%s/A1" % DM})
        self.assertTrue(out["items"][1]["from_me"])
        self.assertEqual(out["items"][1]["sender"], "me")

    def chat(self, who):
        return whatsapp.resolve_chat(make_cfg(), who)

    def test_one_chat_without_since_is_its_last_messages(self):
        self.build()
        out = whatsapp.read_messages(self.chat("Dana"), None, limit=2)
        self.assertEqual([i["text"] for i in out["items"]], ["I replied", "[media]"])
        out = whatsapp.read_messages(self.chat("Dana"))
        self.assertIn("old news", [i["text"] for i in out["items"]])

    def test_across_chats_since(self):
        self.build()
        out = whatsapp.read_messages(None, "2026-10-26T08:30:00Z")
        self.assertEqual([i["text"] for i in out["items"]], ["ship it", "a stranger writes", "note to self"])
        self.assertNotIn("a status update", json.dumps(out))  # status broadcasts are never read
        out = whatsapp.read_messages(None, SINCE, limit=2)
        self.assertEqual([i["text"] for i in out["items"]], ["a stranger writes", "note to self"])

    def test_after_a_row_id_in_insertion_order_even_when_dated_older(self):
        self.build()
        self.assertEqual(whatsapp.max_pk(), len(MSGS))
        out = whatsapp.read_messages_after(6)
        self.assertEqual([i["pk"] for i in out["items"]], [7, 8, 9])  # the status chat (row 10) is left out
        self.assertEqual([i["text"] for i in out["items"]], ["note to self", "from a hidden id", "levi here"])
        self.assertEqual(out["items"][0]["link"], "whatsapp://%s/S2" % SELF)
        self.assertEqual([i["pk"] for i in whatsapp.read_messages_after(0, limit=2)["items"]], [1, 2])  # oldest first
        self.assertEqual(whatsapp.read_messages_after(len(MSGS))["items"], [])
        self.assertNotIn("a status update", json.dumps(whatsapp.read_messages_after(0, limit=50)))

    def test_after_leaves_out_channels_and_status_but_read_does_not(self):
        chats = CHATS + [(9, "120363999999999999@newsletter", "A Channel", 0), (10, "status@s.whatsapp.net", "S", 0)]
        msgs = MSGS + [{"chat": 9, "text": "channel post", "date": core("2026-10-26T12:00:00Z"), "stanza": "N1"},
                       {"chat": 10, "text": "odd status", "date": core("2026-10-26T12:01:00Z"), "stanza": "N2"}]
        fixture_db(self.db, chats, msgs)
        got = json.dumps(whatsapp.read_messages_after(0, limit=50))
        self.assertNotIn("channel post", got)
        self.assertNotIn("odd status", got)
        self.assertIn("note to self", got)
        self.assertIn("channel post", json.dumps(whatsapp.read_messages(None, SINCE)))  # `read` is unchanged

    def test_after_reports_an_unreadable_database(self):
        out = whatsapp.read_messages_after(0)
        self.assertEqual((out["status"], out["reason"]), ("couldnt_check", "missing"))
        with self.assertRaises(whatsapp.ReaderError):
            whatsapp.max_pk()

    def test_a_hidden_id_chat_is_read_by_name(self):
        self.build()
        out = whatsapp.read_messages(self.chat("Hidden One"))
        self.assertEqual([i["text"] for i in out["items"]], ["from a hidden id"])

    def test_link_preview_type_is_text(self):
        fixture_db(self.db, CHATS, [{"chat": 1, "text": "see https://example.com", "type": 7,
                                     "date": core("2026-10-26T08:00:00Z"), "stanza": "P1", "from": DM}])
        out = whatsapp.read_messages(None, SINCE)
        self.assertEqual(out["items"][0]["text"], "see https://example.com")

    def test_group_sender_names_never_a_number(self):
        msgs = [{"chat": 2, "text": "a", "date": core("2026-10-26T09:00:00Z"), "stanza": "G1", "member": 1},
                {"chat": 2, "text": "b", "date": core("2026-10-26T09:01:00Z"), "stanza": "G2", "member": 2},
                {"chat": 2, "text": "c", "date": core("2026-10-26T09:02:00Z"), "stanza": "G3", "member": 3}]
        fixture_db(self.db, CHATS, msgs)
        out = whatsapp.read_messages(None, SINCE)
        self.assertEqual([i["sender"] for i in out["items"]], ["Dana", "Agnes", "a member"])
        self.assertNotIn("97250000000", json.dumps([i["sender"] for i in out["items"]]))

    def test_schema_changed_missing_locked_denied(self):
        out = whatsapp.read_messages(None, SINCE)
        self.assertEqual((out["status"], out["reason"], out["items"]), ("couldnt_check", "missing", []))
        self.build(drop=("ZTEXT",))
        self.assertEqual(whatsapp.read_messages(None, SINCE),
                         {"status": "couldnt_check", "reason": "schema_changed", "items": []})
        self.db.unlink()
        self.build()
        real = sqlite3.connect
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(sqlite3, "connect", lambda *a, **k: SpyConn(real(*a, **k), fail=[locked])):
            self.assertEqual(whatsapp.read_messages(None, SINCE)["status"], "ok")
        with mock.patch.object(sqlite3, "connect", lambda *a, **k: SpyConn(real(*a, **k), fail=[locked, locked])):
            self.assertEqual(whatsapp.read_messages(None, SINCE),
                             {"status": "couldnt_check", "reason": "locked", "items": []})
        with mock.patch.object(sqlite3, "connect", side_effect=sqlite3.OperationalError("unable to open database file")):
            self.assertEqual(whatsapp.read_messages(None, SINCE)["reason"], "denied")

    def test_readonly_uri(self):
        self.build()
        calls = []
        real = sqlite3.connect

        def spy(*a, **k):
            calls.append((a, k))
            return real(*a, **k)
        with mock.patch.object(sqlite3, "connect", spy):
            whatsapp.read_messages(None, SINCE)
        (a, k) = calls[0]
        self.assertTrue(a[0].startswith("file:") and "mode=ro" in a[0], a[0])
        self.assertTrue(k.get("uri"))

    def cli_env(self, home="laptop-a", **wa):
        ws = self.t / "ws"
        ws.mkdir(exist_ok=True)
        cfg = make_cfg(home)
        cfg["whatsapp"].update(wa)
        (ws / "config.json").write_text(json.dumps(cfg))
        return dict(self.env, MOTHER_WORKSPACE=str(ws))

    def test_cli_read(self):
        self.build()
        env = self.cli_env()
        r = cli(env, "read", "--chat", "Dana Roe")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([i["text"] for i in json.loads(r.stdout)["items"]], ["levi here"])
        r = cli(env, "read", "--since", "2026-10-26T09:10:00Z", "--limit", "1")
        self.assertEqual([i["text"] for i in json.loads(r.stdout)["items"]], ["note to self"])
        r = cli(env, "read", "--chat", "Dan")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no WhatsApp chat is named", r.stderr)
        self.assertIn("Dana Roe", r.stderr)
        self.assertEqual(cli(env, "read", "--since", "yesterday").returncode, 1)
        self.assertEqual(cli(env, "read", "--chat").returncode, 1)

    def test_cli_read_defaults_to_the_last_24_hours(self):
        now = time.time()
        msgs = [{"chat": 1, "text": "fresh", "date": now - CORE0 - 3600, "stanza": "F1", "from": DM},
                {"chat": 1, "text": "stale", "date": now - CORE0 - 30 * 3600, "stanza": "F0", "from": DM}]
        fixture_db(self.db, CHATS, msgs)
        r = cli(self.cli_env(), "read")
        self.assertEqual([i["text"] for i in json.loads(r.stdout)["items"]], ["fresh"])

    def test_reading_off_and_not_home(self):
        self.build()
        for wa in ({"enabled": False}, {"read": False}):
            for sub in (["read"], ["chats"]):
                r = cli(self.cli_env(**wa), *sub)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("whatsapp reading is off", r.stderr)
                self.assertIn("mother whatsapp setup", r.stderr)
                self.assertNotIn("hello", r.stdout)
        for sub in (["read", "--since", SINCE], ["chats"], ["send", "--to", "me", "--text", "hi"]):
            r = cli(self.cli_env(home="elsewhere"), *sub)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertNotIn("hello", r.stdout)



# ---------------------------------------------------------------- send and the bridge client

class FakeBridge(object):
    """A thread serving the bridge's socket protocol (one JSON request per connection, one JSON reply)."""

    def __init__(self, sock_path, replies=None):
        self.path = str(sock_path)
        self.replies = replies or {}
        self.requests = []
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(5)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            req = json.loads(buf.split(b"\n")[0])
            self.requests.append(req)
            rep = self.replies.get(req["op"], {"ok": True, "stanza": "STANZA1"})
            if callable(rep):
                rep = rep(req)
            try:
                conn.sendall((json.dumps(rep) + "\n").encode("utf-8"))
            except OSError:
                pass  # the client gave up (a timeout test)
            conn.close()

    def stop(self):
        self.srv.close()


class Cmd7(Base):
    def setUp(self):
        Base.setUp(self)
        self.build()
        self.ws = self.t / "ws"
        self.ws.mkdir()
        self.cfg = make_cfg()
        self.cfg["manager"] = {"chat_dm": "D00000001", "chat_user": "U00000001"}
        self.write_cfg()
        self.wenv = dict(self.env, MOTHER_WORKSPACE=str(self.ws))
        self.sock = self.t / "state" / "whatsapp" / "bridge.sock"
        self.bridge = None

    def write_cfg(self):
        (self.ws / "config.json").write_text(json.dumps(self.cfg))

    def start_bridge(self, **replies):
        self.bridge = FakeBridge(self.sock, replies)
        self.addCleanup(self.bridge.stop)
        return self.bridge

    def run_cli(self, *args):
        return cli(self.wenv, *args)


SIGNATURE = "✨ *Agnes (AI)* · on behalf of Dana"  # the fixture's :sparkles: becomes the emoji


class TestCliSend(Cmd7):
    def send(self, *args):
        return self.run_cli("send", *args)

    def test_a_send_to_someone_by_name_is_signed_and_reports_where_it_went(self):
        b = self.start_bridge()
        r = self.send("--to", "Dana", "--text", "see you at 3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"ok": True, "to": DM, "name": "Dana", "id": "STANZA1"})
        self.assertEqual(b.requests, [{"op": "send", "to": "Dana", "jid": DM, "text": "see you at 3",
                                       "signature": SIGNATURE}])

    def test_by_number_jid_and_group_the_text_as_received(self):
        b = self.start_bridge()
        for args in (["--to", "+972-50-000-0001", "--text", "a\n\n"], ["--to=%s" % GROUP, "--text=b"],
                     ["--text", "c", "--to", "PLAT team"]):
            r = self.send(*args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
        self.assertEqual([(q["to"], q["jid"], q["text"]) for q in b.requests],
                         [("+972-50-000-0001", DM, "a\n\n"), (GROUP, GROUP, "b"), ("PLAT team", GROUP, "c")])

    def test_me_is_the_own_chat_unsigned(self):
        b = self.start_bridge()
        r = self.send("--to", "me", "--text", "note to self")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["to"], SELF)
        self.assertEqual(b.requests, [{"op": "send", "to": "me", "jid": SELF, "text": "note to self"}])

    def test_a_chat_with_only_system_rows_is_cold(self):
        db = sqlite3.connect(str(self.db))
        db.execute("INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZTEXT, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE) "
                   "VALUES (99, 5, 0, NULL, 1, 'SYS1', 6)")
        db.commit()
        db.close()
        b = self.start_bridge()
        r = self.send("--to", "Agnes", "--text", "hi")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no cold contact", r.stderr)
        self.assertEqual(b.requests, [])

    def test_refusals_call_no_bridge(self):
        b = self.start_bridge()
        for args, words in ((["--to", "Agnes", "--text", "hi"], "no cold contact"),
                            (["--to", "Hidden One", "--text", "hi"], "hides the number"),
                            (["--to", "Dan", "--text", "hi"], "no WhatsApp chat is named"),
                            (["--to", "Nobody", "--text", "hi"], "no WhatsApp chat is named"),
                            (["--to", "+972 50 000 0077", "--text", "hi"], "no WhatsApp chat with"),
                            (["--to", "Status", "--text", "hi"], "no WhatsApp chat is named"),
                            (["--to", "Dana", "--text", "  \n"], "empty"),
                            (["--to", "Dana", "--text", "x" * 4097], "longer than"),
                            (["--to", "Dana", "--text", "x" * 4090], "longer than")):  # with the signature
            r = self.send(*args)
            self.assertEqual(r.returncode, 2, (args, r.stderr))
            self.assertIn(words, r.stderr, args)
            self.assertIn("nothing was sent", r.stderr, args)
        self.assertEqual(b.requests, [])

    def test_usage_errors(self):
        for args in ([], ["--to", "Dana"], ["--text", "hi"], ["--to", "Dana", "--text-file", "f"],
                     ["--to", "Dana", "--to", "Gil", "--text", "a"], ["--to", "Dana", "--text"],
                     ["--to", "Dana", "--text", "a", "--cc", "x"], ["w3kx"]):
            self.assertEqual(self.send(*args).returncode, 1, args)

    def test_sending_off(self):
        for wa in ({"enabled": False}, {"send": False}):
            self.cfg["whatsapp"].update(wa)
            self.write_cfg()
            r = self.send("--to", "Dana", "--text", "hi")
            self.assertEqual(r.returncode, 2)
            self.assertIn("whatsapp sending is off", r.stderr)
            self.cfg = make_cfg()

    def test_a_timed_out_send_never_claims_nothing_was_sent(self):
        for err in ("bridge_timeout", "bridge_bad_reply"):
            out, errout = _io.StringIO(), _io.StringIO()
            with mock.patch.object(whatsapp, "bridge_call", return_value={"ok": False, "error": err}), \
                    mock.patch("sys.stdout", new=out), mock.patch("sys.stderr", new=errout), \
                    self.assertRaises(SystemExit) as cm:
                whatsapp.main(["send", "--to", "Dana", "--text", "hi"], self.cfg, self.ws)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("the bridge may still send it; check `mother whatsapp status`", errout.getvalue())
            self.assertNotIn("nothing was sent", errout.getvalue())

    def test_length_is_counted_like_the_bridge(self):
        b = self.start_bridge()
        r = self.send("--to", "me", "--text", "\U0001F600" * 2049)  # 2049 code points, 4098 UTF-16 units
        self.assertEqual(r.returncode, 2)
        self.assertIn("longer than", r.stderr)
        self.assertEqual(b.requests, [])

    def test_bridge_refusal_exits_2_with_the_code(self):
        self.start_bridge(send={"ok": False, "error": "cap"})
        r = self.send("--to", "Dana", "--text", "hi")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout), {"ok": False, "error": "cap"})
        self.assertIn("cap", r.stderr)

    def test_not_installed_exits_2(self):
        out = _io.StringIO()
        with mock.patch.object(whatsapp, "bridge_problem", return_value=("bridge_not_installed", "run `mother whatsapp install`")), \
                mock.patch("sys.stdout", new=out), self.assertRaises(SystemExit) as cm:
            whatsapp.main(["send", "--to", "Dana", "--text", "hi"], self.cfg, self.ws)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "bridge_not_installed")

    def test_an_unreadable_database_refuses(self):
        with mock.patch.object(whatsapp, "_query", side_effect=whatsapp.ReaderError("denied")), \
                mock.patch.object(whatsapp, "bridge_call") as call, self.assertRaises(SystemExit) as cm:
            whatsapp.main(["send", "--to", "Dana", "--text", "hi"], self.cfg, self.ws)
        self.assertEqual(cm.exception.code, 2)
        call.assert_not_called()

    def test_the_signature(self):
        cfg = make_cfg()
        self.assertEqual(whatsapp.signature(cfg), SIGNATURE)  # a known :shortcode: becomes its emoji
        cfg["agent"]["emoji"] = "🦉"
        self.assertEqual(whatsapp.signature(cfg), "🦉 *Agnes (AI)* · on behalf of Dana")
        cfg["agent"]["signature"] = "— Agnes (AI) for Dana"
        self.assertEqual(whatsapp.signature_for(cfg, "hi"), "— Agnes (AI) for Dana")
        self.assertIsNone(whatsapp.signature_for(cfg, "hi\n— Agnes (AI)"))  # never twice
        cfg["agent"]["signature"] = ":robot_face: <https://example.com/agent|Agnes (AI)> · on behalf of Dana"
        self.assertEqual(whatsapp.signature(cfg), "🤖 Agnes (AI) · on behalf of Dana")  # Slack mrkdwn converted
        cfg["agent"]["signature"] = ":no_such_code: *Agnes (AI)* · see <https://example.com>"
        self.assertEqual(whatsapp.signature(cfg), "*Agnes (AI)* · see https://example.com")

    def test_gone_commands_say_what_to_do(self):
        for sub in ("draft", "list", "prompt", "allow", "pending", "verify"):
            r = self.run_cli(sub)
            self.assertEqual(r.returncode, 1, sub)
            self.assertIn("is gone", r.stderr)


class TestBridgeCall(Cmd7):
    def test_missing_socket_not_installed_and_no_start(self):
        with mock.patch.object(whatsapp, "bridge_problem", return_value=("bridge_not_installed", "run `mother whatsapp install`")), \
                mock.patch.object(whatsapp, "_start_bridge") as st:
            r = whatsapp.bridge_call({"op": "status"})
        self.assertEqual(r["error"], "bridge_not_installed")
        st.assert_not_called()

    def test_start_false_reports_bridge_down(self):
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_start_bridge") as st:
            r = whatsapp.bridge_call({"op": "status"}, start=False)
        self.assertEqual(r, {"ok": False, "error": "bridge_down"})
        st.assert_not_called()

    def test_stale_socket_file_counts_as_down(self):
        self.sock.parent.mkdir(parents=True)
        s = _socket.socket(_socket.AF_UNIX)
        s.bind(str(self.sock))
        s.close()  # a socket file nobody listens on: connection refused
        with mock.patch.object(whatsapp, "bridge_problem", return_value=("bridge_not_installed", "run `mother whatsapp install`")):
            self.assertEqual(whatsapp.bridge_call({"op": "status"})["error"], "bridge_not_installed")

    def test_starts_the_bridge_on_demand_and_waits_for_the_socket(self):
        started = []

        def fake_start():
            started.append(1)
            threading.Timer(0.5, lambda: self.start_bridge(status={"ok": True, "linked": True})).start()

        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_start_bridge", side_effect=fake_start):
            r = whatsapp.bridge_call({"op": "status"})
        self.assertEqual(started, [1])
        self.assertEqual(r, {"ok": True, "linked": True})

    def test_gives_up_after_the_wait(self):
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_start_bridge"), mock.patch.object(whatsapp, "START_WAIT", 0.5):
            r = whatsapp.bridge_call({"op": "send", "jid": DM, "text": "hi"})
        self.assertEqual(r["error"], "bridge_down")

    def test_start_spawns_node_detached_with_stdin_closed_and_a_log(self):
        with mock.patch.object(whatsapp, "_node", return_value="/usr/bin/node"), \
                mock.patch("subprocess.Popen") as popen:
            whatsapp._start_bridge()
        argv = popen.call_args[0][0]
        kw = popen.call_args[1]
        self.assertEqual(argv, ["/usr/bin/node", str(self.t / "state/whatsapp/app/bridge.mjs")])  # outside the plugin
        self.assertNotIn("env", kw)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdout"].name, str(self.t / "state/whatsapp/bridge.log"))
        self.assertEqual(oct((self.t / "state/whatsapp").stat().st_mode & 0o777), "0o700")

    def test_timeouts_120_for_send_5_for_status(self):
        seen = []

        def fake(req, timeout):
            seen.append((req["op"], timeout))
            return {"ok": True}

        with mock.patch.object(whatsapp, "_exchange", side_effect=fake):
            for op in ("send", "status"):
                whatsapp.bridge_call({"op": op})
            whatsapp.bridge_call({"op": "send"}, timeout=7)
        self.assertEqual(seen, [("send", 120.0), ("status", 5.0), ("send", 7)])

    def test_slow_bridge_is_a_timeout_reply(self):
        self.start_bridge(status=lambda req: (__import__("time").sleep(1.0), {"ok": True})[1])
        self.assertEqual(whatsapp.bridge_call({"op": "status"}, timeout=0.2), {"ok": False, "error": "bridge_timeout"})


class TestCliLinkStatus(Cmd7):
    APP = "state/whatsapp/app/bridge.mjs"

    def link(self, sub, tty=True):
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_node", return_value="/usr/bin/node"), \
                mock.patch.object(whatsapp, "stop_bridge") as stop, \
                mock.patch("sys.stdout.isatty", return_value=tty), \
                mock.patch("subprocess.call", return_value=0) as call:
            code = whatsapp.cmd_link(sub, [])
        return code, call, stop

    def test_unlink_stops_the_bridge_then_runs_the_installed_copy_in_the_foreground(self):
        for sub in ("unlink",):
            code, call, stop = self.link(sub)
            self.assertEqual(code, 0)
            stop.assert_called_once()
            self.assertEqual(call.call_args[0][0], ["/usr/bin/node", str(self.t / self.APP), "--" + sub])
            self.assertNotIn("stdout", call.call_args[1])  # inherits the terminal: no pipes

    def test_link_needs_a_terminal(self):
        code, call, stop = self.link("link", tty=False)
        self.assertEqual(code, 2)
        call.assert_not_called()
        stop.assert_not_called()

    def test_link_without_an_install_says_how(self):
        out = _io.StringIO()
        with mock.patch.object(whatsapp, "bridge_problem", return_value=("bridge_not_installed", "run `mother whatsapp install`")), \
                mock.patch("sys.stdout.isatty", return_value=True), mock.patch("sys.stdout", new=out), \
                mock.patch("subprocess.call") as call:
            self.assertEqual(whatsapp.cmd_link("unlink", []), 2)
        call.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["error"], "bridge_not_installed")

    def test_status_reports_the_bridge_and_a_read_check(self):
        self.start_bridge(status={"ok": True, "linked": True, "me": SELF, "sent_today": 1})
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["bridge"]["linked"], True)
        self.assertEqual(out["read"], {"db_reachable": True})
        self.assertNotIn("hello", r.stdout)

    def test_status_without_a_bridge_or_database(self):
        self.db.unlink()
        r = self.run_cli("status")
        out = json.loads(r.stdout)
        self.assertIn(out["bridge"]["error"], ("bridge_down", "bridge_not_installed", "node_too_old"))
        self.assertEqual(out["read"], {"db_reachable": False, "reason": "missing"})

    def test_status_says_when_the_database_is_denied(self):
        out = _io.StringIO()
        with mock.patch.object(whatsapp, "_query", side_effect=whatsapp.ReaderError("denied")), \
                mock.patch.object(whatsapp, "bridge_call", return_value={"ok": False, "error": "bridge_down"}), \
                mock.patch("sys.stdout", new=out):
            whatsapp.main(["status"], self.cfg, self.ws)
        read = json.loads(out.getvalue())["read"]
        self.assertEqual(read["reason"], "denied")
        self.assertIn("Full Disk Access", read["hint"])

    def test_status_never_starts_the_bridge(self):
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_start_bridge") as st, \
                mock.patch.object(whatsapp, "_query", return_value=[(1,)]), \
                mock.patch("sys.stdout", new=_io.StringIO()):
            whatsapp.main(["status"], self.cfg, self.ws)
        st.assert_not_called()


class TestStopInstall(Cmd7):
    def test_stop_asks_the_bridge_and_waits_for_the_socket_to_go(self):
        b = self.start_bridge()

        def stop(req):
            threading.Timer(0.2, b.stop).start()
            return {"ok": True}

        b.replies["stop"] = stop
        self.assertTrue(whatsapp.stop_bridge())
        self.assertEqual([r["op"] for r in b.requests][0], "stop")
        self.assertFalse(whatsapp.stop_bridge())  # nothing running

    def test_stop_command(self):
        r = self.run_cli("stop")
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {"ok": True, "stopped": False}))

    def fake_node(self, major=24):
        return mock.patch.multiple(whatsapp, _node=mock.Mock(return_value="/opt/homebrew/bin/node"),
                                   _node_major=mock.Mock(return_value=major))

    def test_install_copies_the_bridge_out_of_the_plugin_and_runs_npm_ci_there(self):
        calls = []
        out = _io.StringIO()
        with self.fake_node(), mock.patch.object(whatsapp, "_run_npm", side_effect=lambda d, n: calls.append((d, n)) or 0), \
                mock.patch("sys.stdout", new=out):
            whatsapp.main(["install"], self.cfg, self.ws)
        app = self.t / "state/whatsapp/app"
        self.assertEqual(calls, [(app, "/opt/homebrew/bin/node")])
        names = sorted(p.name for p in app.iterdir())
        self.assertIn("bridge.mjs", names)
        self.assertIn("checks.mjs", names)
        self.assertIn("package-lock.json", names)
        self.assertFalse([n for n in names if n.endswith(".test.mjs")])
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "app": str(app)})
        self.assertFalse(whatsapp.reinstall_needed())

    def test_install_npm_failure_and_old_node(self):
        out = _io.StringIO()
        with self.fake_node(), mock.patch.object(whatsapp, "_run_npm", return_value=1), \
                mock.patch("sys.stdout", new=out), self.assertRaises(SystemExit) as cm:
            whatsapp.main(["install"], self.cfg, self.ws)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "npm_failed")
        out = _io.StringIO()
        with self.fake_node(major=18), mock.patch.object(whatsapp, "_run_npm") as npm, \
                mock.patch("sys.stdout", new=out), self.assertRaises(SystemExit):
            whatsapp.main(["install"], self.cfg, self.ws)
        npm.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["error"], "node_too_old")

    def test_reinstall_needed_when_the_plugins_files_differ(self):
        with self.fake_node(), mock.patch.object(whatsapp, "_run_npm", return_value=0), \
                mock.patch("sys.stdout", new=_io.StringIO()):
            whatsapp.main(["install"], self.cfg, self.ws)
        app = self.t / "state/whatsapp/app"
        self.assertFalse(whatsapp.reinstall_needed())
        (app / "checks.mjs").write_text("// changed\n")
        self.assertTrue(whatsapp.reinstall_needed())
        out = _io.StringIO()
        with mock.patch.object(whatsapp, "bridge_call", return_value={"ok": False, "error": "bridge_down"}), \
                mock.patch("sys.stdout", new=out):
            whatsapp.main(["status"], self.cfg, self.ws)
        self.assertTrue(json.loads(out.getvalue())["reinstall_needed"])

    def test_problem_codes_use_absolute_paths_and_check_the_node_version(self):
        with mock.patch.object(whatsapp, "_node", return_value=None):
            code, hint = whatsapp.bridge_problem()
        self.assertEqual(code, "bridge_not_installed")
        with self.fake_node(major=18):
            code, hint = whatsapp.bridge_problem()
        self.assertEqual(code, "node_too_old")
        with self.fake_node():
            code, hint = whatsapp.bridge_problem()
        self.assertEqual(code, "bridge_not_installed")
        self.assertIn(str(self.t / "state/whatsapp/app"), hint)

    def test_node_lookup_path_then_homebrew_then_newest_nvm(self):
        with mock.patch("shutil.which", return_value="/p/node"):
            self.assertEqual(whatsapp._node(), "/p/node")
        brew = self.t / "brew"
        brew.mkdir()
        (brew / "node").write_text("#!/bin/sh\n")
        (brew / "node").chmod(0o755)
        with mock.patch("shutil.which", return_value=None), mock.patch.object(whatsapp, "NODE_DIRS", (str(brew),)):
            self.assertEqual(whatsapp._node(), str(brew / "node"))
        nvm = [self.t / ("nvm/v%s/bin/node" % v) for v in ("18.20.0", "20.9.0", "22.1.0", "9.0.0")]
        for n in nvm:
            n.parent.mkdir(parents=True)
            n.write_text("#!/bin/sh\n")
            n.chmod(0o755)
        with mock.patch("shutil.which", return_value=None), mock.patch.object(whatsapp, "NODE_DIRS", ()), \
                mock.patch("glob.glob", return_value=[str(n) for n in nvm]):
            self.assertEqual(whatsapp._node(), str(nvm[2]))  # newest by version, not by name
        with mock.patch("shutil.which", return_value=None), mock.patch.object(whatsapp, "NODE_DIRS", ()), \
                mock.patch("glob.glob", return_value=[]):
            self.assertIsNone(whatsapp._node())


class TestColdStart(Cmd7):
    def test_a_connecting_bridge_is_waited_for_before_the_request(self):
        seen = []
        state = {"n": 0}

        def status(req):
            state["n"] += 1
            return {"ok": True, "linked": state["n"] > 3, "connecting": state["n"] <= 3}

        def started():
            threading.Timer(0.3, lambda: self.start_bridge(status=status,
                                                           send=lambda r: seen.append(state["n"]) or {"ok": True, "stanza": "S"})).start()

        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_start_bridge", side_effect=started):
            r = whatsapp.bridge_call({"op": "send", "jid": DM, "text": "hi"})
        self.assertEqual(r, {"ok": True, "stanza": "S"})
        self.assertGreater(seen[0], 3)  # the send came only after status stopped saying connecting

    def test_unlinked_bridge_is_not_waited_for(self):
        self.start_bridge(status={"ok": True, "linked": False}, send={"ok": False, "error": "unlinked"})
        # already running: no start, no wait
        with mock.patch.object(whatsapp, "_start_bridge") as st:
            self.assertEqual(whatsapp.bridge_call({"op": "send", "jid": DM, "text": "hi"})["error"], "unlinked")
        st.assert_not_called()

    def test_two_callers_start_one_bridge(self):
        started = []

        def fake_start():
            started.append(1)
            time.sleep(0.3)
            self.start_bridge()

        results = []
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_start_bridge", side_effect=fake_start):
            ts = [threading.Thread(target=lambda: results.append(whatsapp.bridge_call({"op": "status"})))
                  for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(started), 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.get("ok") for r in results), results)
        mode = (self.t / "state/whatsapp/start.lock").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_log_is_bounded(self):
        d = self.t / "state/whatsapp"
        d.mkdir(parents=True)
        (d / "bridge.log").write_bytes(b"x" * (1024 * 1024 + 1))
        with mock.patch.object(whatsapp, "_node", return_value="/usr/bin/node"), mock.patch("subprocess.Popen"):
            whatsapp._start_bridge()
        self.assertTrue((d / "bridge.log.1").exists())
        self.assertEqual((d / "bridge.log").stat().st_size, 0)



# ---------------------------------------------------------------- setup and link

def load_validate():
    """validate() from bin/mother (a script without .py, loaded by path)."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("mother_cli", str(CLI))
    spec = importlib.util.spec_from_loader("mother_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod.validate


VALIDATE = load_validate()


class FakeIO(object):
    """The terminal: `ask` returns the next scripted answer (an unscripted question fails the test)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []
        self.lines = []

    def ask(self, prompt):
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError("unexpected question: %s" % prompt)
        return self.answers.pop(0)

    def say(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


class SetupCase(Base):
    def setUp(self):
        Base.setUp(self)
        self.build()
        self.ws = self.t / "ws"
        self.ws.mkdir()
        cfg = json.loads((ROOT / "templates/workspace/config.example.json").read_text())
        cfg["workspace"]["home_machine"] = "laptop-a"
        del cfg["whatsapp"]
        self.cfg = cfg
        self.write_cfg()

    def write_cfg(self, cfg=None):
        (self.ws / "config.json").write_text(json.dumps(cfg or self.cfg, indent=2) + "\n")

    def read_cfg(self):
        return json.loads((self.ws / "config.json").read_text())

    def setup(self, *answers, linked=None, link=SELF, problem=None, reinstall=False, install_ok=True, by_phone=None):
        """Run setup with the bridge faked: `problem` until install runs, `linked` = status.me (None: not linked),
        `link` = what the link learns (a JID, None for a logged-out link, or a list of link_once results);
        `by_phone` = the number the manager typed to link by phone (None: they scanned the QR)."""
        state = {"problem": problem}

        def install():
            state["problem"] = None
            return {"ok": install_ok, "error": None if install_ok else "npm_failed"}

        io = FakeIO(*answers)
        status = {"ok": True, "linked": bool(linked), "me": linked}
        with mock.patch.object(whatsapp, "bridge_problem", side_effect=lambda: state["problem"]), \
                mock.patch.object(whatsapp, "reinstall_needed", return_value=reinstall), \
                mock.patch.object(whatsapp, "install_bridge", side_effect=install) as inst, \
                mock.patch.object(whatsapp, "stop_bridge", return_value=False) as stop, \
                mock.patch.object(whatsapp, "bridge_call", return_value=status), \
                mock.patch.object(whatsapp, "_link_phone", return_value=by_phone), \
                mock.patch.object(whatsapp, "link_once", side_effect=list(link) if isinstance(link, list) else
                                  [{"linked": True, "me": link} if link else {"linked": False,
                                                                                "reason": "replaced"}]) as lk:
            code = whatsapp.setup(self.ws, VALIDATE, io)
        return code, io, {"install": inst, "stop": stop, "link": lk}


class TestSetup(SetupCase):
    def test_first_run_installs_links_and_turns_whatsapp_on(self):
        before = self.read_cfg()
        code, io, m = self.setup(problem=("bridge_not_installed", "run install"), link="972500000099:3@s.whatsapp.net")
        self.assertEqual(code, 0, io.text)
        m["install"].assert_called_once()
        m["link"].assert_called_once()
        cfg = self.read_cfg()
        self.assertEqual(cfg["whatsapp"], {"enabled": True, "self_jid": SELF})
        self.assertEqual({k: v for k, v in cfg.items() if k != "whatsapp"}, before)
        self.assertEqual(list(cfg)[:-1], list(before))  # key order kept; the new block goes last
        self.assertEqual((self.ws / "config.json").read_text(), json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(VALIDATE(cfg), [])
        self.assertEqual(io.asked, [])  # no chat picking, no approval questions
        self.assertIn("asks you first", io.text)
        for text in ("hello from dana", "ship it", "note to self"):
            self.assertNotIn(text, io.text)

    def test_rerun_keeps_the_caps_and_drops_the_old_keys(self):
        self.cfg["whatsapp"] = {"enabled": True, "self_jid": SELF, "allow": [{"jid": DM, "name": "Dana"}],
                                "approve_via": ["session"], "max_prompts_per_day": 10, "max_per_day": 12,
                                "min_gap_seconds": 9, "read": True}
        self.write_cfg()
        self.assertEqual(VALIDATE(self.read_cfg()), [])  # the manager's live config validates as it is
        code, io, m = self.setup(linked=SELF)
        self.assertEqual(code, 0, io.text)
        m["install"].assert_not_called()
        m["link"].assert_not_called()
        self.assertEqual(self.read_cfg()["whatsapp"], {"enabled": True, "self_jid": SELF, "max_per_day": 12,
                                                       "min_gap_seconds": 9, "read": True})

    def test_a_logged_out_status_is_said_before_linking_again(self):
        code, io, m = self.setup(linked=None)
        self.assertEqual(code, 0, io.text)
        with mock.patch.object(whatsapp, "bridge_call", return_value={"ok": True, "linked": False, "logged_out": True}), \
                mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_link_phone", return_value=None), \
                mock.patch.object(whatsapp, "link_once", return_value={"linked": True, "me": SELF}):
            io = FakeIO()
            self.assertEqual(whatsapp._step_link(io), SELF)
        self.assertIn("WhatsApp logged this device out", io.text)

    def test_the_linked_accounts_device_suffix_is_dropped(self):
        code, io, m = self.setup(linked="972500000099:5@s.whatsapp.net")
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.read_cfg()["whatsapp"]["self_jid"], SELF)
        m["link"].assert_not_called()

    def test_a_link_that_fails_writes_nothing(self):
        before = (self.ws / "config.json").read_text()
        code, io, m = self.setup(link=None)
        self.assertEqual(code, 2)
        self.assertEqual((self.ws / "config.json").read_text(), before)

    def test_an_invalid_config_is_never_written(self):
        self.cfg["version"] = 2
        self.write_cfg()
        before = (self.ws / "config.json").read_text()
        code, io, m = self.setup(linked=SELF)
        self.assertEqual(code, 2)
        self.assertEqual((self.ws / "config.json").read_text(), before)
        self.assertIn("version", io.text)

    def test_full_disk_access_denied_stops_with_the_instruction(self):
        with mock.patch.object(whatsapp, "_query", side_effect=whatsapp.ReaderError("denied")):
            code, io, m = self.setup(linked=SELF)
        self.assertEqual(code, 2)
        self.assertIn("Full Disk Access", io.text)
        wa = self.read_cfg()["whatsapp"]
        self.assertEqual((wa["enabled"], wa["self_jid"]), (True, SELF))

    def test_no_database_says_to_install_whatsapp_desktop(self):
        self.db.unlink()
        code, io, m = self.setup(linked=SELF)
        self.assertEqual(code, 2)
        self.assertIn("WhatsApp desktop", io.text)

    def test_a_node_problem_stops_with_its_hint(self):
        code, io, m = self.setup(problem=("node_too_old", "install a newer Node"))
        self.assertEqual(code, 2)
        m["install"].assert_not_called()
        self.assertIn("install a newer Node", io.text)

    def test_a_failed_install_stops(self):
        code, io, m = self.setup(problem=("bridge_not_installed", "x"), install_ok=False)
        self.assertEqual(code, 2)
        m["link"].assert_not_called()

    def test_reinstall_needed_reinstalls_and_stops_the_old_bridge(self):
        code, io, m = self.setup(linked=SELF, reinstall=True)
        self.assertEqual(code, 0, io.text)
        m["install"].assert_called_once()
        m["stop"].assert_called()

    def test_a_private_account_id_asks_for_the_number(self):
        for kw in ({"link": "123456789012@lid"}, {"linked": "123456789012@lid"}):
            cfg = self.read_cfg()
            cfg.pop("whatsapp", None)
            self.write_cfg(cfg)
            code, io, m = self.setup("12", "+972 50 000 0099", **kw)
            self.assertEqual(code, 0, io.text)
            self.assertIn("private id", io.text)
            self.assertEqual(self.read_cfg()["whatsapp"]["self_jid"], SELF)

    def test_a_private_id_linked_by_phone_uses_the_number_already_typed(self):
        code, io, m = self.setup(link="123456789012@lid", by_phone=SELF)
        self.assertEqual(code, 0, io.text)
        self.assertNotIn("private id", io.text)
        self.assertEqual(self.read_cfg()["whatsapp"]["self_jid"], SELF)

    def test_ctrl_c_exits_130_and_changes_nothing_more(self):
        class Interrupt(FakeIO):
            def ask(self, prompt):
                raise KeyboardInterrupt
        io = Interrupt()
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "reinstall_needed", return_value=False), \
                mock.patch.object(whatsapp, "bridge_call", return_value={"ok": True, "linked": False}):
            code = whatsapp.setup(self.ws, VALIDATE, io)
        self.assertEqual(code, 130)
        self.assertIn("setup stopped; nothing more was changed", io.text)
        self.assertNotIn("whatsapp", self.read_cfg())
        self.assertFalse(list(self.ws.glob(".config*.tmp")))

    def test_config_writes_hold_a_lock(self):
        import fcntl
        self.cfg["whatsapp"] = {"enabled": False}
        self.write_cfg()
        lock = self.ws / ".mother" / "config.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        f = open(str(lock), "a")
        fcntl.flock(f, fcntl.LOCK_EX)
        done = []
        t = threading.Thread(target=lambda: done.append(whatsapp.save_whatsapp(self.ws, VALIDATE,
                                                                               lambda wa: wa.update(read=False))))
        t.start()
        time.sleep(0.3)
        self.assertEqual(done, [])  # waits for the lock
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
        t.join(5)
        self.assertEqual(self.read_cfg()["whatsapp"]["read"], False)


class FakePopen(object):
    def __init__(self, lines, code=0):
        self.stdout = iter(lines)
        self.returncode = None
        self._code = code
        self.argv = None

    def wait(self):
        self.returncode = self._code
        return self._code


class TestLinkOnce(Cmd7):
    def link(self, lines, code=0):
        fake = FakePopen(lines, code)
        io = FakeIO()

        def popen(argv, **kw):
            fake.argv, fake.kw = argv, kw
            return fake

        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_node", return_value="/usr/bin/node"), \
                mock.patch.object(whatsapp, "stop_bridge") as stop, \
                mock.patch("subprocess.Popen", side_effect=popen):
            r = whatsapp.link_once(io.say)
        stop.assert_called_once()
        return r, io, fake

    def test_the_qr_passes_through_and_the_json_line_names_the_account(self):
        r, io, fake = self.link([b"scan this\n", b"\xe2\x96\x88\xe2\x96\x88 qr\n",
                                 b'{"linked": true, "me": "972500000099:2@s.whatsapp.net"}\n'])
        self.assertEqual(r, {"linked": True, "me": SELF, "reason": None})
        self.assertEqual(fake.argv[-1], "--link")
        self.assertIn("scan this", io.text)
        self.assertNotIn('"linked"', io.text)

    def test_no_link(self):
        self.assertEqual(self.link([b'{"linked": false, "reason": "qr_expired"}\n'], code=1)[0],
                         {"linked": False, "me": None, "reason": "qr_expired"})
        self.assertEqual(self.link([b"just text\n"], code=0)[0]["linked"], False)
        self.assertEqual(self.link([b'{"linked": true, "me": "123456789@lid"}\n'])[0],
                         {"linked": True, "me": "123456789@lid", "reason": None})


class TestLinkLoop(Cmd7):
    def loop(self, results, *answers):
        io = FakeIO(*answers)
        with mock.patch.object(whatsapp, "link_once", side_effect=list(results)) as lk:
            try:
                return whatsapp.link_loop(io), io, lk
            except whatsapp.SetupStop as e:
                return e, io, lk

    def test_an_expired_qr_offers_a_new_one(self):
        me, io, lk = self.loop([{"linked": False, "reason": "qr_expired"}, {"linked": False, "reason": "qr_expired"},
                                {"linked": True, "me": SELF}], "", "", "y")
        self.assertEqual(me, SELF)
        self.assertEqual(lk.call_count, 3)
        self.assertIn("QR expired — show a new one? [Y/n]", io.asked[1])

    def test_no_to_a_new_qr_stops(self):
        e, io, lk = self.loop([{"linked": False, "reason": "qr_expired"}], "", "n")
        self.assertIsInstance(e, whatsapp.SetupStop)

    def test_logged_out_shows_a_fresh_code_in_the_same_run(self):
        me, io, lk = self.loop([{"linked": False, "reason": "logged_out"}, {"linked": True, "me": SELF}], "")
        self.assertEqual(me, SELF)
        self.assertEqual(lk.call_count, 2)
        self.assertIn("WhatsApp logged this device out; showing a fresh code", io.text)
        self.assertEqual(len(io.asked), 1)  # only how to link: no "try again?" question
        e, io, lk = self.loop([{"linked": False, "reason": "logged_out"}, {"linked": False, "reason": "logged_out"}], "")
        self.assertIsInstance(e, whatsapp.SetupStop)  # twice in a row: stop, don't loop forever
        self.assertIn("Linked devices", str(e))

    def test_replaced_or_no_account_stop_in_plain_words(self):
        for reason, words in (("replaced", "another device"), ("no_account", "which account")):
            e, io, lk = self.loop([{"linked": False, "reason": reason}], "")
            self.assertIsInstance(e, whatsapp.SetupStop)
            self.assertIn(words, str(e).lower())
            self.assertEqual(len(io.asked), 1)  # only how to link

    def test_p_links_by_phone_number_and_a_bad_number_is_asked_again(self):
        me, io, lk = self.loop([{"linked": False, "reason": "qr_expired"}, {"linked": True, "me": SELF}],
                               "p", "12", "+972 50-000-0099", "y")
        self.assertEqual(me, SELF)
        self.assertEqual([c.args[1] for c in lk.call_args_list], [SELF, SELF])
        self.assertIn("isn't a phone number", io.text)
        self.assertIn("code expired — show a new one?", io.asked[-1])

    def test_enter_or_an_empty_number_scans_the_qr(self):
        for answers in (("",), ("p", "")):
            me, io, lk = self.loop([{"linked": True, "me": SELF}], *answers)
            self.assertEqual(lk.call_args.args[1], None)

    def test_the_phone_number_reaches_the_bridge_without_the_server(self):
        fake = mock.Mock()
        fake.stdout = [b'{"linked": true, "me": "972500000099@s.whatsapp.net"}\n']
        fake.returncode = 0
        with mock.patch.object(whatsapp, "_node", return_value="/usr/bin/node"), \
                mock.patch.object(whatsapp, "stop_bridge"), \
                mock.patch("subprocess.Popen", return_value=fake) as po:
            whatsapp.link_once(lambda t: None, SELF)
        self.assertEqual(po.call_args.args[0][-3:], ["--link", "--phone", "972500000099"])

    def test_the_link_command_loops_too(self):
        class Tty(_io.StringIO):
            def isatty(self):
                return True
        out = Tty()
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value=""), mock.patch("sys.stdout", new=out), \
                mock.patch.object(whatsapp, "link_once", side_effect=[{"linked": False, "reason": "qr_expired"},
                                                                     {"linked": True, "me": SELF}]):
            self.assertEqual(whatsapp.cmd_link("link", []), 0)
        self.assertIn(SELF, out.getvalue())


class TestTerminalOnly(Cmd7):
    def test_setup_link_unlink_need_a_terminal(self):
        before = (self.ws / "config.json").read_text()
        for args in (["setup"], ["link"], ["unlink"]):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 2, (args, r.stderr))
            self.assertIn("run `mother whatsapp %s` in your own terminal" % args[0], r.stderr)
        self.assertEqual((self.ws / "config.json").read_text(), before)

    def test_phone_jid(self):
        self.assertEqual(whatsapp.phone_jid("+972-50-000-0001"), DM)
        self.assertEqual(whatsapp.phone_jid("(972) 50 000 0001"), DM)
        self.assertIsNone(whatsapp.phone_jid("12345"))
        self.assertIsNone(whatsapp.phone_jid("972500000001@s.whatsapp.net"))

    def test_help(self):
        r = self.run_cli("help")
        self.assertEqual(r.returncode, 0)
        for sub in ("chats", "read", "send --to WHO", "setup"):
            self.assertIn(sub, r.stdout)


if __name__ == "__main__":
    unittest.main()
