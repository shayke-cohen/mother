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
                _failed(ws, cap, stamp, ent)
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
            _failed(ws, cap, stamp, ent)
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
        _set_errored(ws, cap, False)
    if poll:
        _maybe_sweep(ws, h, at, stamp)
    beat = {"at": stamp, "every_seconds": every, "active": active, "sources": sources, "errors": errors}
    with locked(ws):
        save_json(ws, "watcher.json", beat)
    return beat


def _failed(ws, cap, stamp, ent) -> None:
    """The first failing pass of a source since it last worked: one `error` inbox line. `cursors.json` `_errored`
    remembers that it was emitted (not the status, so a source that was `couldnt_check` for a missing connection
    still gets its line when it later fails). Nothing is written to capabilities.json."""
    cursors = load_json(ws, "cursors.json", {})
    cursors = cursors if isinstance(cursors, dict) else {}
    if cap in (cursors.get("_errored") or []):
        return
    append_inbox(ws, [make_line(cap, "error", "watcher:%s" % cap, stamp, title="%s source unreachable" % cap,
                                summary=ent["reason"])])
    _set_errored(ws, cap, True)


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
(tail, decide, session, done, snooze, show: later)"""


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
    2x `every_seconds` while `active`). The hub wakes on events, so an old `last_wake` is printed as STALE but
    does not fail; a missing hub.json only says the hub never woke."""
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
    if h.get("last_wake"):
        hage = _age(h["last_wake"], at)
        hstale = active and (hage is None or hage > 2 * every)
        print("hub: last wake %s (%s)%s" % (h["last_wake"], "unreadable time" if hage is None else "%ds ago" % hage,
                                           ", STALE" if hstale else ""), file=out)
    else:
        print("hub: never woke", file=out)
    return 2 if stale else 0


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
    print("mother hub: unknown or not yet built verb %r; see `mother hub help`" % verb, file=sys.stderr)
    return 1
