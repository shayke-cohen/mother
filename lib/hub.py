"""mother hub — state, thing keys and the inbox behind the watcher and the hub session (docs/CONTRACT.md §5e).

This part: the machine-local files under <workspace>/.mother/hub/ and the vocabulary they share.

  thing key   one string per piece of work, the same wherever it shows up: tracker `PLAT-123`, code
              `owner/repo#45`, chat `C00000001/<thread ts>`, mail `mail:<thread id>`, calendar `cal:<event id>`.
  inbox line  one JSON object per source update, appended by the watcher; summary at most 300 characters.
  state files written atomically (temp + os.replace) under the flock `hub.lock`; `inbox.jsonl` is only appended to
              (also under `hub.lock`, so a rotation never races an append).

Python 3.9 stdlib only.
"""
import contextlib
import fcntl
import io
import json
import os
import re
import signal
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SUMMARY_MAX = 300
KEEP_DAYS = 7
KINDS = ("update", "resolved", "error", "sweep")

_TRACKER = re.compile(r"(?<![A-Za-z0-9_-])[A-Z][A-Z0-9]+-\d+(?![A-Za-z0-9_-])")
_PR_URL = r"https?://[\w.-]+/(?P<u>[\w.-]+/[\w.-]+)/pull/(?P<un>\d+)"
_PR_REF = r"(?<![\w./#-])(?P<r>[\w.-]+/[\w.-]+)#(?P<rn>\d+)(?![\w-])"
_CODE = re.compile(_PR_URL + "|" + _PR_REF)


def hub_dir(ws: Path) -> Path:
    return Path(ws) / ".mother" / "hub"


@contextmanager
def locked(ws: Path):
    """Exclusive flock on .mother/hub/hub.lock for the with-block. Wrap every load_json -> change ->
    save_json in it. Not re-entrant."""
    d = hub_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / "hub.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_json(ws: Path, name: str, default):
    """The parsed file, or `default` when it is missing or unreadable (a missing file means nothing pending)."""
    try:
        return json.loads((hub_dir(ws) / name).read_text())
    except (OSError, ValueError):
        return default


def save_json(ws: Path, name: str, doc) -> None:
    """Atomic write (temp file + rename). Does not lock: the caller holds `locked(ws)`."""
    d = hub_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ("%s.%d.tmp" % (name, os.getpid()))
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(str(tmp), str(d / name))


def thing_key(source: str, ref: dict) -> str:
    if source == "tracker":
        return str(ref["key"])
    if source == "code":
        return "%s#%s" % (ref["repo"], ref["number"])
    if source == "chat":
        return "%s/%s" % (ref["channel"], ref.get("thread_ts") or ref["ts"])
    if source == "mail":
        return "mail:%s" % ref["thread_id"]
    if source == "calendar":
        return "cal:%s" % ref["event_id"]
    raise ValueError("unknown source: %r" % (source,))


def extract_also(text: str) -> List[str]:
    """Tracker keys and PR references (URL or `owner/repo#N`) in `text`, de-duplicated, in order."""
    found = []
    for m in re.finditer(_TRACKER.pattern + "|" + _CODE.pattern, text or ""):
        if m.group("u"):
            k = "%s#%s" % (m.group("u"), m.group("un"))
        elif m.group("r"):
            k = "%s#%s" % (m.group("r"), m.group("rn"))
        else:
            k = m.group(0)
        if k not in found:
            found.append(k)
    return found


def make_line(source: str, kind: str, key: str, at: str, title: str = "", who: str = "", link: str = "",
              summary: str = "", also=None) -> dict:
    if kind not in KINDS:
        raise ValueError("unknown kind: %r" % (kind,))
    return {"id": "%s:%s:%s" % (source, key, at), "at": at, "source": source, "kind": kind, "key": key,
            "also": list(also or []), "title": title, "who": who, "link": link,
            "summary": (summary or "")[:SUMMARY_MAX]}


def coalesce(lines: List[dict]) -> List[dict]:
    """One line per key: the latest `at` wins, `also` is the union (order kept). First-seen key order."""
    best = {}
    also = {}
    for l in lines:
        k = l["key"]
        also.setdefault(k, [])
        for a in l.get("also") or []:
            if a not in also[k]:
                also[k].append(a)
        if k not in best or l["at"] >= best[k]["at"]:
            best[k] = l
    out = []
    for k, l in best.items():
        l = dict(l)
        l["also"] = also[k]
        out.append(l)
    return out


def append_inbox(ws: Path, lines: List[dict]) -> None:
    """Append one JSON object per line to inbox.jsonl, flushed and fsynced. Takes `locked(ws)` itself (so it
    never interleaves with rotate_inbox); the caller must not already hold it."""
    if not lines:
        return
    with locked(ws):
        d = hub_dir(ws)
        with open(str(d / "inbox.jsonl"), "a") as f:
            for l in lines:
                f.write(json.dumps(l, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _fold(src: Path, dest: Path) -> None:
    """Append src to dest (fsynced), then delete src."""
    with open(str(src), "rb") as r, open(str(dest), "ab") as out:
        out.write(r.read())
        out.flush()
        os.fsync(out.fileno())
    src.unlink()


def rotate_inbox(ws: Path, at: datetime) -> None:
    """Move inbox.jsonl aside as inbox-YYYY-MM-DD.jsonl (the date of `at`, appended to if it exists) and
    delete dated files 7 or more days older than `at`. Takes `locked(ws)` itself, so no append can land
    mid-rotation; the caller must not already hold it. Leftover `inbox.jsonl.*.rotating` files from a crash
    are folded into the dated file first, so no line is stranded."""
    with locked(ws):
        d = hub_dir(ws)
        today = at.date()
        cur = d / "inbox.jsonl"
        dest = d / ("inbox-%s.jsonl" % today.isoformat())
        for old in sorted(d.glob("inbox.jsonl.*.rotating")):
            _fold(old, dest)
        if cur.exists():
            if not cur.stat().st_size:
                cur.unlink()
            elif not dest.exists():
                os.replace(str(cur), str(dest))
            else:
                aside = d / ("inbox.jsonl.%d.rotating" % os.getpid())
                os.replace(str(cur), str(aside))
                _fold(aside, dest)
        for p in d.glob("inbox-*.jsonl"):
            m = re.match(r"inbox-(\d{4}-\d{2}-\d{2})\.jsonl$", p.name)
            if m and (today - datetime.strptime(m.group(1), "%Y-%m-%d").date()).days >= KEEP_DAYS:
                p.unlink()


# ------------------------------------------------------------------------------------------------- watcher
CAPS = ("chat", "mail", "tracker", "calendar", "code")
DEFAULTS = {"every_seconds": 120, "idle_every_seconds": 600, "sweep_minutes": 30}
SERVER_NAME = "hub-watcher"


def _hub_cfg(cfg: dict) -> dict:
    h = (cfg or {}).get("hub")
    return h if isinstance(h, dict) else {}


def _iso(at: datetime) -> str:
    import hub_sources
    return hub_sources.iso(at)


def is_active(cfg: dict, at: datetime) -> bool:
    """Inside the manager's active hours on a workday (routines.tick.active_hours, manager.workdays/timezone)."""
    import now as nowmod
    s = nowmod.Schedule.from_config(cfg)
    local = at.astimezone(s.tz)
    return nowmod.DAYS[local.weekday()] in s.workdays and s.start <= local.time().replace(tzinfo=None) < s.end


def next_active_start(cfg: dict, at: datetime) -> datetime:
    import now as nowmod
    s = nowmod.Schedule.from_config(cfg)
    local = at.astimezone(s.tz)
    day = local.date()
    for _ in range(9):
        start = datetime.combine(day, s.start, tzinfo=s.tz)
        if nowmod.DAYS[day.weekday()] in s.workdays and start > local:
            return start
        day += timedelta(days=1)
    return local + timedelta(hours=1)


def interval(cfg: dict, at: datetime) -> Tuple[bool, int]:
    """(inside active hours, seconds to sleep before the next poll). Outside active hours it is
    `idle_every_seconds`, or, when that is 0, the time until the next active window opens."""
    h = _hub_cfg(cfg)
    if is_active(cfg, at):
        return True, int(h.get("every_seconds", DEFAULTS["every_seconds"]))
    idle = int(h.get("idle_every_seconds", DEFAULTS["idle_every_seconds"]))
    if idle > 0:
        return False, idle
    return False, max(1, int((next_active_start(cfg, at) - at).total_seconds()))


def build_me(cfg: dict) -> dict:
    """What the adapters need to recognise the manager: email, chat id and (from the matching people entry) code login."""
    m = (cfg or {}).get("manager") or {}
    me = {"email": m.get("email") or "", "chat": m.get("chat_dm") or "", "code": m.get("code") or ""}
    for team in (cfg or {}).get("teams") or []:
        for p in (team or {}).get("people") or []:
            if me["email"] and (p.get("email") or "").lower() == me["email"].lower():
                me["chat"] = me["chat"] or p.get("chat") or ""
                me["code"] = me["code"] or p.get("code") or ""
    return me


def pending_work(ws: Path) -> bool:
    """Something is waiting on the hub: `pending` proposals in hub.json, or a live session with unseen > 0.
    Both files are read defensively; a missing or odd one counts as nothing pending."""
    doc = load_json(ws, "hub.json", {})
    if isinstance(doc, dict) and doc.get("pending"):
        return True
    ses = load_json(ws, "sessions.json", {})
    items = ses.get("sessions") if isinstance(ses, dict) else None
    for s in (items.values() if isinstance(items, dict) else []):
        if isinstance(s, dict) and s.get("state") == "live":
            try:
                if int(s.get("unseen") or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def _server_of(conn: dict) -> str:
    return os.path.basename(str(conn.get("command") or "")) or ("gh" if conn.get("cli") else SERVER_NAME)


def _record_health(ws: Path, cap: str, connector: str, server: str, status: str, kind: str = "") -> None:
    import health
    argv = ["record", "--kind", cap, "--cap", "read", "--server", server, "--status", status]
    if kind and status != "ok":
        argv += ["--class", kind]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            health.main(argv, ws)
    except Exception as e:  # health trouble must never stop the watcher
        print("mother hub: could not record health (%s)" % e, file=sys.stderr)


def poll_once(ws: Path, cfg: dict, at: datetime, clients: dict, poll: bool = True) -> dict:
    """One pass over the sources; returns the heartbeat it wrote to watcher.json.

    For each capability in `connectors` that has an adapter: poll, coalesce, append to the inbox, and only then
    save the cursor (a crash between the two repeats lines rather than losing them). A failing source keeps its
    cursor; on the ok -> failing transition one `error` line is appended and the failure recorded through
    `mother health record`. `clients` maps capability -> ReadOnlyClient (or the exception that building it raised).
    With `poll=False` only the heartbeat is refreshed (idle hours with no polling)."""
    import hub_sources
    from hub_mcp import ForbiddenTool
    h = _hub_cfg(cfg)
    stamp = _iso(at)
    old = load_json(ws, "watcher.json", {})
    old_src = old.get("sources") if isinstance(old, dict) and isinstance(old.get("sources"), dict) else {}
    active, every = interval(cfg, at)
    sources = {}  # type: Dict[str, dict]
    errors = []
    if not poll:
        sources = dict(old_src)
        errors = [str(e) for e in (old.get("errors") or [])] if isinstance(old, dict) else []
    connectors = (cfg or {}).get("connectors") or {}
    me = build_me(cfg)
    for cap in CAPS if poll else ():
        connector = connectors.get(cap)
        if not isinstance(connector, str) or not connector:
            continue
        conns = h.get("connections") if isinstance(h.get("connections"), dict) else {}
        adapter = hub_sources.ADAPTERS.get("%s:%s" % (cap, connector))
        client = clients.get(cap)
        prev = old_src.get(cap) or {}
        if not isinstance(prev, dict):
            prev = {}
        since = prev.get("since", stamp) if prev.get("status") == "couldnt_check" else stamp
        if adapter is None or cap not in conns or client is None or isinstance(client, Exception):
            if adapter is None:
                reason = "no adapter for %s" % connector
            elif cap not in conns or client is None:
                reason = "no connection"
            else:
                reason = str(getattr(client, "message", None) or client)[:200]
            ent = {"status": "couldnt_check", "reason": reason, "since": since}
            if isinstance(client, Exception) and cap in conns:  # the server would not start: a source failure
                _failed(ws, cap, connector, conns[cap], client, prev, stamp, ent)
            sources[cap] = ent
            errors.append("%s: %s" % (cap, reason))
            continue
        cursors = load_json(ws, "cursors.json", {})
        cursors = cursors if isinstance(cursors, dict) else {}
        try:
            res = adapter.poll(client, cursors.get(cap), (h.get("watch") or {}).get(cap) or {}, me, at)
        except ForbiddenTool:
            raise  # a programming error: stop the watcher rather than swallow it
        except Exception as e:
            ent = {"status": "couldnt_check", "reason": str(getattr(e, "message", None) or e)[:200], "since": since}
            _failed(ws, cap, connector, conns[cap], e, prev, stamp, ent)
            sources[cap] = ent
            errors.append("%s: %s" % (cap, ent["reason"]))
            continue
        append_inbox(ws, res["lines"])
        with locked(ws):
            cursors = load_json(ws, "cursors.json", {})
            cursors = cursors if isinstance(cursors, dict) else {}
            cursors[cap] = res["cursor"]
            save_json(ws, "cursors.json", cursors)
        if res.get("partial"):
            sources[cap] = {"status": "partial", "reason": "; ".join(res["partial"])[:300],
                            "since": prev.get("since", stamp) if prev.get("status") == "partial" else stamp}
            errors.append("%s: %s" % (cap, sources[cap]["reason"]))
        else:
            sources[cap] = {"status": "ok"}
        if prev.get("status") == "couldnt_check":
            _record_health(ws, cap, connector, _server_of(conns[cap]), "ok")
    if poll:
        _maybe_sweep(ws, h, at, stamp)
    beat = {"at": stamp, "every_seconds": every, "active": active, "sources": sources, "errors": errors}
    with locked(ws):
        save_json(ws, "watcher.json", beat)
    return beat


def _failed(ws, cap, connector, conn, exc, prev, stamp, ent) -> None:
    """On the ok -> failing transition (anything but an already couldnt_check source): one `error` inbox line and
    one `mother health record`. Further failures in a row stay quiet."""
    if prev.get("status") == "couldnt_check":
        return
    kind = getattr(exc, "kind", None) or "other"
    server = _server_of(conn) if isinstance(conn, dict) else SERVER_NAME
    append_inbox(ws, [make_line(cap, "error", "watcher:%s" % cap, stamp, title="%s source unreachable" % cap,
                                summary=ent["reason"])])
    import health
    status = health.classify(ent["reason"])[0]
    if status == "ok" or not health.valid_status(status):
        status = "error:%s" % kind
    _record_health(ws, cap, connector, server, status, kind)


def _maybe_sweep(ws: Path, h: dict, at: datetime, stamp: str) -> None:
    """Append one `sweep` line when sweep_minutes have passed since the last one and something is pending."""
    minutes = int(h.get("sweep_minutes", DEFAULTS["sweep_minutes"]))
    cursors = load_json(ws, "cursors.json", {})
    last = cursors.get("_last_sweep") if isinstance(cursors, dict) else None
    if last:
        import hub_sources
        try:
            if at - hub_sources.parse_iso(last) < timedelta(minutes=minutes):
                return
        except (ValueError, TypeError):
            pass
    if not pending_work(ws):
        return
    append_inbox(ws, [make_line("hub", "sweep", "sweep", stamp, title="sweep",
                                summary="pending proposals or unseen session updates")])
    with locked(ws):
        cursors = load_json(ws, "cursors.json", {})
        cursors = cursors if isinstance(cursors, dict) else {}
        cursors["_last_sweep"] = stamp
        save_json(ws, "cursors.json", cursors)


def build_clients(cfg: dict, have: dict) -> dict:
    """Clients for every `hub.connections` entry that has no live one yet. A client that cannot start is stored as
    the exception, so poll_once reports that source as couldnt_check and the next pass retries."""
    import hub_mcp
    import hub_sources
    from hub_mcp import McpError
    out = dict(have)
    conns = _hub_cfg(cfg).get("connections") or {}
    for cap in CAPS:
        conn = conns.get(cap)
        if not isinstance(conn, dict) or (cap in out and not isinstance(out[cap], Exception)):
            continue
        adapter = hub_sources.ADAPTERS.get("%s:%s" % (cap, ((cfg or {}).get("connectors") or {}).get(cap)))
        if adapter is None:
            continue
        try:
            if conn.get("cli"):  # cli wins over command
                if conn["cli"] != "gh":
                    raise McpError("other", "unsupported cli: %s" % conn["cli"])
                raw = hub_sources.GhClient()
            else:
                raw = hub_mcp.Client(conn.get("command") or "", list(conn.get("args") or []),
                                     dict(conn.get("env") or {}))
                raw.start()
            out[cap] = hub_mcp.ReadOnlyClient(raw, hub_mcp.resolve_tools(raw.tools, adapter.READ_TOOLS))
        except McpError as e:
            out[cap] = e
        except Exception as e:
            out[cap] = McpError("other", str(e))
    return out


def home_status(cfg: dict) -> int:
    """0 on the home machine; 2 on another machine; 3 when no home machine is set (as `mother home check`)."""
    home = ((cfg or {}).get("workspace") or {}).get("home_machine")
    if not home:
        return 3
    here = (os.environ.get("MOTHER_HOSTNAME") or socket.gethostname()).split(".")[0].lower()
    return 0 if home == here else 2


def watch(ws: Path, cfg: dict) -> None:
    """Run the watcher until SIGTERM (returns, so the process exits 0). Exits 2 (3 when no home machine is set)
    if this is not the home machine. Polls every `every_seconds` in active hours, `idle_every_seconds` outside
    (0: not at all, sleeping until the next window); rotates the inbox when the local day changes."""
    import now as nowmod
    code = home_status(cfg)
    if code:
        print("mother hub: not the home machine (see `mother home check`)", file=sys.stderr)
        sys.exit(code)
    stop = {"flag": False}
    old = signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    tz = nowmod.Schedule.from_config(cfg).tz
    clients = {}  # type: dict
    prev_at = datetime.now(tz)
    try:
        while not stop["flag"]:
            at = datetime.now(tz)
            if prev_at.astimezone(tz).date() != at.date():
                rotate_inbox(ws, prev_at)
            active, every = interval(cfg, at)
            idle0 = not active and int(_hub_cfg(cfg).get("idle_every_seconds", DEFAULTS["idle_every_seconds"])) == 0
            if not idle0:
                clients = build_clients(cfg, clients)
            beat = poll_once(ws, cfg, at, clients, poll=not idle0)
            for cap, ent in beat["sources"].items():  # a failed source gets a fresh client next pass
                c = clients.get(cap)
                if ent.get("status") == "couldnt_check" and c is not None and not isinstance(c, Exception):
                    try:
                        c.close()
                    except Exception:
                        pass
                    clients.pop(cap, None)
            prev_at = at
            end = time.time() + beat["every_seconds"]
            while not stop["flag"] and time.time() < end:
                time.sleep(min(1.0, max(0.0, end - time.time())))
    finally:
        signal.signal(signal.SIGTERM, old)
        for c in clients.values():
            if not isinstance(c, Exception):
                try:
                    c.close()
                except Exception:
                    pass


def main(args, ws, cfg, at) -> int:
    print("mother hub: state core only (watch, status and the rest land with later tasks)")
    return 0
