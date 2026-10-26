"""Tests for lib/whatsapp.py (reader). Stdlib only: python3 -m unittest tests.test_whatsapp -v

Every test builds a fixture database with the real table and column names; the real WhatsApp database is
never opened (MOTHER_WHATSAPP_DB points at the fixture)."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
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
CORE0 = 978307200  # 2001-01-01 in unix seconds


def fixture_db(path, chats, messages, drop=()):
    """chats: [(pk, jid, name, sessiontype)]; messages: [dict]. `drop` omits columns from ZWAMESSAGE."""
    cols = ["Z_PK INTEGER PRIMARY KEY", "ZCHATSESSION INTEGER", "ZISFROMME INTEGER", "ZFROMJID TEXT",
            "ZTOJID TEXT", "ZTEXT TEXT", "ZMESSAGEDATE REAL", "ZSTANZAID TEXT", "ZMESSAGETYPE INTEGER",
            "ZPARENTMESSAGE INTEGER", "ZGROUPMEMBER INTEGER"]
    cols = [c for c in cols if c.split()[0] not in drop]
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, "
               "ZSESSIONTYPE INTEGER, ZGROUPINFO INTEGER, ZLASTMESSAGEDATE REAL)")
    db.execute("CREATE TABLE ZWAMESSAGE (%s)" % ", ".join(cols))
    db.execute("CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT, ZCONTACTNAME TEXT)")
    for pk, jid, name, st in chats:
        last = max([m["date"] for m in messages if m["chat"] == pk] or [0])
        db.execute("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?,?,?)", (pk, jid, name, st, None, last))
    for i, m in enumerate(messages, 1):
        row = {"Z_PK": i, "ZCHATSESSION": m["chat"], "ZISFROMME": m.get("me", 0), "ZFROMJID": m.get("from"),
               "ZTOJID": None, "ZTEXT": m.get("text"), "ZMESSAGEDATE": m["date"], "ZSTANZAID": m["stanza"],
               "ZMESSAGETYPE": m.get("type", 0), "ZPARENTMESSAGE": None, "ZGROUPMEMBER": m.get("member")}
        keys = [k for k in row if k not in drop]
        db.execute("INSERT INTO ZWAMESSAGE (%s) VALUES (%s)" % (",".join(keys), ",".join("?" * len(keys))),
                   [row[k] for k in keys])
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (1, ?, 'Dana')", (OTHER,))
    db.commit()
    db.close()


def core(iso):
    return whatsapp.to_core(iso)


CHATS = [(1, DM, "Sam Roe", 0), (2, GROUP, "Platform team", 1), (3, OTHER, "Stranger", 0), (4, SELF, "You", 0)]
MSGS = [
    {"chat": 1, "text": "hello from sam", "date": core("2026-10-26T08:00:00Z"), "stanza": "A1", "from": DM},
    {"chat": 1, "text": "I replied", "date": core("2026-10-26T08:05:00Z"), "stanza": "A2", "me": 1},
    {"chat": 1, "text": None, "date": core("2026-10-26T08:10:00Z"), "stanza": "A3", "from": DM, "type": 1},
    {"chat": 1, "text": "old news", "date": core("2026-10-20T08:00:00Z"), "stanza": "A0", "from": DM},
    {"chat": 2, "text": "ship it", "date": core("2026-10-26T09:00:00Z"), "stanza": "G1", "member": 1},
    {"chat": 3, "text": "secret", "date": core("2026-10-26T09:30:00Z"), "stanza": "X1", "from": OTHER},
    {"chat": 4, "text": "ok w3k", "date": core("2026-10-26T10:00:00Z"), "stanza": "S1", "me": 1},
    {"chat": 4, "text": "note to self", "date": core("2026-10-26T10:05:00Z"), "stanza": "S2", "me": 1},
]
SINCE = "2026-10-25T00:00:00Z"


def make_cfg(home="laptop-a"):
    return {"workspace": {"home_machine": home},
            "whatsapp": {"enabled": True, "self_jid": SELF, "read": True,
                         "allow": [{"jid": DM, "name": "Sam Roe"},
                                   {"jid": GROUP, "name": "Platform team", "group": True}]}}


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


class TestReader(Base):
    def test_only_allowlisted_chats(self):
        self.build()
        out = whatsapp.read_messages(make_cfg(), SINCE)
        self.assertEqual(out["status"], "ok")
        self.assertEqual({i["chat"] for i in out["items"]}, {DM, GROUP})
        self.assertNotIn("secret", json.dumps(out))

    def test_cli_read_prints_json(self):
        self.build()
        ws = self.t / "ws"
        ws.mkdir()
        (ws / "config.json").write_text(json.dumps(make_cfg()))
        env = dict(self.env, MOTHER_WORKSPACE=str(ws))
        r = subprocess.run([sys.executable, str(CLI), "whatsapp", "read", "--since", SINCE, "--chat", DM],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual({i["chat"] for i in out["items"]}, {DM})
        self.assertNotIn("secret", r.stdout)
        r = subprocess.run([sys.executable, str(CLI), "whatsapp", "chats"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Stranger", r.stdout)

    def test_items_shape_and_order(self):
        self.build()
        out = whatsapp.read_messages(make_cfg(), SINCE)
        texts = [i["text"] for i in out["items"]]
        self.assertEqual(texts, ["hello from sam", "I replied", "[media]", "ship it"])
        first = out["items"][0]
        self.assertEqual(first["link"], "whatsapp://%s/A1" % DM)
        self.assertEqual(first["chat_name"], "Sam Roe")
        self.assertEqual(first["at"], "2026-10-26T08:00:00Z")
        self.assertFalse(first["from_me"])
        self.assertTrue(out["items"][1]["from_me"])
        self.assertNotIn("old news", texts)

    def test_chat_filter_cannot_widen(self):
        self.build()
        out = whatsapp.read_messages(make_cfg(), SINCE, chats=[GROUP, OTHER])
        self.assertEqual({i["chat"] for i in out["items"]}, {GROUP})

    def test_unlisted_rows_never_fetched(self):
        self.build()
        log = []
        real = sqlite3.connect
        with mock.patch.object(sqlite3, "connect", lambda *a, **k: SpyConn(real(*a, **k), log=log)):
            whatsapp.read_messages(make_cfg(), SINCE)
        sql, params = log[0]
        self.assertIn("ZCONTACTJID IN (?,?)", sql)
        self.assertEqual(set(params[:2]), {DM, GROUP})
        self.assertNotIn(OTHER, params)
        self.assertNotIn("secret", json.dumps(log))

    def test_media_is_placeholder(self):
        self.build()
        out = whatsapp.read_messages(make_cfg(), SINCE, chats=[DM])
        self.assertIn("[media]", [i["text"] for i in out["items"]])

    def test_link_preview_type_is_text(self):
        fixture_db(self.db, CHATS, [{"chat": 1, "text": "see https://example.com", "type": 7,
                                     "date": core("2026-10-26T08:00:00Z"), "stanza": "P1", "from": DM}])
        out = whatsapp.read_messages(make_cfg(), SINCE)
        self.assertEqual(out["items"][0]["text"], "see https://example.com")

    def test_schema_changed(self):
        self.build(drop=("ZTEXT",))
        out = whatsapp.read_messages(make_cfg(), SINCE)
        self.assertEqual(out, {"status": "couldnt_check", "reason": "schema_changed", "items": []})

    def test_missing_db(self):
        out = whatsapp.read_messages(make_cfg(), SINCE)
        self.assertEqual((out["status"], out["reason"], out["items"]), ("couldnt_check", "missing", []))

    def test_locked_retry_once(self):
        self.build()
        real = sqlite3.connect
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(sqlite3, "connect", lambda *a, **k: SpyConn(real(*a, **k), fail=[locked])):
            out = whatsapp.read_messages(make_cfg(), SINCE)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(out["items"]), 4)
        with mock.patch.object(sqlite3, "connect",
                               lambda *a, **k: SpyConn(real(*a, **k), fail=[locked, locked])):
            out = whatsapp.read_messages(make_cfg(), SINCE)
        self.assertEqual(out, {"status": "couldnt_check", "reason": "locked", "items": []})

    def test_denied(self):
        self.build()
        err = sqlite3.OperationalError("unable to open database file")
        with mock.patch.object(sqlite3, "connect", side_effect=err):
            out = whatsapp.read_messages(make_cfg(), SINCE)
        self.assertEqual((out["status"], out["reason"]), ("couldnt_check", "denied"))

    def test_readonly_uri(self):
        self.build()
        calls = []
        real = sqlite3.connect

        def spy(*a, **k):
            calls.append((a, k))
            return real(*a, **k)
        with mock.patch.object(sqlite3, "connect", spy):
            whatsapp.read_messages(make_cfg(), SINCE)
        (a, k) = calls[0]
        self.assertTrue(a[0].startswith("file:") and "mode=ro" in a[0], a[0])
        self.assertTrue(k.get("uri"))

    def test_not_home_machine(self):
        r = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, %r); import whatsapp, json; "
                            "whatsapp.require_home(json.loads(sys.argv[1]))" % str(ROOT / "lib"),
                            json.dumps(make_cfg(home="elsewhere"))],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        whatsapp.require_home(make_cfg())  # home: returns quietly

    def test_cli_not_home_machine(self):
        self.build()
        ws = self.t / "ws"
        ws.mkdir()
        (ws / "config.json").write_text(json.dumps(make_cfg(home="elsewhere")))
        env = dict(self.env, MOTHER_WORKSPACE=str(ws))
        for sub in (["read", "--since", SINCE], ["chats"]):
            r = subprocess.run([sys.executable, str(CLI), "whatsapp", *sub], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertNotIn("hello", r.stdout)

    def test_group_sender_name(self):
        self.build()
        out = whatsapp.read_messages(make_cfg(), SINCE, chats=[GROUP])
        self.assertEqual(out["items"][0]["sender"], "Dana")

    def test_self_messages(self):
        self.build()
        got = whatsapp.self_messages(make_cfg(), SINCE)
        self.assertEqual([g["text"] for g in got], ["ok w3k", "note to self"])
        self.assertEqual(got[0]["stanza"], "S1")
        self.assertEqual(set(got[0]), {"stanza", "text", "at"})

    def test_has_history(self):
        self.build()
        self.assertTrue(whatsapp.has_history(make_cfg(), DM))
        self.assertFalse(whatsapp.has_history(make_cfg(), "972500000077@s.whatsapp.net"))

    def test_recent_chats_has_no_text(self):
        self.build()
        got = whatsapp.recent_chats()
        self.assertEqual({c["jid"] for c in got}, {DM, GROUP, OTHER, SELF})
        self.assertEqual(got[0]["jid"], SELF)  # newest first
        grp = [c for c in got if c["jid"] == GROUP][0]
        self.assertTrue(grp["group"])
        self.assertEqual(grp["name"], "Platform team")
        blob = json.dumps(got)
        for text in ("hello from sam", "secret", "ship it", "ok w3k", "note to self"):
            self.assertNotIn(text, blob)

    def test_recent_chats_never_touches_messages(self):
        self.build()
        log = []
        real = sqlite3.connect
        with mock.patch.object(sqlite3, "connect", lambda *a, **k: SpyConn(real(*a, **k), log=log)):
            whatsapp.recent_chats()
        self.assertTrue(log)
        for sql, _ in log:
            self.assertNotIn("ZWAMESSAGE", sql)


if __name__ == "__main__":
    unittest.main()
