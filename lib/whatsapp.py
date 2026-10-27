"""mother whatsapp — read the allowlisted WhatsApp chats from the local macOS database.

Reader half of the WhatsApp feature (docs/CONTRACT.md §5d). Everything here is home-machine-only.

Commands:
  read --since ISO [--chat JID]...   reader items (JSON) from the allowlisted chats since ISO
  chats                              recent chats, names and dates only (for setup's allowlist proposal)
  draft --chat JID --text-file PATH [--run ID]
                                     write a draft for an allowlisted chat with history (JSON)
  list [--json]                      open drafts
  drop ID                            delete a draft
  pending                            stdin {"dm": [manager DM messages]}: record Slack `ok`/`no` replies, then
                                     print {"send": [ids with a proven yes], "dropped": [ids]}

The database is opened read-only (`mode=ro`). The allowlist is applied in SQL, so rows from other chats are
never fetched. Nothing is copied to disk. Python 3.9 stdlib only.
"""
import datetime as _dt
import hashlib
import json
import os
import random
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
CORE_EPOCH = 978307200  # Core Data counts seconds from 2001-01-01 UTC
TEXT_TYPES = (0, 7)  # 0 = text, 7 = text with a link preview; every other type is media
GROUP_SESSION = 1  # ZSESSIONTYPE: 0 = DM, 1 = group
DRAFT_TTL_HOURS = 72
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz23456789"
ID_RE = re.compile(r"^w[a-z2-9]{3}$")


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
           "m.ZMESSAGEDATE, m.ZSTANZAID, COALESCE(g.ZCONTACTNAME, g.ZFIRSTNAME) "
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


# ---------------------------------------------------------------- drafts

def _drafts_dir(ws: Path) -> Path:
    return Path(ws) / ".mother" / "whatsapp" / "drafts"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _fmt(t: _dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _expired(d: dict) -> bool:
    try:
        made = _dt.datetime.strptime(d["created"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except (KeyError, ValueError, TypeError):
        return True
    return _now() - made > _dt.timedelta(hours=DRAFT_TTL_HOURS)


def _new_id(ws: Path) -> str:
    taken = set()
    for sub in ("drafts", "approved"):
        d = Path(ws) / ".mother" / "whatsapp" / sub
        if d.is_dir():
            taken.update(p.stem for p in d.glob("*.json"))
    for _ in range(200):
        i = "w" + "".join(random.SystemRandom().choice(ID_ALPHABET) for _ in range(3))
        if i not in taken:
            return i
    _fail("could not find a free draft id; drop old drafts (`mother whatsapp list`)", 1)


def new_draft(ws: Path, cfg: dict, chat: str, text: str, run: str = None) -> dict:
    """Write a draft. Exit 2 for a chat that is not allowlisted or has no history (no cold contact)."""
    allowed = _allowed(cfg)
    if chat not in allowed:
        _fail("%s is not in whatsapp.allow; drafts go only to allowlisted chats" % chat, 2)
    if not has_history(cfg, chat):
        _fail("no cold contact: %s has no history with you; start that chat yourself first" % allowed[chat], 2)
    d = {"id": _new_id(ws), "chat": chat, "chat_name": allowed[chat], "text": text,
         "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "created": _fmt(_now()),
         "run": run, "status": "open"}
    folder = _drafts_dir(ws)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (d["id"] + ".json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))
    return d


def load_draft(ws: Path, draft_id: str):
    """The draft, or None when missing, unreadable or older than 72 hours (an expired file is removed)."""
    if not ID_RE.match(draft_id or ""):
        return None
    path = _drafts_dir(ws) / (draft_id + ".json")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or _expired(d):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return d


def drop_draft(ws: Path, draft_id: str) -> bool:
    if not ID_RE.match(draft_id or ""):
        return False
    try:
        (_drafts_dir(ws) / (draft_id + ".json")).unlink()
        return True
    except OSError:
        return False


def list_drafts(ws: Path) -> list:
    folder = _drafts_dir(ws)
    if not folder.is_dir():
        return []
    out = [d for d in (load_draft(ws, p.stem) for p in sorted(folder.glob("*.json"))) if d]
    return sorted(out, key=lambda d: d["created"])


# ---------------------------------------------------------------- approval proof

PATHS = ("session", "self_chat", "slack_dm")
REPLY_RE = re.compile(r"^(ok|no)((?:[ \t]+w[a-z0-9]+)+|[ \t]+all)$", re.I)
SESSION_MAX_HOURS = DRAFT_TTL_HOURS
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_GUARD = []


def _guard():
    """hooks/guard.py, imported lazily (it imports this module lazily too; no cycle at load time)."""
    if not _GUARD:
        import importlib.util
        spec = importlib.util.spec_from_file_location("mother_guard", str(PLUGIN_ROOT / "hooks" / "guard.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GUARD.append(mod)
    return _GUARD[0]


def _state_dir() -> Path:
    return Path(os.environ.get("MOTHER_STATE_DIR") or "~/.mother").expanduser()


def _epoch(iso):
    try:
        return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def parse_reply(text):
    """("ok"|"no", [ids] | "all") when the whole message is the command, else None."""
    if not isinstance(text, str):
        return None
    m = REPLY_RE.match(text.strip())
    if not m:
        return None
    rest = m.group(2).split()
    if [r.lower() for r in rest] == ["all"]:
        return m.group(1).lower(), "all"
    return m.group(1).lower(), [r.lower() for r in rest]


def approve_via(cfg: dict) -> list:
    v = _wa(cfg).get("approve_via")
    if v is None:
        return list(PATHS)
    return [p for p in v if p in PATHS] if isinstance(v, list) else []


def _jsonl(path: Path):
    """Parsed dict lines of a JSONL file; [] when missing. Raises OSError when it exists but can't be read."""
    if not path.exists():
        return []
    out = []
    with open(str(path), encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                v = json.loads(line)
            except ValueError:
                continue
            if isinstance(v, dict):
                out.append(v)
    return out


def _prompts(ws: Path, path: str) -> list:
    """[(epoch, [ids])] of prompts.jsonl entries for one path, oldest first."""
    try:
        rows = _jsonl(Path(ws) / ".mother" / "whatsapp" / "prompts.jsonl")
    except OSError:
        return []
    out = []
    for r in rows:
        t = _epoch(r.get("at"))
        if r.get("path") == path and t is not None and isinstance(r.get("ids"), list):
            out.append((t, [str(i) for i in r["ids"]]))
    return sorted(out, key=lambda x: x[0])


def resolve_all(ws: Path, path: str, at: float, floor: float) -> list:
    """`all` = the ids of the latest prompt on this path sent at or before `at`. A prompt older than `floor` (the
    draft's creation) named an earlier draft and resolves to nothing."""
    before = [p for p in _prompts(ws, path) if p[0] <= at]
    if not before or before[-1][0] < floor:
        return []
    return before[-1][1]


def _names(word, reply, draft_id, ws, path, at, floor):
    if not reply or reply[0] != word:
        return False
    if reply[1] == "all":
        return path != "session" and draft_id in resolve_all(ws, path, at, floor)
    return draft_id in reply[1]


def _session_hit(word, ws, draft_id, created, transcript_path, now):
    if not isinstance(transcript_path, str) or not os.path.isfile(transcript_path):
        return None
    g = _guard()
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if draft_id not in line.lower():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            text = g.user_text(e)  # genuine human messages only
            ts = g.parse_time(e.get("timestamp")) if isinstance(e, dict) else None
            if not text or ts is None or ts <= created or ts > now + 60 or now - ts > SESSION_MAX_HOURS * 3600:
                continue
            if _names(word, parse_reply(text), draft_id, ws, "session", ts, created):
                return "session:%s" % (e.get("uuid") or e.get("timestamp"))
    return None


def _self_chat_hit(word, ws, cfg, draft_id, created):
    try:
        bridge = {str(r.get("stanza")) for r in _jsonl(_state_dir() / "whatsapp" / "sent.jsonl") if r.get("stanza")}
    except OSError:
        return None  # can't tell the bridge's own messages from the manager's: no proof
    floor = created
    shown = [t for t, ids in _prompts(ws, "self_chat") if draft_id in ids and t >= created]
    if shown:
        floor = max(floor, shown[0])
    for m in self_messages(cfg, _fmt(_dt.datetime.fromtimestamp(floor, _dt.timezone.utc))):
        if not m.get("stanza") or str(m["stanza"]) in bridge:
            continue  # sent by the bridge (a prompt or a send): MOTHER cannot approve itself
        at = _epoch(m.get("at"))
        if at is None or at < floor:
            continue
        if _names(word, parse_reply(m.get("text")), draft_id, ws, "self_chat", at, created):
            return "self_chat:%s" % m["stanza"]
    return None


def _slack_hit(ws, cfg, draft_id, created, now):
    g = _guard()
    scopes, max_age = g.grant_limits(cfg)
    if "whatsapp" not in scopes:
        return None
    data, ok = g.read_json(str(Path(ws) / ".mother" / "grants.json"))
    if not ok or not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        return None
    dms = {str(d) for d in g.dm_ids(cfg)}
    for gr in data["grants"]:
        v = g.valid_grant(gr, "whatsapp", draft_id, cfg, scopes, max_age, now)
        if not v:
            continue
        src = gr["source"]
        ts = g.parse_time(src.get("ts"))
        if str(src.get("channel")) not in dms or ts is None or ts <= created:
            continue  # the manager's DM only, and a yes given after this draft existed
        return "slack_dm:%s" % v[0]
    return None


def _decision(word, ws, cfg, draft_id, transcript_path, now):
    d = load_draft(ws, draft_id)
    created = _epoch((d or {}).get("created"))
    if not d or created is None or d.get("id") != draft_id:
        return None
    now = time.time() if now is None else now
    via = approve_via(cfg)
    if "session" in via:
        ev = _session_hit(word, ws, draft_id, created, transcript_path, now)
        if ev:
            return {"path": "session", "evidence": ev}
    if "self_chat" in via:
        ev = _self_chat_hit(word, ws, cfg, draft_id, created)
        if ev:
            return {"path": "self_chat", "evidence": ev}
    if word == "ok" and "slack_dm" in via:  # a Slack `no` is applied by `pending` at once (the draft is dropped)
        ev = _slack_hit(ws, cfg, draft_id, created, now)
        if ev:
            return {"path": "slack_dm", "evidence": ev}
    return None


def rejected(ws: Path, cfg: dict, draft_id: str, transcript_path=None, now=None) -> bool:
    """True when the manager said `no` to this draft on an enabled path (session or self chat)."""
    try:
        return _decision("no", ws, cfg, draft_id, transcript_path, now) is not None
    except Exception:
        return False


def prove(ws: Path, cfg: dict, draft_id: str, transcript_path=None, now=None):
    """{"path", "evidence"} for the manager's yes to this draft on an enabled path, else None. Fails closed:
    any error, a missing or expired draft, or a `no` on any path means None."""
    try:
        if _decision("no", ws, cfg, draft_id, transcript_path, now):
            return None
        return _decision("ok", ws, cfg, draft_id, transcript_path, now)
    except Exception:
        return None


def _write_grant(ws: Path, cfg: dict, draft_id: str, channel: str, ts: str) -> None:
    g = _guard()
    path = Path(ws) / ".mother" / "grants.json"
    data, ok = g.read_json(str(path))
    if not ok or not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        data = {"grants": []}
    for gr in data["grants"]:
        src = gr.get("source") if isinstance(gr, dict) else None
        if isinstance(src, dict) and gr.get("scope") == "whatsapp" and gr.get("target") == draft_id \
                and str(src.get("channel")) == channel and str(src.get("ts")) == str(ts):
            return  # this message already granted it
    sent = g.parse_time(ts)
    data["grants"].append({"scope": "whatsapp", "target": draft_id, "uses": 1, "run": "whatsapp",
                           "expires": _fmt(_dt.datetime.fromtimestamp(sent + DRAFT_TTL_HOURS * 3600,
                                                                      _dt.timezone.utc)),
                           "source": {"channel": channel, "user": str(g.owner_id(cfg)), "ts": str(ts)}})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(str(tmp), str(path))


def _own_post(cfg: dict, m: dict) -> bool:
    """A bot/app post, or the agent's own post through the manager's token (signed on its first line)."""
    if m.get("bot_id") or m.get("bot_profile") or m.get("app_id"):
        return True
    if m.get("subtype") not in (None, "", "thread_broadcast"):
        return True
    agent = cfg.get("agent") or {}
    marks = ["%s (AI)" % agent["name"]] if agent.get("name") else []
    if isinstance(agent.get("signature"), str) and agent["signature"].strip():
        marks.append(agent["signature"].strip())
    first = str(m.get("text") or "").lstrip().split("\n", 1)[0]
    return any(mk in first for mk in marks)


def pending(ws: Path, cfg: dict, dm: list) -> dict:
    """Apply the manager's Slack DM replies (grants for `ok`, drops for `no`), then list drafts with a proof."""
    g = _guard()
    owner = g.owner_id(cfg)
    chat_dm = g.as_list((cfg.get("manager") or {}).get("chat_dm"))
    dropped = []
    if "slack_dm" in approve_via(cfg) and owner and chat_dm:
        decided = set()
        msgs = [m for m in dm or [] if isinstance(m, dict) and g.parse_time(m.get("ts")) is not None]
        for m in sorted(msgs, key=lambda m: g.parse_time(m.get("ts"))):
            if str(m.get("user")) != str(owner) or _own_post(cfg, m):
                continue  # only the manager approves, and never through the agent's own text
            reply = parse_reply(m.get("text"))
            if not reply:
                continue
            at = g.parse_time(m.get("ts"))
            word, ids = reply
            if ids == "all":
                ids = [i for i in resolve_all(ws, "slack_dm", at, 0) if i not in decided]
            for i in ids:
                d = load_draft(ws, i)
                created = _epoch((d or {}).get("created"))
                if i in decided or not d or created is None or at <= created:
                    continue  # one decision per draft per batch; an old reply can't approve a newer same-id draft
                if reply[1] == "all" and i not in resolve_all(ws, "slack_dm", at, created):
                    continue  # that prompt was about an earlier draft with the same id
                decided.add(i)
                if word == "no":
                    if drop_draft(ws, i):
                        dropped.append(i)
                else:
                    _write_grant(ws, cfg, i, str(chat_dm[0]), str(m.get("ts")))
    send = []
    for d in list_drafts(ws):
        if rejected(ws, cfg, d["id"]):
            if drop_draft(ws, d["id"]):
                dropped.append(d["id"])
        elif prove(ws, cfg, d["id"]):
            send.append(d["id"])
    return {"send": send, "dropped": dropped}


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


def cmd_draft(cfg, args, ws):
    chat, text_file, run, i = None, None, None, 0
    usage = "usage: mother whatsapp draft --chat JID --text-file PATH [--run ID]"
    while i < len(args):
        if args[i] == "--chat" and i + 1 < len(args):
            chat, i = args[i + 1], i + 2
        elif args[i] == "--text-file" and i + 1 < len(args):
            text_file, i = args[i + 1], i + 2
        elif args[i] == "--run" and i + 1 < len(args):
            run, i = args[i + 1], i + 2
        else:
            _fail(usage)
    if not chat or not text_file:
        _fail(usage)
    try:
        text = Path(text_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        _fail("cannot read --text-file: %s" % e)
    text = text.strip("\n")
    if not text.strip():
        _fail("the draft text is empty")
    print(json.dumps(new_draft(ws, cfg, chat, text, run), indent=2, ensure_ascii=False))


def cmd_list(cfg, args, ws):
    if args not in ([], ["--json"]):
        _fail("usage: mother whatsapp list [--json]")
    drafts = list_drafts(ws)
    if args:
        print(json.dumps(drafts, indent=2, ensure_ascii=False))
        return
    if not drafts:
        print("no open WhatsApp drafts")
    for d in drafts:
        one = d["text"].replace("\n", " ")
        print('%s  %s  %s  "%s"' % (d["id"], d["created"], d["chat_name"], one if len(one) <= 60 else one[:57] + "..."))


def cmd_pending(cfg, args, ws):
    if args:
        _fail("usage: mother whatsapp pending  (stdin: {\"dm\": [chat messages]})")
    raw = sys.stdin.read()
    try:
        inp = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        _fail("pending: bad JSON on stdin: %s" % e)
    if not isinstance(inp, dict) or not isinstance(inp.get("dm", []), list):
        _fail("pending: stdin must be {\"dm\": [chat messages]}")
    wa = _wa(cfg)
    if not (wa.get("enabled") and wa.get("send", True)):
        print(json.dumps({"send": [], "dropped": []}))
        return
    print(json.dumps(pending(ws, cfg, inp.get("dm") or []), indent=2))


def cmd_drop(cfg, args, ws):
    if len(args) != 1:
        _fail("usage: mother whatsapp drop ID")
    if not drop_draft(ws, args[0]):
        _fail("no such draft: %s" % args[0])
    print("dropped %s" % args[0])


READ_COMMANDS = {"read": cmd_read, "chats": cmd_chats}
DRAFT_COMMANDS = {"draft": cmd_draft, "list": cmd_list, "drop": cmd_drop, "pending": cmd_pending}


def main(argv, cfg=None, ws=None):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cfg = cfg or {}
    if argv[0] not in READ_COMMANDS and argv[0] not in DRAFT_COMMANDS:
        _fail("unknown whatsapp command '%s' — see `mother whatsapp help`" % argv[0])
    require_home(cfg)
    wa = _wa(cfg)
    if argv[0] in READ_COMMANDS and not (wa.get("enabled") and wa.get("read", True)):
        _fail("whatsapp reading is off (whatsapp.enabled / whatsapp.read in config.json)", 2)
    if argv[0] == "draft" and not (wa.get("enabled") and wa.get("send", True)):
        _fail("whatsapp sending is off (whatsapp.enabled / whatsapp.send in config.json)", 2)
    if argv[0] in READ_COMMANDS:
        READ_COMMANDS[argv[0]](cfg, argv[1:])
    else:
        if ws is None:
            _fail("no workspace")
        DRAFT_COMMANDS[argv[0]](cfg, argv[1:], ws)
