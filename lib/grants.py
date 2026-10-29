"""mother grant — the one writer of <workspace>/.mother/grants.json for unattended runs (CONTRACT §5).

  add <scope> <target> --by <user-id> --cites <link|ts> [--channel ID] [--uses N] [--hours H] [--run ID] [--thread TS]
  remove --run <id>       drop every grant that run added
  list [--json]

The guard asks before any other write to grants.json (Write, Edit or a shell command) and denies it under PAUSED,
so a session that obeys a message cannot forge a grant by editing the file. `add` checks the grant exactly as the
guard will (`valid_grant`): the scope, the target (a team channel or a team's tracker project), the author (the
manager's own chat id), the cited message's age, the uses and the expiry. It writes nothing it would not honour.
`answer` grants are written only by `mother channels`. Python 3.9 stdlib only.
"""
import contextlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("mother_guard", str(PLUGIN_ROOT / "hooks" / "guard.py"))
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

USAGE = __doc__.split("\n\n")[1]
SCOPES = ("chat-post", "chat-reply", "tracker-create", "tracker-comment", "tracker-transition")
DEFAULT_HOURS = 24
LINK_RE = re.compile(r"/([A-Z0-9]{6,})/p(\d{10})(\d{6})(?:\D|$)")
TS_RE = re.compile(r"^\d{9,10}(?:\.\d{1,6})?$")


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@contextlib.contextmanager
def locked(ws: Path):
    d = ws / ".mother"
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / "grants.lock", "a")
    try:
        if guard.fcntl:
            guard.fcntl.flock(f, guard.fcntl.LOCK_EX)
        yield
    finally:
        f.close()


def read(ws: Path):
    """(doc, problem). A missing file is an empty doc; an unreadable one is a problem (never overwritten)."""
    p = ws / ".mother" / "grants.json"
    if not p.exists():
        return {"grants": []}, None
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        return None, "grants.json is unreadable (%s); fix or remove it by hand" % type(e).__name__
    if not isinstance(doc, dict) or not isinstance(doc.get("grants"), list):
        return None, "grants.json has no grants list; fix or remove it by hand"
    return doc, None


def write(ws: Path, doc) -> None:
    p = ws / ".mother" / "grants.json"
    tmp = p.with_name(".grants.%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, p)


def parse_cites(cites: str, channel, cfg):
    """(channel, ts) from a message link (…/<CHANNEL>/p<16 digits>) or a bare ts with --channel (default: the
    manager's own DM)."""
    m = LINK_RE.search(cites)
    if m:
        return m.group(1), "%s.%s" % (m.group(2), m.group(3))
    if TS_RE.match(cites):
        dm = guard.as_list((cfg.get("manager") or {}).get("chat_dm"))
        return channel or (dm[0] if dm else None), cites
    return None, None


def build(cfg: dict, scope: str, target: str, by: str, cites: str, channel=None, uses=None, hours=None,
          run=None, thread=None, now=None):
    """(grant, None) or (None, reason)."""
    now = time.time() if now is None else now
    if scope == "answer":
        return None, "answer grants are written only by `mother channels`"
    if scope not in SCOPES:
        return None, "scope must be one of %s" % ", ".join(SCOPES)
    scopes, max_age = guard.grant_limits(cfg)
    if scope not in scopes:
        return None, "policy.grants turns %s off" % scope
    if target not in guard.allowed_targets(scope, cfg):
        return None, "%s is not a target a %s grant may name (a team channel, or a team's tracker project)" % (
            target, scope)
    if str(by) != str(guard.owner_id(cfg)):
        return None, "--by must be the manager's own chat id: only the manager's message authorises a write"
    src_channel, ts = parse_cites(cites, channel, cfg)
    if not src_channel or not ts:
        return None, "--cites must be a message link or a message ts (with --channel)"
    t = guard.parse_time(ts)
    if t is None or not (now - max_age * 3600 <= t <= now + 60):
        return None, "the cited message must be from the last %s hours" % max_age
    cap = scopes[scope]
    try:
        n = cap if uses is None else int(uses)
        h = DEFAULT_HOURS if hours is None else float(hours)
    except ValueError:
        return None, "--uses must be a whole number and --hours a number"
    if n < 1 or h <= 0:
        return None, "--uses and --hours must be positive"
    g = {"scope": scope, "target": target, "uses": min(n, cap),
         "expires": iso(min(now + h * 3600, t + max_age * 3600)), "run": run or "manual",
         "source": {"channel": src_channel, "user": str(by), "ts": ts}}
    if thread:
        g["thread"] = str(thread)
    if not guard.valid_grant(g, scope, target, cfg, scopes, max_age, now, thread=g.get("thread")):
        return None, "the guard would not honour this grant (cited channel %s is not the manager's DM or a team " \
                     "channel, or it has expired)" % src_channel
    return g, None


def _opts(args, valued):
    pos, o, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and a[2:] in valued:
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % a)
            o[a[2:]] = args[i + 1]
            i += 2
        elif a.startswith("--"):
            o[a[2:]] = True
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, o


def main(args, ws, cfg, out=sys.stdout, err=sys.stderr) -> int:
    verb, rest = (args[0], args[1:]) if args else ("", [])
    if verb in ("", "help", "-h", "--help"):
        print(USAGE, file=out)
        return 0
    ws = Path(ws)
    try:
        pos, o = _opts(rest, ("by", "cites", "channel", "uses", "hours", "run", "thread"))
    except ValueError as e:
        print("mother grant: %s" % e, file=err)
        return 2
    if verb == "list":
        doc, problem = read(ws)
        if problem:
            print("mother grant: %s" % problem, file=err)
            return 1
        print(json.dumps(doc["grants"], indent=1) if o.get("json") else
              "\n".join("%s %s uses=%s expires=%s run=%s" % (g.get("scope"), g.get("target"), g.get("uses"),
                                                             g.get("expires"), g.get("run"))
                        for g in doc["grants"] if isinstance(g, dict)) or "no grants", file=out)
        return 0
    if verb not in ("add", "remove"):
        print("usage:\n" + USAGE, file=err)
        return 2
    if (ws / ".mother" / "PAUSED").exists():
        print("mother grant: outbound is PAUSED (.mother/PAUSED exists); no grant is written", file=err)
        return 1
    if verb == "remove":
        if pos or not isinstance(o.get("run"), str):
            print("usage: mother grant remove --run <id>", file=err)
            return 2
        with locked(ws):
            doc, problem = read(ws)
            if problem:
                print("mother grant: %s" % problem, file=err)
                return 1
            keep = [g for g in doc["grants"] if not (isinstance(g, dict) and g.get("run") == o["run"])]
            gone = len(doc["grants"]) - len(keep)
            if gone:
                doc["grants"] = keep
                write(ws, doc)
        print(json.dumps({"ok": True, "removed": gone}), file=out)
        return 0
    if len(pos) != 2 or not isinstance(o.get("by"), str) or not isinstance(o.get("cites"), str):
        print("usage: mother grant add <scope> <target> --by <user-id> --cites <link|ts> [--channel ID] "
              "[--uses N] [--hours H] [--run ID] [--thread TS]", file=err)
        return 2
    g, why = build(cfg, pos[0], pos[1], o["by"], o["cites"], channel=o.get("channel"), uses=o.get("uses"),
                   hours=o.get("hours"), run=o.get("run"), thread=o.get("thread"))
    if not g:
        print("mother grant: %s" % why, file=err)
        return 1
    with locked(ws):
        doc, problem = read(ws)
        if problem:
            print("mother grant: %s" % problem, file=err)
            return 1
        doc["grants"].append(g)
        write(ws, doc)
    print(json.dumps({"ok": True, "grant": g}), file=out)
    return 0
