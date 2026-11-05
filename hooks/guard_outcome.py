#!/usr/bin/env python3
"""What a decision's call answered (hooks.json: PostToolUse and PostToolUseFailure on `mcp__…`; CONTRACT §5g).

The guard writes a use to .mother/ledger.json when it lets a bound grant's call through, which proves nothing about the
service: Slack's `ok: false` comes back as a successful tool call. This hook writes what the tool answered onto that use,
so `decisions.settle` can tell the manager the truth: `outcome` is "ok", "failed" or "unclear", and `error` a short code.
Never any other part of the response: it can hold a message's words.

It decides nothing and can't stop a tool: it always exits 0 and prints nothing. Most calls have no use waiting, and they
leave after one small read, without loading guard.py. A hook that doesn't run leaves no outcome, which reads as "its
result wasn't seen". `draft_sha` and the lock are guard.py's, repeated for that speed; a test holds them equal.

Python 3.9 stdlib only.
"""
import hashlib
import json
import os
import re
import signal
import sys
import time

try:
    import fcntl
except ImportError:  # not on Windows; as the guard's ledger, it then works without a lock
    fcntl = None

DEADLINE_SECONDS = 5  # hooks.json allows 10
LOCK_SECONDS = 2      # the ledger's lock is held for one read and write: past this, no outcome is the honest record
DEPTH = 4
ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,59}$")
# The call may have reached the service and the answer been lost, so the send may have landed. Not a refused connection:
# that call never left.
UNSURE = re.compile(r"time[ -]?d? ?out|interrupt|abort|cancel|connection (?:closed|reset|lost|dropped|terminated)"
                    r"|socket hang ?up|econnreset|epipe|broken pipe|fetch failed|(?:stream|socket) (?:closed|ended)"
                    r"|premature close", re.I)
WRAPPERS = ("data", "result", "structuredContent", "structured_content", "response", "content")
ERROR_KEYS = ("error", "errors", "errorMessages", "error_message")
FOUND_KEYS = ("ts", "id")  # a write answered with what it made
TRACKER_KEYS = ("key", "issueKey", "issue_key")
TRACKER_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")  # `key` alone can be a validation error's name
BAD_STATUS = {"fail", "failed", "failure", "error", "errored", "rejected", "denied", "refused", "invalid"}


def draft_sha(tool, inp):
    """As guard.draft_sha."""
    blob = json.dumps({"tool": tool, "input": inp}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _code(v, fallback="error"):
    """`v` when it is shaped like an error code, else `fallback`: a sentence can hold a message's words."""
    return v if isinstance(v, str) and ERROR_CODE.match(v) else fallback


def _tool_error(text):
    """A tool's error result or failure event, whose text is free-form: ("failed", code) or ("unclear", None)."""
    found = outcome_of(text)
    if found[0] == "failed":
        return found
    blob = text if isinstance(text, str) else json.dumps(text, default=str)
    return ("unclear", None) if UNSURE.search(blob) else ("failed", "tool_error")


def _bad_status(resp):
    """`status` or `statusCode` says this answer failed."""
    for v in (resp.get("status"), resp.get("statusCode")):
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v)
        if isinstance(v, str) and v.strip().lower() in BAD_STATUS:
            return True
        if isinstance(v, int) and not isinstance(v, bool) and v >= 400:
            return True
    return False


def _found(resp):
    """The answer carries what a write made, and nothing says it failed."""
    if _bad_status(resp):
        return False
    return any(resp.get(k) for k in FOUND_KEYS) \
        or any(isinstance(resp.get(k), str) and TRACKER_KEY.match(resp[k]) for k in TRACKER_KEYS)


def outcome_of(resp, depth=0):
    """("ok" | "failed" | "unclear", short code or None). Success is only what says so: `ok: true`, or a write's answer
    carrying what it made. Words or an empty answer are "unclear". A failure anywhere at this level beats a success in
    a wrapper: a tool can echo a draft into `content`."""
    if depth > DEPTH:
        return "unclear", None
    if isinstance(resp, str):
        try:
            return outcome_of(json.loads(resp), depth + 1) if resp.strip()[:1] in ("{", "[") else ("unclear", None)
        except ValueError:
            return "unclear", None
    if isinstance(resp, list):
        found = [outcome_of(x.get("text") if isinstance(x, dict) and x.get("type") == "text" else x, depth + 1)
                 for x in resp]
        return next((f for f in found if f[0] == "failed"), next((f for f in found if f[0] == "ok"), ("unclear", None)))
    if not isinstance(resp, dict):
        return "unclear", None
    if resp.get("isError") is True or resp.get("is_error") is True:
        return _tool_error(resp.get("content", resp.get("error", "")))
    if resp.get("ok") is False or resp.get("success") is False:
        return "failed", _code(resp.get("error"))
    for k in ERROR_KEYS:
        if resp.get(k):
            return "failed", _code(resp[k])
    if resp.get("ok") is True or resp.get("success") is True:
        return "ok", None
    # A bare string `content` may be the draft echoed back, so it proves nothing. A failure in any wrapper wins.
    found = [outcome_of(resp[k], depth + 1) for k in WRAPPERS
             if isinstance(resp.get(k), (dict, list)) or (k != "content" and isinstance(resp.get(k), str))]
    hit = next((f for f in found if f[0] == "failed"), next((f for f in found if f[0] == "ok"), None))
    return hit or (("ok", None) if _found(resp) else ("unclear", None))


def _workspace():
    p = os.environ.get("MOTHER_WORKSPACE", "").strip()
    if not p:
        with open(os.path.join(os.path.expanduser("~"), ".mother", "workspace")) as f:
            p = f.readline().strip()
    return os.path.expanduser(p)


def _waiting(uses, sha=None):
    """The uses still waiting for a result, the guard's order (newest last), of one call when `sha` is given."""
    return [r for r in uses if isinstance(r, dict) and r.get("draft_sha") and "outcome" not in r
            and (sha is None or r["draft_sha"] == sha)]


def _read_uses(path):
    with open(path) as f:
        doc = json.load(f)
    return doc, doc.get("uses") if isinstance(doc, dict) and isinstance(doc.get("uses"), list) else []


def _lock(folder):
    """The ledger's lock, or None when it can't be had in time; the caller closes it."""
    f = open(os.path.join(folder, "ledger.lock"), "a")
    if not fcntl:
        return f
    end = time.monotonic() + LOCK_SECONDS
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() >= end:
                f.close()
                return None
            time.sleep(0.02)


def record(raw):
    p = json.loads(raw)
    tool, inp = p.get("tool_name"), p.get("tool_input")
    if not isinstance(tool, str) or not tool.startswith("mcp__") or not isinstance(inp, dict):
        return
    folder = os.path.join(_workspace(), ".mother")
    path = os.path.join(folder, "ledger.json")
    if not _waiting(_read_uses(path)[1]):
        return
    sha = draft_sha(tool, inp)
    if p.get("hook_event_name") == "PostToolUseFailure":
        seen, code = ("unclear", None) if p.get("is_interrupt") else _tool_error(p.get("error", ""))
    elif "tool_response" in p:
        seen, code = outcome_of(p["tool_response"])
    else:  # JSON drops an undefined response: nothing is known
        seen, code = "unclear", None
    lock = _lock(folder)
    if lock is None:
        return
    try:
        doc, uses = _read_uses(path)
        mine = _waiting(uses, sha)
        if mine:
            # the newest: an older one still waiting is a call that was never answered, and has already closed
            mine[-1]["outcome"] = seen
            if code:
                mine[-1]["error"] = code
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(doc, f, indent=1)
            os.replace(tmp, path)
    finally:
        lock.close()


def main():
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.alarm(DEADLINE_SECONDS)
    try:
        record(sys.stdin.read())
    except Exception:  # a missing ledger, a broken payload: there is no result to record, and a tool must not fail for it
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
