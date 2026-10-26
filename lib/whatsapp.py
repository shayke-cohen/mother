"""mother whatsapp — read the allowlisted WhatsApp chats from the local macOS database.

Reader half of the WhatsApp feature (docs/CONTRACT.md §5d). Everything here is home-machine-only.

Commands:
  read --since ISO [--chat JID]...   reader items (JSON) from the allowlisted chats since ISO
  chats                              recent chats, names and dates only (for setup's allowlist proposal)

The database is opened read-only (`mode=ro`). The allowlist is applied in SQL, so rows from other chats are
never fetched. Nothing is copied to disk. Python 3.9 stdlib only.
"""
import datetime as _dt
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
CORE_EPOCH = 978307200  # Core Data counts seconds from 2001-01-01 UTC
TEXT_TYPES = (0, 7)  # 0 = text, 7 = text with a link preview; every other type is media
GROUP_SESSION = 1  # ZSESSIONTYPE: 0 = DM, 1 = group


class ReaderError(Exception):
    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


# ---------------------------------------------------------------- small helpers

def db_path() -> str:
    return str(Path(os.environ.get("MOTHER_WHATSAPP_DB") or DEFAULT_DB).expanduser())


def core_ts(seconds_since_2001: float) -> str:
    t = _dt.datetime.fromtimestamp(CORE_EPOCH + float(seconds_since_2001), _dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_core(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    t = _dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.timestamp() - CORE_EPOCH


def _hostname() -> str:
    return (os.environ.get("MOTHER_HOSTNAME") or socket.gethostname()).split(".")[0].lower()


def require_home(cfg: dict) -> None:
    """Exit 2 unless this is workspace.home_machine (same semantics as `mother home check`; unset = 3)."""
    home = (cfg.get("workspace") or {}).get("home_machine")
    if not home:
        print("mother: home machine not set; WhatsApp runs only on the home machine (`mother home set`)",
              file=sys.stderr)
        sys.exit(3)
    if home != _hostname():
        print("mother: not the home machine (%s); WhatsApp runs on %s" % (_hostname(), home), file=sys.stderr)
        sys.exit(2)


def _wa(cfg: dict) -> dict:
    return cfg.get("whatsapp") or {}


# ---------------------------------------------------------------- database access

def _classify(e) -> str:
    m = str(e).lower()
    if "no such column" in m or "no such table" in m or "has no column" in m:
        return "schema_changed"
    if "locked" in m or "busy" in m:
        return "locked"
    if "authorization denied" in m or "unable to open" in m or "not authorized" in m or "permission" in m:
        return "denied"
    if isinstance(e, sqlite3.DatabaseError) and "not a database" in m:
        return "schema_changed"
    return "denied" if isinstance(e, (PermissionError, sqlite3.OperationalError)) else "schema_changed"


def _query(sql: str, params=()) -> list:
    """One read-only query. A locked database is retried once. Raises ReaderError(reason)."""
    path = db_path()
    if not os.path.exists(path):
        raise ReaderError("missing")
    uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        last = None
        for _attempt in range(2):
            try:
                return list(conn.execute(sql, tuple(params)).fetchall())
            except sqlite3.OperationalError as e:
                last = e
                if _classify(e) != "locked":
                    raise
        raise last
    except (sqlite3.Error, PermissionError) as e:
        raise ReaderError(_classify(e))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _couldnt(reason: str) -> dict:
    return {"status": "couldnt_check", "reason": reason, "items": []}


# ---------------------------------------------------------------- reader

def _allowed(cfg: dict) -> dict:
    return {a["jid"]: a.get("name") or a["jid"] for a in _wa(cfg).get("allow") or []
            if isinstance(a, dict) and a.get("jid")}


def read_messages(cfg: dict, since_iso: str, chats: list = None) -> dict:
    allowed = _allowed(cfg)
    jids = [j for j in allowed if chats is None or j in chats]  # a filter can narrow, never widen
    if not jids:
        return {"status": "ok", "items": []}
    marks = ",".join("?" * len(jids))
    sql = ("SELECT s.ZCONTACTJID, s.ZPARTNERNAME, s.ZSESSIONTYPE, m.ZISFROMME, m.ZTEXT, m.ZMESSAGETYPE, "
           "m.ZMESSAGEDATE, m.ZSTANZAID, g.ZCONTACTNAME "
           "FROM ZWAMESSAGE m JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK "
           "LEFT JOIN ZWAGROUPMEMBER g ON m.ZGROUPMEMBER = g.Z_PK "
           "WHERE s.ZCONTACTJID IN (%s) AND m.ZMESSAGEDATE >= ? ORDER BY m.ZMESSAGEDATE, m.Z_PK" % marks)
    try:
        rows = _query(sql, jids + [to_core(since_iso)])
    except ReaderError as e:
        return _couldnt(e.reason)
    items = []
    for jid, pname, stype, from_me, text, mtype, mdate, stanza, member in rows:
        name = allowed.get(jid) or pname or jid
        is_text = mtype in TEXT_TYPES and text is not None
        if from_me:
            sender = "me"
        elif stype == GROUP_SESSION:
            sender = member or "a member"
        else:
            sender = name
        items.append({"chat": jid, "chat_name": name, "from_me": bool(from_me), "sender": sender,
                      "text": text if is_text else "[media]", "at": core_ts(mdate),
                      "link": "whatsapp://%s/%s" % (jid, stanza)})
    return {"status": "ok", "items": items}


def self_messages(cfg: dict, since_iso: str) -> list:
    """The manager's own texts in the self chat (from_me only). Used as approval evidence."""
    self_jid = _wa(cfg).get("self_jid")
    if not self_jid:
        return []
    sql = ("SELECT m.ZSTANZAID, m.ZTEXT, m.ZMESSAGEDATE FROM ZWAMESSAGE m "
           "JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK "
           "WHERE s.ZCONTACTJID = ? AND m.ZISFROMME = 1 AND m.ZMESSAGETYPE IN (0,7) AND m.ZTEXT IS NOT NULL "
           "AND m.ZMESSAGEDATE >= ? ORDER BY m.ZMESSAGEDATE, m.Z_PK")
    try:
        rows = _query(sql, [self_jid, to_core(since_iso)])
    except ReaderError:
        return []
    return [{"stanza": r[0], "text": r[1], "at": core_ts(r[2])} for r in rows]


def has_history(cfg: dict, jid: str) -> bool:
    """True when the database holds at least one message in this chat. Reads no content."""
    sql = ("SELECT 1 FROM ZWAMESSAGE m JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK "
           "WHERE s.ZCONTACTJID = ? LIMIT 1")
    try:
        return bool(_query(sql, [jid]))
    except ReaderError:
        return False


def recent_chats(limit=30) -> list:
    """Chat names and dates from ZWACHATSESSION only. Never reads a message. Raises ReaderError."""
    sql = ("SELECT ZCONTACTJID, ZPARTNERNAME, ZLASTMESSAGEDATE, ZSESSIONTYPE FROM ZWACHATSESSION "
           "WHERE ZCONTACTJID IS NOT NULL AND ZCONTACTJID NOT LIKE '%broadcast' "
           "ORDER BY ZLASTMESSAGEDATE DESC LIMIT ?")
    rows = _query(sql, [int(limit)])
    return [{"jid": r[0], "name": r[1] or "", "last_at": core_ts(r[2]) if r[2] else None,
             "group": r[3] == GROUP_SESSION} for r in rows]


# ---------------------------------------------------------------- CLI

def _fail(msg, code=1):
    print("mother: %s" % msg, file=sys.stderr)
    sys.exit(code)


def cmd_read(cfg, args):
    since, chats, i = None, [], 0
    while i < len(args):
        if args[i] == "--since" and i + 1 < len(args):
            since, i = args[i + 1], i + 2
        elif args[i] == "--chat" and i + 1 < len(args):
            chats.append(args[i + 1])
            i += 2
        else:
            _fail("usage: mother whatsapp read --since ISO [--chat JID]...")
    if not since:
        _fail("usage: mother whatsapp read --since ISO [--chat JID]...")
    try:
        to_core(since)
    except ValueError:
        _fail("--since must be an ISO date-time, e.g. 2026-10-26T08:00:00Z")
    print(json.dumps(read_messages(cfg, since, chats or None), indent=2, ensure_ascii=False))


def cmd_chats(cfg, args):
    try:
        print(json.dumps(recent_chats(), indent=2, ensure_ascii=False))
    except ReaderError as e:
        print(json.dumps({"status": "couldnt_check", "reason": e.reason, "items": []}))
        sys.exit(1)


COMMANDS = {"read": cmd_read, "chats": cmd_chats}


def main(argv, cfg=None):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cmd = COMMANDS.get(argv[0])
    if not cmd:
        _fail("unknown whatsapp command '%s' — see `mother whatsapp help`" % argv[0])
    require_home(cfg or {})
    cmd(cfg or {}, argv[1:])
