"""mother health — per-server connector health in capabilities.json (docs/CONTRACT.md §4).

The single owner of writes to <workspace>/capabilities.json. Routines and the doctor never edit the
file by hand; they call these commands, which take a file lock and write atomically.

Commands (JSON out, one object per call):
  record --kind K --cap C --server S --status V [--class X] [--gateway H] [--fixed-by F]
                               store one server's result for one capability -> {previous, rollup}
  classify --error TEXT        map an error message to a status and failure class -> {status, class}
  summary [--days N]           failures and fixes from health.md, most frequent first (text lines)
  pick --kind K --cap C [--target dm|other] [--max-age-hours N]
                               the server to use now -> {server, status, fresh, fallback, stamped}
  merge --file F               merge the doctor's fresh capabilities JSON in -> {changed: [kind.cap, ...]}
  alert --kind K --cap C --server S [--urgent] [--sent]
                               once-a-day alert decision -> {action: send|pending|skip}; --sent records it
  pending                      held alerts now outside quiet hours -> [{kind, cap, server, status, class}]

Alerts: at most one per (server, capability) per manager-local day; held (`pending`) during
manager.quiet_hours unless --urgent.
`record` also appends one line per failure or fix to <workspace>/health.md (no ids, no content).
Exit codes: 0 ok; 2 bad file or bad arguments; 3 `pick` found no usable server.
$MOTHER_NOW (epoch seconds) pins the clock. Python 3.9 stdlib only.
"""
import contextlib
import copy
import datetime as _dt
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

import channels  # one-way: channels never imports health

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
    tmp.write_text(json.dumps(caps, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
        health["_gateway"] = gateway
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


def merge(old, new):
    """The doctor's fresh capabilities `new` over the stored `old` (both normalized).

    `new` wins for statuses, stamps, tokens and the top-level checked_at/host. Server order: old order for
    servers in both, new ones appended, absent ones dropped. `alerted_at`/`pending` survive while the
    entry is not ok and its class is unchanged.
    """
    out = copy.deepcopy(new)
    for vendor, conn in (out.get("connectors") or {}).items():
        oc = (old.get("connectors") or {}).get(vendor)
        if not isinstance(conn, dict) or not isinstance(oc, dict):
            continue
        for cap, fresh in list((conn.get("servers") or {}).items()):
            prior = (oc.get("servers") or {}).get(cap) or []
            conn["servers"][cap] = [s for s in prior if s in fresh] + [s for s in fresh if s not in prior]
        for server, caps_ in (conn.get("health") or {}).items():
            for cap, e in caps_.items():
                o = (((oc.get("health") or {}).get(server) or {}).get(cap)) if not cap.startswith("_") else None
                if isinstance(e, dict) and o and e.get("status") != "ok" and o.get("class") == e.get("class"):
                    for k in ("alerted_at", "pending"):
                        if k in o:
                            e[k] = o[k]
    return out


def _local_date(t, cfg):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo((cfg.get("manager") or {}).get("timezone") or "UTC")
    except Exception:
        tz = _dt.timezone.utc
    return _dt.datetime.fromtimestamp(t, tz).date()


def _entry(caps, vendor, cap, server):
    e = ((((caps.get("connectors") or {}).get(vendor) or {}).get("health") or {}).get(server) or {}).get(cap)
    return e if isinstance(e, dict) else None


def alert_action(caps, cfg, vendor, cap, server, urgent=False, now=None):
    """`send`, `pending` or `skip` for one failing entry (`now`: epoch seconds); `pending` is recorded."""
    t = _clock() if now is None else now
    e = _entry(caps, vendor, cap, server)
    if not e or e.get("status") == "ok":
        return "skip"
    last = parse_iso(e.get("alerted_at"))
    if last is not None and _local_date(last, cfg) == _local_date(t, cfg):
        return "skip"
    if not urgent and channels.quiet_now(cfg, now=t):
        e["pending"] = True
        return "pending"
    return "send"


def mark_alerted(caps, vendor, cap, server, now=None):
    e = _entry(caps, vendor, cap, server)
    if e is not None:
        e["alerted_at"] = iso(now)
        e.pop("pending", None)


def due_pending(caps, cfg, now=None):
    """Held alerts, once quiet hours are over: [{kind, cap, server, status, class}]."""
    t = _clock() if now is None else now
    if channels.quiet_now(cfg, now=t):
        return []
    kinds = {v: k for k, v in (cfg.get("connectors") or {}).items() if isinstance(v, str)}
    out = []
    for vendor, conn in (caps.get("connectors") or {}).items():
        for server, caps_ in ((conn or {}).get("health") or {}).items():
            for cap, e in caps_.items():
                if isinstance(e, dict) and e.get("pending") and e.get("status") != "ok":
                    out.append({"kind": kinds.get(vendor, vendor), "cap": cap, "server": server,
                                "status": e.get("status"), "class": e.get("class")})
    return out


# ---------------------------------------------------------------- classify and log

_SCOPE = re.compile(r"[a-z]+:[a-z.:_]+")
_RULES = (
    ("invalidated", "expired", ("invalidated", "reconnect it from connector settings", "needs_auth",
                                "needs authentication", "sign in", "token expired", "re-authenticate",
                                "invalid_auth", r"\b401\b")),
    ("denied", "denied", (r"\b403\b", "permission", "admin must enable", "not allowed")),
    ("unreachable", "error:unreachable", ("econnrefused", "enotfound", "getaddrinfo", "timed out", "timeout",
                                          "unreachable", "fetch failed", "connection refused", "network")),
    ("dropped", "error:dropped", ("connection closed", "not connected", "disconnected", "transport closed",
                                  "server failed")),
)


def scrub(text):
    text = re.sub(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", "#", text, flags=re.I)
    return re.sub(r"\d+", "#", text)


def classify(error):
    """Map an error message to (status, class); see the failure classes in CLASSES."""
    low = error.lower()
    if re.search(r"missing_scope|insufficient scope|not allowed", low):
        m = _SCOPE.search(low.replace("missing_scope", ""))
        if m:
            return "missing_scope:" + m.group(0).rstrip(".:_"), "missing_scope"
    for cls, status, pats in _RULES:
        if any(re.search(p, low) for p in pats):
            return status, cls
    return "error:" + scrub(error.strip())[:40], "other"


def server_short(prefix):
    """Server name for the log: drop `mcp__`, shorten opaque id segments to their first 8 characters."""
    name = prefix[5:] if prefix.startswith("mcp__") else prefix
    parts = [p[:8] if re.fullmatch(r"[0-9a-fA-F-]{16,}", p) else p for p in name.split("__")]
    return "__".join(parts)


LOG_HEADER = "One line per connector failure or fix, appended by `mother health record`."


def health_line(now, kind, cap, server, cls, fixed_by, gateway=None):
    line = "- %s %s.%s %s %s \u2192 %s" % (now, kind, cap, server_short(server), cls, fixed_by or "open")
    return line + (" @" + gateway if gateway else "")


def append_log(ws, line):
    p = Path(ws) / "health.md"
    new = not p.is_file()
    with open(str(p), "a", encoding="utf-8") as f:
        if new:
            f.write(LOG_HEADER + "\n")
        f.write(line + "\n")


_LINE = re.compile(r"^- (\S+) \S+ (\S+) (\S+) \u2192 (\S+)(?: @(\S+))?$")


def summary(ws, days=7, now=None):
    """Lines for the last `days`: '<server> <class> \u00d7N (fixed: retry 1; open 1)', gateway failures folded."""
    p = Path(ws) / "health.md"
    if not p.is_file():
        return []
    cutoff = (_clock() if now is None else now) - days * 86400
    rows = []  # (server, class, outcome, gateway)
    for raw in p.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(raw)
        t = parse_iso(m.group(1)) if m else None
        if t is not None and t >= cutoff:
            rows.append((m.group(2), m.group(3), m.group(4), m.group(5)))
    by_gw = {}
    for srv, cls, _, gw in rows:
        if gw and cls == "unreachable":
            by_gw.setdefault(gw, set()).add(srv)
    folded = {gw for gw, srvs in by_gw.items() if len(srvs) > 1}
    groups = {}
    for srv, cls, out, gw in rows:
        key = ("gateway " + gw, cls) if gw in folded and cls == "unreachable" else (srv, cls)
        groups.setdefault(key, []).append(out)
    lines = []
    for (name, cls), outs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        fixed = {}
        for o in outs:
            if o != "open":
                fixed[o] = fixed.get(o, 0) + 1
        parts = []
        if fixed:
            parts.append("fixed: " + ", ".join("%s %d" % kv for kv in sorted(fixed.items())))
        if outs.count("open"):
            parts.append("open %d" % outs.count("open"))
        lines.append("%s %s \u00d7%d (%s)" % (name, cls, len(outs), "; ".join(parts)))
    return lines


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
        old = (((caps["connectors"].get(vendor) or {}).get("health") or {}).get(o["server"]) or {}).get(o["cap"]) or {}
        stamp = iso()
        previous = record(caps, vendor, o["cap"], o["server"], o["status"], o.get("class"), o.get("gateway"), stamp)
        save(ws, caps)
        gw = o.get("gateway") or (caps["connectors"][vendor]["health"][o["server"]].get("_gateway"))
        if o["status"] != "ok":
            cls = o.get("class") or "other"
            if not (old.get("status") == o["status"] and old.get("class") == o.get("class")):
                append_log(ws, health_line(stamp, o["kind"], o["cap"], o["server"], cls, None, gw))
        elif previous not in (None, "ok") and o.get("fixed_by"):
            append_log(ws, health_line(stamp, o["kind"], o["cap"], o["server"], old.get("class") or "other",
                                       o["fixed_by"], gw))
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


def cmd_classify(ws, args):
    o = parse_args(args, ("--error",))
    need(o, "error")
    status, cls = classify(o["error"])
    return 0, {"status": status, "class": cls}


def cmd_summary(ws, args):
    o = parse_args(args, ("--days",))
    try:
        days = float(o["days"]) if o.get("days") else 7
    except ValueError:
        raise HealthError("bad --days '%s'" % o["days"])
    for line in summary(ws, days):
        print(line)
    return 0, None


def rollups(caps):
    return {(v, c): rollup(conn, c) for v, conn in caps["connectors"].items() if isinstance(conn, dict)
            for c in cap_names(conn)}


def cmd_merge(ws, args):
    o = parse_args(args, ("--file",))
    need(o, "file")
    try:
        fresh = json.loads(Path(o["file"]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HealthError("cannot read --file (%s)" % e)
    if not isinstance(fresh, dict) or not isinstance(fresh.get("connectors", {}), dict):
        raise HealthError("--file has an unexpected shape")
    cfg = load_config(ws)
    with locked(ws):
        old = normalize(load(ws), cfg)
        new = normalize(fresh, cfg)
        merged = merge(old, new)
        before, after = rollups(old), rollups(merged)
        save(ws, merged)
    kinds = {v: k for k, v in (cfg.get("connectors") or {}).items() if isinstance(v, str)}
    changed = ["%s.%s" % (kinds.get(v, v), c) for (v, c), r in after.items()
               if r != "ok" and before.get((v, c), "untested") == "ok"]
    return 0, {"changed": changed}


def cmd_alert(ws, args):
    o = parse_args([a for a in args if a not in ("--urgent", "--sent")], ("--kind", "--cap", "--server"))
    need(o, "kind", "cap", "server")
    urgent, sent = "--urgent" in args, "--sent" in args
    cfg = load_config(ws)
    vendor = vendor_of(cfg, o["kind"])
    with locked(ws):
        caps = normalize(load(ws), cfg)
        if sent:
            mark_alerted(caps, vendor, o["cap"], o["server"])
            action = "sent"
        else:
            action = alert_action(caps, cfg, vendor, o["cap"], o["server"], urgent)
        save(ws, caps)
    return 0, {"action": action}


def cmd_pending(ws, args):
    parse_args(args, ())
    cfg = load_config(ws)
    return 0, due_pending(normalize(load(ws), cfg), cfg)


COMMANDS = {"record": cmd_record, "pick": cmd_pick, "classify": cmd_classify, "summary": cmd_summary,
            "merge": cmd_merge, "alert": cmd_alert, "pending": cmd_pending}


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
    if out is not None:
        print(json.dumps(out, ensure_ascii=False))
    return code
