"""Tests for lib/whatsapp.py (reader). Stdlib only: python3 -m unittest tests.test_whatsapp -v

Every test builds a fixture database with the real table and column names; the real WhatsApp database is
never opened (MOTHER_WHATSAPP_DB points at the fixture)."""
import datetime as _dt
import hashlib
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
    db.execute("CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT, ZCONTACTNAME TEXT, "
               "ZFIRSTNAME TEXT, ZCHATSESSION INTEGER)")
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
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (1, ?, 'Dana', NULL, 2)", (OTHER,))
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (2, ?, NULL, 'Agnes', 2)", ("972500000003@s.whatsapp.net",))
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (3, ?, NULL, NULL, 2)", ("972500000004@s.whatsapp.net",))
    db.commit()
    db.close()


def core(iso):
    return whatsapp.to_core(iso)


CHATS = [(1, DM, "Dana", 0), (2, GROUP, "PLAT team", 1), (3, OTHER, "Stranger", 0), (4, SELF, "You", 0)]
MSGS = [
    {"chat": 1, "text": "hello from dana", "date": core("2026-10-26T08:00:00Z"), "stanza": "A1", "from": DM},
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
                         "allow": [{"jid": DM, "name": "Dana"},
                                   {"jid": GROUP, "name": "PLAT team", "group": True}]}}


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
        self.assertEqual(texts, ["hello from dana", "I replied", "[media]", "ship it"])
        first = out["items"][0]
        self.assertEqual(first["link"], "whatsapp://%s/A1" % DM)
        self.assertEqual(first["chat_name"], "Dana")
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

    def test_group_sender_first_name_and_never_a_number(self):
        msgs = [{"chat": 2, "text": "a", "date": core("2026-10-26T09:00:00Z"), "stanza": "G1", "member": 2},
                {"chat": 2, "text": "b", "date": core("2026-10-26T09:01:00Z"), "stanza": "G2", "member": 3}]
        fixture_db(self.db, CHATS, msgs)
        out = whatsapp.read_messages(make_cfg(), SINCE, chats=[GROUP])
        self.assertEqual([i["sender"] for i in out["items"]], ["Agnes", "a member"])
        self.assertNotIn("97250000000", json.dumps(out))

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
        self.assertEqual(grp["name"], "PLAT team")
        blob = json.dumps(got)
        for text in ("hello from dana", "secret", "ship it", "ok w3k", "note to self"):
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


def cli(env, *args):
    return subprocess.run([sys.executable, str(CLI), "whatsapp", *args], capture_output=True, text=True, env=env)


class TestOffSwitch(Base):
    def run_with(self, wa_patch, *args):
        if not self.db.exists():
            self.build()
        ws = self.t / "ws"
        ws.mkdir(exist_ok=True)
        cfg = make_cfg()
        cfg["whatsapp"].update(wa_patch)
        (ws / "config.json").write_text(json.dumps(cfg))
        return cli(dict(self.env, MOTHER_WORKSPACE=str(ws)), *args)

    def test_reading_off(self):
        for patch in ({"enabled": False}, {"read": False}):
            for sub in (["read", "--since", SINCE], ["chats"]):
                r = self.run_with(patch, *sub)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("whatsapp reading is off", r.stderr)
                self.assertNotIn("hello", r.stdout)

    def test_sending_off(self):
        txt = self.t / "t.txt"
        txt.write_text("hi")
        for patch in ({"enabled": False}, {"send": False}):
            r = self.run_with(patch, "draft", "--chat", DM, "--text-file", str(txt))
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("whatsapp sending is off", r.stderr)


class TestDrafts(Base):
    def setUp(self):
        Base.setUp(self)
        self.build()
        self.ws = self.t / "ws"
        self.ws.mkdir()
        subprocess.run(["git", "init", "-q", str(self.ws)], check=True)
        (self.ws / ".gitignore").write_text(".mother/\n")
        self.cfg = make_cfg()
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        self.wenv = dict(self.env, MOTHER_WORKSPACE=str(self.ws))

    def test_id_hash_and_shape(self):
        d = whatsapp.new_draft(self.ws, self.cfg, DM, "héllo", run="brief-1")
        self.assertRegex(d["id"], r"^w[a-z2-9]{3}$")
        self.assertEqual(d["sha256"], hashlib.sha256("héllo".encode("utf-8")).hexdigest())
        self.assertEqual((d["chat"], d["chat_name"], d["text"], d["run"], d["status"]),
                         (DM, "Dana", "héllo", "brief-1", "open"))
        self.assertRegex(d["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(whatsapp.load_draft(self.ws, d["id"]), d)
        self.assertTrue((self.ws / ".mother/whatsapp/drafts" / (d["id"] + ".json")).exists())

    def test_used_or_failed_approval_keeps_its_id(self):
        folder = self.ws / ".mother/whatsapp/approved"
        folder.mkdir(parents=True)
        for i, suffix in (("wabc", "used"), ("wabd", "failed"), ("wabe", "json")):
            (folder / ("%s.%s" % (i, suffix))).touch()
        with mock.patch.object(whatsapp.random.SystemRandom, "choice", side_effect=list("abc") + list("abd")
                               + list("abe") + list("abf")):
            self.assertEqual(whatsapp._new_id(self.ws), "wabf")

    def test_unlisted_chat_refused(self):
        with self.assertRaises(SystemExit) as c:
            whatsapp.new_draft(self.ws, self.cfg, OTHER, "x")
        self.assertEqual(c.exception.code, 2)

    def test_cold_chat_refused(self):
        cold = "972500000077@s.whatsapp.net"
        self.cfg["whatsapp"]["allow"].append({"jid": cold, "name": "New"})
        with self.assertRaises(SystemExit) as c:
            whatsapp.new_draft(self.ws, self.cfg, cold, "x")
        self.assertEqual(c.exception.code, 2)
        self.assertEqual(list((self.ws / ".mother/whatsapp/drafts").glob("*")) if
                         (self.ws / ".mother/whatsapp/drafts").exists() else [], [])

    def test_expires_after_72h(self):
        d = whatsapp.new_draft(self.ws, self.cfg, DM, "x")
        p = self.ws / ".mother/whatsapp/drafts" / (d["id"] + ".json")
        old = json.loads(p.read_text())
        old["created"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=73)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p.write_text(json.dumps(old))
        self.assertIsNone(whatsapp.load_draft(self.ws, d["id"]))
        self.assertFalse(p.exists())
        d2 = whatsapp.new_draft(self.ws, self.cfg, DM, "y")
        p2 = self.ws / ".mother/whatsapp/drafts" / (d2["id"] + ".json")
        old = json.loads(p2.read_text())
        old["created"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=71)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p2.write_text(json.dumps(old))
        self.assertIsNotNone(whatsapp.load_draft(self.ws, d2["id"]))

    def test_ids_unique(self):
        ids = {whatsapp.new_draft(self.ws, self.cfg, DM, "t%d" % i)["id"] for i in range(30)}
        self.assertEqual(len(ids), 30)

    def test_drop(self):
        d = whatsapp.new_draft(self.ws, self.cfg, DM, "x")
        self.assertTrue(whatsapp.drop_draft(self.ws, d["id"]))
        self.assertIsNone(whatsapp.load_draft(self.ws, d["id"]))
        self.assertFalse(whatsapp.drop_draft(self.ws, d["id"]))
        self.assertFalse(whatsapp.drop_draft(self.ws, "../config"))

    def test_text_stays_under_dot_mother(self):
        whatsapp.new_draft(self.ws, self.cfg, DM, "private words")
        st = subprocess.run(["git", "-C", str(self.ws), "status", "--porcelain"], capture_output=True, text=True)
        self.assertNotIn("private words", st.stdout)
        self.assertNotIn(".mother", st.stdout)
        for p in self.ws.rglob("*"):
            if p.is_file() and ".git" not in p.parts and ".mother" not in p.parts:
                self.assertNotIn("private words", p.read_text())

    def test_cli_draft_list_drop(self):
        txt = self.t / "t.txt"
        txt.write_text("see you at 3")
        r = cli(self.wenv, "draft", "--chat", DM, "--text-file", str(txt), "--run", "r1")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["text"], "see you at 3")
        r = cli(self.wenv, "list", "--json")
        self.assertEqual([x["id"] for x in json.loads(r.stdout)], [d["id"]])
        r = cli(self.wenv, "list")
        self.assertIn(d["id"], r.stdout)
        self.assertIn("Dana", r.stdout)
        r = cli(self.wenv, "drop", d["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(cli(self.wenv, "list", "--json").stdout), [])
        self.assertEqual(cli(self.wenv, "drop", d["id"]).returncode, 1)

    def test_cli_draft_refusals(self):
        txt = self.t / "t.txt"
        txt.write_text("x")
        r = cli(self.wenv, "draft", "--chat", OTHER, "--text-file", str(txt))
        self.assertEqual(r.returncode, 2, r.stderr)
        r = cli(self.wenv, "draft", "--chat", DM, "--text-file", str(self.t / "missing.txt"))
        self.assertNotEqual(r.returncode, 0)
        txt.write_text("  \n")
        self.assertEqual(cli(self.wenv, "draft", "--chat", DM, "--text-file", str(txt)).returncode, 1)

    def test_cli_not_home(self):
        cfg = make_cfg(home="elsewhere")
        (self.ws / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(cli(self.wenv, "list").returncode, 2)



# ---------------------------------------------------------------- approval proof

MGR = "U00000001"
MGR_DM = "D00000001"


def ago(**kw):
    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(**kw)


def fmt(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestProof(Base):
    def setUp(self):
        Base.setUp(self)
        self.ws = self.t / "ws"
        (self.ws / ".mother").mkdir(parents=True)
        self.cfg = make_cfg()
        self.cfg["manager"] = {"chat_dm": MGR_DM, "chat_user": MGR}
        self.cfg["agent"] = {"name": "Agnes"}
        self.selfmsgs = []
        self.draft("w3kx", ago(hours=2))

    def draft(self, i, created, chat=DM, text="see you at 3"):
        folder = self.ws / ".mother/whatsapp/drafts"
        folder.mkdir(parents=True, exist_ok=True)
        d = {"id": i, "chat": chat, "chat_name": "Dana", "text": text,
             "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "created": fmt(created),
             "run": None, "status": "open"}
        (folder / (i + ".json")).write_text(json.dumps(d))
        return d

    def say(self, text, when, stanza):
        """A from-me text in the self chat."""
        self.selfmsgs.append({"chat": 4, "text": text, "date": core(fmt(when)), "stanza": stanza, "me": 1})
        fixture_db_fresh(self.db, self.selfmsgs)

    def sent(self, *lines):
        p = self.t / "state" / "whatsapp" / "sent.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")

    def sha(self, i):
        p = self.ws / ".mother/whatsapp/drafts" / (i + ".json")
        return json.loads(p.read_text())["sha256"] if p.exists() else "0" * 64

    def prompt(self, path, ids, when, sha=None, log=True):
        """A prompt as Task 7 records it; a self-chat prompt also lands in the DB and in sent.jsonl."""
        p = self.ws / ".mother/whatsapp/prompts.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        shas = sha if sha is not None else {i: self.sha(i) for i in ids}
        with open(p, "a") as f:
            f.write(json.dumps({"at": fmt(when), "path": path, "ids": ids, "sha256": shas}) + "\n")
        if path == "self_chat":
            stanza = "PR%d" % len(self.selfmsgs)
            self.say('WhatsApp -> Dana: "see you at 3" - reply ok %s' % " ".join(ids), when, stanza)
            if log:
                self.sent({"at": fmt(when), "kind": "prompt", "chat": SELF, "sha256": "x", "stanza": stanza})

    def transcript(self, *entries):
        p = self.t / ("t%d.jsonl" % len(list(self.t.glob("t*.jsonl"))))
        p.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return str(p)

    @staticmethod
    def user(text, when, **extra):
        e = {"type": "user", "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "origin": {"kind": "human"},
             "message": {"role": "user", "content": text}}
        e.update(extra)
        return e

    def grant(self, target="w3kx", user=MGR, channel=MGR_DM, when=None, sha=None, shown=True, sig=True):
        when = when or ago(minutes=30)
        if shown:
            self.prompt("slack_dm", [target], when - _dt.timedelta(minutes=5))
        g = {"scope": "whatsapp", "target": target, "uses": 1, "run": "whatsapp",
             "sha256": sha if sha is not None else self.sha(target),
             "expires": fmt(when + _dt.timedelta(hours=72)),
             "source": {"channel": channel, "user": user, "ts": "%.6f" % when.timestamp()}}
        if sig is True:
            g["sig"] = whatsapp.grant_sig("whatsapp", target, g["source"]["ts"], g["sha256"])
        elif sig:
            g["sig"] = sig
        p = self.ws / ".mother/grants.json"
        data = json.loads(p.read_text()) if p.exists() else {"grants": []}
        data["grants"].append(g)
        p.write_text(json.dumps(data))

    def via(self, *paths):
        self.cfg["whatsapp"]["approve_via"] = list(paths)

    # -- grammar

    def test_parse_reply(self):
        self.assertEqual(whatsapp.parse_reply("ok w3k"), ("ok", ["w3k"]))
        self.assertEqual(whatsapp.parse_reply("OK w3k w9p"), ("ok", ["w3k", "w9p"]))
        self.assertEqual(whatsapp.parse_reply("ok all"), ("ok", "all"))
        self.assertEqual(whatsapp.parse_reply("no w3k"), ("no", ["w3k"]))
        self.assertEqual(whatsapp.parse_reply("  ok w3kx \n"), ("ok", ["w3kx"]))
        for bad in ("ok w3k but change it", "okay", "w3k ok", "ok", "ok w3k all", "", None,
                    "WhatsApp -> Dana: \"hi\" - reply ok w3k", "ok w3k\nalso this"):
            self.assertIsNone(whatsapp.parse_reply(bad), bad)

    # -- session

    def test_session_ok(self):
        self.via("session")
        t = self.transcript(self.user("ok w3kx", ago(minutes=10)))
        got = whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t)
        self.assertEqual(got["path"], "session")
        self.assertTrue(got["evidence"])
        t = self.transcript(self.user("ok w9pa", ago(minutes=10)))
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t))
        t = self.transcript(self.user("OK W3KX", ago(minutes=10)))
        self.assertEqual(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t)["path"], "session")
        t = self.transcript(self.user("ok w3kx", ago(hours=1)))
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t,
                                         now=_dt.datetime.now(_dt.timezone.utc).timestamp() + 72 * 3600))

    def test_session_ignores_tool_results_and_sidechain(self):
        self.via("session")
        when = ago(minutes=10)
        for e in (self.user("ok w3kx", when, isSidechain=True),
                  self.user("ok w3kx", when, toolUseResult={}),
                  self.user("ok w3kx", when, isMeta=True),
                  self.user("ok w3kx", when, origin={"kind": "peer"}),
                  self.user("ok w3kx", when, origin=None),
                  self.user([{"type": "tool_result", "content": "ok w3kx"}], when),
                  {"type": "assistant", "timestamp": fmt(when),
                   "message": {"role": "assistant", "content": "ok w3kx"}}):
            t = self.transcript(e)
            self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t), e)

    def test_session_before_draft_ignored(self):
        self.via("session")
        t = self.transcript(self.user("ok w3kx", ago(hours=3)))  # the draft was created 2 h ago
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t))

    def test_session_ok_all_never_counts(self):
        self.via("session")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        t = self.transcript(self.user("ok all", ago(minutes=10)))
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t))

    def test_session_no_rejects(self):
        self.via("session")
        t = self.transcript(self.user("ok w3kx", ago(minutes=20)), self.user("no w3kx", ago(minutes=10)))
        self.assertTrue(whatsapp.rejected(self.ws, self.cfg, "w3kx", transcript_path=t))
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t))

    # -- self chat

    def test_self_chat_ok(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("ok w3kx", ago(minutes=30), "M1")
        got = whatsapp.prove(self.ws, self.cfg, "w3kx")
        self.assertEqual(got["path"], "self_chat")
        self.assertIn("M1", got["evidence"])

    def test_self_chat_ignores_bridge_sent(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("ok w3kx", ago(minutes=30), "B1")
        self.sent({"at": fmt(ago(minutes=30)), "kind": "send", "chat": SELF, "sha256": "x", "stanza": "B1"})
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_self_chat_prompt_text_never_counts(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        prompt = 'WhatsApp -> Dana: "see you at 3" - reply ok w3kx'
        self.say(prompt, ago(minutes=30), "P1")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))  # even before sent.jsonl has it
        self.sent({"at": fmt(ago(minutes=30)), "kind": "prompt", "chat": SELF, "sha256": "x", "stanza": "P1"})
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_self_chat_before_draft_ignored(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("ok w3kx", ago(hours=3), "M0")  # an old yes for an earlier draft with the same id
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_self_chat_unreadable_sent_log_fails_closed(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1), log=False)
        self.say("ok w3kx", ago(minutes=30), "M1")
        (self.t / "state" / "whatsapp" / "sent.jsonl").mkdir(parents=True)  # exists, cannot be read as a file
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_ok_all_resolves_latest_prompt_only(self):
        self.via("self_chat")
        self.draft("w9pa", ago(hours=2))
        self.prompt("self_chat", ["w3kx", "w9pa"], ago(minutes=90))
        self.prompt("slack_dm", ["w3kx", "w9pa"], ago(minutes=70))  # another path's prompt does not count
        self.prompt("self_chat", ["w9pa"], ago(minutes=60))
        self.say("ok all", ago(minutes=30), "M1")
        self.prompt("self_chat", ["w3kx"], ago(minutes=10))  # after the yes: does not count
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))
        self.assertEqual(whatsapp.prove(self.ws, self.cfg, "w9pa")["path"], "self_chat")

    def test_ok_all_ignores_prompt_older_than_draft(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=3))  # named an earlier draft that had the same id
        self.say("ok all", ago(minutes=30), "M1")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_self_chat_no_rejects(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("no w3kx", ago(minutes=30), "M1")
        self.assertTrue(whatsapp.rejected(self.ws, self.cfg, "w3kx"))
        self.say("ok w3kx", ago(minutes=20), "M2")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    # -- slack dm grant

    def test_slack_grant_ok(self):
        self.via("slack_dm")
        self.grant()
        got = whatsapp.prove(self.ws, self.cfg, "w3kx")
        self.assertEqual(got["path"], "slack_dm")
        self.assertIn(MGR_DM, got["evidence"])

    def test_slack_grant_needs_valid_signature(self):
        self.via("slack_dm")
        self.grant(sig=False)  # any agent can write grants.json; only `pending` holds the key
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))
        self.grant(sig="0" * 64)
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))
        (self.ws / ".mother/grants.json").unlink()
        self.grant()
        g = json.loads((self.ws / ".mother/grants.json").read_text())["grants"][0]
        self.assertTrue(whatsapp.grant_sig_ok(g))
        for k, v in (("target", "w9pa"), ("sha256", "f" * 64)):
            self.assertFalse(whatsapp.grant_sig_ok(dict(g, **{k: v})))
        self.assertFalse(whatsapp.grant_sig_ok(dict(g, source=dict(g["source"], ts="1.0"))))
        key = self.t / "state" / "whatsapp" / "grant.key"
        self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")
        self.assertEqual(whatsapp.prove(self.ws, self.cfg, "w3kx")["path"], "slack_dm")
        key.unlink()  # no key, no valid signature
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_malformed_signature_is_unsigned(self):
        self.via("slack_dm")
        self.grant()
        g = json.loads((self.ws / ".mother/grants.json").read_text())["grants"][0]
        for bad in ("é" * 64, "\x00" * 64, 123, None, ["a"], {"a": 1}, ""):
            self.assertFalse(whatsapp.grant_sig_ok(dict(g, sig=bad)), repr(bad))
        (self.ws / ".mother/grants.json").unlink()
        self.grant(sig="é" * 64)  # one bad grant in the agent-writable file must not take `prove` down
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_slack_grant_from_manager_only(self):
        self.via("slack_dm")
        self.grant(user="U00000002")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_slack_grant_checks(self):
        self.via("slack_dm")
        self.cfg["teams"] = [{"id": "plat", "channels": [{"id": "C00000001", "mode": "mention"}]}]
        self.grant(channel="C00000001")  # the manager, but not in the DM
        self.grant(target="w9pa")  # another draft
        self.grant(when=ago(hours=3))  # before this draft existed
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    # -- paths

    def test_path_disabled(self):
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("ok w3kx", ago(minutes=30), "M1")
        self.grant()
        t = self.transcript(self.user("ok w3kx", ago(minutes=10)))
        self.via("session")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))
        self.via("self_chat")
        self.assertEqual(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t)["path"], "self_chat")
        self.via("slack_dm")
        self.assertEqual(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t)["path"], "slack_dm")

    def test_missing_or_expired_draft(self):
        self.say("ok w9pa", ago(minutes=30), "M1")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w9pa"))
        self.draft("w4ab", ago(hours=73))
        self.say("ok w4ab", ago(minutes=20), "M2")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w4ab"))

    # -- fix round 1: fail closed, bind to the text

    def test_self_chat_needs_prompt(self):
        self.via("self_chat")
        self.sent({"at": fmt(ago(hours=1)), "kind": "send", "chat": DM, "sha256": "x", "stanza": "Z1"})
        self.say("ok w3kx", ago(minutes=30), "M1")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))  # never shown in the self chat
        self.prompt("slack_dm", ["w3kx"], ago(hours=1))
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))  # shown on another path only
        self.prompt("self_chat", ["w3kx"], ago(minutes=40), sha={"w3kx": "f" * 64})
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))  # shown with another text
        self.say("no w3kx", ago(minutes=20), "M2")
        self.assertFalse(whatsapp.rejected(self.ws, self.cfg, "w3kx"))

    def test_self_chat_ok_all_needs_matching_hash(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1), sha={"w3kx": "f" * 64})
        self.say("ok all", ago(minutes=30), "M1")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_self_chat_missing_sent_log_fails_closed(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1), log=False)
        self.say("ok w3kx", ago(minutes=30), "M1")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_self_chat_malformed_sent_line_fails_closed(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("ok w3kx", ago(minutes=30), "M1")
        self.assertEqual(whatsapp.prove(self.ws, self.cfg, "w3kx")["path"], "self_chat")
        p = self.t / "state" / "whatsapp" / "sent.jsonl"
        good = p.read_text()
        for bad in ({"at": fmt(ago(minutes=30)), "kind": "send", "chat": SELF, "sha256": "x"},
                    {"at": fmt(ago(minutes=30)), "kind": "send", "chat": SELF, "sha256": "x", "stanza": ""}):
            p.write_text(good + json.dumps(bad) + "\n")
            self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"), bad)
        p.write_text(good + "{not json\n")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_self_chat_survives_a_sent_line_with_null_stanza(self):
        """M4: the bridge writes `"stanza": null` when WhatsApp returned no id. That well-formed line is
        skipped, not malformed: it must not switch the self-chat path off for good."""
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("ok w3kx", ago(minutes=30), "M1")
        self.sent({"at": fmt(ago(minutes=40)), "kind": "send", "chat": DM, "draft": "w9pa", "sha256": "x",
                   "stanza": None},
                  {"at": fmt(ago(minutes=35)), "kind": "prompt", "chat": SELF, "sha256": "y", "stanza": None})
        self.assertEqual(whatsapp.prove(self.ws, self.cfg, "w3kx")["path"], "self_chat")

    def test_self_chat_null_stanza_never_counts(self):
        self.via("self_chat")
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("ok w3kx", ago(minutes=30), None)
        self.say("ok w3kx", ago(minutes=25), "")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def rewrite(self, i, text, sha=None):
        p = self.ws / ".mother/whatsapp/drafts" / (i + ".json")
        d = json.loads(p.read_text())
        d["text"] = text
        if sha:
            d["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        p.write_text(json.dumps(d))

    def test_proof_returns_sha(self):
        self.via("session")
        t = self.transcript(self.user("ok w3kx", ago(minutes=10)))
        got = whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t)
        self.assertEqual(got["sha256"], hashlib.sha256(b"see you at 3").hexdigest())

    def test_text_changed_after_yes(self):
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("ok w3kx", ago(minutes=30), "M1")
        self.grant()
        t = self.transcript(self.user("ok w3kx", ago(minutes=10)))
        for via in ("self_chat", "slack_dm", "session"):
            self.via(via)
            self.assertIsNotNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t), via)
        self.rewrite("w3kx", "wire the money")  # stale stored hash
        for via in ("self_chat", "slack_dm", "session"):
            self.via(via)
            self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t), via)
        self.rewrite("w3kx", "wire the money", sha=True)  # consistent file, but not what was shown
        for via in ("self_chat", "slack_dm"):
            self.via(via)
            self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t), via)

    def test_slack_needs_prompt_and_matching_hash(self):
        self.via("slack_dm")
        self.grant(shown=False)
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))
        (self.ws / ".mother/grants.json").unlink()
        self.grant(sha="f" * 64)
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_db_error_hides_nothing(self):
        self.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.say("no w3kx", ago(minutes=40), "M1")
        self.grant()
        t = self.transcript(self.user("ok w3kx", ago(minutes=10)))
        self.via("self_chat", "slack_dm", "session")
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t))
        with mock.patch.object(whatsapp, "_query", side_effect=whatsapp.ReaderError("locked")):
            self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t))
        self.db.unlink()
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t))
        self.via("slack_dm", "session")  # self chat off: the DB is not needed
        self.assertEqual(whatsapp.prove(self.ws, self.cfg, "w3kx", transcript_path=t)["path"], "session")

    def test_slack_source_must_be_dm_channel(self):
        self.via("slack_dm")
        self.grant(channel=MGR)  # the manager's user id is not the DM channel
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))
        (self.ws / ".mother/grants.json").unlink()
        self.grant()
        self.assertIsNotNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))
        self.cfg["manager"] = {"chat_user": MGR}  # no DM channel: the Slack path is unavailable
        self.assertIsNone(whatsapp.prove(self.ws, self.cfg, "w3kx"))

    def test_pending_needs_slack_prompt(self):
        fixture_db_fresh(self.db, [])
        out = self.pending([self.slack("ok w3kx", ago(minutes=5))])
        self.assertEqual(out["send"], [])
        self.assertEqual(self.grants(), [])
        self.prompt("slack_dm", ["w3kx"], ago(minutes=30), sha={"w3kx": "f" * 64})
        out = self.pending([self.slack("ok w3kx", ago(minutes=4))])
        self.assertEqual((out["send"], self.grants()), ([], []))

    def test_pending_locks_grants(self):
        import fcntl
        import time as _time
        fixture_db_fresh(self.db, [])
        self.prompt("slack_dm", ["w3kx"], ago(minutes=30))
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        env = dict(self.env, MOTHER_WORKSPACE=str(self.ws))
        lock = open(str(self.ws / ".mother" / "grants.lock"), "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            pr = subprocess.Popen([sys.executable, str(CLI), "whatsapp", "pending"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            pr.stdin.write(json.dumps({"dm": [self.slack("ok w3kx", ago(minutes=5))]}))
            pr.stdin.close()
            _time.sleep(1.5)
            self.assertIsNone(pr.poll())  # waiting for the lock
            self.assertEqual(self.grants(), [])
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        out = json.loads(pr.stdout.read())
        pr.wait(timeout=30)
        self.assertEqual(out["send"], ["w3kx"])
        self.assertEqual(len(self.grants()), 1)

    def test_draft_refuses_self_chat(self):
        fixture_db_fresh(self.db, [{"chat": 4, "text": "x", "date": core("2026-10-20T08:00:00Z"),
                                    "stanza": "S0", "me": 1}])
        self.cfg["whatsapp"]["allow"].append({"jid": SELF, "name": "Me"})
        with self.assertRaises(SystemExit) as c:
            whatsapp.new_draft(self.ws, self.cfg, SELF, "ok w9pa")
        self.assertEqual(c.exception.code, 2)
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        txt = self.t / "t.txt"
        txt.write_text("ok w9pa")
        r = cli(dict(self.env, MOTHER_WORKSPACE=str(self.ws)), "draft", "--chat", SELF, "--text-file", str(txt))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("self chat", r.stderr)

    # -- mother whatsapp pending

    def pending(self, dm):
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        env = dict(self.env, MOTHER_WORKSPACE=str(self.ws))
        r = subprocess.run([sys.executable, str(CLI), "whatsapp", "pending"], input=json.dumps({"dm": dm}),
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    @staticmethod
    def slack(text, when, user=MGR, **extra):
        m = {"user": user, "ts": "%.6f" % when.timestamp(), "text": text}
        m.update(extra)
        return m

    def grants(self):
        p = self.ws / ".mother/grants.json"
        return json.loads(p.read_text())["grants"] if p.exists() else []

    def test_pending_cli(self):
        fixture_db_fresh(self.db, [])
        self.prompt("slack_dm", ["w3kx"], ago(minutes=30))
        yes = self.slack("ok w3kx", ago(minutes=5))
        out = self.pending([yes])
        self.assertEqual(out, {"send": ["w3kx"], "dropped": []})
        g = self.grants()
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0]["scope"], g[0]["target"], g[0]["uses"]), ("whatsapp", "w3kx", 1))
        self.assertEqual((g[0]["source"]["channel"], g[0]["source"]["user"]), (MGR_DM, MGR))
        self.assertEqual(g[0]["sha256"], self.sha("w3kx"))
        self.assertTrue(whatsapp.grant_sig_ok(g[0]))
        self.pending([yes])  # the same message again: no second grant
        self.assertEqual(len(self.grants()), 1)

    def test_pending_ignores_others_and_own_posts(self):
        fixture_db_fresh(self.db, [])
        out = self.pending([
            self.slack("ok w3kx", ago(minutes=5), user="U00000002"),
            self.slack("ok w3kx", ago(minutes=5), bot_id="B1"),
            self.slack("ok w3kx", ago(minutes=5), subtype="bot_message"),
            self.slack('Agnes (AI)\nWhatsApp -> Dana: "see you at 3" - reply ok w3kx', ago(minutes=5)),
            self.slack("ok w3kx", ago(hours=3)),  # before the draft existed
        ])
        self.assertEqual(out, {"send": [], "dropped": []})
        self.assertEqual(self.grants(), [])

    def test_pending_ok_all_and_no(self):
        fixture_db_fresh(self.db, [])
        self.draft("w9pa", ago(hours=2))
        self.draft("w4ab", ago(hours=2))
        self.prompt("slack_dm", ["w3kx", "w4ab"], ago(minutes=90))
        self.prompt("slack_dm", ["w9pa"], ago(minutes=60))
        out = self.pending([self.slack("no w4ab", ago(minutes=8)), self.slack("ok all", ago(minutes=5))])
        self.assertEqual(out, {"send": [], "dropped": ["w4ab"]})  # `ok all` never counts on Slack (I3)
        self.assertEqual(self.grants(), [])
        self.assertIsNone(whatsapp.load_draft(self.ws, "w4ab"))
        self.assertIsNotNone(whatsapp.load_draft(self.ws, "w3kx"))

    def test_pending_slack_explicit_ids_still_count(self):
        fixture_db_fresh(self.db, [])
        self.draft("w9pa", ago(hours=2))
        self.prompt("slack_dm", ["w3kx", "w9pa"], ago(minutes=60))
        out = self.pending([self.slack("ok w9pa", ago(minutes=5))])
        self.assertEqual(out, {"send": ["w9pa"], "dropped": []})

    def test_pending_self_chat(self):
        self.draft("w9pa", ago(hours=2))
        self.prompt("self_chat", ["w3kx", "w9pa"], ago(hours=1))
        self.say("ok w3kx", ago(minutes=30), "M1")
        self.say("no w9pa", ago(minutes=20), "M2")
        out = self.pending([])
        self.assertEqual(out, {"send": ["w3kx"], "dropped": ["w9pa"]})

    def test_pending_slack_path_off(self):
        fixture_db_fresh(self.db, [])
        self.via("session", "self_chat")
        out = self.pending([self.slack("ok w3kx", ago(minutes=5))])
        self.assertEqual(out["send"], [])


class TestVerify(Base):
    """`mother whatsapp verify <id>`: the bridge's own re-check of the yes, from the evidence in approved/<id>.json."""

    def setUp(self):
        Base.setUp(self)
        self.p = TestProof("test_parse_reply")  # borrow its fixture helpers on this workspace
        self.p.t, self.p.db, self.p.env = self.t, self.db, self.env
        self.ws = self.p.ws = self.t / "ws"
        (self.ws / ".mother").mkdir(parents=True)
        self.cfg = self.p.cfg = make_cfg()
        self.cfg["manager"] = {"chat_dm": MGR_DM, "chat_user": MGR}
        self.cfg["agent"] = {"name": "Agnes"}
        self.p.selfmsgs = []
        fixture_db_fresh(self.db, [])
        self.d = self.p.draft("w3kx", ago(hours=2))
        self.t_ok = self.p.transcript(self.p.user("ok w3kx", ago(minutes=10)))

    def approve(self, path="session", sha=None, transcript=True, **extra):
        """approved/<id>.json as the guard writes it."""
        rec = {"id": "w3kx", "chat": DM, "sha256": sha or self.d["sha256"], "path": path,
               "evidence": "%s:x" % path, "at": fmt(ago(minutes=5))}
        if transcript is True:
            rec["transcript"] = self.t_ok
        elif transcript:
            rec["transcript"] = transcript
        rec.update(extra)
        folder = self.ws / ".mother/whatsapp/approved"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "w3kx.json").write_text(json.dumps(rec))
        return rec

    def verify(self, i="w3kx"):
        return whatsapp.verify(self.ws, self.cfg, i)

    def test_session_yes_verifies(self):
        self.approve()
        self.assertEqual(self.verify(), (True, "w3kx: verified via session"))

    def test_no_record(self):
        ok, why = self.verify()
        self.assertFalse(ok)
        self.assertIn("no approval", why)
        (self.ws / ".mother/whatsapp/approved").mkdir(parents=True)
        (self.ws / ".mother/whatsapp/approved/w3kx.json").write_text("{not json")
        self.assertFalse(self.verify()[0])
        self.approve()
        self.assertFalse(self.verify("w9pa")[0])  # a record for another id proves nothing
        self.assertFalse(self.verify("../w3kx")[0])

    def test_record_for_other_id_or_chat(self):
        self.approve(id="w9pa")
        self.assertFalse(self.verify()[0])
        self.approve(chat=GROUP)
        self.assertFalse(self.verify()[0])
        self.approve(path="email")
        self.assertFalse(self.verify()[0])

    def test_draft_text_changed_after_approval(self):
        self.approve()
        text = "see you at 4"
        d = dict(self.d, text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
        (self.ws / ".mother/whatsapp/drafts/w3kx.json").write_text(json.dumps(d))  # consistent, but not what was approved
        ok, why = self.verify()
        self.assertFalse(ok)
        d["sha256"] = self.d["sha256"]  # text edited, stored hash left alone
        (self.ws / ".mother/whatsapp/drafts/w3kx.json").write_text(json.dumps(d))
        self.assertFalse(self.verify()[0])

    def test_record_hash_differs(self):
        self.approve(sha="0" * 64)
        ok, why = self.verify()
        self.assertFalse(ok)
        self.assertIn("hash", why)

    def test_session_needs_recorded_transcript(self):
        self.approve(transcript=False)
        self.assertFalse(self.verify()[0])
        self.approve(transcript=self.p.transcript(self.p.user("ok w9pa", ago(minutes=10))))
        self.assertFalse(self.verify()[0])
        self.approve(transcript=str(self.t / "gone.jsonl"))
        self.assertFalse(self.verify()[0])

    def test_no_after_yes(self):
        t = self.p.transcript(self.p.user("ok w3kx", ago(minutes=20)), self.p.user("no w3kx", ago(minutes=5)))
        self.approve(transcript=t)
        self.assertFalse(self.verify()[0])

    def test_expired_or_dropped_draft(self):
        self.approve()
        whatsapp.drop_draft(self.ws, "w3kx")
        self.assertFalse(self.verify()[0])

    def test_self_chat_yes_verifies(self):
        self.p.prompt("self_chat", ["w3kx"], ago(hours=1))
        self.p.say("ok w3kx", ago(minutes=30), "M1")
        self.approve(path="self_chat", transcript=False)
        self.assertEqual(self.verify(), (True, "w3kx: verified via self_chat"))

    def test_slack_yes_verifies_after_its_grant_is_used(self):
        self.p.grant()
        self.approve(path="slack_dm", transcript=False)
        self.assertTrue(self.verify()[0])  # the guard consumed the grant's one use; the proof still stands

    def test_bound_to_the_hash_the_bridge_sends(self):
        """M1: --sha256 is the hash of the bytes the bridge is about to send; verify must agree with it too."""
        self.approve()
        self.assertTrue(whatsapp.verify(self.ws, self.cfg, "w3kx", sha=self.d["sha256"])[0])
        ok, why = whatsapp.verify(self.ws, self.cfg, "w3kx", sha="0" * 64)
        self.assertFalse(ok)
        self.assertIn("hash", why)
        self.assertFalse(whatsapp.verify(self.ws, self.cfg, "w3kx", sha="")[0])

    def test_only_the_recorded_path(self):
        """M8: a yes on another path doesn't stand in for the one the guard recorded."""
        self.approve(path="slack_dm")  # recorded slack, but only the session (recorded transcript) has a yes
        self.assertFalse(self.verify()[0])
        self.approve(path="self_chat")
        self.assertFalse(self.verify()[0])
        self.approve(path="session")
        self.assertTrue(self.verify()[0])

    def test_read_only_on_an_expired_draft(self):
        """M6: verify never deletes or changes anything, not even an expired draft."""
        self.approve()
        f = self.ws / ".mother/whatsapp/drafts/w3kx.json"
        d = json.loads(f.read_text())
        d["created"] = fmt(ago(hours=73))
        f.write_text(json.dumps(d))
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.ws.rglob("*"))
        ok, why = self.verify()
        self.assertFalse(ok)
        self.assertIn("expired", why)
        self.assertTrue(f.exists())
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.ws.rglob("*")), before)

    def test_cli(self):
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        env = dict(self.env, MOTHER_WORKSPACE=str(self.ws))
        r = cli(env, "verify", "w3kx")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no approval", r.stderr)
        self.approve()
        r = cli(env, "verify", "w3kx")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(self.d["text"], r.stdout + r.stderr)
        self.assertEqual(cli(env, "verify", "w3kx", "--sha256", self.d["sha256"]).returncode, 0)
        r = cli(env, "verify", "w3kx", "--sha256", "0" * 64)
        self.assertEqual(r.returncode, 1)
        self.assertIn("hash", r.stderr)
        self.assertEqual(cli(env, "verify", "w3kx", "--sha256").returncode, 1)
        self.assertEqual(cli(env, "verify").returncode, 1)
        self.assertEqual(cli(env, "verify", "w3kx", "w9pa").returncode, 1)
        self.cfg["whatsapp"]["send"] = False
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        self.assertEqual(cli(env, "verify", "w3kx").returncode, 2)
        self.cfg["whatsapp"]["send"] = True
        (self.ws / "config.json").write_text(json.dumps(self.cfg))
        r = cli(dict(env, MOTHER_HOSTNAME="laptop-b"), "verify", "w3kx")
        self.assertNotEqual(r.returncode, 0)


def fixture_db_fresh(path, self_msgs):
    """A fixture DB with history in the DM chat plus the given self-chat messages."""
    if Path(path).exists():
        Path(path).unlink()
    hist = [{"chat": 1, "text": "hello from dana", "date": core("2026-10-20T08:00:00Z"), "stanza": "A1", "from": DM}]
    fixture_db(path, CHATS, hist + list(self_msgs))



# ---------------------------------------------------------------- Task 7: send, prompt, link, status

import socket as _socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


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
        self.cfg["whatsapp"]["send"] = True
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

    def draft(self, text="see you at 3", chat=DM):
        return whatsapp.new_draft(self.ws, self.cfg, chat, text)

    def approve(self, d):
        f = self.ws / ".mother/whatsapp/approved"
        f.mkdir(parents=True, exist_ok=True)
        (f / (d["id"] + ".json")).write_text(json.dumps({"id": d["id"], "chat": d["chat"], "sha256": d["sha256"],
                                                          "path": "session", "evidence": "session:x"}))

    def prompts(self):
        p = self.ws / ".mother/whatsapp/prompts.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def run_cli(self, *args):
        return cli(self.wenv, *args)


class TestCliSend(Cmd7):
    def test_no_approval_file_refuses_without_calling_the_bridge(self):
        b = self.start_bridge()
        d = self.draft()
        r = self.run_cli("send", d["id"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(b.requests, [])
        self.assertEqual(whatsapp.load_draft(self.ws, d["id"])["status"], "open")

    def test_send_calls_the_bridge_and_marks_the_draft_sent(self):
        b = self.start_bridge()
        d = self.draft()
        self.approve(d)
        r = self.run_cli("send", d["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"ok": True, "stanza": "STANZA1"})
        self.assertEqual(b.requests, [{"op": "send", "draft": d["id"]}])
        self.assertEqual(whatsapp.load_draft(self.ws, d["id"])["status"], "sent")
        self.assertEqual(whatsapp.list_drafts(self.ws), [])  # a sent draft is not open
        # the CLI leaves the approval to the bridge
        self.assertTrue((self.ws / ".mother/whatsapp/approved" / (d["id"] + ".json")).exists())

    def test_not_installed_exits_2_and_keeps_the_draft_open(self):
        d = self.draft()
        self.approve(d)
        empty = self.t / "nobin"
        empty.mkdir()
        self.wenv["PATH"] = str(empty)  # no node on PATH: the CLI can't start a bridge, and must not try
        with mock.patch.object(whatsapp, "NODE_DIRS", ()), mock.patch("glob.glob", return_value=[]):
            r = whatsapp.bridge_call({"op": "send", "draft": d["id"]})
        self.assertEqual(r["error"], "bridge_not_installed")
        self.assertIn("mother whatsapp install", r["hint"])
        r = self.run_cli("send", d["id"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(json.loads(r.stdout)["error"], ("bridge_not_installed", "bridge_down"))
        self.assertEqual(whatsapp.load_draft(self.ws, d["id"])["status"], "open")

    def test_dead_socket_is_bridge_down(self):
        d = self.draft()
        self.approve(d)
        self.sock.parent.mkdir(parents=True)
        s = _socket.socket(_socket.AF_UNIX)
        s.bind(str(self.sock))
        s.close()  # a socket file nobody listens on
        out = __import__("io").StringIO()
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "_start_bridge"), mock.patch.object(whatsapp, "START_WAIT", 0.4), \
                mock.patch("sys.stdout", new=out), self.assertRaises(SystemExit) as cm:
            whatsapp.main(["send", d["id"]], self.cfg, self.ws)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "bridge_down")
        self.assertEqual(whatsapp.load_draft(self.ws, d["id"])["status"], "open")

    def test_already_sent_marks_the_draft_sent_and_exits_0(self):
        self.start_bridge(send={"ok": False, "error": "already_sent"})
        d = self.draft()
        self.approve(d)
        r = self.run_cli("send", d["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["already_sent"], True)
        self.assertEqual(whatsapp.load_draft(self.ws, d["id"])["status"], "sent")

    def test_a_failed_mark_still_reports_the_send(self):
        self.start_bridge()
        d = self.draft()
        self.approve(d)
        with mock.patch.object(whatsapp, "_mark_sent", side_effect=OSError("disk")), \
                mock.patch("sys.stdout", new=__import__("io").StringIO()) as out:
            whatsapp.cmd_send(self.cfg, [d["id"]], self.ws)
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "stanza": "STANZA1"})

    def test_bridge_refusal_exits_2_with_the_code(self):
        self.start_bridge(send={"ok": False, "error": "cap"})
        d = self.draft()
        self.approve(d)
        r = self.run_cli("send", d["id"])
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout), {"ok": False, "error": "cap"})
        self.assertIn("cap", r.stderr)
        self.assertEqual(whatsapp.load_draft(self.ws, d["id"])["status"], "open")

    def test_bad_id_and_off_switch(self):
        self.assertEqual(self.run_cli("send", "../x").returncode, 1)
        self.assertEqual(self.run_cli("send").returncode, 1)
        self.cfg["whatsapp"]["send"] = False
        self.write_cfg()
        r = self.run_cli("send", "w3kx")
        self.assertEqual(r.returncode, 2)
        self.assertIn("sending is off", r.stderr)


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
            r = whatsapp.bridge_call({"op": "send", "draft": "w3kx"})
        self.assertEqual(r["error"], "bridge_down")

    def test_start_spawns_node_detached_with_stdin_closed_and_a_log(self):
        with mock.patch.object(whatsapp, "_node", return_value="/usr/bin/node"), \
                mock.patch("subprocess.Popen") as popen:
            whatsapp._start_bridge()
        argv = popen.call_args[0][0]
        kw = popen.call_args[1]
        self.assertEqual(argv, ["/usr/bin/node", str(self.t / "state/whatsapp/app/bridge.mjs")])  # outside the plugin
        self.assertEqual(kw["env"]["MOTHER_PLUGIN_ROOT"], str(ROOT))
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdout"].name, str(self.t / "state/whatsapp/bridge.log"))
        self.assertEqual(oct((self.t / "state/whatsapp").stat().st_mode & 0o777), "0o700")

    def test_timeouts_default_60_for_send_and_prompt_5_for_status(self):
        seen = []

        def fake(req, timeout):
            seen.append((req["op"], timeout))
            return {"ok": True}

        with mock.patch.object(whatsapp, "_exchange", side_effect=fake):
            for op in ("send", "prompt", "status"):
                whatsapp.bridge_call({"op": op})
            whatsapp.bridge_call({"op": "send"}, timeout=7)
        self.assertEqual(seen, [("send", 60.0), ("prompt", 60.0), ("status", 5.0), ("send", 7)])

    def test_slow_bridge_is_a_timeout_reply(self):
        self.start_bridge(status=lambda req: (__import__("time").sleep(1.0), {"ok": True})[1])
        self.assertEqual(whatsapp.bridge_call({"op": "status"}, timeout=0.2), {"ok": False, "error": "bridge_timeout"})


class TestCliPrompt(Cmd7):
    def quiet_window(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        a, b = now - _dt.timedelta(hours=1), now + _dt.timedelta(hours=1)
        self.cfg["manager"]["quiet_hours"] = "%s-%s" % (a.strftime("%H:%M"), b.strftime("%H:%M"))
        self.write_cfg()

    def test_prompt_lists_every_id_ends_with_the_reply_hint_and_records_each_path(self):
        b = self.start_bridge()
        d1, d2 = self.draft("see you at 3"), self.draft("line one\nline two")
        r = self.run_cli("prompt", d1["id"], d2["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["ids"], [d1["id"], d2["id"]])
        self.assertEqual(out["self_chat"], {"ok": True, "stanza": "STANZA1"})
        self.assertEqual(out["slack_dm"]["channel"], "D00000001")
        stext = out["slack_dm"]["text"]
        text = b.requests[0]["text"]
        self.assertEqual(b.requests, [{"op": "prompt", "text": text}])
        self.assertEqual(stext.splitlines()[-1], "Reply with explicit ids: ok %s %s  |  no <id>" % (d1["id"], d2["id"]))
        self.assertNotIn("ok all", stext)  # Slack takes explicit ids only (I3)
        for d in (d1, d2):
            self.assertIn(d["id"], text)
        self.assertIn('%s WhatsApp -> Dana: "see you at 3"' % d1["id"], text)
        self.assertIn('%s WhatsApp -> Dana: "line one\n> line two"' % d2["id"], text)
        last = text.splitlines()[-1]
        self.assertEqual(last, "Reply with: ok %s %s  |  ok all  |  no <id>" % (d1["id"], d2["id"]))
        rec = self.prompts()
        self.assertEqual(sorted(x["path"] for x in rec), ["self_chat", "slack_dm"])
        for x in rec:
            self.assertEqual(x["ids"], [d1["id"], d2["id"]])
            self.assertEqual(x["sha256"], {d1["id"]: d1["sha256"], d2["id"]: d2["sha256"]})
            self.assertRegex(x["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_only_enabled_paths_run(self):
        b = self.start_bridge()
        self.cfg["whatsapp"]["approve_via"] = ["slack_dm", "session"]
        self.write_cfg()
        d = self.draft()
        out = json.loads(self.run_cli("prompt", d["id"]).stdout)
        self.assertNotIn("self_chat", out)
        self.assertEqual(b.requests, [])
        self.assertEqual([x["path"] for x in self.prompts()], ["slack_dm"])
        self.cfg["whatsapp"]["approve_via"] = ["self_chat"]
        self.write_cfg()
        out = json.loads(self.run_cli("prompt", d["id"]).stdout)
        self.assertNotIn("slack_dm", out)
        self.assertEqual(len(b.requests), 1)

    def test_quiet_hours_hold_everything(self):
        b = self.start_bridge()
        self.quiet_window()
        d = self.draft()
        r = self.run_cli("prompt", d["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"held": True})
        self.assertEqual(b.requests, [])
        self.assertEqual(self.prompts(), [])

    def test_every_id_must_be_an_open_draft(self):
        b = self.start_bridge()
        d = self.draft()
        for ids in (["w9pa"], [d["id"], "w9pa"], []):
            r = self.run_cli("prompt", *ids)
            self.assertNotEqual(r.returncode, 0)
        self.assertEqual(b.requests, [])
        self.assertEqual(self.prompts(), [])
        whatsapp._mark_sent(self.ws, d["id"])
        self.assertEqual(self.run_cli("prompt", d["id"]).returncode, 2)

    def test_a_draft_whose_hash_no_longer_matches_is_refused(self):
        self.start_bridge()
        d = self.draft()
        f = self.ws / ".mother/whatsapp/drafts" / (d["id"] + ".json")
        f.write_text(json.dumps(dict(d, text="something else")))
        self.assertEqual(self.run_cli("prompt", d["id"]).returncode, 2)
        self.assertEqual(self.prompts(), [])

    def test_bridge_failure_records_nothing_for_self_chat_but_slack_still_goes(self):
        self.start_bridge(prompt={"ok": False, "error": "cap"})
        d = self.draft()
        r = self.run_cli("prompt", d["id"])
        self.assertEqual(r.returncode, 2, r.stderr)  # any enabled path failing exits 2; results are in the JSON
        out = json.loads(r.stdout)
        self.assertEqual(out["self_chat"], {"ok": False, "error": "cap"})
        self.assertEqual(out["slack_dm"]["ok"], True)
        self.assertEqual([x["path"] for x in self.prompts()], ["slack_dm"])

    def test_all_paths_failing_exits_2(self):
        self.cfg["whatsapp"]["approve_via"] = ["self_chat"]
        self.write_cfg()
        self.start_bridge(prompt={"ok": False, "error": "cap"})
        d = self.draft()
        r = self.run_cli("prompt", d["id"])
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.prompts(), [])

    def test_log_failed_still_counts_as_shown(self):
        self.cfg["whatsapp"]["approve_via"] = ["self_chat"]
        self.write_cfg()
        self.start_bridge(prompt={"ok": False, "error": "log_failed", "stanza": "S9"})
        d = self.draft()
        self.run_cli("prompt", d["id"])
        self.assertEqual([x["path"] for x in self.prompts()], ["self_chat"])

    def test_no_chat_dm_skips_slack(self):
        self.cfg["manager"].pop("chat_dm")
        self.write_cfg()
        self.start_bridge()
        d = self.draft()
        r = self.run_cli("prompt", d["id"])
        self.assertEqual(r.returncode, 2)
        out = json.loads(r.stdout)
        self.assertEqual(out["slack_dm"], {"ok": False, "error": "no_chat_dm"})
        self.assertEqual([x["path"] for x in self.prompts()], ["self_chat"])

    def test_prompts_recorded_here_are_what_the_proof_reads(self):
        self.start_bridge()
        d = self.draft()
        self.run_cli("prompt", d["id"])
        created = whatsapp._epoch(d["created"])
        for path in ("self_chat", "slack_dm"):
            self.assertEqual(len(whatsapp._shown(self.ws, path, d["id"], d["sha256"], created)), 1)

    def test_the_bridges_own_check_accepts_the_text(self):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        drafts = [self.draft("see you at 3"), self.draft("ok w3kx\nok all\nno w3kx"),
                  self.draft("multi\nline text")]
        text = whatsapp.prompt_text(drafts) + "\n" + whatsapp.prompt_text(drafts, allow_all=False)
        script = ("import {looksLikeReply} from %s; "
                  "process.stdout.write(String(looksLikeReply(process.argv[1])))"
                  % json.dumps((ROOT / "bridge/whatsapp/checks.mjs").as_uri()))
        r = subprocess.run([node, "--input-type=module", "-e", script, text], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, "false"), r.stderr)
        # and the check does fire on a bare reply, so this is not vacuous
        r = subprocess.run([node, "--input-type=module", "-e", script, "hi\nok all"], capture_output=True, text=True)
        self.assertEqual(r.stdout, "true")


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
            self.assertEqual(call.call_args[1]["env"]["MOTHER_PLUGIN_ROOT"], str(ROOT))
            self.assertNotIn("stdout", call.call_args[1])  # inherits the terminal: no pipes

    def test_link_needs_a_terminal(self):
        code, call, stop = self.link("link", tty=False)
        self.assertEqual(code, 2)
        call.assert_not_called()
        stop.assert_not_called()

    def test_link_without_an_install_says_how(self):
        out = __import__("io").StringIO()
        with mock.patch.object(whatsapp, "bridge_problem", return_value=("bridge_not_installed", "run `mother whatsapp install`")), \
                mock.patch("sys.stdout.isatty", return_value=True), mock.patch("sys.stdout", new=out), \
                mock.patch("subprocess.call") as call:
            self.assertEqual(whatsapp.cmd_link("unlink", []), 2)
        call.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["error"], "bridge_not_installed")

    def test_status_reports_the_bridge_and_a_read_check(self):
        self.start_bridge(status={"ok": True, "linked": True, "me": SELF, "sent_today": 1, "prompts_today": 0})
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
        out = __import__("io").StringIO()
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
                mock.patch("sys.stdout", new=__import__("io").StringIO()):
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
        out = __import__("io").StringIO()
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
        out = __import__("io").StringIO()
        with self.fake_node(), mock.patch.object(whatsapp, "_run_npm", return_value=1), \
                mock.patch("sys.stdout", new=out), self.assertRaises(SystemExit) as cm:
            whatsapp.main(["install"], self.cfg, self.ws)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "npm_failed")
        out = __import__("io").StringIO()
        with self.fake_node(major=18), mock.patch.object(whatsapp, "_run_npm") as npm, \
                mock.patch("sys.stdout", new=out), self.assertRaises(SystemExit):
            whatsapp.main(["install"], self.cfg, self.ws)
        npm.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["error"], "node_too_old")

    def test_reinstall_needed_when_the_plugins_files_differ(self):
        with self.fake_node(), mock.patch.object(whatsapp, "_run_npm", return_value=0), \
                mock.patch("sys.stdout", new=__import__("io").StringIO()):
            whatsapp.main(["install"], self.cfg, self.ws)
        app = self.t / "state/whatsapp/app"
        self.assertFalse(whatsapp.reinstall_needed())
        (app / "checks.mjs").write_text("// changed\n")
        self.assertTrue(whatsapp.reinstall_needed())
        out = __import__("io").StringIO()
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
            r = whatsapp.bridge_call({"op": "send", "draft": "w3kx"})
        self.assertEqual(r, {"ok": True, "stanza": "S"})
        self.assertGreater(seen[0], 3)  # the send came only after status stopped saying connecting

    def test_unlinked_bridge_is_not_waited_for(self):
        self.start_bridge(status={"ok": True, "linked": False}, send={"ok": False, "error": "unlinked"})
        # already running: no start, no wait
        with mock.patch.object(whatsapp, "_start_bridge") as st:
            self.assertEqual(whatsapp.bridge_call({"op": "send", "draft": "w3kx"})["error"], "unlinked")
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



# ---------------------------------------------------------------- setup, allow, disallow (Task 7b)

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

    def setup(self, *answers, linked=None, link=SELF, problem=None, reinstall=False, install_ok=True):
        """Run setup with the bridge faked: `problem` until install runs, `linked` = status.me (None: not linked),
        `link` = what the QR link learns (a JID, None for a logged-out link, or a list of link_once results)."""
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
                mock.patch.object(whatsapp, "link_once", side_effect=list(link) if isinstance(link, list) else
                                  [{"linked": True, "me": link} if link else {"linked": False,
                                                                                "reason": "logged_out"}]) as lk:
            code = whatsapp.setup(self.ws, VALIDATE, io)
        return code, io, {"install": inst, "stop": stop, "link": lk}


class TestSetup(SetupCase):
    def test_first_run_installs_links_writes_the_block_and_adds_the_chats_picked(self):
        before = self.read_cfg()
        code, io, m = self.setup("3 2", "y", "n", problem=("bridge_not_installed", "run install"),
                                 link="972500000099:3@s.whatsapp.net")
        self.assertEqual(code, 0, io.text)
        m["install"].assert_called_once()
        m["link"].assert_called_once()
        cfg = self.read_cfg()
        self.assertEqual(cfg["whatsapp"], {
            "enabled": True, "self_jid": SELF,
            "allow": [{"jid": DM, "name": "Dana"}, {"jid": GROUP, "name": "PLAT team", "group": True}],
            "approve_via": ["session", "self_chat"]})
        self.assertEqual({k: v for k, v in cfg.items() if k != "whatsapp"}, before)
        self.assertEqual(list(cfg)[:-1], list(before))  # key order kept; the new block goes last
        self.assertEqual((self.ws / "config.json").read_text(),
                         json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(VALIDATE(cfg), [])
        listed = [ln for ln in io.lines if __import__("re").match(r"^\s*\d+\. ", ln)]
        self.assertEqual(len(listed), 3, io.text)  # the self chat is not offered
        self.assertIn("Stranger", listed[0])
        self.assertIn("group", listed[1])
        for text in ("hello from dana", "secret", "ship it", "note to self"):
            self.assertNotIn(text, io.text)  # names and dates only, never a message
        self.assertIn("ok <id>", io.text)  # the next step
        self.assertIn("Dana", io.lines[-1] + io.lines[-2] + io.text[-300:])

    def test_rerun_skips_what_is_done_and_keeps_the_block(self):
        self.cfg["whatsapp"] = {"enabled": True, "self_jid": SELF, "allow": [{"jid": DM, "name": "Dana"}],
                                "approve_via": ["session", "self_chat"], "max_per_day": 12}
        self.write_cfg()
        before = (self.ws / "config.json").read_text()
        code, io, m = self.setup("", "", "", linked=SELF)
        self.assertEqual(code, 0, io.text)
        m["install"].assert_not_called()
        m["link"].assert_not_called()
        self.assertEqual(json.loads(before), self.read_cfg())
        dana = [ln for ln in io.lines if "Dana" in ln and "DM" in ln]
        self.assertTrue(dana and "allowed" in dana[0], io.text)

    def test_the_linked_accounts_device_suffix_is_dropped(self):
        code, io, m = self.setup("", "n", "n", linked="972500000099:5@s.whatsapp.net")
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.read_cfg()["whatsapp"]["self_jid"], SELF)
        self.assertEqual(self.read_cfg()["whatsapp"]["approve_via"], ["session"])
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

    def test_full_disk_access_denied_stops_before_the_chat_list(self):
        real = whatsapp._query

        def q(sql, params=()):
            if "ZWACHATSESSION" in sql:
                raise whatsapp.ReaderError("denied")
            return real(sql, params)

        with mock.patch.object(whatsapp, "_query", side_effect=q):
            code, io, m = self.setup(linked=SELF)
        self.assertEqual(code, 2)
        self.assertIn("Full Disk Access", io.text)
        self.assertEqual(io.asked, [])
        wa = self.read_cfg()["whatsapp"]
        self.assertEqual((wa["enabled"], wa["self_jid"]), (True, SELF))

    def test_no_database_says_to_install_whatsapp_desktop(self):
        self.db.unlink()
        code, io, m = self.setup(linked=SELF)
        self.assertEqual(code, 2)
        self.assertIn("WhatsApp desktop", io.text)

    def test_slack_dm_is_offered_only_with_a_chat_dm(self):
        self.cfg["manager"]["chat_dm"] = ""
        self.write_cfg()
        code, io, m = self.setup("", "y", linked=SELF)  # no third question
        self.assertEqual(code, 0, io.text)
        self.assertEqual(len(io.asked), 2)
        self.assertEqual(self.read_cfg()["whatsapp"]["approve_via"], ["session", "self_chat"])
        self.cfg["manager"]["chat_dm"] = "U00000000"
        self.write_cfg()
        code, io, m = self.setup("", "n", "y", linked=SELF)
        self.assertEqual(self.read_cfg()["whatsapp"]["approve_via"], ["session", "slack_dm"])

    def test_bad_picks_are_asked_again(self):
        code, io, m = self.setup("9", "x", "1", "n", "n", linked=SELF)
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.read_cfg()["whatsapp"]["allow"], [{"jid": OTHER, "name": "Stranger"}])

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
        code, io, m = self.setup("", "n", "n", linked=SELF, reinstall=True)
        self.assertEqual(code, 0, io.text)
        m["install"].assert_called_once()
        m["stop"].assert_called()


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
                                {"linked": True, "me": SELF}], "", "y")
        self.assertEqual(me, SELF)
        self.assertEqual(lk.call_count, 3)
        self.assertIn("QR expired — show a new one? [Y/n]", io.asked[0])

    def test_no_to_a_new_qr_stops(self):
        e, io, lk = self.loop([{"linked": False, "reason": "qr_expired"}], "n")
        self.assertIsInstance(e, whatsapp.SetupStop)

    def test_logged_out_or_replaced_stop_in_plain_words(self):
        for reason, words in (("logged_out", "logged"), ("replaced", "another device")):
            e, io, lk = self.loop([{"linked": False, "reason": reason}])
            self.assertIsInstance(e, whatsapp.SetupStop)
            self.assertIn(words, str(e).lower())
            self.assertEqual(io.asked, [])

    def test_the_link_command_loops_too(self):
        class Tty(__import__("io").StringIO):
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


class TestSetupRound1(SetupCase):
    def test_a_private_account_id_asks_for_the_number(self):
        for kw in ({"link": "123456789012@lid"}, {"linked": "123456789012@lid"}):
            del_cfg = self.read_cfg()
            del_cfg.pop("whatsapp", None)
            self.write_cfg(del_cfg)
            code, io, m = self.setup("12", "+972 50 000 0099", "", "n", "n", **kw)
            self.assertEqual(code, 0, io.text)
            self.assertIn("private id", io.text)
            self.assertNotIn("didn't complete", io.text)
            self.assertEqual(self.read_cfg()["whatsapp"]["self_jid"], SELF)

    def test_hidden_id_chats_are_shown_and_skipped(self):
        db = sqlite3.connect(str(self.db))
        db.execute("INSERT INTO ZWACHATSESSION VALUES (9, '987654321012345@lid', 'Hidden One', 0, NULL, ?)",
                   (core("2026-10-27T08:00:00Z"),))
        db.commit()
        db.close()
        code, io, m = self.setup("1 4", "n", "n", linked=SELF)
        self.assertEqual(code, 0, io.text)
        first = [ln for ln in io.lines if ln.strip().startswith("1.")][0]
        self.assertIn("Hidden One", first)
        self.assertIn("id hidden by WhatsApp", first)
        self.assertIn("skipped", io.text)
        self.assertEqual(self.read_cfg()["whatsapp"]["allow"], [{"jid": DM, "name": "Dana"}])

    def test_ctrl_c_exits_130_and_changes_nothing_more(self):
        class Interrupt(FakeIO):
            def ask(self, prompt):
                raise KeyboardInterrupt
        io = Interrupt()
        status = {"ok": True, "linked": True, "me": SELF}
        with mock.patch.object(whatsapp, "bridge_problem", return_value=None), \
                mock.patch.object(whatsapp, "reinstall_needed", return_value=False), \
                mock.patch.object(whatsapp, "bridge_call", return_value=status):
            code = whatsapp.setup(self.ws, VALIDATE, io)
        self.assertEqual(code, 130)
        self.assertIn("setup stopped; nothing more was changed", io.text)
        self.assertEqual(VALIDATE(self.read_cfg()), [])
        self.assertEqual(whatsapp.allow_chat(self.ws, VALIDATE, Interrupt(), []), 130)
        self.assertFalse(list(self.ws.glob(".config*.tmp")))

    def test_config_writes_hold_a_lock(self):
        import fcntl
        import threading as th
        self.cfg["whatsapp"] = {"enabled": False}
        self.write_cfg()
        lock = self.ws / ".mother" / "config.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        f = open(str(lock), "a")
        fcntl.flock(f, fcntl.LOCK_EX)
        done = []
        t = th.Thread(target=lambda: done.append(whatsapp.save_whatsapp(self.ws, VALIDATE,
                                                                        lambda wa: wa.update(read=False))))
        t.start()
        time.sleep(0.3)
        self.assertEqual(done, [])  # waits for the lock
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
        t.join(5)
        self.assertEqual(self.read_cfg()["whatsapp"]["read"], False)


class TestAllow(SetupCase):
    def setUp(self):
        SetupCase.setUp(self)
        self.cfg["whatsapp"] = {"enabled": True, "self_jid": SELF, "allow": [], "approve_via": ["session"]}
        self.write_cfg()

    def test_allow_needs_setup_first(self):
        del self.cfg["whatsapp"]
        self.write_cfg()
        code, io = self.allow(DM)
        self.assertEqual(code, 2)
        self.assertIn("mother whatsapp setup", io.text)
        self.assertNotIn("whatsapp", self.read_cfg())

    def allow(self, *args, answers=()):
        io = FakeIO(*answers)
        code = whatsapp.allow_chat(self.ws, VALIDATE, io, list(args))
        return code, io

    def allowed(self):
        return (self.read_cfg().get("whatsapp") or {}).get("allow")

    def test_a_phone_number_becomes_a_jid_named_from_the_database(self):
        code, io = self.allow("+972-50-000-0001")
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.allowed(), [{"jid": DM, "name": "Dana"}])
        self.assertEqual(VALIDATE(self.read_cfg()), [])

    def test_a_list_index_and_an_explicit_name(self):
        self.assertEqual(self.allow("2")[0], 0)
        self.assertEqual(self.allow(DM, "--name", "Dana L")[0], 0)
        self.assertEqual(self.allowed(), [{"jid": GROUP, "name": "PLAT team", "group": True},
                                          {"jid": DM, "name": "Dana L"}])

    def test_no_argument_shows_the_list_and_asks(self):
        code, io = self.allow(answers=("1",))
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.allowed(), [{"jid": OTHER, "name": "Stranger"}])

    def test_refusals(self):
        before = (self.ws / "config.json").read_text()
        for args in (["+972 50 000 0007"],  # no history: no cold contact
                     [SELF],                # the self chat carries approvals
                     ["abc"], ["12"], ["123@lid"]):
            code, io = self.allow(*args)
            self.assertEqual(code, 2, (args, io.text))
        self.assertEqual((self.ws / "config.json").read_text(), before)

    def test_a_hidden_id_chat_cannot_be_allowed(self):
        db = sqlite3.connect(str(self.db))
        db.execute("INSERT INTO ZWACHATSESSION VALUES (9, '987654321012345@lid', 'Hidden One', 0, NULL, ?)",
                   (core("2026-10-27T08:00:00Z"),))
        db.commit()
        db.close()
        code, io = self.allow("1")
        self.assertEqual(code, 2)
        self.assertIn("id hidden by WhatsApp", io.text)

    def test_already_allowed_is_left_alone(self):
        self.allow(DM)
        code, io = self.allow("972500000001")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.allowed()), 1)
        self.assertIn("already", io.text)

    def test_unreadable_database_adds_with_a_note(self):
        with mock.patch.object(whatsapp, "_query", side_effect=whatsapp.ReaderError("denied")):
            code, io = self.allow("972500000005", "--name", "Agnes")
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.allowed(), [{"jid": "972500000005@s.whatsapp.net", "name": "Agnes"}])
        self.assertIn("couldn't check", io.text)

    def test_disallow(self):
        self.allow(DM)
        self.allow(GROUP)
        io = FakeIO()
        self.assertEqual(whatsapp.disallow_chat(self.ws, VALIDATE, io, ["+972 50 000 0001"]), 0)
        self.assertEqual(self.allowed(), [{"jid": GROUP, "name": "PLAT team", "group": True}])
        self.assertEqual(whatsapp.disallow_chat(self.ws, VALIDATE, FakeIO(), [DM]), 1)
        self.assertEqual(whatsapp.disallow_chat(self.ws, VALIDATE, FakeIO(), []), 2)


class TestTerminalOnly(Cmd7):
    def test_setup_allow_disallow_need_a_terminal(self):
        before = (self.ws / "config.json").read_text()
        for args in (["setup"], ["allow", DM], ["disallow", DM]):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 2, (args, r.stderr))
            self.assertIn("run `mother whatsapp %s` in your own terminal" % args[0], r.stderr)
        self.assertEqual((self.ws / "config.json").read_text(), before)

    def test_off_messages_point_at_setup(self):
        self.cfg["whatsapp"]["enabled"] = False
        self.write_cfg()
        self.assertIn("mother whatsapp setup", self.run_cli("chats").stderr)

    def test_phone_jid(self):
        self.assertEqual(whatsapp.phone_jid("+972-50-000-0001"), DM)
        self.assertEqual(whatsapp.phone_jid("(972) 50 000 0001"), DM)
        self.assertIsNone(whatsapp.phone_jid("12345"))
        self.assertIsNone(whatsapp.phone_jid("972500000001@s.whatsapp.net"))


if __name__ == "__main__":
    unittest.main()
