"""mother hub — state, thing keys and the inbox behind the watcher and the hub session (docs/CONTRACT.md §5e).

This part: the machine-local files under <workspace>/.mother/hub/ and the vocabulary they share.

  thing key   one string per piece of work, the same wherever it shows up: tracker `PLAT-123`, code
              `owner/repo#45`, chat `C00000001/<thread ts>`, mail `mail:<thread id>`, calendar `cal:<event id>`.
  inbox line  one JSON object per source update, appended by the watcher; summary at most 300 characters.
  state files written atomically (temp + os.replace) under the flock `hub.lock`; `inbox.jsonl` is only appended to
              (also under `hub.lock`, so a rotation never races an append).

Python 3.9 stdlib only.
"""
import fcntl
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


def _ttl(cfg: dict) -> float:
    try:
        return float(_hub_cfg(cfg).get("update_ttl_hours", 24))
    except (TypeError, ValueError):
        return 24.0


def _older_than_ttl(stamp, cfg: dict, at: datetime) -> bool:
    """True when `stamp` is more than hub.update_ttl_hours working hours before `at`; False when unreadable."""
    import now as nowmod
    import hub_sources
    try:
        return nowmod.working_hours(hub_sources.parse_iso(stamp), at, _schedule(cfg)) > _ttl(cfg)
    except (TypeError, ValueError, AttributeError):
        return False


def pending_work(ws: Path, cfg: dict, at: datetime) -> bool:
    """Something is waiting on the hub: any `pending` proposal in hub.json, or a live session with unseen > 0 that
    was routed to within hub.update_ttl_hours working hours (older unseen news no longer wakes the hub).
    Both files are read defensively; a missing or odd one counts as nothing pending."""
    doc = load_json(ws, "hub.json", {})
    if isinstance(doc, dict) and doc.get("pending"):
        return True
    ses = load_json(ws, "sessions.json", {})
    items = ses.get("sessions") if isinstance(ses, dict) else None
    for s in (items.values() if isinstance(items, dict) else []):
        if isinstance(s, dict) and s.get("state") == "live":
            try:
                unseen = int(s.get("unseen") or 0)
            except (TypeError, ValueError):
                continue
            if unseen > 0 and s.get("last_routed_at") and not _older_than_ttl(s["last_routed_at"], cfg, at):
                return True
    return False


def poll_once(ws: Path, cfg: dict, at: datetime, clients: dict, poll: bool = True) -> dict:
    """One pass over the sources; returns the heartbeat it wrote to watcher.json.

    For each capability in `connectors` that has an adapter: poll, coalesce, append to the inbox, and only then
    save the cursor (a crash between the two repeats lines rather than losing them). A failing source keeps its
    cursor; on the first failing pass one `error` line is appended (the health records are not touched). `clients` maps capability -> ReadOnlyClient (or the exception that building it raised).
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
    woke = False  # a line the hub must decide (anything but `resolved`) was appended this pass
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
                woke = _failed(ws, cap, stamp, ent) or woke
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
            woke = _failed(ws, cap, stamp, ent) or woke
            sources[cap] = ent
            errors.append("%s: %s" % (cap, ent["reason"]))
            continue
        append_inbox(ws, res["lines"])
        woke = woke or any(l.get("kind") != "resolved" for l in res["lines"])
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
        _set_errored(ws, cap, False)
    if poll:
        woke = _maybe_sweep(ws, cfg, at, stamp) or woke
    beat = {"at": stamp, "every_seconds": every, "active": active, "sources": sources, "errors": errors}
    last = stamp if woke else (old.get("last_append_at") if isinstance(old, dict) else None)
    if last:
        beat["last_append_at"] = last
    with locked(ws):
        save_json(ws, "watcher.json", beat)
    return beat


def _failed(ws, cap, stamp, ent) -> bool:
    """The first failing pass of a source since it last worked: one `error` inbox line. `cursors.json` `_errored`
    remembers that it was emitted (not the status, so a source that was `couldnt_check` for a missing connection
    still gets its line when it later fails). Nothing is written to capabilities.json."""
    cursors = load_json(ws, "cursors.json", {})
    cursors = cursors if isinstance(cursors, dict) else {}
    if cap in (cursors.get("_errored") or []):
        return False
    append_inbox(ws, [make_line(cap, "error", "watcher:%s" % cap, stamp, title="%s source unreachable" % cap,
                                summary=ent["reason"])])
    _set_errored(ws, cap, True)
    return True


def _set_errored(ws, cap, on: bool) -> None:
    with locked(ws):
        cursors = load_json(ws, "cursors.json", {})
        cursors = cursors if isinstance(cursors, dict) else {}
        cur = [c for c in (cursors.get("_errored") or []) if c != cap]
        if on:
            cur.append(cap)
        if cur != list(cursors.get("_errored") or []):
            cursors["_errored"] = cur
            save_json(ws, "cursors.json", cursors)


def _maybe_sweep(ws: Path, cfg: dict, at: datetime, stamp: str) -> bool:
    """Append one `sweep` line when sweep_minutes have passed since the last one and something is pending.
    True when it appended one."""
    h = _hub_cfg(cfg)
    minutes = int(h.get("sweep_minutes", DEFAULTS["sweep_minutes"]))
    cursors = load_json(ws, "cursors.json", {})
    last = cursors.get("_last_sweep") if isinstance(cursors, dict) else None
    if last:
        import hub_sources
        try:
            if at - hub_sources.parse_iso(last) < timedelta(minutes=minutes):
                return False
        except (ValueError, TypeError):
            pass
    if not pending_work(ws, cfg, at):
        return False
    append_inbox(ws, [make_line("hub", "sweep", "sweep", stamp, title="sweep",
                                summary="pending proposals or unseen session updates")])
    with locked(ws):
        cursors = load_json(ws, "cursors.json", {})
        cursors = cursors if isinstance(cursors, dict) else {}
        cursors["_last_sweep"] = stamp
        save_json(ws, "cursors.json", cursors)
    return True


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


def watch(ws: Path, cfg: dict, clock=None, sleep=None, factory=None, stop=None) -> None:
    """Run the watcher until SIGTERM (returns, so the process exits 0). Exits 2 (3 when no home machine is set)
    if this is not the home machine. Polls every `every_seconds` in active hours, `idle_every_seconds` outside
    (0: not at all, sleeping until the next window); rotates the inbox when the local day changes.
    `clock() -> aware datetime`, `sleep(seconds)`, `factory(cfg, have) -> clients` and `stop` (a threading.Event)
    are injectable for tests."""
    import threading
    import now as nowmod
    code = home_status(cfg)
    if code:
        print("mother hub: not the home machine (see `mother home check`)", file=sys.stderr)
        sys.exit(code)
    tz = nowmod.Schedule.from_config(cfg).tz
    clock = clock or (lambda: datetime.now(tz))
    sleep = sleep or time.sleep
    factory = factory or build_clients
    stop = stop or threading.Event()
    try:
        old = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except ValueError:  # not the main thread: the caller stops us through `stop`
        old = None
    clients = {}  # type: dict
    prev_at = clock()
    try:
        while not stop.is_set():
            at = clock()
            if prev_at.astimezone(tz).date() != at.astimezone(tz).date():
                rotate_inbox(ws, prev_at.astimezone(tz))
            active, every = interval(cfg, at)
            idle0 = not active and int(_hub_cfg(cfg).get("idle_every_seconds", DEFAULTS["idle_every_seconds"])) == 0
            if not idle0:
                clients = factory(cfg, clients)
            beat = poll_once(ws, cfg, at, clients, poll=not idle0)
            for cap, ent in beat["sources"].items():  # a failed source gets a fresh client next pass
                c = clients.get(cap)
                if ent.get("status") == "couldnt_check" and c is not None:
                    clients.pop(cap, None)
                    if not isinstance(c, Exception):
                        _close(c)
            prev_at = at
            waited = 0
            while not stop.is_set() and waited < beat["every_seconds"]:
                step = min(1, beat["every_seconds"] - waited)
                sleep(step)
                waited += step
    finally:
        if old is not None:
            signal.signal(signal.SIGTERM, old)
        for c in clients.values():
            if not isinstance(c, Exception):
                _close(c)


def _close(c) -> None:
    try:
        c.close()
    except Exception:
        pass


LABEL = "com.mother.hub"
USAGE = """mother hub <verb>
  watch       run the watcher in the foreground (what the launchd job runs); home machine only
  install     write ~/Library/LaunchAgents/com.mother.hub.plist and load it (home machine only)
  uninstall   unload the job and remove the plist
  check       one read per hub.connections entry, under the plist's environment: cap | ok/✗ | reason; exit 1 if any fails
  status      watcher and hub heartbeats; exit 2 if either is stale or the watcher never ran
  tail        [--once] [--follow]  inbox lines after hub.json inbox_cursor, one JSON line per key (the hub's Monitor runs --follow)
  decide      stdin: one decision object or a list {line_id, key, decision: log|now|route|propose|error, ...}
  pending     raised <key> --name <slug> [--task <task_id>] | drop <key>
  session     add <key> --id ID --name N --title T [--link URL] [--also K,K] | drop <key> | seen <key> | for <key>
  done        <U-id>  mark an update done
  snooze      <U-id> 1h|tomorrow|next-week
  show        [--json]  pending proposals, updates and sessions"""


def default_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def _conns(cfg: dict) -> dict:
    c = _hub_cfg(cfg).get("connections")
    return {k: v for k, v in (c if isinstance(c, dict) else {}).items() if k in CAPS and isinstance(v, dict)}


def _program(conn: dict) -> str:
    """The executable a connection needs: `gh` when `cli` is set (it wins over `command`), else `command`."""
    return conn.get("cli") or conn.get("command") or ""


def resolve_programs(cfg: dict, which=None) -> Tuple[Dict[str, str], List[str]]:
    """({cap: absolute path}, [names that were not found])."""
    import shutil
    which = which or shutil.which
    found, missing = {}, []
    for cap, conn in _conns(cfg).items():
        name = _program(conn)
        path = which(name) if name else None
        if path:
            found[cap] = os.path.abspath(path)
        else:
            missing.append(name or "(%s: no command)" % cap)
    return found, missing


def launch_path(found: Dict[str, str]) -> str:
    dirs = []  # type: List[str]
    for p in found.values():
        d = os.path.dirname(p)
        if d not in dirs:
            dirs.append(d)
    for d in ("/usr/bin", "/bin"):
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def plist(ws: Path, cfg: dict, python: str, mother: str, found: Optional[Dict[str, str]] = None) -> str:
    """The launchd job: absolute python and bin/mother, MOTHER_WORKSPACE and a PATH from the connection commands."""
    import plistlib
    if found is None:
        found, _ = resolve_programs(cfg)
    doc = {"Label": LABEL,
           "ProgramArguments": [python, mother, "hub", "watch"],
           "EnvironmentVariables": {"MOTHER_WORKSPACE": str(ws), "PATH": launch_path(found)},
           "KeepAlive": True,
           "RunAtLoad": True,
           "StandardOutPath": str(hub_dir(ws) / "watcher.log"),
           "StandardErrorPath": str(hub_dir(ws) / "watcher.log")}
    return plistlib.dumps(doc).decode("utf-8")


def launchctl(*args) -> Tuple[int, str]:
    import subprocess
    try:
        p = subprocess.run(["launchctl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "").strip()


def install(ws: Path, cfg: dict, plist_path=None, ctl=None, which=None, python=None, mother=None) -> int:
    ctl = ctl or launchctl
    plist_path = Path(plist_path or default_plist_path())
    code = home_status(cfg)
    if code:
        print("mother hub install: not the home machine (see `mother home check`)", file=sys.stderr)
        return code
    if not _conns(cfg):
        print("mother hub install: hub.connections is empty; nothing to watch", file=sys.stderr)
        return 1
    found, missing = resolve_programs(cfg, which)
    if missing:
        print("mother hub install: not found on PATH: %s" % ", ".join(missing), file=sys.stderr)
        return 1
    python = python or os.path.abspath(sys.executable)
    mother = mother or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "mother"))
    hub_dir(ws).mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist(ws, cfg, python, mother, found))
    domain = "gui/%d" % os.getuid()
    ctl("bootout", "%s/%s" % (domain, LABEL))  # a reinstall replaces the loaded job; not loaded is fine
    rc, out = ctl("bootstrap", domain, str(plist_path))
    if rc:
        print("mother hub install: launchctl bootstrap failed: %s" % out, file=sys.stderr)
        return 1
    print("installed %s (logs: %s)" % (plist_path, hub_dir(ws) / "watcher.log"))
    return 0


def uninstall(plist_path=None, ctl=None) -> int:
    ctl = ctl or launchctl
    plist_path = Path(plist_path or default_plist_path())
    ctl("bootout", "gui/%d/%s" % (os.getuid(), LABEL))
    existed = plist_path.exists()
    if existed:
        plist_path.unlink()
    print("removed %s" % plist_path if existed else "not installed")
    return 0


def _gh_user() -> None:
    import subprocess
    try:
        p = subprocess.run(["gh", "api", "user"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(str(e))
    if p.returncode:
        raise RuntimeError((p.stderr or "gh api user failed").strip()[:200])


def check(cfg: dict, factory=None, gh_user=None, which=None, out=None) -> int:
    """One read per connection, under the PATH the plist sets: an MCP server must start and answer `tools/list`
    with at least one tool on its adapter's read allow-list; `gh` must answer `gh api user`. Never calls a write
    tool. Prints `cap | ok/✗ | reason`; exit 1 if any fails."""
    import hub_mcp
    import hub_sources
    out = out or sys.stdout
    factory = factory or build_clients
    gh_user = gh_user or _gh_user
    conns = _conns(cfg)
    if not conns:
        print("mother hub check: hub.connections is empty", file=sys.stderr)
        return 1
    found, _ = resolve_programs(cfg, which)
    saved = os.environ.get("PATH")
    os.environ["PATH"] = launch_path(found)
    rows = []
    try:
        for cap in CAPS:
            conn = conns.get(cap)
            if conn is None:
                continue
            adapter = hub_sources.ADAPTERS.get("%s:%s" % (cap, ((cfg or {}).get("connectors") or {}).get(cap)))
            if adapter is None:
                rows.append((cap, False, "no adapter for connectors.%s" % cap))
                continue
            if conn.get("cli"):
                try:
                    if conn["cli"] != "gh":
                        raise RuntimeError("unsupported cli: %s" % conn["cli"])
                    gh_user()
                    rows.append((cap, True, "gh api user"))
                except Exception as e:
                    rows.append((cap, False, str(e)))
                continue
            got = factory({"hub": {"connections": {cap: conn}}, "connectors": (cfg or {}).get("connectors")}, {})
            c = got.get(cap)
            try:
                if c is None or isinstance(c, Exception):
                    rows.append((cap, False, str(c) if c is not None else "no client"))
                elif not hub_mcp.resolve_tools(c.tools, adapter.READ_TOOLS):
                    rows.append((cap, False, "no read tool (%s) in tools/list" % ", ".join(sorted(adapter.READ_TOOLS))))
                else:
                    rows.append((cap, True, "tools/list: %d tools" % len(c.tools)))
            finally:
                if c is not None and not isinstance(c, Exception):
                    _close(c)
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved
    for cap, ok, why in rows:
        print("%s | %s | %s" % (cap, "ok" if ok else "\u2717", why), file=out)
    return 0 if all(r[1] for r in rows) else 1


def _age(stamp, at: datetime):
    import hub_sources
    try:
        return (at - hub_sources.parse_iso(stamp)).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return None


def status(ws: Path, at: datetime, out=None) -> int:
    """Heartbeats of the watcher and the hub. Exit 2 only when the watcher never ran or is stale (`at` older than
    2x `every_seconds` while `active`). The hub is printed STALE (exit unchanged) when `hub_lagging` while active."""
    out = out or sys.stdout
    w = load_json(ws, "watcher.json", {})
    h = load_json(ws, "hub.json", {})
    w = w if isinstance(w, dict) else {}
    h = h if isinstance(h, dict) else {}
    if not w.get("at"):
        print("watcher: never ran (no watcher.json)", file=out)
        print("hub: never woke" if not h.get("last_wake") else "hub: last wake %s" % h["last_wake"], file=out)
        return 2
    try:
        every = int(w.get("every_seconds") or DEFAULTS["every_seconds"])
    except (TypeError, ValueError):
        every = DEFAULTS["every_seconds"]
    active = bool(w.get("active"))
    stale = False
    age = _age(w.get("at"), at)
    wstale = active and (age is None or age > 2 * every)
    stale = stale or wstale
    print("watcher: last poll %s (%s), every %ds, %s%s" % (
        w["at"], "unreadable time" if age is None else "%ds ago" % age, every,
        "active hours" if active else "outside active hours", ", STALE" if wstale else ""), file=out)
    for cap, ent in sorted((w.get("sources") or {}).items()):
        why = ent.get("reason")
        print("  %s: %s%s" % (cap, ent.get("status"), " (%s)" % why if why else ""), file=out)
    lagging = hub_lagging(w, h, at)
    if h.get("last_wake"):
        hage = _age(h["last_wake"], at)
        hstale = active and lagging
        print("hub: last wake %s (%s)%s" % (h["last_wake"], "unreadable time" if hage is None else "%ds ago" % hage,
                                           ", STALE" if hstale else ""), file=out)
    else:
        print("hub: never woke%s" % (", STALE" if active and lagging else ""), file=out)
    return 2 if stale else 0


def hub_lagging(w: dict, h: dict, at: datetime) -> bool:
    """The hub has not caught up: the watcher appended a line it must decide (`last_append_at`) after the hub last
    woke (or it never woke), more than 2x `every_seconds` ago. A quiet day's old `last_wake` is not lagging."""
    import hub_sources
    try:
        appended = hub_sources.parse_iso(w.get("last_append_at"))
        every = int(w.get("every_seconds") or DEFAULTS["every_seconds"])
    except (TypeError, ValueError, AttributeError):
        return False
    if h.get("last_wake"):
        try:
            if hub_sources.parse_iso(h["last_wake"]) >= appended:
                return False
        except (TypeError, ValueError, AttributeError):
            pass
    return (at - appended).total_seconds() > 2 * every


# ------------------------------------------------------------------------------------------- hub verbs
ROUTED_MAX = 50
ALSO_MAX = 50
DECISIONS = ("log", "now", "route", "propose", "error")


def _inbox_files(ws: Path) -> List[Path]:
    d = hub_dir(ws)
    files = sorted(d.glob("inbox-*.jsonl"))
    cur = d / "inbox.jsonl"
    if cur.exists():
        files.append(cur)
    return files


def read_inbox(ws: Path) -> List[dict]:
    """Every readable line of the rotated files then inbox.jsonl, oldest first. A torn or odd line is skipped."""
    out = []
    for f in _inbox_files(ws):
        try:
            text = f.read_text()
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                l = json.loads(raw)
            except ValueError:
                continue
            if isinstance(l, dict) and isinstance(l.get("id"), str) and isinstance(l.get("key"), str):
                out.append(l)
    return out


def after_cursor(lines: List[dict], cursor) -> Tuple[List[dict], bool]:
    """(lines after the cursor line, cursor_missing). No cursor = everything; a cursor that is no longer in the
    files (pruned) = everything kept, with cursor_missing True."""
    if not cursor:
        return lines, False
    idx = [i for i, l in enumerate(lines) if l["id"] == cursor]
    if not idx:
        return lines, True
    return lines[idx[-1] + 1:], False


def _hub_doc(ws: Path) -> dict:
    h = load_json(ws, "hub.json", {})
    h = h if isinstance(h, dict) else {}
    h.setdefault("version", 1)
    for k in ("routed", "pending", "updates"):
        if not isinstance(h.get(k), list):
            h[k] = []
    return h


def _sessions_doc(ws: Path) -> dict:
    d = load_json(ws, "sessions.json", {})
    d = d if isinstance(d, dict) else {}
    d.setdefault("version", 1)
    if not isinstance(d.get("sessions"), dict):
        d["sessions"] = {}
    return d


def find_session(sessions: dict, keys, live_only: bool = True):
    """(session key, session) for the first of `keys` that is a session key or in a session's `also`; live
    sessions only unless live_only is False. None when there is no such session."""
    items = sessions.get("sessions") or {}
    for k in keys:
        cands = [(sk, s) for sk, s in items.items() if isinstance(s, dict)
                 and (sk == k or k in (s.get("also") or []))]
        for sk, s in cands:
            if not live_only or s.get("state") == "live":
                return sk, s
    return None


def _apply_resolved(ws: Path, keys) -> None:
    keys = set(keys)
    if not keys:
        return
    with locked(ws):
        h = _hub_doc(ws)
        pend = [p for p in h["pending"] if p.get("key") not in keys]
        upd = [u for u in h["updates"] if u.get("key") not in keys]
        if len(pend) != len(h["pending"]) or len(upd) != len(h["updates"]):
            h["pending"], h["updates"] = pend, upd
            save_json(ws, "hub.json", h)


def digest(ws: Path, batch: List[dict]) -> List[dict]:
    """What tail prints for a batch: sweep and error lines as they are, update lines collapsed to one per key,
    and no output for a key whose latest line is `resolved` (that resolution is applied to hub.json)."""
    pos = {}
    for i, l in enumerate(batch):
        pos[l["id"]] = i
    by_key = {}
    keep = []
    for l in batch:
        if l.get("kind") in ("sweep", "error"):
            keep.append(l)
        else:
            by_key.setdefault(l["key"], []).append(l)
    resolved = []
    for k, ls in by_key.items():
        latest = ls[0]
        for l in ls:
            if l["at"] >= latest["at"]:
                latest = l
        if latest.get("kind") == "resolved":
            resolved.append(k)
            continue
        keep.extend(coalesce([l for l in ls if l.get("kind") == "update"]))
    _apply_resolved(ws, resolved)
    keep.sort(key=lambda l: pos.get(l["id"], 0))
    return keep


def tail(ws: Path, follow: bool = False, out=None, err=None, sleep=time.sleep, stop=None,
         every: float = 1.5) -> int:
    """Print the inbox lines after hub.json `inbox_cursor`, one JSON line per collapsed item, flushed. Never moves
    the cursor (`decide` does). With `follow`, keep polling the files: a rotation needs no special handling because
    the rotated and current files are read together and lines already printed are remembered by id."""
    out = out or sys.stdout
    err = err or sys.stderr
    seen = set()
    warned = False
    sig = None
    while True:
        files = _inbox_files(ws)
        try:
            now_sig = [(f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in files]
        except OSError:
            now_sig = None
        if now_sig is None or now_sig != sig:
            sig = now_sig
            cursor = _hub_doc(ws).get("inbox_cursor")
            lines, missing = after_cursor(read_inbox(ws), cursor)
            if missing and not warned:
                warned = True
                print("mother hub tail: cursor line %s is no longer in the inbox (pruned after %d days); "
                      "starting from the oldest kept line" % (cursor, KEEP_DAYS), file=err)
                err.flush()
            batch = [l for l in lines if l["id"] not in seen]
            seen.update(l["id"] for l in batch)
            for l in digest(ws, batch):
                out.write(json.dumps(l, sort_keys=True) + "\n")
            out.flush()
        if not follow or (stop is not None and stop()):
            return 0
        sleep(every)


def _schedule(cfg: dict):
    import now as nowmod
    return nowmod.Schedule.from_config(cfg or {})


def _prune_updates(h: dict, sessions: dict, cfg: dict, at: datetime) -> None:
    """Drop updates older than hub.update_ttl_hours working hours (counted from the later of `at` and a snooze end)
    and those whose key has a live session; snoozes that have ended become open again."""
    import now as nowmod
    import hub_sources
    try:
        ttl = float(_hub_cfg(cfg).get("update_ttl_hours", 24))
    except (TypeError, ValueError):
        ttl = 24.0
    sched = _schedule(cfg)
    keep = []
    for u in h["updates"]:
        if find_session(sessions, [u.get("key")]):
            continue
        try:
            ref = hub_sources.parse_iso(u.get("at"))
            if u.get("snooze_until"):
                ref = max(ref, hub_sources.parse_iso(u["snooze_until"]))
                if u.get("state") == "snoozed" and hub_sources.parse_iso(u["snooze_until"]) <= at:
                    u["state"] = "open"
            if nowmod.working_hours(ref, at, sched) > ttl:
                continue
        except (TypeError, ValueError, AttributeError):
            pass
        keep.append(u)
    h["updates"] = keep


def _prune_pending(h: dict, cfg: dict, at: datetime) -> None:
    """Drop proposals older than hub.update_ttl_hours working hours, counted from `raised_at` (else
    `proposed_at`)."""
    h["pending"] = [p for p in h["pending"] if not (isinstance(p, dict) and _older_than_ttl(
        p.get("raised_at") or p.get("proposed_at"), cfg, at))]


def _save_hub(ws: Path, h: dict, sessions: dict, cfg: dict, at: datetime) -> None:
    _prune_updates(h, sessions, cfg, at)
    _prune_pending(h, cfg, at)
    save_json(ws, "hub.json", h)


def decide(ws: Path, cfg: dict, at: datetime, items: List[dict]) -> List[dict]:
    """Record the hub's decisions. Returns one result per item: {line_id, key, decision (as stored), session_id?,
    note?}. A `route` to a key with no live session (directly or through `also`) is stored as `propose`."""
    stamp = _iso(at)
    lines = read_inbox(ws)
    by_id = {l["id"]: l for l in lines}
    order = {}
    for i, l in enumerate(lines):
        order[l["id"]] = i
    unknown = [it["line_id"] for it in items if it["line_id"] not in order]
    if unknown:
        raise ValueError("line_id not in the kept inbox: %s" % ", ".join(unknown))
    items = sorted(items, key=lambda it: order[it["line_id"]])
    results = []
    with locked(ws):
        h = _hub_doc(ws)
        sess = _sessions_doc(ws)
        done = {r.get("line_id") for r in h["routed"] if isinstance(r, dict)}
        for it in items:
            if it["line_id"] in done:  # a second decide for the same line (a replayed backlog) changes nothing
                results.append({"line_id": it["line_id"], "key": it["key"], "already_decided": True})
                continue
            done.add(it["line_id"])
            line = by_id.get(it["line_id"]) or {}
            key, dec = it["key"], it["decision"]
            res = {"line_id": it["line_id"], "key": key, "decision": dec}
            also = list(it.get("also") or line.get("also") or [])
            entry = {"line_id": it["line_id"], "key": key, "decision": dec, "at": stamp}
            if dec in ("route", "now"):
                hit = find_session(sess, [key] + also)
                if hit and dec == "now":
                    dec = res["decision"] = entry["decision"] = "route"
                    res["note"] = "live session for %s; recorded as a route, not an update" % key
                if hit:
                    sk, s = hit
                    s["unseen"] = int(s.get("unseen") or 0) + 1
                    s["last_routed_at"] = stamp
                    # the line's own key and the keys it mentions now belong to this session too, so the next
                    # reply in the thread finds it even when it no longer names the key it was routed by
                    keep = [a for a in (s.get("also") or []) if isinstance(a, str)]
                    for k in [key] + also:
                        if k != sk and k not in keep:
                            keep.append(k)
                    s["also"] = keep[-ALSO_MAX:]
                    entry["session_id"] = res["session_id"] = s.get("session_id")
                elif dec == "route":
                    dec = res["decision"] = entry["decision"] = "propose"
                    res["note"] = "no live session for %s; stored as a proposal" % key
            title = it.get("title") or line.get("title") or key
            link = it.get("link") or line.get("link") or ""
            if dec == "propose":
                prev = next((p for p in h["pending"] if p.get("key") == key), {})
                h["pending"] = [p for p in h["pending"] if p.get("key") != key]
                keep = [a for a in (prev.get("also") or []) if isinstance(a, str)]
                for k in also:
                    if k != key and k not in keep:
                        keep.append(k)
                h["pending"].append({"key": key, "title": title, "link": link, "read": it.get("read") or "",
                                     "next_step": it.get("next_step") or "",
                                     "proposed_at": prev.get("proposed_at") or stamp,
                                     "raised_at": prev.get("raised_at"),
                                     "task_id": prev.get("task_id"),
                                     "last_update_at": stamp,
                                     "also": keep[-ALSO_MAX:],
                                     "name": it.get("name") or prev.get("name") or ""})
            elif dec == "now":
                old = [u for u in h["updates"] if u.get("key") == key and u.get("state") != "done"]
                if old:
                    u = old[0]
                    u.update({"title": title, "why": it.get("why") or u.get("why") or "", "link": link,
                              "at": stamp, "state": "open"})
                    u.pop("snooze_until", None)
                    res["update_id"] = u["id"]
                else:
                    n = int(h.get("next_update") or 0)
                    for u in h["updates"]:
                        m = re.match(r"U(\d+)$", str(u.get("id")))
                        n = max(n, int(m.group(1)) if m else 0)
                    n += 1
                    h["next_update"] = n
                    h["updates"].append({"id": "U%d" % n, "key": key, "title": title,
                                         "why": it.get("why") or "", "link": link, "at": stamp, "state": "open"})
                    res["update_id"] = "U%d" % n
            h["routed"].append(entry)
            cur = h.get("inbox_cursor")
            if cur not in order or order[it["line_id"]] > order[cur]:
                h["inbox_cursor"] = it["line_id"]
            results.append(res)
        if all(r.get("already_decided") for r in results):
            return results  # nothing new: no write at all
        h["routed"] = h["routed"][-ROUTED_MAX:]
        h["last_wake"] = stamp
        save_json(ws, "sessions.json", sess)
        _save_hub(ws, h, sess, cfg, at)
    return results


def _parse_flags(args, valued) -> Tuple[List[str], dict]:
    pos, opts = [], {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and a[2:] in valued:
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % a)
            opts[a[2:]] = args[i + 1]
            i += 2
        elif a.startswith("--"):
            opts[a[2:]] = True
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, opts


def session_cmd(ws: Path, cfg: dict, at: datetime, args, as_json: bool, out) -> int:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    try:
        pos, o = _parse_flags(rest, ("id", "name", "title", "link", "also"))
    except ValueError as e:
        print("mother hub session: %s" % e, file=sys.stderr)
        return 2
    if sub not in ("add", "drop", "seen", "for") or len(pos) != 1:
        print("usage: mother hub session add <key> --id ID --name N --title T [--link URL] [--also K,K] | "
              "drop <key> | seen <key> | for <key>", file=sys.stderr)
        return 2
    key = pos[0]
    stamp = _iso(at)
    catch_up = None
    if sub == "for":
        hit = find_session(_sessions_doc(ws), [key])
        if not hit:
            print("no live session for %s" % key, file=sys.stderr)
            return 1
        print(json.dumps({"key": hit[0], "session_id": hit[1].get("session_id")}) if as_json
              else hit[1].get("session_id"), file=out)
        return 0
    with locked(ws):
        sess = _sessions_doc(ws)
        h = _hub_doc(ws)
        items = sess["sessions"]
        if sub == "add":
            if not o.get("id") or not o.get("name") or not o.get("title"):
                print("mother hub session add needs --id, --name and --title", file=sys.stderr)
                return 2
            also = [a for a in str(o.get("also") or "").split(",") if a]
            prev = items.get(key) or {}
            was = next((p for p in h["pending"] if p.get("key") == key), None)
            if was and was.get("raised_at") and (was.get("last_update_at") or "") > was["raised_at"]:
                catch_up = {"key": key, "read": was.get("read") or "", "next_step": was.get("next_step") or "",
                            "link": was.get("link") or "", "at": was["last_update_at"]}
            items[key] = {"session_id": o["id"], "name": o["name"], "title": o["title"],
                          "link": o.get("link") or None, "also": also,
                          "opened_at": prev.get("opened_at") or stamp, "last_routed_at": prev.get("last_routed_at"),
                          "unseen": 0, "state": "live"}
            gone = set([key] + also)
            h["pending"] = [p for p in h["pending"] if p.get("key") not in gone]
        else:
            if key not in items:
                print("no session for %s" % key, file=sys.stderr)
                return 1
            if sub == "drop":
                items[key]["state"] = "dropped"
            else:
                items[key]["unseen"] = 0
        save_json(ws, "sessions.json", sess)
        _save_hub(ws, h, sess, cfg, at)
    res = {"ok": True, "key": key, "action": sub}
    if catch_up:
        res["catch_up"] = catch_up
    print(json.dumps(res) if as_json else "%s: %s%s" % (
        sub, key, "; catch up: %s" % json.dumps(catch_up) if catch_up else ""), file=out)
    return 0


def pending_cmd(ws: Path, cfg: dict, at: datetime, args, as_json: bool, out) -> int:
    try:
        pos, o = _parse_flags(args, ("name", "task"))
    except ValueError as e:
        print("mother hub pending: %s" % e, file=sys.stderr)
        return 2
    raised = len(pos) == 2 and pos[0] == "raised" and isinstance(o.get("name"), str) and o["name"]
    if not raised and not (len(pos) == 2 and pos[0] == "drop"):
        print("usage: mother hub pending raised <key> --name <slug> [--task <task_id>] | drop <key>",
              file=sys.stderr)
        return 2
    key, stamp = pos[1], _iso(at)
    with locked(ws):
        h, sess = _hub_doc(ws), _sessions_doc(ws)
        hit = next((p for p in h["pending"] if p.get("key") == key), None)
        if hit is None:
            print("no pending proposal for %s" % key, file=sys.stderr)
            return 1
        if not raised:
            h["pending"] = [p for p in h["pending"] if p.get("key") != key]
            _save_hub(ws, h, sess, cfg, at)
            print(json.dumps({"ok": True, "key": key, "action": "drop", "task_id": hit.get("task_id")}) if as_json
                  else "dropped: %s%s" % (key, " (card %s)" % hit["task_id"] if hit.get("task_id") else ""), file=out)
            return 0
        hit["raised_at"], hit["name"] = stamp, o["name"]
        if isinstance(o.get("task"), str) and o["task"]:
            hit["task_id"] = o["task"]
        _save_hub(ws, h, sess, cfg, at)
    print(json.dumps({"ok": True, "key": key, "name": o["name"], "raised_at": stamp}) if as_json
          else "raised: %s as %s" % (key, o["name"]), file=out)
    return 0


def update_cmd(ws: Path, cfg: dict, at: datetime, verb: str, args, as_json: bool, out) -> int:
    need = 2 if verb == "snooze" else 1
    if len(args) != need:
        print("usage: mother hub %s" % ("snooze <U-id> 1h|tomorrow|next-week" if verb == "snooze" else "done <U-id>"),
              file=sys.stderr)
        return 2
    uid = args[0]
    until = None
    if verb == "snooze":
        import now as nowmod
        try:
            until = _iso(nowmod.snooze_until(args[1], at, _schedule(cfg)))
        except ValueError as e:
            print("mother hub snooze: %s" % e, file=sys.stderr)
            return 2
    with locked(ws):
        h = _hub_doc(ws)
        sess = _sessions_doc(ws)
        u = next((u for u in h["updates"] if u.get("id") == uid), None)
        if not u:
            print("no update %s" % uid, file=sys.stderr)
            return 1
        if verb == "done":
            u["state"] = "done"
            u.pop("snooze_until", None)
        else:
            u["state"] = "snoozed"
            u["snooze_until"] = until
        _save_hub(ws, h, sess, cfg, at)
    print(json.dumps({"ok": True, "id": uid, "state": u["state"], "snooze_until": u.get("snooze_until")})
          if as_json else "%s: %s%s" % (uid, u["state"], " until %s" % until if until else ""), file=out)
    return 0


def show(ws: Path, as_json: bool, out) -> int:
    h, s = _hub_doc(ws), _sessions_doc(ws)
    if as_json:
        print(json.dumps({"hub": h, "sessions": s["sessions"]}, indent=2, sort_keys=True), file=out)
        return 0
    print("last wake: %s" % (h.get("last_wake") or "never"), file=out)
    print("pending proposals: %d" % len(h["pending"]), file=out)
    for p in h["pending"]:
        print("  %s | %s | %s" % (p.get("key"), p.get("title"), p.get("next_step") or ""), file=out)
    print("updates: %d" % len(h["updates"]), file=out)
    for u in h["updates"]:
        print("  %s | %s | %s | %s%s" % (u.get("id"), u.get("state"), u.get("key"), u.get("title"),
                                       " (until %s)" % u["snooze_until"] if u.get("snooze_until") else ""), file=out)
    live = [(k, v) for k, v in s["sessions"].items() if isinstance(v, dict)]
    print("sessions: %d" % len(live), file=out)
    for k, v in sorted(live):
        print("  %s | %s | %s | unseen %s | %s" % (k, v.get("name"), v.get("state"), v.get("unseen"),
                                                  v.get("link") or "no link"), file=out)
    return 0


def _read_decisions(stdin) -> List[dict]:
    try:
        doc = json.loads(stdin.read())
    except ValueError as e:
        raise ValueError("stdin is not JSON: %s" % e)
    items = doc if isinstance(doc, list) else [doc]
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("line_id"), str) or not isinstance(it.get("key"), str):
            raise ValueError("each decision needs a string line_id and key")
        if it.get("decision") not in DECISIONS:
            raise ValueError("decision must be one of %s" % ", ".join(DECISIONS))
    return items


def main(args, ws, cfg, at) -> int:
    verb, rest = (args[0], args[1:]) if args else ("", [])
    if verb in ("", "help", "-h", "--help"):
        print(USAGE)
        return 0
    ws = Path(ws)
    if verb == "watch":
        watch(ws, cfg)
        return 0
    if verb == "install":
        return install(ws, cfg)
    if verb == "uninstall":
        return uninstall()
    if verb == "check":
        return check(cfg)
    if verb == "status":
        return status(ws, at)
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    if verb == "tail":
        try:
            return tail(ws, follow="--follow" in rest)
        except (KeyboardInterrupt, BrokenPipeError):
            try:
                sys.stdout = open(os.devnull, "w")
            except OSError:
                pass
            return 0
    if verb == "decide":
        try:
            items = _read_decisions(sys.stdin)
        except ValueError as e:
            print("mother hub decide: %s" % e, file=sys.stderr)
            return 2
        try:
            res = decide(ws, cfg, at, items)
        except ValueError as e:
            print("mother hub decide: %s" % e, file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            for r in res:
                print("%s -> %s%s" % (r["key"], r["decision"], " (%s)" % r["note"] if r.get("note") else ""))
        return 0
    if verb == "session":
        return session_cmd(ws, cfg, at, rest, as_json, sys.stdout)
    if verb == "pending":
        return pending_cmd(ws, cfg, at, rest, as_json, sys.stdout)
    if verb in ("done", "snooze"):
        return update_cmd(ws, cfg, at, verb, rest, as_json, sys.stdout)
    if verb == "show":
        return show(ws, as_json, sys.stdout)
    print("mother hub: unknown verb %r; see `mother hub help`" % verb, file=sys.stderr)
    return 1
