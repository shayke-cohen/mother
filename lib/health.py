"""mother health — per-server connector health in capabilities.json (docs/CONTRACT.md §4).

The single owner of writes to <workspace>/capabilities.json. Routines and the doctor never edit the
file by hand; they call these commands, which take a file lock and write atomically.

Commands (JSON out, one object per call):
  record --kind K --cap C --server S --status V [--class X] [--gateway H] [--fixed-by F]
                               store one server's result for one capability -> {previous, rollup}
  pick --kind K --cap C [--target dm|other] [--max-age-hours N]
                               the server to use now -> {server, status, fresh, fallback, stamped}

Exit codes: 0 ok; 2 bad file or bad arguments; 3 `pick` found no usable server.
$MOTHER_NOW (epoch seconds) pins the clock. Python 3.9 stdlib only.
"""
import contextlib
import copy
import datetime as _dt
import fcntl
import json
import os
import sys
import time
from pathlib import Path

CLASSES = ("unreachable", "dropped", "invalidated", "missing_scope", "denied", "other")
FIXED_BY = ("retry", "reconnect", "fallback", "you", "—")
TARGETS = ("dm", "other")
# Keys of a connector that are not capabilities.
RESERVED = ("server", "servers", "health", "stamps", "token")
STATUS_WORDS = ("ok", "untested", "denied", "expired")
STATUS_PREFIXES = ("missing_scope:", "error:")


class HealthError(Exception):
    """A bad capabilities.json, config.json or argument; the CLI exits 2."""


# ---------------------------------------------------------------- time

def now():
    v = os.environ.get("MOTHER_NOW")
    return float(v) if v else time.time()


_clock = now


def iso(t=None):
    return _dt.datetime.fromtimestamp(now() if t is None else t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- file

def caps_path(ws):
    return Path(ws) / "capabilities.json"


def load(ws):
    p = caps_path(ws)
    if not p.is_file():
        return {"connectors": {}}
    try:
        caps = json.loads(p.read_text())
    except ValueError as e:
        raise HealthError("capabilities.json is not valid JSON (%s); fix or delete it" % e)
    if not isinstance(caps, dict) or not isinstance(caps.get("connectors", {}), dict):
        raise HealthError("capabilities.json has an unexpected shape")
    caps.setdefault("connectors", {})
    return caps


def load_config(ws):
    try:
        cfg = json.loads((Path(ws) / "config.json").read_text())
    except (OSError, ValueError) as e:
        raise HealthError("cannot read config.json (%s)" % e)
    if not isinstance(cfg, dict):
        raise HealthError("config.json has an unexpected shape")
    return cfg


@contextlib.contextmanager
def locked(ws):
    d = Path(ws) / ".mother"
    d.mkdir(exist_ok=True)
    with open(d / "health.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def save(ws, caps):
    p = caps_path(ws)
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(caps, indent=2, ensure_ascii=False) + "\n")
    os.replace(str(tmp), str(p))


# ---------------------------------------------------------------- model

def vendor_of(cfg, kind):
    v = (cfg.get("connectors") or {}).get(kind)
    if not isinstance(v, str) or not v:
        raise HealthError("config.json has no connector for kind '%s'" % kind)
    return v


def cap_names(conn):
    names = [k for k, v in conn.items() if k not in RESERVED and isinstance(v, str)]
    for k in (conn.get("servers") or {}):
        if k not in names:
            names.append(k)
    return names


def normalize(caps, cfg):
    """Return a copy in the new shape (ordered `servers` lists, per-server `health`)."""
    caps = copy.deepcopy(caps)
    conns = caps.setdefault("connectors", {})
    top = caps.pop("servers", None)
    if isinstance(top, dict):  # legacy "<kind>.<cap>": prefix
        for key, prefix in top.items():
            kind, _, cap = key.partition(".")
            vendor = (cfg.get("connectors") or {}).get(kind)
            conn = conns.get(vendor) if vendor else None
            if isinstance(conn, dict) and cap and isinstance(prefix, str):
                conn.setdefault("servers", {}).setdefault(cap, prefix)
    stamp = caps.get("checked_at")
    for conn in conns.values():
        if not isinstance(conn, dict):
            continue
        if isinstance(conn.get("token"), str):  # legacy: one identity, the main server's
            conn["token"] = {conn["server"]: conn["token"]} if conn.get("server") else {}
        servers = conn.setdefault("servers", {})
        health = conn.setdefault("health", {})
        for cap in cap_names(conn):
            cur = servers.get(cap)
            seed = None  # status for the preferred server when it has no health entry
            if isinstance(cur, str):
                servers[cap] = [cur]
                if cur == conn.get("server"):
                    seed = conn.get(cap)
                else:  # the old doctor only recorded an alternate server when it worked
                    seed = "ok"
            elif not cur:
                servers[cap] = [conn["server"]] if conn.get("server") else []
                seed = conn.get(cap)
            else:
                seed = conn.get(cap)
            if servers[cap] and isinstance(seed, str):
                entry = health.setdefault(servers[cap][0], {})
                if cap not in entry:
                    entry[cap] = {"status": seed, "class": None, "checked_at": stamp}
    return caps


def status_of(conn, server, cap):
    e = ((conn.get("health") or {}).get(server) or {}).get(cap)
    return (e or {}).get("status") or "untested"


def rollup(conn, cap):
    servers = (conn.get("servers") or {}).get(cap) or []
    if not servers:
        return "untested"
    for s in servers:
        if status_of(conn, s, cap) == "ok":
            return "ok"
    return status_of(conn, servers[0], cap)


def record(caps, vendor, cap, server, status, cls=None, gateway=None, now=None):
    """Set health[server][cap] (`now`: ISO stamp, default the clock); return the previous status (None if new)."""
    conn = caps.setdefault("connectors", {}).setdefault(vendor, {})
    health = conn.setdefault("health", {}).setdefault(server, {})
    old = health.get(cap)
    entry = {"status": status, "class": cls, "checked_at": now or iso()}
    if gateway:
        entry["gateway"] = gateway
    if old and status != "ok" and old.get("class") == cls:
        for k in ("alerted_at", "pending"):
            if k in old:
                entry[k] = old[k]
    health[cap] = entry
    lst = conn.setdefault("servers", {}).setdefault(cap, [])
    if server not in lst:
        lst.append(server)
    conn[cap] = rollup(conn, cap)
    return old.get("status") if old else None


def pick(caps, vendor, cap, target=None, max_age_h=None, now=None):
    """Choose a server (`now`: epoch seconds, default the clock). See the module docstring for the result."""
    conn = (caps.get("connectors") or {}).get(vendor) or {}
    servers = (conn.get("servers") or {}).get(cap) or []
    stamps = conn.get("stamps") or []
    tokens = conn.get("token") or {}
    pref_tok = tokens.get(servers[0]) if servers else None
    result = {"server": None, "status": status_of(conn, servers[0], cap) if servers else "untested",
              "fresh": False, "fallback": False, "stamped": False}
    for s in servers:
        if status_of(conn, s, cap) != "ok":
            continue
        if target == "other":
            if s in stamps:
                continue
            tok = tokens.get(s)
            if tok and pref_tok and tok != pref_tok:
                continue
        entry = conn["health"][s][cap]
        fresh = True
        if max_age_h is not None:
            t = parse_iso(entry.get("checked_at"))
            fresh = t is not None and (now if now is not None else _clock()) - t <= max_age_h * 3600
        return {"server": s, "status": "ok", "fresh": fresh, "fallback": s != servers[0], "stamped": s in stamps}
    return result


# ---------------------------------------------------------------- CLI

def valid_status(s):
    return s in STATUS_WORDS or any(s.startswith(p) and len(s) > len(p) for p in STATUS_PREFIXES)


def parse_args(args, flags):
    out, i = {}, 0
    while i < len(args):
        a = args[i]
        if a not in flags or i + 1 >= len(args):
            raise HealthError("bad argument '%s'" % a)
        out[a[2:].replace("-", "_")] = args[i + 1]
        i += 2
    return out


def need(opts, *names):
    for n in names:
        if not opts.get(n):
            raise HealthError("missing --%s" % n.replace("_", "-"))


def cmd_record(ws, args):
    o = parse_args(args, ("--kind", "--cap", "--server", "--status", "--class", "--gateway", "--fixed-by"))
    need(o, "kind", "cap", "server", "status")
    if not valid_status(o["status"]):
        raise HealthError("bad --status '%s'" % o["status"])
    if o.get("class") and o["class"] not in CLASSES:
        raise HealthError("bad --class '%s' (one of %s)" % (o["class"], ", ".join(CLASSES)))
    if o.get("fixed_by") and o["fixed_by"] not in FIXED_BY:
        raise HealthError("bad --fixed-by '%s' (one of %s)" % (o["fixed_by"], ", ".join(FIXED_BY)))
    cfg = load_config(ws)
    vendor = vendor_of(cfg, o["kind"])
    with locked(ws):
        caps = normalize(load(ws), cfg)
        previous = record(caps, vendor, o["cap"], o["server"], o["status"], o.get("class"), o.get("gateway"))
        save(ws, caps)
    return 0, {"previous": previous, "rollup": caps["connectors"][vendor][o["cap"]]}


def cmd_pick(ws, args):
    o = parse_args(args, ("--kind", "--cap", "--target", "--max-age-hours"))
    need(o, "kind", "cap")
    if o.get("target") and o["target"] not in TARGETS:
        raise HealthError("bad --target '%s' (dm or other)" % o["target"])
    try:
        age = float(o["max_age_hours"]) if o.get("max_age_hours") else None
    except ValueError:
        raise HealthError("bad --max-age-hours '%s'" % o["max_age_hours"])
    cfg = load_config(ws)
    vendor = vendor_of(cfg, o["kind"])
    res = pick(normalize(load(ws), cfg), vendor, o["cap"], o.get("target"), age)
    return (0 if res["server"] else 3), res


COMMANDS = {"record": cmd_record, "pick": cmd_pick}


def main(argv, ws):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd = COMMANDS.get(argv[0])
    if not cmd:
        print("mother health: unknown command '%s' — see `mother health help`" % argv[0], file=sys.stderr)
        return 2
    try:
        code, out = cmd(ws, argv[1:])
    except HealthError as e:
        print("mother health: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return code
