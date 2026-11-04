#!/usr/bin/env python3
"""MOTHER approval guard, fast path for every tool the main hook entry doesn't take (hooks.json; CONTRACT §5).

hooks.json hands guard.py the tools it has rules for in every session (Bash, the file writers, web, agents, MCP
…). Every other tool comes here: Read, Grep, Glob and LS, which are the most frequent calls, and any tool the
host adds (ToolSearch, TodoWrite, Artifact, …). guard.py has a rule for them only where an allow-list or a fence
applies: a call that touches the grant key (Read/Grep/Glob/LS), a channel reply session (MOTHER_CHANNEL_ROLE),
one of MOTHER's sub-agents (agent_type), or a hub or hub-fed session. Loading guard.py costs tens of
milliseconds, so this file checks those cases first and exits with no decision when none applies. When any
might apply, or anything is unclear, it hands the call to guard.py unchanged. It never decides anything itself.

Python 3.9 stdlib only.
"""
import json
import os
import signal
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
DEADLINE_SECONDS = 8  # as guard.py's; hooks.json allows 60, and a killed hook lets the call run unguarded


def _expired(_signum, _frame):
    """Out of time, maybe inside guard.py: a question (a deny where nobody watches) rather than no decision."""
    alone = os.environ.get("MOTHER_CHIEF_WORKER", "").strip() or os.environ.get("MOTHER_CHIEF_ROLE") == "chief"
    os.write(1, (json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny" if alone else "ask",
        "permissionDecisionReason": "[mother guard] The guard ran out of time reading this call, so it can't vouch "
                                    "for it."}}) + "\n").encode("utf-8"))
    os._exit(0)


def arm_deadline(seconds=DEADLINE_SECONDS):
    if not (hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")):
        return False
    try:
        signal.signal(signal.SIGALRM, _expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except (ValueError, OSError):
        return False
    return True


def disarm_deadline():
    if hasattr(signal, "setitimer"):
        signal.setitimer(signal.ITIMER_REAL, 0)
KEY_TOOLS = ("Read", "Grep", "Glob", "LS")
SUBAGENTS = ("reader", "critic")  # guard.SUBAGENT_TOOLS (tests check the two agree)
HUB_FILES = ("hub-session.json", "sessions.json", "fed-sessions.json")  # the hub session and hub-fed sessions


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
    if not isinstance(p, dict) or not isinstance(p.get("tool_name"), str) or not isinstance(p.get("tool_input"), dict):
        return True
    if os.environ.get("MOTHER_CHANNEL_ROLE", "").strip():
        return True
    agent = p.get("agent_type")
    if agent is not None and not isinstance(agent, str):
        return True
    agent = (agent or "").strip()
    if (agent[len("mother:"):] if agent.startswith("mother:") else agent) in SUBAGENTS:
        return True
    cwd = p.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return True
    inp = p["tool_input"]
    try:
        if p["tool_name"] in KEY_TOOLS:
            state = os.environ.get("MOTHER_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".mother")
            key = os.path.realpath(os.path.join(state, "grant.key")).casefold()
            target = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
            base = cwd or os.getcwd()
            fp = os.path.realpath(os.path.join(base, os.path.expanduser(str(target))) if target else base).casefold()
            if _inside(key, fp):  # the key itself, or a folder holding it
                return True
        sid = p.get("session_id")
        if isinstance(sid, str) and sid:
            for ws in _workspaces():
                for name in HUB_FILES:
                    try:
                        with open(os.path.join(ws, ".mother", "hub", name)) as f:
                            if sid in f.read():
                                return True
                    except FileNotFoundError:
                        pass
    except Exception:
        return True
    return False


def main():
    arm_deadline()  # one deadline for the whole run, guard.py's import and checks included
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if not needs_guard(raw):
        disarm_deadline()
        sys.exit(0)
    sys.path.insert(0, HERE)
    import guard  # not as __main__, so it arms nothing of its own; this file's alarm covers it
    r = guard.run(raw)
    disarm_deadline()
    if r:
        guard.out(*r)
    sys.exit(0)


if __name__ == "__main__":
    main()
