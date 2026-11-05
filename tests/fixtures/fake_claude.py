#!/usr/bin/env python3
"""Stand-in for `claude -p` in chief tests. Appends {argv, stdin, env} as one JSON line to $FAKE_CLAUDE_LOG and
prints stream-json. FAKE_CLAUDE_RESULT, FAKE_CLAUDE_SLEEP (seconds), FAKE_CLAUDE_EXIT, FAKE_CLAUDE_STDERR and
FAKE_CLAUDE_EVENT_FILE (a JSON file, printed as the result event) control it."""
import json
import os
import sys
import time

stdin = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "env": dict(os.environ)}) + "\n")
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
result = os.environ.get("FAKE_CLAUDE_RESULT", "ok\nPROGRESS: did the thing")
argv = sys.argv[1:]
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else (
    argv[argv.index("--resume") + 1] if "--resume" in argv else "")
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": result}]}}))
event_file = os.environ.get("FAKE_CLAUDE_EVENT_FILE")
if event_file:
    with open(event_file) as f:
        print(json.dumps(json.load(f)))
else:
    print(json.dumps({"type": "result", "subtype": "success", "result": result, "total_cost_usd": 0.01,
                      "session_id": sid}))
sys.stderr.write(os.environ.get("FAKE_CLAUDE_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
