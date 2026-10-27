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
  send ID                            send an approved draft through the bridge (the guard requires a proven yes)
  prompt ID [ID]...                  ask the manager to approve open drafts: self-chat via the bridge, Slack DM
                                     text returned for the skill to post; held during quiet hours
  install                            copy the bridge out of the plugin into <state>/whatsapp/app and npm ci there
  stop                               stop a running bridge (link and unlink do it first)
  link | unlink                      pair the bridge with WhatsApp (QR in this terminal) | log it out, delete keys
  setup                              the manager's guided setup, in their own terminal: install, link, learn
                                     self_jid, write the whatsapp block, check database access, pick chats,
                                     pick approval paths. Safe to re-run
  allow [NUMBER|JID|N] [--name NAME] add a chat to whatsapp.allow (N = the n-th chat setup lists; no argument:
                                     show that list and add every number typed). Own terminal only
  disallow JID|NUMBER                remove a chat from whatsapp.allow. Own terminal only
  status                             bridge state and whether the local database is readable (JSON)
  verify ID [--sha256 HEX]           exit 0 only if approved/ID.json still proves a yes, on the path it
                                     records, for the draft's current text (and HEX, the hash the bridge is
                                     about to send); else exit 1. Read-only. The bridge runs it per send

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
    """The manager's own texts in the self chat (from_me only); [] when the database can't be read."""
    if not _wa(cfg).get("self_jid"):
        return []
    try:
        return self_messages_strict(cfg, since_iso)
    except ReaderError:
        return []


def self_messages_strict(cfg: dict, since_iso: str) -> list:
    """Like self_messages, but raises ReaderError when the self chat can't be read. Approval proof uses this:
    a list that silently comes back empty could hide the manager's `no`."""
    self_jid = _wa(cfg).get("self_jid")
    if not self_jid:
        raise ReaderError("missing")
    sql = ("SELECT m.ZSTANZAID, m.ZTEXT, m.ZMESSAGEDATE FROM ZWAMESSAGE m "
           "JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK "
           "WHERE s.ZCONTACTJID = ? AND m.ZISFROMME = 1 AND m.ZMESSAGETYPE IN (0,7) AND m.ZTEXT IS NOT NULL "
           "AND m.ZMESSAGEDATE >= ? ORDER BY m.ZMESSAGEDATE, m.Z_PK")
    rows = _query(sql, [self_jid, to_core(since_iso)])
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
            taken.update(p.name.split(".")[0] for p in d.iterdir())  # approved/<id>.used|.failed keep the id
    for _ in range(200):
        i = "w" + "".join(random.SystemRandom().choice(ID_ALPHABET) for _ in range(3))
        if i not in taken:
            return i
    _fail("could not find a free draft id; drop old drafts (`mother whatsapp list`)", 1)


def new_draft(ws: Path, cfg: dict, chat: str, text: str, run: str = None) -> dict:
    """Write a draft. Exit 2 for a chat that is not allowlisted or has no history (no cold contact)."""
    allowed = _allowed(cfg)
    if chat == _wa(cfg).get("self_jid"):
        _fail("%s is your own self chat, which carries approvals; drafts can't go there" % chat, 2)
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


def load_draft(ws: Path, draft_id: str, expire: bool = True):
    """The draft, or None when missing, unreadable or older than 72 hours (an expired file is removed, unless
    `expire` is False: the proof and `verify` only read)."""
    if not ID_RE.match(draft_id or ""):
        return None
    path = _drafts_dir(ws) / (draft_id + ".json")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or _expired(d):
        if not expire:
            return None
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
    out = [d for d in (load_draft(ws, p.stem) for p in sorted(folder.glob("*.json")))
           if d and d.get("status") != "sent"]  # `send` marks a draft sent; it is no longer open
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


def _prompts(ws: Path, path: str) -> list:
    """[(epoch, [ids], {id: sha256})] of prompts.jsonl entries for one path, oldest first."""
    p = Path(ws) / ".mother" / "whatsapp" / "prompts.jsonl"
    out = []
    try:
        with open(str(p), encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("path") != path:
            continue
        t, ids, shas = _epoch(r.get("at")), r.get("ids"), r.get("sha256")
        if t is not None and isinstance(ids, list) and isinstance(shas, dict):
            out.append((t, [str(i) for i in ids], {str(k): str(v) for k, v in shas.items()}))
    return sorted(out, key=lambda x: x[0])


def _shown(ws: Path, path: str, draft_id: str, sha: str, created: float, until: float = None) -> list:
    """Times a prompt on this path showed this draft with this exact text, after the draft existed."""
    return [t for t, ids, shas in _prompts(ws, path)
            if t >= created and (until is None or t <= until) and draft_id in ids and shas.get(draft_id) == sha]


def _all_covers(ws: Path, path: str, at: float, draft_id: str, sha: str, created: float) -> bool:
    """`all` = the latest prompt on this path sent at or before `at`. It covers this draft only if that prompt
    came after the draft existed and showed this draft's current text."""
    before = [p for p in _prompts(ws, path) if p[0] <= at]
    if not before:
        return False
    t, ids, shas = before[-1]
    return t >= created and draft_id in ids and shas.get(draft_id) == sha


def resolve_all(ws: Path, path: str, at: float) -> list:
    """The ids of the latest prompt on this path sent at or before `at`."""
    before = [p for p in _prompts(ws, path) if p[0] <= at]
    return list(before[-1][1]) if before else []


def _bridge_stanzas():
    """Stanza ids the bridge sent (sent.jsonl), or None when that can't be told: the file is missing or
    unreadable, or any line is malformed or lacks a stanza id. A line whose stanza is JSON null (WhatsApp
    returned no id) is well-formed and skipped: there is no id to recognise, and it must not shut the path."""
    p = _state_dir() / "whatsapp" / "sent.jsonl"
    try:
        with open(str(p), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            return None
        if isinstance(r, dict) and "stanza" in r and r["stanza"] is None:
            continue
        if not isinstance(r, dict) or not isinstance(r.get("stanza"), str) or not r["stanza"].strip():
            return None
        out.add(r["stanza"])
    return out


class _Case(object):
    """Everything one proof needs about one draft, read once."""

    def __init__(self, ws, cfg, draft_id, transcript_path, now):
        self.ws, self.cfg, self.id, self.transcript = ws, cfg, draft_id, transcript_path
        self.now = time.time() if now is None else now
        self.via = approve_via(cfg)
        self.ok = False
        d = load_draft(ws, draft_id, expire=False)  # a proof only reads
        self.created = _epoch((d or {}).get("created"))
        if not d or self.created is None or d.get("id") != draft_id or not isinstance(d.get("text"), str):
            return
        self.sha = hashlib.sha256(d["text"].encode("utf-8")).hexdigest()
        if d.get("sha256") != self.sha:
            return  # the text changed after the draft was written: nothing approves it
        self.self_rows = None
        if "self_chat" in self.via:  # raises ReaderError: a self-chat `no` we can't see blocks every path
            self.self_rows = self_messages_strict(cfg, _fmt(_dt.datetime.fromtimestamp(self.created,
                                                                                      _dt.timezone.utc)))
        self.ok = True


def _session_hit(word, c):
    if not isinstance(c.transcript, str) or not os.path.isfile(c.transcript):
        return None
    g = _guard()
    with open(c.transcript, encoding="utf-8", errors="replace") as f:
        for line in f:
            if c.id not in line.lower():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            text = g.user_text(e)  # genuine human messages only
            ts = g.parse_time(e.get("timestamp")) if isinstance(e, dict) else None
            if not text or ts is None or ts <= c.created or ts > c.now + 60 \
                    or c.now - ts > SESSION_MAX_HOURS * 3600:
                continue
            reply = parse_reply(text)
            if reply and reply[0] == word and reply[1] != "all" and c.id in reply[1]:  # explicit ids only
                return "session:%s" % (e.get("uuid") or e.get("timestamp"))
    return None


def _self_chat_hit(word, c):
    shown = _shown(c.ws, "self_chat", c.id, c.sha, c.created)
    if not shown:
        return None  # never shown in the self chat with this text: nothing there counts
    bridge = _bridge_stanzas()
    if bridge is None:
        if word == "ok":
            return None  # can't tell the bridge's own messages from the manager's: no yes
        bridge = set()  # a `no` only ever reduces
    for m in c.self_rows or []:
        stanza = m.get("stanza")
        if not isinstance(stanza, str) or not stanza.strip() or stanza in bridge:
            continue  # sent by the bridge, or unidentifiable: MOTHER cannot approve itself
        at = _epoch(m.get("at"))
        if at is None or at < shown[0]:
            continue
        reply = parse_reply(m.get("text"))
        if not reply or reply[0] != word:
            continue
        if reply[1] == "all":
            hit = _all_covers(c.ws, "self_chat", at, c.id, c.sha, c.created)
        else:
            hit = c.id in reply[1]
        if hit:
            return "self_chat:%s" % stanza
    return None


def _dm_channels(cfg):
    """The manager's DM channel ids (manager.chat_dm), never the user id."""
    return [str(x) for x in _guard().as_list((cfg.get("manager") or {}).get("chat_dm")) if x]


def _slack_hit(c):
    g = _guard()
    dms = set(_dm_channels(c.cfg))
    scopes, max_age = g.grant_limits(c.cfg)
    if "whatsapp" not in scopes or not dms:
        return None
    data, ok = g.read_json(str(Path(c.ws) / ".mother" / "grants.json"))
    if not ok or not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        return None
    for gr in data["grants"]:
        v = g.valid_grant(gr, "whatsapp", c.id, c.cfg, scopes, max_age, c.now)
        if not v or gr.get("sha256") != c.sha or not grant_sig_ok(gr):
            continue  # a grant for other text, or one `pending` didn't sign
        src = gr["source"]
        ts = g.parse_time(src.get("ts"))
        if str(src.get("channel")) not in dms or ts is None or ts <= c.created:
            continue  # the manager's DM only, and a yes given after this draft existed
        if not _shown(c.ws, "slack_dm", c.id, c.sha, c.created, until=ts):
            continue  # the DM never showed this text before the yes
        return "slack_dm:%s" % v[0]
    return None


def _hit(word, c, only=None):
    """The first proof of `word` on an enabled path (only that path when `only` is given)."""
    if only is not None:
        c = _Only(c, only)
    if "session" in c.via:
        ev = _session_hit(word, c)
        if ev:
            return {"path": "session", "evidence": ev}
    if "self_chat" in c.via:
        ev = _self_chat_hit(word, c)
        if ev:
            return {"path": "self_chat", "evidence": ev}
    if word == "ok" and "slack_dm" in c.via:  # a Slack `no` is applied by `pending` at once (the draft is dropped)
        ev = _slack_hit(c)
        if ev:
            return {"path": "slack_dm", "evidence": ev}
    return None


class _Only(object):
    """A case seen through one approval path: same draft, `via` narrowed to that path if it is enabled."""

    def __init__(self, c, path):
        self.__dict__.update(c.__dict__)
        self.via = [path] if path in c.via else []


def rejected(ws: Path, cfg: dict, draft_id: str, transcript_path=None, now=None) -> bool:
    """True when the manager said `no` to this draft on an enabled path (session or self chat)."""
    try:
        c = _Case(ws, cfg, draft_id, transcript_path, now)
        return c.ok and _hit("no", c) is not None
    except Exception:
        return False


def prove(ws: Path, cfg: dict, draft_id: str, transcript_path=None, now=None, path=None):
    """{"path", "evidence", "sha256"} for the manager's yes to this draft's current text on an enabled path
    (only on `path` when given), else None. Fails closed: any error, a missing, expired or altered draft, an
    unreadable self chat (when that path is on), or a `no` on any enabled path means None. Writes nothing."""
    try:
        c = _Case(ws, cfg, draft_id, transcript_path, now)
        if not c.ok or _hit("no", c):
            return None
        got = _hit("ok", c, only=path)
        if got:
            got["sha256"] = c.sha
        return got
    except Exception:
        return None


def verify(ws: Path, cfg: dict, draft_id: str, sha: str = None):
    """(True, why) when approved/<id>.json still stands: prove() run again on the evidence it records (the
    transcript the guard read, the self chat, the signed DM grant), on the path it records only, finds a yes
    for the draft's current text, and that text's sha256 equals the draft's stored hash, the approval's, and
    `sha` (the hash of the bytes the bridge is about to send) when given. Else (False, reason).
    Read-only: it never deletes or changes a file, an expired draft included. The bridge runs it before every
    send."""
    if not isinstance(draft_id, str) or not ID_RE.match(draft_id):
        return False, "%s: not a draft id" % draft_id
    p = Path(ws) / ".mother" / "whatsapp" / "approved" / (draft_id + ".json")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "%s: no approval record" % draft_id
    if not isinstance(rec, dict) or rec.get("id") != draft_id or rec.get("path") not in PATHS \
            or not isinstance(rec.get("sha256"), str):
        return False, "%s: malformed approval record" % draft_id
    if sha is not None and (not isinstance(sha, str) or not re.match(r"^[0-9a-f]{64}$", sha)):
        return False, "%s: --sha256 must be 64 hex characters" % draft_id
    try:
        d = json.loads((_drafts_dir(ws) / (draft_id + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = None
    if not isinstance(d, dict) or not isinstance(d.get("text"), str):
        return False, "%s: no open draft" % draft_id
    if _expired(d):
        return False, "%s: the draft expired" % draft_id
    if rec.get("chat") != d.get("chat"):
        return False, "%s: the approval names another chat" % draft_id
    transcript = rec.get("transcript") if isinstance(rec.get("transcript"), str) else None
    proof = prove(ws, cfg, draft_id, transcript_path=transcript, path=rec["path"])
    if not proof:
        return False, "%s: no proven yes via %s on re-check" % (draft_id, rec["path"])
    text_sha = hashlib.sha256(d["text"].encode("utf-8")).hexdigest()
    if not (proof.get("sha256") == rec["sha256"] == d.get("sha256") == text_sha) \
            or (sha is not None and sha != text_sha):
        return False, "%s: text hash differs between draft, approval and proof" % draft_id
    return True, "%s: verified via %s" % (draft_id, proof["path"])


class _GrantsLock(object):
    """Exclusive lock on <ws>/.mother/grants.lock around a read-modify-write of grants.json."""

    def __init__(self, ws):
        self.path = Path(ws) / ".mother" / "grants.lock"
        self.f = None

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(str(self.path), "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()


def _grant_key(create: bool = False):
    """The HMAC key in <state>/whatsapp/grant.key (0600), or None. Only `mother whatsapp` creates it. The guard
    denies shell access to <state>/whatsapp/, but the macOS account (and so the agent's Read tool) can read the
    key: the signature stops a grant written by mistake or by an injected instruction, not a deliberately
    evasive agent (CONTRACT §5d)."""
    p = _state_dir() / "whatsapp" / "grant.key"
    if create and not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as f:
                f.write(os.urandom(32).hex())
    try:
        if p.is_symlink():
            return None
        key = bytes.fromhex(p.read_text().strip())
    except (OSError, ValueError):
        return None
    return key if len(key) >= 16 else None


def grant_sig(scope: str, target: str, ts: str, sha: str, create: bool = True):
    """HMAC-SHA256 over (scope, target, DM message ts, text sha256): what `pending` puts in a whatsapp grant."""
    import hmac
    key = _grant_key(create=create)
    if key is None:
        return None
    msg = "\n".join([str(scope), str(target), str(ts), str(sha)]).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def grant_sig_ok(g) -> bool:
    """A whatsapp grant carries a valid `sig`. A grant any other writer made (grants.json is agent-writable for
    the digest) has none and proves nothing."""
    import hmac
    if not isinstance(g, dict) or not isinstance(g.get("sig"), str) or not isinstance(g.get("source"), dict):
        return False
    want = grant_sig(g.get("scope"), g.get("target"), g["source"].get("ts"), g.get("sha256"), create=False)
    if want is None:
        return False
    try:  # bytes, so a non-ASCII sig compares unequal instead of raising: a malformed sig is no sig
        return hmac.compare_digest(want.encode("utf-8"), g["sig"].encode("utf-8", "surrogatepass"))
    except (TypeError, ValueError):
        return False


def _write_grant(ws: Path, cfg: dict, draft_id: str, sha: str, channel: str, ts: str) -> None:
    """Append one whatsapp grant. The caller holds _GrantsLock."""
    g = _guard()
    path = Path(ws) / ".mother" / "grants.json"
    data, ok = g.read_json(str(path))
    if not ok or not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        if path.exists():
            raise ValueError("grants.json is unreadable; not overwriting it")
        data = {"grants": []}
    for gr in data["grants"]:
        src = gr.get("source") if isinstance(gr, dict) else None
        if isinstance(src, dict) and gr.get("scope") == "whatsapp" and gr.get("target") == draft_id \
                and str(src.get("channel")) == channel and str(src.get("ts")) == str(ts):
            return  # this message already granted it
    sent = g.parse_time(ts)
    sig = grant_sig("whatsapp", draft_id, str(ts), sha, create=True)
    if sig is None:
        raise ValueError("can't create or read the grant key in %s" % (_state_dir() / "whatsapp"))
    data["grants"].append({"scope": "whatsapp", "target": draft_id, "sha256": sha, "uses": 1, "run": "whatsapp",
                           "expires": _fmt(_dt.datetime.fromtimestamp(sent + DRAFT_TTL_HOURS * 3600,
                                                                      _dt.timezone.utc)),
                           "source": {"channel": channel, "user": str(g.owner_id(cfg)), "ts": str(ts)},
                           "sig": sig})
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
    chat_dm = _dm_channels(cfg)
    dropped = []
    if "slack_dm" in approve_via(cfg) and owner and chat_dm:
        decided = set()
        msgs = [m for m in dm or [] if isinstance(m, dict) and g.parse_time(m.get("ts")) is not None]
        with _GrantsLock(ws):
            for m in sorted(msgs, key=lambda m: g.parse_time(m.get("ts"))):
                if str(m.get("user")) != str(owner) or _own_post(cfg, m):
                    continue  # only the manager approves, and never through the agent's own text
                reply = parse_reply(m.get("text"))
                if not reply:
                    continue
                at = g.parse_time(m.get("ts"))
                word, ids = reply
                if ids == "all":
                    if word == "ok":
                        continue  # a Slack yes names explicit ids: no post can be proven to be what `all` meant
                    ids = resolve_all(ws, "slack_dm", at)  # `no all` only drops, so the latest prompt is fine
                for i in ids:
                    d = load_draft(ws, i)
                    created = _epoch((d or {}).get("created"))
                    if i in decided or not d or created is None or at <= created:
                        continue  # one decision per draft per batch; an old reply can't touch a newer same-id draft
                    if word == "no":
                        decided.add(i)
                        if drop_draft(ws, i):
                            dropped.append(i)
                        continue
                    sha = hashlib.sha256(str(d.get("text")).encode("utf-8")).hexdigest()
                    if d.get("sha256") != sha:
                        continue
                    if not _shown(ws, "slack_dm", i, sha, created, until=at):
                        continue  # the DM never showed this text before the yes
                    decided.add(i)
                    try:
                        _write_grant(ws, cfg, i, sha, chat_dm[0], str(m.get("ts")))
                    except (OSError, ValueError) as e:
                        print("mother: no grant for %s: %s" % (i, e), file=sys.stderr)
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


def cmd_verify(cfg, args, ws):
    usage = "usage: mother whatsapp verify ID [--sha256 HEX]"
    if len(args) == 3 and args[1] == "--sha256":
        sha = args[2]
    elif len(args) == 1:
        sha = None
    else:
        _fail(usage)
    ok, why = verify(ws, cfg, args[0], sha)
    if not ok:
        _fail("verify %s" % why)
    print(why)


# ---------------------------------------------------------------- bridge client

BRIDGE_SRC = PLUGIN_ROOT / "bridge" / "whatsapp"  # the plugin's copy; `install` copies it out of the plugin
START_WAIT = 15.0  # seconds to wait for a bridge we just started to open its socket
READY_WAIT = 20.0  # then up to this long for its WhatsApp connection to open
CALL_TIMEOUT = {"status": 5.0}
DEFAULT_TIMEOUT = 60.0  # send/prompt: up to 30 s for the gap, plus typing and verify
MIN_NODE = 20
NODE_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def wa_state() -> Path:
    return _state_dir() / "whatsapp"


def bridge_sock() -> Path:
    return wa_state() / "bridge.sock"


def app_dir() -> Path:
    """Where the bridge runs from: outside the plugin, which a plugin update replaces."""
    return wa_state() / "app"


def _node():
    """node from PATH, else Homebrew, else the newest nvm install; None when there is none."""
    import glob
    import shutil
    found = shutil.which("node")
    if found:
        return found
    for d in NODE_DIRS:
        c = os.path.join(d, "node")
        if os.access(c, os.X_OK):
            return c

    def key(path):
        m = re.search(r"/v?(\d+)\.(\d+)\.(\d+)/bin/node$", path)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    cands = [c for c in glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")) if os.access(c, os.X_OK)]
    return max(cands, key=key) if cands else None


def _node_major(node):
    import subprocess
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def bridge_problem():
    """None when the bridge can be started, else (code, hint) with an absolute path in the hint."""
    node = _node()
    if not node:
        return "bridge_not_installed", "install Node %d or later (nodejs.org or Homebrew), then run `mother whatsapp install`" % MIN_NODE
    if _node_major(node) < MIN_NODE:
        return "node_too_old", "%s is older than Node %d; install a newer one, then run `mother whatsapp install`" % (
            node, MIN_NODE)
    if not (app_dir() / "bridge.mjs").is_file() or not (app_dir() / "node_modules").is_dir():
        return "bridge_not_installed", "run `mother whatsapp install` (installs the bridge into %s)" % app_dir()
    return None


def bridge_installed() -> bool:
    return bridge_problem() is None


def _problem_reply(prob):
    return {"ok": False, "error": prob[0], "hint": prob[1]}


def _bridge_files(folder: Path) -> list:
    names = ["package.json", "package-lock.json"]
    names += sorted(p.name for p in folder.glob("*.mjs") if not p.name.endswith(".test.mjs"))
    return [n for n in names if (folder / n).is_file()]


def _files_sha(folder: Path) -> str:
    h = hashlib.sha256()
    for n in _bridge_files(folder):
        h.update(n.encode("utf-8") + b"\0" + (folder / n).read_bytes() + b"\0")
    return h.hexdigest()


def reinstall_needed() -> bool:
    """True when an installed copy exists and differs from the plugin's bridge files."""
    if not (app_dir() / "bridge.mjs").is_file():
        return False
    return _files_sha(app_dir()) != _files_sha(BRIDGE_SRC)


def _run_npm(folder: Path, node: str) -> int:
    import subprocess
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(node) + os.pathsep + env.get("PATH", "")
    return subprocess.call(["npm", "ci", "--ignore-scripts", "--omit=dev"], cwd=str(folder), env=env)


def install_bridge() -> dict:
    """Copy the bridge out of the plugin into app_dir() and npm ci there. {"ok": True, "app"} or an error reply."""
    import shutil
    node = _node()
    if not node or _node_major(node) < MIN_NODE:
        return _problem_reply(bridge_problem() or ("node_too_old", ""))
    dest = app_dir()
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(str(dest.parent), 0o700)
    for n in _bridge_files(BRIDGE_SRC):
        shutil.copyfile(str(BRIDGE_SRC / n), str(dest / n))
    code = _run_npm(dest, node)
    if code != 0:
        return {"ok": False, "error": "npm_failed", "code": code}
    return {"ok": True, "app": str(dest)}


def cmd_install(cfg, args, ws):
    if args:
        _fail("usage: mother whatsapp install")
    r = install_bridge()
    print(json.dumps(r))
    if not r.get("ok"):
        sys.exit(2)


def _exchange(req: dict, timeout: float) -> dict:
    """One request on the socket. Raises FileNotFoundError/ConnectionRefusedError when nothing listens."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(bridge_sock()))
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.split(b"\n")[0].decode("utf-8"))


def _start_bridge():
    """Start `node app/bridge.mjs` detached (stdin closed, output to bridge.log: ids and codes only)."""
    import subprocess
    d = wa_state()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(d), 0o700)
    logp = d / "bridge.log"
    try:
        if logp.stat().st_size > 1024 * 1024:
            os.replace(str(logp), str(d / "bridge.log.1"))  # keep the log bounded
    except OSError:
        pass
    log = open(str(logp), "ab")
    try:
        env = dict(os.environ)
        env["MOTHER_PLUGIN_ROOT"] = str(PLUGIN_ROOT)  # the bridge runs outside the plugin but verifies with it
        return subprocess.Popen([_node(), str(app_dir() / "bridge.mjs")], stdin=subprocess.DEVNULL, stdout=log,
                                stderr=log, start_new_session=True, cwd=str(app_dir()), env=env)
    finally:
        log.close()


def _try(req: dict, timeout: float):
    """(reply, None) or (None, "down") when nothing listens; other failures are replies."""
    try:
        return _exchange(req, timeout), None
    except (FileNotFoundError, ConnectionRefusedError):
        return None, "down"
    except socket.timeout:
        return {"ok": False, "error": "bridge_timeout"}, None
    except (OSError, ValueError):
        return {"ok": False, "error": "bridge_bad_reply"}, None


def _start_and_wait() -> dict:
    """Under start.lock: start the bridge unless another caller already did, wait for its socket, then for its
    WhatsApp connection. Returns {} when ready (or settled unlinked), else an error reply."""
    import fcntl
    wa_state().mkdir(parents=True, exist_ok=True)
    os.chmod(str(wa_state()), 0o700)
    fd = os.open(str(wa_state() / "start.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        r, down = _try({"op": "status"}, CALL_TIMEOUT["status"])
        if down:
            try:
                _start_bridge()
            except OSError:
                return {"ok": False, "error": "bridge_start_failed"}
            deadline = time.time() + START_WAIT
            while down and time.time() < deadline:
                time.sleep(0.2)
                r, down = _try({"op": "status"}, CALL_TIMEOUT["status"])
            if down:
                return {"ok": False, "error": "bridge_down",
                        "hint": "the bridge did not open its socket in %d s; see %s" % (
                            START_WAIT, wa_state() / "bridge.log")}
        deadline = time.time() + READY_WAIT
        while r and r.get("ok") and r.get("connecting") and time.time() < deadline:
            time.sleep(0.3)
            r, down = _try({"op": "status"}, CALL_TIMEOUT["status"])
            if down:
                break
        return {}
    finally:
        os.close(fd)  # releases the lock


def bridge_call(req: dict, timeout=None, start: bool = True) -> dict:
    """Send one request to the bridge and return its reply. A missing or refusing socket starts the bridge on
    demand (`start`), under a lock, waits up to 15 s for the socket and up to 20 s for its WhatsApp connection,
    then calls. Errors are replies, never exceptions: bridge_down (not running and `start` off, or it never
    opened its socket), bridge_not_installed, node_too_old, bridge_start_failed, bridge_timeout,
    bridge_bad_reply."""
    t = timeout if timeout is not None else CALL_TIMEOUT.get(req.get("op"), DEFAULT_TIMEOUT)
    r, down = _try(req, t)
    if not down:
        return r
    prob = bridge_problem()
    if prob:
        return _problem_reply(prob)
    if not start:
        return {"ok": False, "error": "bridge_down"}
    err = _start_and_wait()
    if err:
        return err
    r, down = _try(req, t)
    return r if not down else {"ok": False, "error": "bridge_down"}


def stop_bridge(wait: float = 5.0) -> bool:
    """Ask a running bridge to exit and wait for its socket to go. True when it was running."""
    r, down = _try({"op": "stop"}, CALL_TIMEOUT["status"])
    if down:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        _, down = _try({"op": "status"}, 1.0)
        if down:
            break
        time.sleep(0.1)
    return True


def cmd_stop(cfg, args, ws):
    if args:
        _fail("usage: mother whatsapp stop")
    print(json.dumps({"ok": True, "stopped": stop_bridge()}))


# ---------------------------------------------------------------- send, prompt, link, status

def _approved_dir(ws: Path) -> Path:
    return Path(ws) / ".mother" / "whatsapp" / "approved"


def _mark_sent(ws: Path, draft_id: str) -> None:
    d = load_draft(ws, draft_id, expire=False)
    if not d:
        return
    d["status"] = "sent"
    path = _drafts_dir(ws) / (draft_id + ".json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def cmd_send(cfg, args, ws):
    if len(args) != 1 or not ID_RE.match(args[0]):
        _fail("usage: mother whatsapp send ID")
    i = args[0]
    if not (_approved_dir(ws) / (i + ".json")).is_file():
        _fail("no approval for %s: it needs the manager's yes first (the guard records it)" % i, 2)
    r = bridge_call({"op": "send", "draft": i})
    if not r.get("ok") and r.get("error") != "already_sent":
        print(json.dumps(r))
        _fail("send %s failed: %s" % (i, r.get("error") or "error"), 2)
    try:
        _mark_sent(ws, i)
    except OSError as e:
        print("mother: sent, but couldn't mark %s sent: %s" % (i, e), file=sys.stderr)
    if r.get("ok"):
        print(json.dumps({"ok": True, "stanza": r.get("stanza")}))
    else:  # already sent earlier: it can't go twice, and it must not be prompted again
        print(json.dumps({"ok": True, "already_sent": True, "note": "this draft was already sent"}))


def _in_quiet_hours(cfg: dict) -> bool:
    import channels
    return channels.quiet_now(cfg)


def prompt_text(drafts: list, allow_all: bool = True) -> str:
    """The batched approval prompt. No line of it may read as an approval reply (the bridge refuses such text):
    every draft starts `<id> WhatsApp ->`, text continuation lines are quoted with `> `, and the hint starts
    `Reply with`. The Slack path takes explicit ids only (`allow_all` False): its post can't be proven to be
    the prompt `all` would resolve to."""
    lines = []
    for d in drafts:
        body = str(d["text"]).split("\n")
        lines.append('%s WhatsApp -> %s: "%s' % (d["id"], d.get("chat_name") or "chat", body[0]))
        lines.extend("> " + b for b in body[1:])
        lines[-1] += '"'
    lines.append("")
    ids = " ".join(d["id"] for d in drafts)
    if allow_all:
        lines.append("Reply with: ok %s  |  ok all  |  no <id>" % ids)
    else:
        lines.append("Reply with explicit ids: ok %s  |  no <id>" % ids)
    return "\n".join(lines)


def _record_prompt(ws: Path, path: str, drafts: list) -> None:
    rec = {"at": _fmt(_now()), "path": path, "ids": [d["id"] for d in drafts],
           "sha256": {d["id"]: d["sha256"] for d in drafts}}
    p = Path(ws) / ".mother" / "whatsapp" / "prompts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec) + "\n").encode("utf-8"))  # one write: a torn line shows nothing
    finally:
        os.close(fd)


def cmd_prompt(cfg, args, ws):
    if not args or not all(ID_RE.match(a) for a in args):
        _fail("usage: mother whatsapp prompt ID [ID]...")
    drafts = []
    for i in dict.fromkeys(args):
        d = load_draft(ws, i)
        if not d or d.get("status") == "sent":
            _fail("%s is not an open draft" % i, 2)
        if not d.get("sha256") or d["sha256"] != hashlib.sha256(str(d.get("text")).encode("utf-8")).hexdigest():
            _fail("%s: the draft's hash does not match its text; drop it and draft again" % i, 2)
        drafts.append(d)
    if _in_quiet_hours(cfg):
        print(json.dumps({"held": True}))
        return
    out = {"ids": [d["id"] for d in drafts]}
    failed = False
    for path in approve_via(cfg):
        if path == "self_chat":
            r = bridge_call({"op": "prompt", "text": prompt_text(drafts)})
            out["self_chat"] = r
            if r.get("ok") or r.get("error") == "log_failed":  # log_failed: the message did go out
                _record_prompt(ws, "self_chat", drafts)
            if not r.get("ok"):
                failed = True
        elif path == "slack_dm":
            dms = _dm_channels(cfg)
            if not dms:
                out["slack_dm"] = {"ok": False, "error": "no_chat_dm"}
                failed = True
                continue
            out["slack_dm"] = {"ok": True, "channel": dms[0], "text": prompt_text(drafts, allow_all=False)}
            _record_prompt(ws, "slack_dm", drafts)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if failed:  # per-path results are in the JSON; the caller must read them
        sys.exit(2)


def _bridge_argv(flag: str) -> list:
    return [_node(), str(app_dir() / "bridge.mjs"), flag]


def cmd_link(sub, args) -> int:
    """`link` / `unlink`: stop a running bridge, then run the installed bridge in this terminal (the QR shows
    here; an expired QR offers a new one). Returns the exit code."""
    import subprocess
    if args:
        _fail("usage: mother whatsapp %s" % sub)
    if sub == "link" and not (sys.stdout.isatty() and sys.stdin.isatty()):
        print("mother: run `mother whatsapp link` in your own terminal (the QR needs one)", file=sys.stderr)
        return 2
    prob = bridge_problem()
    if prob:
        print(json.dumps(_problem_reply(prob)))
        return 2
    if sub == "link":
        io = TerminalIO()
        try:
            me = link_loop(io)
        except SetupStop as e:
            io.say("Stopped: %s" % e)
            return 1
        except KeyboardInterrupt:
            io.say("\nlink stopped; nothing more was changed")
            return 130
        io.say("Linked: %s. Run `mother whatsapp setup` to finish (it is safe to re-run)." % me)
        return 0
    stop_bridge()  # a running bridge would hold the old session (and block the link)
    env = dict(os.environ)
    env["MOTHER_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    return subprocess.call(_bridge_argv("--" + sub), env=env)


FDA_HINT = ("grant Full Disk Access to the app that runs Claude Code (your terminal or the desktop app) in "
            "System Settings > Privacy & Security > Full Disk Access")


def db_check() -> dict:
    """{"db_reachable": True} or {"db_reachable": False, "reason", "hint"?}. Reads no message."""
    try:
        _query("SELECT 1 FROM ZWACHATSESSION LIMIT 1")
    except ReaderError as e:
        read = {"db_reachable": False, "reason": e.reason}
        if e.reason == "denied":
            read["hint"] = FDA_HINT
        return read
    except Exception:
        return {"db_reachable": False, "reason": "error"}
    return {"db_reachable": True}


def cmd_status(cfg, args):
    if args:
        _fail("usage: mother whatsapp status")
    read = db_check()
    out = {"bridge": bridge_call({"op": "status"}, start=False), "read": read}
    if reinstall_needed():
        out["reinstall_needed"] = True
        out["hint"] = "the plugin's bridge changed; run `mother whatsapp install`"
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------- setup, allow, disallow (the manager's terminal)

ALLOW_JID_RE = re.compile(r"\d{6,15}@s\.whatsapp\.net|\d{6,30}(-\d+)?@g\.us", re.ASCII)  # = bin/mother WA_JID_RE
SETUP_CHATS = 100
PATH_ORDER = ("session", "self_chat", "slack_dm")


class TerminalIO(object):
    """The manager's terminal. Tests pass an object with the same two methods."""

    def ask(self, prompt):
        try:
            return input(prompt)
        except EOFError:
            return ""

    def say(self, text=""):
        print(text, flush=True)


class SetupStop(Exception):
    """A step can't go on; the message says what to do."""


def bare_jid(jid):
    """A user JID without its device suffix, or None when it isn't one."""
    if not isinstance(jid, str):
        return None
    j = re.sub(r":\d+(?=@)", "", jid.strip())
    return j if re.fullmatch(r"\d{6,15}@s\.whatsapp\.net", j, re.ASCII) else None


def phone_jid(text):
    """`+972-50-000-0001` -> `972500000001@s.whatsapp.net`; None unless it is 6-15 digits after dropping + - ( ) and
    spaces."""
    if not isinstance(text, str):
        return None
    digits = re.sub(r"[\s+()\-]", "", text)
    return digits + "@s.whatsapp.net" if re.fullmatch(r"\d{6,15}", digits, re.ASCII) else None


def _load_cfg(ws: Path) -> dict:
    try:
        cfg = json.loads((Path(ws) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SetupStop("can't read %s: %s" % (Path(ws) / "config.json", e))
    if not isinstance(cfg, dict):
        raise SetupStop("config.json is not a JSON object")
    return cfg


def save_whatsapp(ws: Path, validate, change) -> dict:
    """Re-read config.json, let `change(block)` edit the whatsapp block, validate the whole config and write it
    atomically (temp file + rename, mode kept). An invalid result is never written: SetupStop with the errors."""
    import fcntl
    lockp = Path(ws) / ".mother" / "config.lock"  # .mother/ is git-ignored: the lock is never synced
    lockp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lockp), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _save_whatsapp_locked(ws, validate, change)
    finally:
        os.close(fd)  # releases the lock


def _save_whatsapp_locked(ws: Path, validate, change) -> dict:
    path = Path(ws) / "config.json"
    cfg = _load_cfg(ws)
    wa = cfg.get("whatsapp")
    wa = dict(wa) if isinstance(wa, dict) else {}
    change(wa)
    cfg["whatsapp"] = wa
    errs = validate(cfg) if validate else ["no validator"]
    if errs:
        raise SetupStop("config.json would not be valid, so nothing was written:\n  " + "\n  ".join(errs) +
                        "\nFix it (ask MOTHER: /mother:setup), then run this again.")
    tmp = path.with_name(".config.json.%d.tmp" % os.getpid())
    try:
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            os.chmod(str(tmp), path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()
    return cfg


def pickable_chats(cfg: dict, limit: int = SETUP_CHATS) -> list:
    """Recent chats with a last message (history), not the self chat: ones config accepts, and ones WhatsApp
    keys by a private `@lid` id (marked "hidden": shown, never allowed). Names and dates only. Raises ReaderError."""
    me = _wa(cfg).get("self_jid")
    out = []
    for c in recent_chats(limit * 4):
        if c["jid"] == me or not c["last_at"]:
            continue
        if (c["jid"] or "").endswith("@lid"):
            c = dict(c, hidden=True)
        elif not ALLOW_JID_RE.fullmatch(c["jid"] or ""):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _chat_name(c: dict) -> str:
    if c.get("name"):
        return c["name"]
    if c.get("hidden"):
        return "a chat"
    return "+" + c["jid"].split("@")[0] if not c.get("group") else "group " + c["jid"].split("@")[0]


def _allow_entry(c: dict, name: str = None) -> dict:
    e = {"jid": c["jid"], "name": name or _chat_name(c)}
    if c.get("group") or c["jid"].endswith("@g.us"):
        e["group"] = True
    return e


def _show_chats(io, chats: list, allowed: set) -> None:
    for n, c in enumerate(chats, 1):
        io.say("  %2d. %s  (%s, last %s)%s%s" % (n, _chat_name(c), "group" if c["group"] else "DM",
                                                 (c["last_at"] or "")[:10], "  [allowed]" if c["jid"] in allowed else "",
                                                 "  (id hidden by WhatsApp)" if c.get("hidden") else ""))


def _pick(io, chats: list) -> list:
    """Numbers the manager types (`1 4`, commas fine), asked again until each is on the list; Enter = none."""
    while True:
        raw = io.ask("Chats MOTHER may read and draft to (numbers, e.g. `1 4`; Enter to skip): ").strip()
        if not raw:
            return []
        toks = [t for t in re.split(r"[\s,]+", raw) if t]
        if all(t.isdigit() and 1 <= int(t) <= len(chats) for t in toks):
            picked = [chats[int(t) - 1] for t in dict.fromkeys(toks)]
            for c in picked:
                if c.get("hidden"):
                    io.say("%s skipped: WhatsApp hides its id, so MOTHER can't allow it yet." % _chat_name(c))
            return [c for c in picked if not c.get("hidden")]
        io.say("Type numbers from 1 to %d, separated by spaces." % len(chats))


def _yes(io, question: str, default: bool) -> bool:
    while True:
        a = io.ask("%s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        io.say("Answer y or n.")


def _db_stop(read: dict) -> str:
    reason = read.get("reason")
    if reason == "denied":
        return "MOTHER can't read WhatsApp's database: %s, then run this again." % FDA_HINT
    if reason == "missing":
        return ("WhatsApp's database isn't at %s: install WhatsApp desktop from the App Store, sign in, let it "
                "sync, then run this again." % db_path())
    if reason == "locked":
        return "WhatsApp's database is busy; run this again in a moment."
    return "WhatsApp's database can't be read (%s); MOTHER may need an update for this WhatsApp version." % reason


def strip_device(jid):
    """`<id>:<device>@<server>` -> `<id>@<server>`; anything else as it is (None for a non-string)."""
    return re.sub(r":\d+(?=@)", "", jid.strip()) if isinstance(jid, str) and jid.strip() else None


LINK_STOPS = {"logged_out": "WhatsApp logged this device out while linking; run it again for a fresh code.",
              "replaced": "another device replaced this link; run it again for a fresh code.",
              "no_account": "the device linked but WhatsApp didn't say which account; run it again."}


def link_once(say, phone=None) -> dict:
    """One `bridge.mjs --link` attempt in this terminal (`--phone <digits>` for a pairing code instead of a QR):
    its QR or code and sentences pass through, and its last stdout line names the outcome.
    {"linked": bool, "me": JID without device suffix or None, "reason": str or None}."""
    import subprocess
    stop_bridge()  # a running bridge would hold the old session (and block the link)
    env = dict(os.environ)
    env["MOTHER_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    argv = _bridge_argv("--link") + (["--phone", phone.split("@")[0]] if phone else [])
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, env=env)  # stdin, stderr: the terminal
    res = None
    for raw in p.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        try:
            r = json.loads(line) if line.startswith("{") else None
        except ValueError:
            r = None
        if isinstance(r, dict) and "linked" in r:
            res = r
            continue
        say(line)
    p.wait()
    if not isinstance(res, dict) or res.get("linked") is not True or p.returncode != 0:
        return {"linked": False, "me": None, "reason": (res or {}).get("reason") or "no_result"}
    me = strip_device(res.get("me"))
    return {"linked": bool(me), "me": me, "reason": None if me else "no_account"}


def _link_phone(io):
    """The manager's choice of how to link: None for a QR, else their number as a JID (they typed it)."""
    a = io.ask("Link by scanning a QR code (Enter), or type p to link with your phone number instead: ").strip()
    if a.lower() not in ("p", "phone"):
        return None
    while True:
        raw = io.ask("Your WhatsApp phone number, with country code (e.g. +972 50 000 0001): ").strip()
        if not raw:
            return None
        jid = phone_jid(raw)
        if jid:
            return jid
        io.say("That isn't a phone number (6-15 digits with the country code).")


def link_loop(io, want=None) -> str:
    """Link until it works: an expired QR or code (or a dropped connection) offers a new one. Returns the linked
    account's JID as WhatsApp gave it (it may be a private `@lid` id). Raises SetupStop. `want` (a dict) gets
    "phone": the number the manager typed to link by phone, or None."""
    phone = _link_phone(io)
    if want is not None:
        want["phone"] = phone
    what = "code" if phone else "QR"
    while True:
        r = link_once(io.say, phone)
        if r.get("linked") and r.get("me"):
            return r["me"]
        reason = r.get("reason")
        if reason in LINK_STOPS:
            raise SetupStop(LINK_STOPS[reason])
        q = "%s expired — show a new one?" % what if reason in ("qr_expired", "timeout") else \
            "The link didn't complete — try again with a new %s?" % what
        if not _yes(io, q, True):
            raise SetupStop("not linked; run `mother whatsapp setup` again when you're ready to link.")


def _phone_from_manager(io, private_id) -> str:
    """WhatsApp named the account by a private id: the manager types their number instead."""
    io.say("WhatsApp gave MOTHER a private id for your account (%s), not your phone number." % private_id)
    while True:
        raw = io.ask("Your WhatsApp phone number, with country code (e.g. +972 50 000 0001): ").strip()
        if not raw:
            raise SetupStop("MOTHER needs your number to tell your own chat apart; run setup again with it.")
        jid = phone_jid(raw)
        if jid:
            return jid
        io.say("That isn't a phone number (6-15 digits with the country code).")


def _step_install(io) -> None:
    prob = bridge_problem()
    if prob and prob[0] != "bridge_not_installed":
        raise SetupStop(prob[1])
    if not prob and not reinstall_needed():
        io.say("Bridge: installed.")
        return
    io.say("Installing the WhatsApp bridge (Node packages from the npm registry; a minute or two)...")
    r = install_bridge()
    if not r.get("ok"):
        raise SetupStop("the bridge didn't install (%s%s). Fix that, then run this again." % (
            r.get("error"), (": " + r["hint"]) if r.get("hint") else ""))
    stop_bridge()  # an old copy may still be running
    io.say("Bridge: installed in %s." % app_dir())


def _step_link(io) -> str:
    r = bridge_call({"op": "status"})
    got = strip_device(r.get("me")) if r.get("ok") and r.get("linked") else None
    if not got:
        io.say("Link MOTHER as a device on your phone: WhatsApp > Settings > Linked devices > Link a device.")
        want = {}
        got = link_loop(io, want)
        if got and got.endswith("@lid") and want.get("phone"):
            got = want["phone"]  # WhatsApp named the account privately; the manager already typed the number
    me = bare_jid(got) or _phone_from_manager(io, got)
    io.say("Linked: your WhatsApp account %s." % me)
    return me


def setup(ws: Path, validate, io, args=None) -> int:
    """`mother whatsapp setup`. Returns the exit code: 0 done, 2 a step stopped."""
    if args:
        io.say("usage: mother whatsapp setup")
        return 2
    try:
        _load_cfg(ws)
        _step_install(io)
        me = _step_link(io)

        def enable(wa):
            if wa.get("self_jid") and wa["self_jid"] != me:
                io.say("whatsapp.self_jid was %s; the linked account is %s, so it is now that." % (wa["self_jid"], me))
            wa["enabled"] = True
            wa["self_jid"] = me
            wa.setdefault("allow", [])
            wa.setdefault("approve_via", ["session"])
        cfg = save_whatsapp(ws, validate, enable)
        io.say("Config: whatsapp is on for %s." % me)

        read = db_check()
        if not read["db_reachable"]:
            raise SetupStop(_db_stop(read))
        io.say("Database: readable.")

        try:
            chats = pickable_chats(cfg)
        except ReaderError as e:
            raise SetupStop(_db_stop({"reason": e.reason}))
        allowed = {a.get("jid") for a in _wa(cfg).get("allow") or [] if isinstance(a, dict)}
        if chats:
            io.say("")
            io.say("Your recent chats (names and dates only):")
            _show_chats(io, chats, allowed)
            new = [c for c in _pick(io, chats) if c["jid"] not in allowed]
            if new:
                cfg = save_whatsapp(ws, validate, lambda wa: wa.setdefault("allow", []).extend(
                    _allow_entry(c) for c in new))
                io.say("Allowed: %s." % ", ".join(_chat_name(c) for c in new))
        else:
            io.say("No recent chats to offer; add one later with `mother whatsapp allow <number>`.")

        io.say("")
        io.say("Approving drafts: you can always reply `ok <id>` in a Claude session.")
        via = approve_via(cfg)
        want = {"session": True}
        want["self_chat"] = _yes(io, "Also approve in your WhatsApp self chat (\"Message yourself\"): MOTHER posts "
                                     "the drafts there and you reply `ok <id>`?", "self_chat" in via)
        if _dm_channels(cfg):
            want["slack_dm"] = _yes(io, "Also approve in your chat DM (manager.chat_dm): MOTHER posts the drafts "
                                        "there and you reply `ok <id>`?", "slack_dm" in via)
        else:
            want["slack_dm"] = False
        new_via = [p for p in PATH_ORDER if want.get(p)]
        if new_via != _wa(cfg).get("approve_via"):
            cfg = save_whatsapp(ws, validate, lambda wa: wa.__setitem__("approve_via", new_via))

        names = [a.get("name") for a in _wa(cfg).get("allow") or [] if isinstance(a, dict)]
        io.say("")
        io.say("WhatsApp is set up: account %s; chats: %s; approve via: %s." % (
            me, ", ".join(names) or "none yet", ", ".join(new_via)))
        if names:
            io.say("Next: in a Claude session, ask MOTHER to draft a WhatsApp message to %s; reply `ok <id>` to "
                   "send it." % names[0])
        else:
            io.say("Next: add a chat with `mother whatsapp allow <number>`, then ask MOTHER in a Claude session to "
                   "draft a message to it; reply `ok <id>` to send it.")
        return 0
    except SetupStop as e:
        io.say("Stopped: %s" % e)
        return 2


def _resolve_chat(cfg: dict, arg: str, io):
    """(entry, note) for `allow`'s argument, or raise SetupStop."""
    try:
        chats = pickable_chats(cfg)
    except ReaderError:
        chats = None
    if re.fullmatch(r"\d{1,3}", arg):
        if not chats or not 1 <= int(arg) <= len(chats):
            raise SetupStop("%s is not a number on the list (run `mother whatsapp allow` to see it)" % arg)
        if chats[int(arg) - 1].get("hidden"):
            raise SetupStop("%s (id hidden by WhatsApp) can't be allowed yet" % _chat_name(chats[int(arg) - 1]))
        return chats[int(arg) - 1]
    jid = arg if ALLOW_JID_RE.fullmatch(arg) else phone_jid(arg)
    if not jid:
        raise SetupStop("%s is not a phone number, a WhatsApp JID or a list number" % arg)
    for c in chats or []:
        if c["jid"] == jid:
            return c
    try:
        rows = _query("SELECT ZPARTNERNAME, ZSESSIONTYPE FROM ZWACHATSESSION WHERE ZCONTACTJID = ? LIMIT 1", [jid])
    except ReaderError:
        return {"jid": jid, "name": "", "group": jid.endswith("@g.us"), "unchecked": True}
    if not rows or not has_history({"whatsapp": {}}, jid):
        raise SetupStop("no cold contact: you have no chat with %s yet; message them yourself first" % arg)
    return {"jid": jid, "name": rows[0][0] or "", "group": rows[0][1] == GROUP_SESSION}


def allow_chat(ws: Path, validate, io, args) -> int:
    """`mother whatsapp allow [NUMBER|JID|N] [--name NAME]`. Returns the exit code."""
    usage = "usage: mother whatsapp allow [NUMBER|JID|N] [--name NAME]"
    name, rest, i = None, [], 0
    while i < len(args):
        if args[i] == "--name" and i + 1 < len(args) and args[i + 1].strip():
            name, i = args[i + 1].strip(), i + 2
        elif args[i].startswith("-"):
            io.say(usage)
            return 2
        else:
            rest.append(args[i])
            i += 1
    if len(rest) > 1:
        io.say(usage)
        return 2
    try:
        cfg = _load_cfg(ws)
        if not _wa(cfg).get("self_jid"):
            raise SetupStop("run `mother whatsapp setup` first: it links your account, which tells your own self "
                            "chat apart from the chats you allow")
        if rest:
            c = _resolve_chat(cfg, rest[0], io)
        else:
            try:
                chats = pickable_chats(cfg)
            except ReaderError as e:
                raise SetupStop(_db_stop({"reason": e.reason}))
            if not chats:
                raise SetupStop("no recent chats to offer; give a number: mother whatsapp allow <number>")
            allowed = {a.get("jid") for a in _wa(cfg).get("allow") or [] if isinstance(a, dict)}
            _show_chats(io, chats, allowed)
            picked = _pick(io, chats)
            if not picked:
                return 0
            if len(picked) > 1:
                me = _wa(cfg).get("self_jid")
                new = [c for c in picked if c["jid"] not in allowed and c["jid"] != me]
                if new:
                    save_whatsapp(ws, validate, lambda wa: wa.setdefault("allow", []).extend(
                        _allow_entry(c) for c in new))
                    io.say("Allowed: %s." % ", ".join(_chat_name(c) for c in new))
                if len(new) < len(picked):
                    io.say("Skipped (already allowed or your own chat): %s." % ", ".join(
                        _chat_name(c) for c in picked if c not in new))
                if not _wa(cfg).get("enabled"):
                    io.say("WhatsApp is still off: run `mother whatsapp setup`.")
                return 0
            c = picked[0]
        if c["jid"] == _wa(cfg).get("self_jid"):
            raise SetupStop("that is your own self chat, which carries approvals; it can't be allowed")
        if any(isinstance(a, dict) and a.get("jid") == c["jid"] for a in _wa(cfg).get("allow") or []):
            io.say("%s is already allowed." % c["jid"])
            return 0
        if not name and not c.get("name"):
            name = io.ask("Name for %s: " % c["jid"]).strip()
            if not name:
                raise SetupStop("a chat needs a name")
        entry = _allow_entry(c, name)
        save_whatsapp(ws, validate, lambda wa: wa.setdefault("allow", []).append(entry))
        io.say("Allowed: %s (%s)." % (entry["name"], entry["jid"]))
        if c.get("unchecked"):
            io.say("Note: couldn't check the database for history with it; drafts to it are refused until you have "
                   "chatted (no cold contact).")
        if not _wa(cfg).get("enabled"):
            io.say("WhatsApp is still off: run `mother whatsapp setup`.")
        return 0
    except SetupStop as e:
        io.say("Stopped: %s" % e)
        return 2


def disallow_chat(ws: Path, validate, io, args) -> int:
    """`mother whatsapp disallow JID|NUMBER`. Returns the exit code (1 when it wasn't allowed)."""
    if len(args) != 1:
        io.say("usage: mother whatsapp disallow JID|NUMBER")
        return 2
    jid = args[0] if ALLOW_JID_RE.fullmatch(args[0]) else phone_jid(args[0])
    try:
        cfg = _load_cfg(ws)
        if not jid or not any(isinstance(a, dict) and a.get("jid") == jid for a in _wa(cfg).get("allow") or []):
            io.say("%s is not in whatsapp.allow." % args[0])
            return 1
        save_whatsapp(ws, validate, lambda wa: wa.__setitem__(
            "allow", [a for a in wa.get("allow") or [] if not (isinstance(a, dict) and a.get("jid") == jid)]))
        io.say("Removed %s from whatsapp.allow." % jid)
        return 0
    except SetupStop as e:
        io.say("Stopped: %s" % e)
        return 2


def _interruptible(name, f):
    """Ctrl-C at any prompt: exit 130 with one plain line. Every write is atomic, so the config is whole."""
    def run(ws, validate, io, args=None):
        try:
            return f(ws, validate, io, args)
        except KeyboardInterrupt:
            io.say("\n%s stopped; nothing more was changed" % name)
            return 130
    run.__name__ = f.__name__
    run.__doc__ = f.__doc__
    return run


setup = _interruptible("setup", setup)
allow_chat = _interruptible("allow", allow_chat)
disallow_chat = _interruptible("disallow", disallow_chat)
TERMINAL_COMMANDS = {"setup": setup, "allow": allow_chat, "disallow": disallow_chat}

READ_COMMANDS = {"read": cmd_read, "chats": cmd_chats}
DRAFT_COMMANDS = {"draft": cmd_draft, "list": cmd_list, "drop": cmd_drop, "pending": cmd_pending,
                  "verify": cmd_verify, "send": cmd_send, "prompt": cmd_prompt, "install": cmd_install,
                  "stop": cmd_stop}


def main(argv, cfg=None, ws=None, validate=None):
    """`validate` is bin/mother's config validator: setup, allow and disallow write config.json only through it."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cfg = cfg or {}
    if argv[0] not in READ_COMMANDS and argv[0] not in DRAFT_COMMANDS and argv[0] not in TERMINAL_COMMANDS \
            and argv[0] not in ("status", "link", "unlink"):
        _fail("unknown whatsapp command '%s' — see `mother whatsapp help`" % argv[0])
    if argv[0] == "status":
        return cmd_status(cfg, argv[1:])
    if argv[0] in TERMINAL_COMMANDS and not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail("run `mother whatsapp %s` in your own terminal: it changes who MOTHER may message, so only you "
              "run it" % argv[0], 2)
    require_home(cfg)
    wa = _wa(cfg)
    if argv[0] in READ_COMMANDS and not (wa.get("enabled") and wa.get("read", True)):
        _fail("whatsapp reading is off (whatsapp.enabled / whatsapp.read in config.json): run `mother whatsapp "
              "setup` in your own terminal", 2)
    if argv[0] in ("draft", "verify", "send", "prompt") and not (wa.get("enabled") and wa.get("send", True)):
        _fail("whatsapp sending is off (whatsapp.enabled / whatsapp.send in config.json): run `mother whatsapp "
              "setup` in your own terminal", 2)
    if argv[0] in TERMINAL_COMMANDS:
        if ws is None or validate is None:
            _fail("no workspace")
        sys.exit(TERMINAL_COMMANDS[argv[0]](Path(ws), validate, TerminalIO(), argv[1:]))
    if argv[0] in ("link", "unlink"):
        sys.exit(cmd_link(argv[0], argv[1:]))
    if argv[0] in READ_COMMANDS:
        READ_COMMANDS[argv[0]](cfg, argv[1:])
    else:
        if ws is None:
            _fail("no workspace")
        DRAFT_COMMANDS[argv[0]](cfg, argv[1:], ws)
