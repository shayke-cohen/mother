"""`mother inbox archive|read <mail key or link>...`: the Now panel's triage, with no model in the loop.

It speaks to the hub's own mail connection (`hub.connections.mail`) through the watcher's stdio MCP client, on an
allow-list of the mail adapter's list tool and its two triage tools (archive, mark read). Nothing goes around the
guard: before each write it asks the guard's own classifier about the same call (`guard.classify_mcp`), stops on
anything the guard would ask about or deny, writes nothing while `.mother/PAUSED` exists, and appends one record
per thread to `.mother/guard.log`. Exit 0 all done, 1 a thread failed, 2 usage, config or PAUSED.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import hub
import hub_mcp
import hub_sources

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
DONE = {"archive": "archived", "read": "marked read"}
USAGE = "usage: mother inbox archive|read [--] <mail:ID | mail link>..."


def _guard_says(tool: str, arguments: dict) -> Optional[str]:
    """None when the guard lets this MCP call through unasked, else what it would ask or deny about."""
    if str(HOOKS) not in sys.path:
        sys.path.insert(0, str(HOOKS))
    import guard
    a = guard.classify_mcp("mcp__mail__" + tool, arguments)
    return None if a is None else str(a.get("what") or a.get("category") or "an outbound call")


def _log(ws: Path, verb: str, key: str, decision: str, reason: str) -> None:
    rec = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": "mother inbox " + verb,
           "kind": "mail", "category": "mail_triage", "target": key, "decision": decision, "reason": reason}
    try:
        with open(ws / ".mother" / "guard.log", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def main(args, ws: Path, cfg: dict, out=sys.stdout) -> int:
    verb, rest = (args[0], args[1:]) if args else ("", [])
    if verb in ("", "help", "-h", "--help"):
        print(USAGE, file=out)
        return 0 if verb else 2
    rest = [a for a in rest if a != "--"]
    if verb not in DONE or not rest:
        print(USAGE, file=sys.stderr)
        return 2
    ws = Path(ws)
    if (ws / ".mother" / "PAUSED").exists():
        print("mother inbox: MOTHER is paused (.mother/PAUSED); nothing changed", file=sys.stderr)
        return 2
    adapter = hub_sources.ADAPTERS.get("mail:%s" % hub.connector_of(cfg, "mail"))
    if adapter is None or not getattr(adapter, "TRIAGE", None):
        print("mother inbox: the mail connector (connectors.mail) has no archive or mark-read support", file=sys.stderr)
        return 2
    threads = []
    for x in rest:
        t = adapter.thread_of(x)
        if t is None:
            print("mother inbox: %r is not a mail thread (mail:<id> or a mail link)" % x, file=sys.stderr)
            return 2
        if t not in threads:
            threads.append(t)
    conn = (hub._hub_cfg(cfg).get("connections") or {}).get("mail")
    if not isinstance(conn, dict) or not conn.get("command"):
        print("mother inbox: needs hub.connections.mail (the watcher's mail connection)", file=sys.stderr)
        return 2
    tool, _ = adapter.triage_call(verb, "")
    allowed = {"list-emails", tool}
    raw = hub_mcp.Client(conn["command"], list(conn.get("args") or []), dict(conn.get("env") or {}))
    rc = 0
    try:
        raw.start()
        client = hub_mcp.ReadOnlyClient(raw, hub_mcp.resolve_tools(raw.tools, allowed), allowed)
        full = hub_mcp.resolve_tools(raw.tools, {tool})
        if len(full) != 1:
            print("mother inbox: the mail server offers %s %s tool" % ("no" if not full else "more than one", tool),
                  file=sys.stderr)
            return 1
        name = next(iter(full))
        plan = adapter.triage_messages(client, verb, threads)
        for t in threads:
            key = "mail:" + t
            try:
                for mid in plan[t]:
                    suffix, arguments = adapter.triage_call(verb, mid)
                    said = _guard_says(name, arguments)
                    if said:
                        _log(ws, verb, key, "refused", said)
                        print("mother inbox: the guard would not let this through unasked (%s); ask MOTHER instead"
                              % said, file=sys.stderr)
                        rc = 1
                        break
                    client.call(suffix, arguments)
                else:
                    n = len(plan[t])
                    _log(ws, verb, key, "allow", "%s %d message%s: reversible, seen by no one else"
                         % (DONE[verb], n, "" if n == 1 else "s"))
                    print("%s %s (%d message%s)" % (DONE[verb], key, n, "" if n == 1 else "s"), file=out)
            except hub_mcp.McpError as e:
                _log(ws, verb, key, "failed", e.kind)
                print("mother inbox: couldn't %s %s: %s" % (verb, key, e.message[:200]), file=sys.stderr)
                rc = 1
    except hub_mcp.McpError as e:
        print("mother inbox: the mail connection failed: %s" % e.message[:200], file=sys.stderr)
        return 1
    finally:
        raw.close()
    return rc
