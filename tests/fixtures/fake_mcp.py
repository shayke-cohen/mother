#!/usr/bin/env python3
"""A stdio MCP server for tests. Reads FAKE_MCP_FIXTURE (JSON: {"tools": [...], "results": {tool: value},
"errors": {tool: text}}) and appends every tools/call to FAKE_MCP_LOG. Refuses tools/call before tools/list.
FAKE_MCP_DIE_AFTER=N exits after N tools/call (to test restart)."""
import json
import os
import sys

fx = json.load(open(os.environ["FAKE_MCP_FIXTURE"]))
log = os.environ.get("FAKE_MCP_LOG")
die = int(os.environ.get("FAKE_MCP_DIE_AFTER", "0") or 0)
listed = False
calls = 0


def reply(i, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": i}
    if error:
        m["error"] = {"code": -32000, "message": error}
    else:
        m["result"] = result
    sys.stdout.write(json.dumps(m) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    msg = json.loads(raw)
    m, i = msg.get("method"), msg.get("id")
    if m == "initialize":
        reply(i, {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}})
    elif m == "tools/list":
        listed = True
        reply(i, {"tools": [{"name": t, "inputSchema": {}} for t in fx["tools"]]})
    elif m == "tools/call":
        if not listed:
            reply(i, error="tools/list required first")
            continue
        p = msg["params"]
        if log:
            with open(log, "a") as f:
                f.write(json.dumps(p) + "\n")
        calls += 1
        if die and calls > die:
            sys.exit(0)
        if p["name"] in fx.get("errors", {}):
            reply(i, {"isError": True, "content": [{"type": "text", "text": fx["errors"][p["name"]]}]})
        else:
            reply(i, {"content": [{"type": "text", "text": json.dumps(fx["results"].get(p["name"], {}))}]})
