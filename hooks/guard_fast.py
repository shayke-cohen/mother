#!/usr/bin/env python3
"""MOTHER approval guard, fast path for Read, Grep, Glob and LS (hooks.json; CONTRACT §5).

These are the most frequent tool calls, and the guard has a rule for them only in four cases: the call touches
the grant key, the session is a channel reply session (MOTHER_CHANNEL_ROLE), a sub-agent makes it (agent_type),
or the session is the pinned hub session. Loading guard.py costs tens of milliseconds, so this file checks
those four cases first and exits with no decision when none applies. When any might apply, or anything is
unclear, it hands the call to guard.py unchanged. It never decides anything itself.

Python 3.9 stdlib only.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
FAST_TOOLS = ("Read", "Grep", "Glob", "LS")


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _workspaces():
    """Every workspace guard.py could resolve (MOTHER_WORKSPACE, then ~/.mother/workspace): both are checked."""
    out = []
    p = os.environ.get("MOTHER_WORKSPACE", "").strip()
    if p:
        out.append(p)
    try:
        with open(os.path.join(os.path.expanduser("~"), ".mother", "workspace")) as f:
            p = f.readline().strip()
        if p:
            out.append(p)
    except Exception:
        pass
    return [os.path.realpath(os.path.expanduser(x)) for x in out]


def needs_guard(raw):
    """False only when guard.py would certainly return no decision for this call."""
    try:
        p = json.loads(raw or "")
    except Exception:
        return True
    if not isinstance(p, dict) or p.get("tool_name") not in FAST_TOOLS or not isinstance(p.get("tool_input"), dict):
        return True
    if os.environ.get("MOTHER_CHANNEL_ROLE", "").strip() or str(p.get("agent_type") or "").strip():
        return True
    inp = p["tool_input"]
    try:
        state = os.environ.get("MOTHER_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".mother")
        key = os.path.realpath(os.path.join(state, "grant.key")).casefold()
        target = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
        base = str(p.get("cwd") or os.getcwd())
        fp = os.path.realpath(os.path.join(base, os.path.expanduser(str(target))) if target else base).casefold()
        if _inside(key, fp):  # the key itself, or a folder holding it
            return True
        sid = p.get("session_id")
        if isinstance(sid, str) and sid:
            for ws in _workspaces():
                try:
                    with open(os.path.join(ws, ".mother", "hub", "hub-session.json")) as f:
                        if sid in f.read():
                            return True
                except FileNotFoundError:
                    pass
    except Exception:
        return True
    return False


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if not needs_guard(raw):
        sys.exit(0)
    sys.path.insert(0, HERE)
    import guard
    r = guard.run(raw)
    if r:
        guard.out(*r)
    sys.exit(0)


if __name__ == "__main__":
    main()
