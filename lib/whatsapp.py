"""mother whatsapp — read the manager's WhatsApp chats from the local macOS database, and send through the bridge.

The WhatsApp feature (docs/CONTRACT.md §5d). Everything here is home-machine-only, except `status`.

Commands:
  chats [--query TEXT] [--limit N]   recent chats (default 30): name, DM/group, last date ("pinned"), JID. JSON
  read [--chat WHO] [--since ISO] [--limit N]
                                     messages of one chat, or across chats since ISO (default 24 h ago). JSON
  send --to WHO --text TEXT          send one message through the bridge. WHO = me, a chat's exact name, a phone
                                     number or a JID; only a chat with history (no cold contact). The guard asks
                                     the manager first for anyone but `me` and writes the one-use ticket the
                                     bridge needs. Others get the signature line appended
  install                            copy the bridge out of the plugin into <state>/whatsapp/app and npm ci there
  stop                               stop a running bridge
  status                             bridge state and whether the local database is readable (JSON)
  setup                              the manager's guided setup, in their own terminal: install, link, turn
                                     WhatsApp on for the linked account, check database access. Safe to re-run
  link | unlink                      pair the bridge (QR or phone code) | log it out and delete its keys. Own
                                     terminal only

The database is opened read-only (`mode=ro`); nothing read is written anywhere. Python 3.9 stdlib only.
"""
import datetime as _dt
import hashlib
import json
import os
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
PIN_AHEAD = 86400  # a ZLASTMESSAGEDATE more than a day in the future is a pinned chat, not a date
DEFAULT_CHATS = 30
DEFAULT_LIMIT = 100
DEFAULT_SINCE_HOURS = 24
MAX_TEXT = 4096
USER_JID_RE = re.compile(r"\d{6,15}@s\.whatsapp\.net", re.ASCII)
GROUP_JID_RE = re.compile(r"\d{6,30}(-\d+)?@g\.us", re.ASCII)
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class ReaderError(Exception):
    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


class ChatError(Exception):
    """WHO can't be used: the message is a plain sentence for the manager."""


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
    w = cfg.get("whatsapp")
    return w if isinstance(w, dict) else {}


def _state_dir() -> Path:
    return Path(os.environ.get("MOTHER_STATE_DIR") or "~/.mother").expanduser()


def _fail(msg, code=1):
    print("mother: %s" % msg, file=sys.stderr)
    sys.exit(code)


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


# ---------------------------------------------------------------- chats

CHAT_COLS = "Z_PK, ZCONTACTJID, ZPARTNERNAME, ZLASTMESSAGEDATE, ZSESSIONTYPE"
NOT_BROADCAST = "ZCONTACTJID IS NOT NULL AND ZCONTACTJID NOT LIKE '%broadcast'"


def _chat(row, now=None) -> dict:
    """A ZWACHATSESSION row as a chat: {pk, jid, name, group, last, hidden_id?}. `last` is an ISO date, "pinned"
    (WhatsApp keeps a pinned chat's date far in the future) or None."""
    pk, jid, name, last, stype = row
    now = time.time() if now is None else now
    if not last:
        when = None
    elif CORE_EPOCH + float(last) > now + PIN_AHEAD:
        when = "pinned"
    else:
        when = core_ts(last)
    c = {"pk": pk, "jid": jid, "name": name or "", "group": stype == GROUP_SESSION, "last": when}
    if (jid or "").endswith("@lid"):
        c["hidden_id"] = True
    return c


def chat_name(c: dict) -> str:
    if c.get("name"):
        return c["name"]
    if c.get("hidden_id"):
        return "a chat"
    head = (c.get("jid") or "").split("@")[0]
    return ("group " + head) if c.get("group") else ("+" + head)


def _sort_chats(chats: list) -> list:
    """Pinned first (in the order given), then the newest; chats with no date last."""
    pinned = [c for c in chats if c["last"] == "pinned"]
    dated = sorted([c for c in chats if c["last"] not in (None, "pinned")], key=lambda c: c["last"], reverse=True)
    return pinned + dated + [c for c in chats if c["last"] is None]


def recent_chats(limit=DEFAULT_CHATS, query=None) -> list:
    """Chats from ZWACHATSESSION only (never a message), pinned first, then newest. Raises ReaderError."""
    sql = "SELECT %s FROM ZWACHATSESSION WHERE %s" % (CHAT_COLS, NOT_BROADCAST)
    params = []
    if query:
        sql += " AND (ZPARTNERNAME LIKE ? ESCAPE '\\' OR ZCONTACTJID LIKE ? ESCAPE '\\')"
        q = "%" + re.sub(r"([%_\\])", r"\\\1", query) + "%"
        params += [q, q]
    sql += " ORDER BY ZLASTMESSAGEDATE DESC LIMIT ?"
    params.append(int(limit))
    return _sort_chats([_chat(r) for r in _query(sql, params)])


def chat_out(c: dict) -> dict:
    """A chat as `chats` prints it."""
    o = {"name": chat_name(c), "type": "group" if c["group"] else "dm", "last": c["last"], "jid": c["jid"]}
    if c.get("hidden_id"):
        o["hidden_id"] = True
    return o


def phone_jid(text):
    """`+972-50-000-0001` -> `972500000001@s.whatsapp.net`; None unless it is 6-15 digits after dropping + - ( ) and
    spaces."""
    if not isinstance(text, str):
        return None
    digits = re.sub(r"[\s+()\-]", "", text)
    return digits + "@s.whatsapp.net" if re.fullmatch(r"\d{6,15}", digits, re.ASCII) else None


def _by_jid(jid: str):
    rows = _query("SELECT %s FROM ZWACHATSESSION WHERE ZCONTACTJID = ? ORDER BY ZLASTMESSAGEDATE DESC LIMIT 1"
                  % CHAT_COLS, [jid])
    return _chat(rows[0]) if rows else None


def _listing(chats: list) -> str:
    return "\n".join("  %s  (%s, last %s)  %s" % (chat_name(c), "group" if c["group"] else "DM",
                                                  (c["last"] or "never")[:10], c["jid"]) for c in chats)


def resolve_chat(cfg: dict, who: str) -> dict:
    """WHO -> one chat {pk, jid, name, group, last, hidden_id?, self?}. `me` is whatsapp.self_jid. Raises ChatError
    (a plain sentence) or ReaderError."""
    who = (who or "").strip()
    if not who:
        raise ChatError("say which chat: a name, a phone number, a JID, or me")
    me = _wa(cfg).get("self_jid")
    if who.casefold() == "me":
        if not me:
            raise ChatError("whatsapp.self_jid isn't set: run `mother whatsapp setup` in your own terminal")
        c = _by_jid(me) or {"pk": None, "jid": me, "name": "", "group": False, "last": None}
        return dict(c, self=True, name=c.get("name") or "you")
    jid = who if "@" in who else phone_jid(who)
    if jid:
        c = _by_jid(jid)
        if not c:
            raise ChatError("there is no WhatsApp chat with %s on this Mac" % who)
        return dict(c, self=(jid == me))
    # Every named chat, compared in Python: casefold handles any script, and no row cap can hide an exact name.
    rows = _query("SELECT %s FROM ZWACHATSESSION WHERE %s AND ZPARTNERNAME IS NOT NULL" % (CHAT_COLS, NOT_BROADCAST))
    found = _sort_chats([_chat(r) for r in rows])
    key = who.casefold()
    exact = [c for c in found if c["name"].strip().casefold() == key]
    if not exact:
        near = [c for c in found if key in c["name"].casefold()][:5]
        hint = (":\n%s\nName one exactly, or give its number or JID" % _listing(near)) if near else \
            "; `mother whatsapp chats --query` lists chats"
        raise ChatError("no WhatsApp chat is named %r%s" % (who, hint))
    if len(exact) > 1:
        raise ChatError("%d chats are named %r; give the JID of the one you mean:\n%s" % (
            len(exact), who, _listing(exact[:5])))
    return dict(exact[0], self=(exact[0]["jid"] == me))


def has_history(c: dict) -> bool:
    """True when the chat holds a text message, or any message the manager sent: system rows (the encryption
    notice, a missed call) don't count. Reads no content."""
    if c.get("pk") is None:
        return False
    try:
        return bool(_query("SELECT 1 FROM ZWAMESSAGE WHERE ZCHATSESSION = ? AND (ZISFROMME = 1 OR "
                           "(ZMESSAGETYPE IN (0, 7) AND ZTEXT IS NOT NULL)) LIMIT 1", [c["pk"]]))
    except ReaderError:
        return False


# ---------------------------------------------------------------- reader

def read_messages(chat: dict = None, since_iso: str = None, limit: int = DEFAULT_LIMIT) -> dict:
    """The last `limit` messages of one chat (since `since_iso` if given), or across chats since `since_iso`,
    oldest first, as reader items."""
    where, params = ["s.ZCONTACTJID IS NOT NULL", "s.ZCONTACTJID NOT LIKE '%broadcast'"], []
    if chat is not None:
        if chat.get("pk") is None:
            return {"status": "ok", "items": []}
        where.append("m.ZCHATSESSION = ?")
        params.append(chat["pk"])
    if since_iso:
        where.append("m.ZMESSAGEDATE >= ?")
        params.append(to_core(since_iso))
    sql = ("SELECT s.ZCONTACTJID, s.ZPARTNERNAME, s.ZSESSIONTYPE, m.ZISFROMME, m.ZTEXT, m.ZMESSAGETYPE, "
           "m.ZMESSAGEDATE, m.ZSTANZAID, COALESCE(g.ZCONTACTNAME, g.ZFIRSTNAME) "
           "FROM ZWAMESSAGE m JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK "
           "LEFT JOIN ZWAGROUPMEMBER g ON m.ZGROUPMEMBER = g.Z_PK "
           "WHERE %s ORDER BY m.ZMESSAGEDATE DESC, m.Z_PK DESC LIMIT ?" % " AND ".join(where))
    try:
        rows = _query(sql, params + [int(limit)])
    except ReaderError as e:
        return _couldnt(e.reason)
    items = []
    for jid, pname, stype, from_me, text, mtype, mdate, stanza, member in reversed(rows):
        name = chat_name({"jid": jid, "name": pname, "group": stype == GROUP_SESSION,
                          "hidden_id": (jid or "").endswith("@lid")})
        if from_me:
            sender = "me"
        elif stype == GROUP_SESSION:
            sender = member or "a member"
        else:
            sender = name
        items.append({"chat": jid, "chat_name": name, "from_me": bool(from_me), "sender": sender,
                      "text": text if mtype in TEXT_TYPES and text is not None else "[media]",
                      "at": core_ts(mdate), "link": "whatsapp://%s/%s" % (jid, stanza)})
    return {"status": "ok", "items": items}


# ---------------------------------------------------------------- CLI: chats, read

def _opts(args, known, usage):
    """{option: value} for `--opt VALUE` / `--opt=VALUE`; each option once. _fail(usage) on anything else."""
    out, i = {}, 0
    while i < len(args):
        a = args[i]
        name, val = (a.split("=", 1) + [None])[:2] if a.startswith("--") and "=" in a else (a, None)
        if name not in known or name in out:
            _fail(usage)
        if val is None:
            if i + 1 >= len(args):
                _fail(usage)
            val, i = args[i + 1], i + 2
        else:
            i += 1
        out[name] = val
    return out


def _int(v, usage, lo=1, hi=1000):
    try:
        n = int(v)
    except (TypeError, ValueError):
        _fail(usage)
    if not lo <= n <= hi:
        _fail(usage)
    return n


def cmd_chats(cfg, args):
    usage = "usage: mother whatsapp chats [--query TEXT] [--limit N]"
    o = _opts(args, ("--query", "--limit"), usage)
    limit = _int(o.get("--limit", DEFAULT_CHATS), usage)
    try:
        chats = recent_chats(limit, o.get("--query"))
    except ReaderError as e:
        print(json.dumps(_couldnt(e.reason)))
        sys.exit(1)
    print(json.dumps([chat_out(c) for c in chats], indent=2, ensure_ascii=False))


def cmd_read(cfg, args):
    usage = "usage: mother whatsapp read [--chat WHO] [--since ISO] [--limit N]"
    o = _opts(args, ("--chat", "--since", "--limit"), usage)
    limit = _int(o.get("--limit", DEFAULT_LIMIT), usage)
    since = o.get("--since")
    if since:
        try:
            to_core(since)
        except ValueError:
            _fail("--since must be an ISO date-time, e.g. 2026-10-26T08:00:00Z")
    chat = None
    if "--chat" in o:
        try:
            chat = resolve_chat(cfg, o["--chat"])
        except ChatError as e:
            _fail(str(e), 2)
        except ReaderError as e:
            print(json.dumps(_couldnt(e.reason)))
            sys.exit(1)
    elif not since:
        since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=DEFAULT_SINCE_HOURS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps(read_messages(chat, since, limit), indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- CLI: send

SHORTCODES = {"robot_face": "🤖", "sparkles": "✨", "star": "⭐", "rocket": "🚀", "wave": "👋", "bulb": "💡",
              "memo": "📝", "zap": "⚡", "brain": "🧠", "owl": "🦉", "crystal_ball": "🔮", "dove_of_peace": "🕊️"}


def to_whatsapp(text: str) -> str:
    """Slack mrkdwn as WhatsApp shows it: `<url|label>` -> label, `<url>` -> url, a known `:shortcode:` -> its
    emoji, an unknown one left out. `*bold*` is the same in both."""
    t = re.sub(r"<([^<>|]+)\|([^<>]+)>", r"\2", text)
    t = re.sub(r"<((?:https?|mailto):[^<>]+)>", r"\1", t)
    t = re.sub(r":([a-z0-9_+-]+):", lambda m: SHORTCODES.get(m.group(1), ""), t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def signature(cfg: dict) -> str:
    """The disclosure line (rules/outbound.md) as WhatsApp shows it (Slack formatting converted, to_whatsapp)."""
    a = cfg.get("agent") or {}
    if isinstance(a.get("signature"), str) and a["signature"].strip():
        return to_whatsapp(a["signature"])
    emoji = to_whatsapp(a.get("emoji")) if isinstance(a.get("emoji"), str) else ""
    line = "*%s (AI)* · on behalf of %s" % (a.get("name") or "Agent",
                                             a.get("manager_short") or a.get("manager") or "the manager")
    return (emoji + " " + line) if emoji else line


def _units(text: str) -> int:
    """Length as the bridge (JavaScript) counts it: UTF-16 code units, so an emoji counts 2."""
    return len(text.encode("utf-16-le")) // 2


def signature_for(cfg: dict, text: str):
    """The signature the bridge appends to `text`, or None when the text already carries `<name> (AI)`."""
    name = (cfg.get("agent") or {}).get("name") or "Agent"
    return None if "%s (AI)" % name in text else signature(cfg)


def cmd_send(cfg, args):
    usage = "usage: mother whatsapp send --to WHO --text TEXT"
    o = _opts(args, ("--to", "--text"), usage)
    if "--to" not in o or "--text" not in o:
        _fail(usage)
    to, text = o["--to"], o["--text"]  # as received: the guard's ticket is for exactly these
    if not text.strip():
        _fail("the message is empty; nothing was sent", 2)
    try:
        c = resolve_chat(cfg, to)
    except ChatError as e:
        _fail("%s; nothing was sent" % e, 2)
    except ReaderError as e:
        _fail("can't read WhatsApp's database (%s), so MOTHER can't check the chat; nothing was sent" % e.reason, 2)
    sig = None
    if not c.get("self"):
        if c.get("hidden_id"):
            _fail("WhatsApp hides the number of %s, so MOTHER can't address it; send it yourself; nothing was sent"
                  % chat_name(c), 2)
        if not (USER_JID_RE.fullmatch(c["jid"] or "") or GROUP_JID_RE.fullmatch(c["jid"] or "")):
            _fail("%s isn't a person or a group MOTHER can message; nothing was sent" % chat_name(c), 2)
        if not has_history(c):
            _fail("no cold contact: you have no messages with %s yet; start that chat yourself; nothing was sent"
                  % chat_name(c), 2)
        sig = signature_for(cfg, text)
    if _units(text) + (_units(sig) + 2 if sig else 0) > MAX_TEXT:
        _fail("the message is longer than %d characters; nothing was sent" % MAX_TEXT, 2)
    req = {"op": "send", "to": to, "jid": c["jid"], "text": text}
    if sig:
        req["signature"] = sig
    r = bridge_call(req)
    if not r.get("ok"):
        print(json.dumps(r))
        if r.get("error") in UNKNOWN_OUTCOME:
            _fail("the bridge didn't answer in time (%s): the bridge may still send it; check `mother whatsapp "
                  "status` (last_sent_at) before retrying" % r["error"], 2)
        _fail("the send failed: %s; nothing was sent" % (r.get("error") or "error"), 2)
    print(json.dumps({"ok": True, "to": c["jid"], "name": chat_name(c), "id": r.get("stanza")},
                     ensure_ascii=False))


# ---------------------------------------------------------------- bridge client

BRIDGE_SRC = PLUGIN_ROOT / "bridge" / "whatsapp"  # the plugin's copy; `install` copies it out of the plugin
START_WAIT = 15.0  # seconds to wait for a bridge we just started to open its socket
READY_WAIT = 20.0  # then up to this long for its WhatsApp connection to open
# send: the bridge's worst case is a send queued behind another, then up to 30 s for the gap, 20 s for a
# reconnect, 9 s of typing and WhatsApp's own reply. A CLI timeout can't tell whether it went out.
CALL_TIMEOUT = {"status": 5.0, "send": 120.0}
DEFAULT_TIMEOUT = 60.0
UNKNOWN_OUTCOME = ("bridge_timeout", "bridge_bad_reply")
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


def cmd_install(cfg, args):
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
        return subprocess.Popen([_node(), str(app_dir() / "bridge.mjs")], stdin=subprocess.DEVNULL, stdout=log,
                                stderr=log, start_new_session=True, cwd=str(app_dir()))
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


def cmd_stop(cfg, args):
    if args:
        _fail("usage: mother whatsapp stop")
    print(json.dumps({"ok": True, "stopped": stop_bridge()}))


# ---------------------------------------------------------------- link, status

def _bridge_argv(flag: str) -> list:
    return [_node(), str(app_dir() / "bridge.mjs"), flag]


def cmd_link(sub, args) -> int:
    """`link` / `unlink`: stop a running bridge, then run the installed bridge in this terminal (link: the QR or
    phone-code choice and retry loop of setup). Returns the exit code."""
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
    return subprocess.call(_bridge_argv("--" + sub))


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


# ---------------------------------------------------------------- setup (the manager's terminal)

LEGACY_KEYS = ("allow", "approve_via", "max_prompts_per_day")  # the earlier design's keys; setup drops them


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
    return j if USER_JID_RE.fullmatch(j) else None


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


LOGGED_OUT_WORDS = "WhatsApp logged this device out; showing a fresh code."
LINK_STOPS = {"replaced": "another device replaced this link; run it again for a fresh code.",
              "no_account": "the device linked but WhatsApp didn't say which account; run it again."}


def link_once(say, phone=None) -> dict:
    """One `bridge.mjs --link` attempt in this terminal (`--phone <digits>` for a pairing code instead of a QR):
    its QR or code and sentences pass through, and its last stdout line names the outcome.
    {"linked": bool, "me": JID without device suffix or None, "reason": str or None}."""
    import subprocess
    stop_bridge()  # a running bridge would hold the old session (and block the link)
    argv = _bridge_argv("--link") + (["--phone", phone.split("@")[0]] if phone else [])
    p = subprocess.Popen(argv, stdout=subprocess.PIPE)  # stdin, stderr: the terminal
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
    relinked = False
    while True:
        r = link_once(io.say, phone)
        if r.get("linked") and r.get("me"):
            return r["me"]
        reason = r.get("reason")
        if reason == "logged_out":  # the manager removed the device, or it expired: the bridge cleared it
            if relinked:
                raise SetupStop("WhatsApp logged the new link out too; check Linked devices on your phone, then "
                                "run `mother whatsapp setup` again.")
            relinked = True
            io.say(LOGGED_OUT_WORDS)
            continue
        relinked = False
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
    if not got and r.get("logged_out"):
        io.say("WhatsApp logged this device out (it was removed under Linked devices, or it expired).")
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
            for k in LEGACY_KEYS:
                wa.pop(k, None)
        save_whatsapp(ws, validate, enable)
        io.say("Config: whatsapp is on for %s." % me)

        read = db_check()
        if not read["db_reachable"]:
            raise SetupStop(_db_stop(read))
        io.say("Database: readable.")
        io.say("")
        io.say("WhatsApp is set up for %s. In a Claude session, ask MOTHER to read a chat or send a message: "
               "every message to someone else asks you first, in the normal permission prompt, and MOTHER only "
               "messages chats you have already used." % me)
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

TERMINAL_COMMANDS = ("setup", "link", "unlink")  # the manager's own terminal only; the guard denies them too
READ_COMMANDS = {"read": cmd_read, "chats": cmd_chats}
OTHER_COMMANDS = {"install": cmd_install, "stop": cmd_stop}
GONE = {"draft": "write the text and run `mother whatsapp send --to WHO --text ...`; the prompt asks the manager",
        "list": "there are no drafts any more", "drop": "there are no drafts any more",
        "pending": "there are no drafts any more", "prompt": "a send asks the manager in the permission prompt",
        "verify": "a send asks the manager in the permission prompt",
        "allow": "there is no allowlist: MOTHER may message any chat the manager has used",
        "disallow": "there is no allowlist: MOTHER may message any chat the manager has used"}


def main(argv, cfg=None, ws=None, validate=None):
    """`validate` is bin/mother's config validator: setup writes config.json only through it."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cfg = cfg or {}
    sub = argv[0]
    if sub in GONE:
        _fail("`mother whatsapp %s` is gone: %s. See `mother whatsapp help`." % (sub, GONE[sub]))
    if sub not in READ_COMMANDS and sub not in OTHER_COMMANDS and sub not in TERMINAL_COMMANDS \
            and sub not in ("status", "send"):
        _fail("unknown whatsapp command '%s' — see `mother whatsapp help`" % sub)
    if sub == "status":
        return cmd_status(cfg, argv[1:])
    if sub in TERMINAL_COMMANDS and not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail("run `mother whatsapp %s` in your own terminal: it links WhatsApp to MOTHER, so only you run it" % sub, 2)
    require_home(cfg)
    wa = _wa(cfg)
    if sub in READ_COMMANDS and not (wa.get("enabled") and wa.get("read", True)):
        _fail("whatsapp reading is off (whatsapp.enabled / whatsapp.read in config.json): run `mother whatsapp "
              "setup` in your own terminal", 2)
    if sub == "send" and not (wa.get("enabled") and wa.get("send", True)):
        _fail("whatsapp sending is off (whatsapp.enabled / whatsapp.send in config.json): run `mother whatsapp "
              "setup` in your own terminal", 2)
    if sub == "setup":
        if ws is None or validate is None:
            _fail("no workspace")
        sys.exit(setup(Path(ws), validate, TerminalIO(), argv[1:]))
    if sub in ("link", "unlink"):
        sys.exit(cmd_link(sub, argv[1:]))
    if sub == "send":
        return cmd_send(cfg, argv[1:])
    if sub in READ_COMMANDS:
        return READ_COMMANDS[sub](cfg, argv[1:])
    return OTHER_COMMANDS[sub](cfg, argv[1:])
