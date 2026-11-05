"""mother decide — the decision queue. A worker files the exact call it wants to make; the manager answers; code
(never a model) writes a single-use grant bound to that call (`answer`).

  propose <key> --tool T --recommend R [--why W] [--urgent] --stdin     stdin: the tool's input JSON
  answer <n> <ok|no|edit|say> --via pane [-- <text>]                    the Chief pane's answer (mod only)
  batch [--json]                                                        the message due for the manager's DM, if any
  sent --ts TS [--ids D7,D8|'']                                         that message went out (TS: its Slack ts, re-read)

State is <workspace>/.mother/chief/decisions.json, changed only under `locked(ws)` (flock on decisions.lock) and
written atomically. Python 3.9 stdlib only.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Set, Tuple

import channels
import chief
import grants
import hub

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("mother_guard", str(PLUGIN_ROOT / "hooks" / "guard.py"))
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

USAGE = __doc__.split("\n\n")[1].rstrip()
KINDS = ("chat-post", "chat-reply", "tracker-create", "tracker-comment", "tracker-transition")
STATUSES = ("pending", "approved", "rejected", "redirected", "expired", "sent", "failed", "manual")
SIGNED_KINDS = ("chat-post", "chat-reply", "tracker-comment")  # the kinds run_grant_scope wants a signature on
TOP_TEXT_KEYS = ("text", "message", "body", "comment", "markdown", "content")
NESTED_TEXT_KEYS = ("text", "body", "comment", "commentBody", "content")


def _path(ws: Path, name: str = "decisions.json") -> Path:
    return chief.chief_dir(ws) / name


@contextlib.contextmanager
def locked(ws: Path):
    d = chief.chief_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    f = open(str(d / "decisions.lock"), "a")
    try:
        if guard.fcntl:
            guard.fcntl.flock(f, guard.fcntl.LOCK_EX)
        yield
    finally:
        f.close()


def _empty() -> dict:
    return {"version": 1, "next": 1, "decisions": [], "batch": {"last_at": None, "announced": {}}, "confirm": [],
            "replied": {}, "retry_at": {}, "void": []}


def _id_number(d: dict) -> Optional[int]:
    m = re.match(r"^D(\d+)$", str(d.get("id")))
    return int(m.group(1)) if m else None


def _past_used(ws: Path) -> int:
    """The first id no grant or grant use names: after a lost decisions.json, an old ledger entry or a live grant must
    not attach to a new decision under the same number."""
    top = 0
    for name, key in (("ledger.json", "uses"), ("grants.json", "grants")):
        data, ok = guard.read_json(os.path.join(str(ws), ".mother", name))
        rows = data.get(key) if ok and isinstance(data, dict) else None
        for r in rows if isinstance(rows, list) else []:
            for v in (r.get("run"), r.get("decision")) if isinstance(r, dict) else ():
                m = re.match(r"^(?:chief:)?D(\d+)$", str(v))
                top = max(top, int(m.group(1))) if m else top
    return top + 1


def load(ws: Path) -> dict:
    """decisions.json with every field present; a missing or unreadable file reads as empty. `batch.announced` maps a
    decision id to the stamp of the batch that showed it to the manager; any other shape reads as no announcements.
    `batch.offered`, `offered_ids` and `offered_at` are the live offer: the confirm-line count, the ids and the time of
    the last `batch` command, until `sent` uses it.
    `batch.offered_text` is the live offer's text, which is all the guard lets the chief post to the self-DM. `offers`
    holds each offer since the last `sent` (a post that may have landed can still be proved), `posts` the batches proved
    posted: their ts and every text they could have shown.
    `replied` maps an inbox line that looked like a reply to whether its re-read message was one (a line behind the
    cursor is applied once), or to the failed reads so far; `retry_at` says when a line past REPLY_TRIES is read next.
    `void` lists the app-posted messages that looked like a batch and weren't: the ids they name, and from when until
    when a reply to those ids is held. `next` is past every id in the file: an id is never reused."""
    doc = _empty()
    try:
        raw = json.loads(_path(ws).read_text())
    except (OSError, ValueError):
        return doc
    if not isinstance(raw, dict):
        return doc
    if isinstance(raw.get("decisions"), list):
        doc["decisions"] = [d for d in raw["decisions"] if isinstance(d, dict)]
    if isinstance(raw.get("next"), int) and raw["next"] >= 1:
        doc["next"] = raw["next"]
    doc["next"] = max([doc["next"]] + [n + 1 for n in map(_id_number, doc["decisions"]) if n is not None])
    b = raw.get("batch")
    if isinstance(b, dict):
        doc["batch"]["last_at"] = b.get("last_at") if isinstance(b.get("last_at"), str) else None
        if isinstance(b.get("announced"), dict):
            doc["batch"]["announced"] = {k: v for k, v in b["announced"].items() if isinstance(v, str)}
        if isinstance(b.get("offered"), int) and not isinstance(b["offered"], bool) and b["offered"] >= 0:
            doc["batch"]["offered"] = b["offered"]
        if isinstance(b.get("offered_ids"), list):
            doc["batch"]["offered_ids"] = [i for i in b["offered_ids"] if isinstance(i, str)]
        if isinstance(b.get("offered_at"), str):
            doc["batch"]["offered_at"] = b["offered_at"]
        if isinstance(b.get("offered_text"), str):
            doc["batch"]["offered_text"] = b["offered_text"]
        if isinstance(b.get("offers"), list):
            doc["batch"]["offers"] = [o for o in b["offers"] if isinstance(o, dict) and isinstance(o.get("text"), str)
                                      and isinstance(o.get("ids"), list) and isinstance(o.get("at"), str)]
        if isinstance(b.get("posts"), list):
            doc["batch"]["posts"] = [x for x in b["posts"] if isinstance(x, dict) and isinstance(x.get("ts"), str)
                                     and isinstance(x.get("texts"), list)]
    if isinstance(raw.get("confirm"), list):
        doc["confirm"] = [x for x in raw["confirm"] if isinstance(x, str)]
    if isinstance(raw.get("replied"), dict):
        doc["replied"] = {k: v for k, v in raw["replied"].items() if isinstance(v, int)}  # a bool is an int too
    if isinstance(raw.get("retry_at"), dict):
        doc["retry_at"] = {k: v for k, v in raw["retry_at"].items() if isinstance(v, str)}
    if isinstance(raw.get("void"), list):
        doc["void"] = [v for v in raw["void"] if isinstance(v, dict) and isinstance(v.get("from"), str)
                       and isinstance(v.get("ids"), list)]
    return doc


def save(ws: Path, doc: dict) -> None:
    """Atomic write; the caller holds `locked(ws)`."""
    chief._write_atomic(_path(ws), json.dumps(doc, indent=1))


def pending(ws: Path) -> List[dict]:
    return [d for d in load(ws)["decisions"] if d.get("status") == "pending"]


def kind_of(tool: str, inp: dict) -> tuple:
    """(kind, target, thread) for a call; anything a grant can't cover is ("manual", None, None)."""
    a = guard.classify_mcp(tool, inp)
    scope = guard.grant_scope_of(a) if a else None
    if not scope or scope[0] not in KINDS:
        return "manual", None, None
    return scope[0], scope[1], a.get("thread") if a.get("kind") == "chat" else None


def _signed(kind: str, tool: str, inp: dict, cfg: dict) -> bool:
    """The guard would accept this draft's signature. A kind it wants none on always passes."""
    return kind not in SIGNED_KINDS or guard.carries_signature(guard.classify_mcp(tool, inp), cfg)


TARGET_REFUSED = "the guard refused the call: target not allowed"


def grant_problem(kind: str, target, cfg: dict) -> Optional[str]:
    """Why the guard could never honour a grant for this call, whatever the manager answers, or None. It asks the guard's
    own functions, so the two can't disagree."""
    if kind == "manual":
        return None
    scopes = guard.grant_limits(cfg)[0]
    if kind not in scopes:
        return "policy.grants turns %s off" % kind
    if scopes[kind] < 1:
        return "policy.grants allows %s no uses" % kind
    if not target or str(target) not in guard.allowed_targets(kind, cfg):
        return TARGET_REFUSED
    return None


def propose(ws: Path, cfg: dict, at: datetime, key: str, tool: str, inp: dict, recommendation: str,
            why: str = "", urgent: bool = False) -> dict:
    if not isinstance(inp, dict):
        raise ValueError("the tool input must be a JSON object")
    sha = guard.draft_sha(tool, inp)
    kind, target, thread = kind_of(tool, inp)
    if not _signed(kind, tool, inp, cfg):  # the guard refuses it however it's answered: say so now
        marks = guard.signature_marks(cfg)
        if not marks:
            raise ValueError("config.json names no agent to sign this %s with, so it can't be sent" % kind)
        if any(m in draft_text(inp) for m in marks):
            raise ValueError("this %s's signature isn't in a field the guard reads, so it would refuse the call even "
                             "with the manager's yes. Put %s in text, message, body, comment, markdown or content, "
                             "then file it again" % (kind, marks[0]))
        raise ValueError("this %s isn't signed, so the guard would refuse it even with the manager's yes. Sign it: put "
                         "%s in the text%s, then file it again" % (
                             kind, marks[0], " and the blocks" if guard.classify_mcp(tool, inp).get("blocks") else ""))
    problem = grant_problem(kind, target, cfg)
    if problem:  # the manager's yes couldn't send it: don't let them give one
        where = "%s isn't a team channel a grant can reach (a DM, an external channel or one set to off never is)" \
            % target if kind.startswith("chat-") else "%s isn't a tracker project in config.json" % target
        raise ValueError("%s, so even the manager's yes couldn't send this. Say in your PROGRESS line what the manager "
                         "should do by hand" % (problem if problem != TARGET_REFUSED else "%s: %s" % (problem, where)))
    hours = chief.settings(cfg)["decision_hours"]
    with locked(ws):
        if key not in chief.workers(ws):
            raise ValueError("%s is not a live CLI worker" % key)
        doc = load(ws)
        for d in doc["decisions"]:
            if d.get("status") == "pending" and d.get("key") == key and d.get("draft_sha") == sha:
                raise ValueError("%s already has this call pending as %s" % (key, d.get("id")))
        doc["next"] = max(doc["next"], _past_used(ws))
        d = {"id": "D%d" % doc["next"], "key": key, "kind": kind, "tool": tool, "input": inp, "target": target,
             "thread": thread, "text": guard.text_of(inp), "draft_sha": sha, "recommendation": recommendation,
             "why": why, "urgent": bool(urgent), "status": "pending", "created": hub._iso(at),
             "expires": hub._iso(at + timedelta(hours=hours)), "answer": None}
        doc["next"] += 1
        doc["decisions"].append(d)
        save(ws, doc)
        chief.update_worker(ws, key, status="waiting_decision")
    return d


def edited_input(inp: dict, text: str) -> dict:
    """A copy of `inp` with the text field `guard.text_of` reads replaced. ValueError when there is none, or when
    that field is not a plain string (`blocks`)."""
    out = json.loads(json.dumps(inp))
    if guard.first(out, "blocks") is not None:  # the reader sees the blocks, so new text alone wouldn't reach them
        raise ValueError("the text of this call is blocks, which can't be edited as plain text")
    for k in TOP_TEXT_KEYS:
        if guard.first(out, k) is not None:
            if not isinstance(out[k], str):
                raise ValueError("the text of this call is %s, which can't be edited as plain text" % k)
            out[k] = text
            return out
    nested = out.get("inputs")
    if isinstance(nested, dict):
        for k in NESTED_TEXT_KEYS:
            if guard.first(nested, k) is not None:
                if not isinstance(nested[k], str):
                    raise ValueError("inputs.%s is not plain text" % k)
                nested[k] = text
                return out
    raise ValueError("the call has no text field to edit")


# what routes a call rather than what it says. Every one of them is shown, in `plain_target`, whatever the tool.
ROUTING_PRIMARY = ("issueKey", "issueIdOrKey", "issue_key", "issueId", "channel", "channel_id", "channelId", "to",
                   "projectKey", "project_key")
ROUTING_KEYS = set(ROUTING_PRIMARY) | {"thread_ts", "threadTs", "ts", "project", "issueType", "issueTypeId",
                                       "issuetype", "issue_type", "transitionId", "transition", "transition_id"}
SHOWN_SETTINGS = ("reply_broadcast",)  # a setting that changes who reads it


def _input_levels(inp) -> list:
    return [("", inp), ("inputs.", inp.get("inputs") if isinstance(inp, dict) else None)]


def draft_fields(inp, settings: bool = True) -> List[Tuple[str, str]]:
    """(field, what it says) for everything a call would send: every field but ROUTING_KEYS, at the top and under
    `inputs`, in the input's order. A list or object (blocks) reads as JSON. Booleans and numbers are settings, left out
    but for SHOWN_SETTINGS when on (and `settings`)."""
    out = []
    for prefix, d in _input_levels(inp):
        for k, v in (d.items() if isinstance(d, dict) else ()):
            if k in SHOWN_SETTINGS and v is True and settings:
                out.append((prefix + k, "true"))
            elif k in ROUTING_KEYS or k == "inputs" or v is None or v == "" or isinstance(v, (bool, int, float)):
                continue
            else:
                out.append((prefix + k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)))
    return out


def draft_text(inp) -> str:
    """All the call would send, for the manager to read. One field is that field, byte for byte. Several are each
    labelled, so nothing that would go out is hidden behind the first."""
    fields = draft_fields(inp)
    if len(fields) == 1:
        return fields[0][1]
    return "\n\n".join("%s: %s" % f for f in fields)


def plain_target(d: dict) -> str:
    """Where the call goes, as one safe line: the first of ROUTING_PRIMARY the input names (the tracker issue, the
    channel, the recipient), then every other routing field by name. With none, the grant's target."""
    tool, inp = d.get("tool"), d.get("input")
    if not isinstance(inp, dict) or not isinstance(tool, str):
        return ""
    found = []
    for _, level in _input_levels(inp):
        for k, v in (level.items() if isinstance(level, dict) else ()):
            if k in ROUTING_KEYS and v not in (None, ""):
                found.append((k, ", ".join(map(str, v)) if isinstance(v, list) else str(v)))
    head = next((p for k in ROUTING_PRIMARY for p in found if p[0] == k), None)
    if head is None:
        head = ("", (guard.classify_mcp(tool, inp) or {}).get("channel") or d.get("target") or "")
        rest = found
    else:
        rest = [p for p in found if p is not head]
    parts = [str(head[1])] if head[1] else []
    return chief.safe_line(", ".join(parts + ["%s %s" % p for p in rest]), 200)


def signed_edit(d: dict, cfg: dict, text: str) -> dict:
    """`d`'s input with the manager's `text`, still carrying the signature the guard wants, or the grant could never
    match. Their words stay as typed; the signature the draft carried goes on a line after them when they left it
    out. ValueError when that isn't possible."""
    tell = "Tell the worker what to change: %s: <your words>" % str(d.get("id"))[1:]
    fields = [chief.safe_line(f, 60) for f, _ in draft_fields(d["input"], settings=False)]
    if len(fields) > 1:  # new text in one would send the others unread: the manager can say what to change instead
        raise ValueError("this call sends %s, and an edit can only replace one. %s" % (" and ".join(fields), tell))
    try:
        tool, inp = d["tool"], edited_input(d["input"], text)
    except ValueError as e:
        raise ValueError("%s. %s" % (e, tell))
    kind = kind_of(tool, inp)[0]
    if _signed(kind, tool, inp, cfg):
        return inp
    marks = guard.signature_marks(cfg)
    if not marks:
        raise ValueError("config.json names no agent to sign the edited text with")
    mark = next((m for m in reversed(marks) if m in (d.get("text") or "")), marks[0])
    inp = edited_input(d["input"], "%s\n\n%s" % (text, mark))
    if not _signed(kind, tool, inp, cfg):
        raise ValueError("the edited text can't carry the signature")
    return inp


def edit_view(d: dict, cfg: dict) -> dict:
    """What the Chief pane's Edit may do with `d`, known before anyone presses it: `edit_refusal`, why it can't be
    edited (or None), and `edit_text`, the one field an edit replaces (None when refused). `full_text` won't do for that:
    it can hold labels and settings an edit must not send."""
    try:
        signed_edit(d, cfg, "x")
    except (ValueError, KeyError, TypeError) as e:
        return {"edit_refusal": chief.safe_line(str(e), 300) or "this call can't be edited", "edit_text": None}
    fields = draft_fields(d["input"], settings=False)
    return {"edit_refusal": None, "edit_text": fields[0][1] if fields else ""}


_NUM = re.compile(r"^d?(\d+)$")
_START_NUM = re.compile(r"^d?\d+(?!\w)(.*)$", re.I | re.S)
_SAY = re.compile(r"^d?(\d+)\s*:(?:\s(.*))?$", re.I | re.S)  # a colon and whitespace: "10:30 call" is a note
_SAY_START = re.compile(r"^\s*d?\d+[ \t]*:(?:\s|$)", re.I)
_START_WORD = re.compile(r"^(\S+)\s+(?:d?\d+|all)(?!\w)", re.I)
_EDIT = re.compile(r"^\s*d?(\d+)\s+edit(?!\w)(.*)$", re.I | re.S)


def _starts_like_reply(t: str) -> bool:
    """A number counts only when a verb, a colon or nothing follows it. Ruling T2: "2 things for tomorrow" is a note."""
    m = _START_NUM.match(t)
    if m:
        rest = m.group(1).lstrip(" \t")
        if rest[:1] == ":":
            return not rest[1:] or rest[1].isspace()
        if not rest.strip() or rest[0] in ",;\r\n":
            return True
        word = re.split(r"[\s,;]", rest, maxsplit=1)[0].lower()
        return bool(re.match(r"edit(?!\w)", word)) or word in (channels.SEND_WORDS | channels.NO_WORDS)
    m = _START_WORD.match(t)
    return bool(m) and m.group(1).lower() in (channels.SEND_WORDS | channels.NO_WORDS)


def _clause(c: str, open_ids: Set[int]) -> Optional[list]:
    """Items for one non-edit clause, or None when it doesn't parse."""
    said = _SAY.match(c)
    if said:
        words = " ".join((said.group(2) or "").split())
        return [(int(said.group(1)), "say", words)] if words else None
    toks = c.split()
    if len(toks) < 2:
        return None
    w, verb = toks[0].lower(), None
    if w in channels.SEND_WORDS:
        verb = "ok"
    elif w in channels.NO_WORDS:
        verb = "no"
    if verb:
        if verb == "ok" and len(toks) == 2 and toks[1].lower() == "all":
            return [(n, "ok", "") for n in sorted(open_ids)] or None
        nums = [_NUM.match(x.lower()) for x in toks[1:]]
        if all(nums):
            return [(int(m.group(1)), verb, "") for m in nums]
        # "no 8 ..." with trailing words falls through to None: that form is "N no <words>"
        return None
    m = _NUM.match(w)
    if not m:
        return None
    n, w2, rest = int(m.group(1)), toks[1].lower(), toks[2:]
    if w2 in channels.SEND_WORDS:
        return [(n, "ok", "")] if not rest else None
    if w2 in channels.NO_WORDS:
        out = [(n, "no", "")]
        if rest:
            out.append((n, "say", " ".join(rest)))
        return out
    return None  # a number and free words: words for the worker take a colon, `7: <words>`


def parse_reply(text: str, open_ids: Set[int]) -> Optional[List[Tuple[int, str, str]]]:
    """The manager's answer to the decision queue as (n, verb, payload) items, verb ok|no|edit|say.
    None: not a reply at all (the text keeps its normal path). []: it starts like a reply but doesn't parse fully,
    or names an id that isn't open — apply nothing."""
    t = (text or "").lstrip()  # only what precedes the payload: an edit's text is kept byte for byte
    if not t.strip() or not _starts_like_reply(t):
        return None
    items, rest = [], t
    while rest.strip():
        e = _EDIT.match(rest)
        if e:
            c = re.match(r"\s*: ?(.*)$", e.group(2), re.S)
            if not c or not c.group(1).strip():
                return []
            payload = c.group(1)
            items.append((int(e.group(1)), "edit", payload))
            break
        # the words of `7: ...` are the manager's, commas and all: only a new line ends them
        parts = re.split(r"\n" if _SAY_START.match(rest) else r"[,;\n]", rest, maxsplit=1)
        got = _clause(parts[0].strip(), open_ids)
        if not got:
            return []
        items.extend(got)
        rest = parts[1] if len(parts) > 1 else ""
    if not items or any(n not in open_ids for n, _, _ in items):
        return []
    return items


# ---- answering ---------------------------------------------------------------------------------------

GRANT_HOURS = 2
PREVIEW_MAX = 200
VERBS = ("ok", "no", "edit", "say")
VIAS = ("pane",)  # a Slack reply goes through answer_reply, which reads what the manager wrote
UNREADABLE = "I couldn't read that reply, so nothing was applied. Use: 7 ok · 7 no · 7 edit: your text · ok all"
UNCONFIRMED = "I couldn't confirm that reply came from you (%s)."
_SLACK_LINK = re.compile(r"<(https?:[^|>\s]*)(?:\|([^>]*))?>")


def canonical(inp) -> str:
    return json.dumps(inp, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chat_clients(cfg: dict) -> dict:
    """The hub's read-only clients with only the chat server started. The caller closes them."""
    from hub_mcp import McpError
    h = hub._hub_cfg(cfg)
    conn = (h.get("connections") or {}).get("chat")
    if not isinstance(conn, dict):
        raise McpError("other", "no hub chat connection in config")
    return hub.build_clients(dict(cfg, hub=dict(h, connections={"chat": conn})), {})


def _read_message(clients: dict, channel: str, ts: str) -> Optional[dict]:
    """The message at `ts`, or None when Slack read fine and has none. Raises on a read that failed, `ok: false` included:
    Slack's refusal arrives as a successful tool call, and says nothing about the message."""
    import hub_sources
    from hub_mcp import McpError
    c = clients.get("chat")
    if c is None or isinstance(c, Exception):
        raise c if isinstance(c, Exception) else McpError("other", "no chat client")
    res = c.call("get_channel_history", {"channel_id": channel, "oldest": ts, "latest": ts, "inclusive": True,
                                         "limit": 1})
    why = hub_sources.refused(res)
    if why:
        raise McpError("other", "Slack refused the read: %s" % why)
    for m in (res or {}).get("messages") or []:
        if isinstance(m, dict) and m.get("ts") == ts:
            return m
    return None


def _close_clients(clients: dict) -> None:
    for c in clients.values():
        if not isinstance(c, Exception):
            hub._close(c)


def _fetch_slack(cfg: dict, channel: str, ts: str) -> Optional[dict]:
    """The one message at `ts` in `channel`, read through the hub's read-only chat client; None when Slack has no
    such message. Raises when the chat connection can't be made or read."""
    clients = _chat_clients(cfg)
    try:
        return _read_message(clients, channel, ts)
    finally:
        _close_clients(clients)


HISTORY_PAGE = 100
HISTORY_PAGES = 5


def _read_history(clients: dict, channel: str, oldest: str) -> List[dict]:
    """Every message in `channel` from `oldest` on. Raises when it can't get them all: a truncated read must not pass as
    a clean one."""
    from hub_mcp import McpError
    c = clients.get("chat")
    if c is None or isinstance(c, Exception):
        raise c if isinstance(c, Exception) else McpError("other", "no chat client")
    out, cursor = [], None
    for _ in range(HISTORY_PAGES):
        args = {"channel_id": channel, "oldest": oldest, "inclusive": True, "limit": HISTORY_PAGE}
        if cursor:
            args["cursor"] = cursor
        res = c.call("get_channel_history", args)
        msgs = res.get("messages") if isinstance(res, dict) else None
        if not isinstance(msgs, list):
            raise McpError("other", "the DM's history came back with no messages list")
        out.extend(m for m in msgs if isinstance(m, dict))
        if not res.get("has_more"):
            return out
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    raise McpError("other", "the DM's history is longer than one read can check")


class _Slack:
    """The tick's reads: callable as a `fetch`, with `history`. It starts the chat server on its first read and keeps it
    for the rest of the `with`: one client per tick. A connection that can't be made fails every read of the session
    without trying again."""

    def __init__(self, cfg: dict):
        self.cfg, self.clients, self.error = cfg, None, None

    def _open(self) -> dict:
        if self.error is not None:
            raise self.error
        if self.clients is None:
            try:
                self.clients = _chat_clients(self.cfg)
            except Exception as e:
                self.error = e
                raise
        return self.clients

    def __call__(self, _cfg, channel, ts):
        return _read_message(self._open(), channel, ts)

    def history(self, channel, oldest):
        return _read_history(self._open(), channel, oldest)


@contextlib.contextmanager
def _slack_session(cfg: dict):
    s = _Slack(cfg)
    try:
        yield s
    finally:
        if s.clients is not None:
            _close_clients(s.clients)


def _typed_message(cfg: dict, channel: str, ts: str, fetch=None) -> Tuple[Optional[dict], Optional[str], bool]:
    """(message, None, False) when Slack shows the message at (channel, ts) typed by the manager
    (`hub_sources.typed_by`) in the manager's own DM; otherwise (None, the reason it can't be confirmed, whether a
    later read could change that: only a failed read can). Reads Slack once."""
    import hub_sources
    if not isinstance(channel, str) or not channel.startswith("D") or channel not in guard.dm_ids(cfg):
        return None, "it isn't in your own DM", False
    mine = guard.owner_id(cfg)
    if not mine:
        return None, "config.json has no manager chat id", False
    if not isinstance(ts, str) or guard.parse_time(ts) is None:
        return None, "the message has no timestamp", False
    try:
        msg = (fetch or _fetch_slack)(cfg, channel, ts)
    except Exception as e:
        return None, "Slack couldn't be read: %s" % (str(e) or type(e).__name__), True
    if not isinstance(msg, dict) or msg.get("ts") != ts:
        return None, "Slack doesn't show that message", False
    if not hub_sources.typed_by(msg, mine):
        return None, "Slack doesn't show it as typed by you", False
    return msg, None, False


def slack_text(raw: str) -> str:
    """Slack's wire form as the words the manager wrote: links unwrapped and &amp; &lt; &gt; unescaped. Mentions,
    channel references, newlines, tabs and everything else stay. &amp; goes last, so it can't make a new entity."""
    def link(m):
        url, label = m.group(1), m.group(2)
        return url if not label or label == url else "%s (%s)" % (label, url)
    return _SLACK_LINK.sub(link, raw).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _reply_open_ids(ws: Path, ts: str) -> Set[int]:
    """What a Slack reply can answer: pending decisions filed and announced to the manager before the reply was typed.
    Anything else is a call the manager hadn't seen."""
    sent = guard.parse_time(ts)
    doc = load(ws)
    out = set()
    for d in doc["decisions"]:
        shown = guard.parse_time(doc["batch"]["announced"].get(d.get("id")))
        filed = guard.parse_time(d.get("created"))
        if d.get("status") == "pending" and shown is not None and shown < sent and filed is not None and filed < sent:
            m = _NUM.match(str(d["id"]).lower())
            if m:
                out.add(int(m.group(1)))
    return out


def answer_reply(ws: Path, cfg: dict, at: datetime, channel: str, ts: str, fetch=None) -> Optional[List[str]]:
    """Apply a reply the manager typed in the self-DM. The verb and any edit text come from the message re-read from
    Slack here, never from a caller or the watcher's flattened summary. None: the message isn't a reply. Otherwise
    the result lines: one for an unconfirmed or unreadable message (nothing changes), else one per parsed item."""
    state, lines = _reply(ws, cfg, at, channel, ts, fetch)
    return None if state == "not_reply" else lines


def _reply(ws: Path, cfg: dict, at: datetime, channel: str, ts: str, fetch=None) -> Tuple[str, List[str]]:
    """answer_reply's work: (state, result lines), state applied | held | not_reply | unreadable | unconfirmed | retry.
    Only `retry` could come out differently on another read. `held`: it answers a row a counterfeit named, so nothing is
    applied, even `ok all` or a reply naming other rows too; the manager was told once, when the counterfeit was found."""
    ws = Path(ws)
    msg, why, again = _typed_message(cfg, channel, ts, fetch)
    if why:
        return ("retry" if again else "unconfirmed"), [UNCONFIRMED % why]
    with locked(ws):
        open_ids = _reply_open_ids(ws, ts)
        voided = _voided_at(load(ws), ts)
    items = parse_reply(msg.get("text"), open_ids)
    if items is None:
        return "not_reply", []
    if not items:
        return "unreadable", [UNREADABLE]
    if any("D%d" % n in voided for n, _, _ in items):
        return "held", []
    source = {"via": "slack", "channel": channel, "ts": ts}
    lines, refused = [], set()
    for n, verb, payload in items:
        # `8 no <words>` is a no and then the words: they still reach the worker after the no has landed
        after_no = verb == "say" and n in refused
        try:
            ok, line = _answer(ws, cfg, at, n, verb, slack_text(payload) if verb == "edit" else payload, source,
                               after_no=after_no)
        except Exception as e:  # what the items before it did stands, so the result lines must say so
            _log(ws, "answer D%d: %r" % (n, e))
            ok, line = False, "D%d is still open: something went wrong applying your answer. Answer it again." % n
        if ok and verb == "no":
            refused.add(n)
        lines.append(line)
    return "applied", lines


def _grant(d: dict, cfg: dict, at: datetime, owner: str, source: dict) -> dict:
    """The grant for an approved call. ValueError with the reason when the guard could never honour it: nothing is
    written, and the decision stays open rather than being reported as approved."""
    now = at.timestamp()
    if source.get("via") == "slack":
        src = {"channel": source["channel"], "user": owner, "ts": source["ts"]}
    else:
        src = {"channel": "pane", "user": owner, "ts": "%.6f" % now}
    problem = grant_problem(d["kind"], d.get("target"), cfg)
    if problem:
        raise ValueError(problem)
    max_age = guard.grant_limits(cfg)[1]
    t = guard.parse_time(src["ts"])
    if t is None:
        raise ValueError("the reply's timestamp can't be read")
    if t + max_age * 3600 <= now:
        raise ValueError("your reply is older than the %s hours a grant may cite" % max_age)
    ends = [now + GRANT_HOURS * 3600, t + max_age * 3600]
    exp = guard.parse_time(d.get("expires"))
    if exp is not None:
        ends.append(exp)
    if min(ends) <= now:
        raise ValueError("the grant would already have expired")
    return {"scope": d["kind"], "target": d.get("target"), "thread": d.get("thread"), "uses": 1,
            "expires": grants.iso(min(ends)), "run": "chief:%s" % d["id"], "decision": d["id"],
            "draft_sha": d["draft_sha"], "source": src}


def _set_grant(ws: Path, did: str, g: Optional[dict]) -> Optional[str]:
    """One live grant per decision: replace any grant for `did` with `g`, or remove it when `g` is None. Returns the
    problem when grants.json can't be read (nothing is written). Holds the grants lock, inside the decisions lock."""
    with grants.locked(ws):
        gdoc, problem = grants.read(ws)
        if problem:
            return problem
        keep = [x for x in gdoc["grants"] if not (isinstance(x, dict) and x.get("decision") == did)]
        if g is not None or len(keep) != len(gdoc["grants"]):
            gdoc["grants"] = keep + ([g] if g is not None else [])
            grants.write(ws, gdoc)
    return None


def answer(ws: Path, cfg: dict, at: datetime, n: int, verb: str, payload: str, source: dict) -> str:
    """Apply a Chief pane answer to decision D<n>; the one-line result for the manager. A Slack reply goes
    through `answer_reply` instead. A yes writes a grant bound to the exact call (`draft_sha`), single-use and
    short-lived. Lock order: decisions, then grants, then the hub lock (enqueue, update_worker)."""
    if (source or {}).get("via") not in VIAS:
        return "D%d is still open: I couldn't confirm that answer came from you (unknown source)." % n
    return _answer(ws, cfg, at, n, verb, payload, source)[1]


def _answer(ws: Path, cfg: dict, at: datetime, n: int, verb: str, payload: str, source: dict,
            after_no: bool = False) -> Tuple[bool, str]:
    """`source` is already established by the caller: answer() for the pane, answer_reply() for a verified message."""
    ws = Path(ws)
    did = "D%d" % n
    if verb not in VERBS:
        return False, "%s is still open: I don't know the answer %r." % (did, verb)
    via = source["via"]
    with locked(ws):
        doc = load(ws)
        d = next((x for x in doc["decisions"] if x.get("id") == did), None)
        if d is None:
            return False, "%s isn't a decision I know." % did
        key = d.get("key")
        if after_no and d.get("status") == "rejected":
            chief.enqueue(ws, at, key, "The manager's words about %s, their instruction to you: %s" % (did, payload),
                          "decision")
            return True, "%s: your words went to %s." % (did, key)
        if d.get("status") == "pending":
            exp = guard.parse_time(d.get("expires"))
            if exp is not None and at.timestamp() >= exp:
                _expire(ws, doc, d, at)
        if d.get("status") != "pending":
            return False, "%s is already %s." % (did, d.get("status"))
        if chief.paused(ws):
            return False, "%s is still open: outbound is paused." % did
        if key not in chief.workers(ws):
            return False, "%s is still open: %s is no longer a live worker." % (did, key)
        rec = {"verb": verb, "via": via, "at": hub._iso(at)}
        if via == "slack":
            rec["ts"] = source.get("ts")
        if verb in ("edit", "say"):
            rec["payload"] = payload
        if verb == "edit":
            try:
                inp = signed_edit(d, cfg, payload)
            except ValueError as e:
                return False, "%s is still open: %s." % (did, e)
            kind, target, thread = kind_of(d["tool"], inp)
            d.update(input=inp, text=guard.text_of(inp), draft_sha=guard.draft_sha(d["tool"], inp), kind=kind,
                     target=target, thread=thread)
        if verb in ("ok", "edit") and d.get("kind") != "manual":
            owner = guard.owner_id(cfg)
            if not owner:
                return False, "%s is still open: config.json has no manager chat id." % did
            try:
                g = _grant(d, cfg, at, owner, source)
            except ValueError as e:
                return False, "%s is still open: %s. Answer again." % (did, e)
            problem = _set_grant(ws, did, g)
            if problem:
                return False, "%s is still open: %s." % (did, problem)
            d.update(status="approved", answer=rec, grant_expires=g["expires"])
            save(ws, doc)
            chief.enqueue(ws, at, key, "%s approved by the manager. Make exactly this one call now, with this input "
                                       "unchanged, then end the turn.\nTool: %s\nInput: %s"
                          % (did, d["tool"], canonical(d["input"])), "decision")
            _resume(ws, key)
            return True, _approved_line(ws, cfg, at, did, key)
        problem = _set_grant(ws, did, None)  # no live grant survives an answer that doesn't send
        if problem:
            return False, "%s is still open: %s." % (did, problem)
        if verb in ("ok", "edit"):
            d.update(status="manual", answer=rec)
            save(ws, doc)
            _resume(ws, key)
            return True, "%s is yours to do by hand (%s isn't something I can send): %s. The full text is in the Chief " \
                         "pane." % (did, chief.safe_line(d["tool"], 80), chief.safe_line(d.get("text"), PREVIEW_MAX))
        if verb == "no":
            d.update(status="rejected", answer=rec)
            text = "%s was rejected by the manager. Don't make that call." % did
            result = "%s rejected: %s won't make that call." % (did, key)
        else:
            d.update(status="redirected", answer=rec)
            text = "The manager's words about %s, their instruction to you: %s" % (did, payload)
            result = "%s: your words went to %s." % (did, key)
        save(ws, doc)
        chief.enqueue(ws, at, key, text, "decision")
        _resume(ws, key)
        return True, result


def _approved_line(ws: Path, cfg: dict, at: datetime, did: str, key: str) -> str:
    """An approval's result, which promises the call only when a run can start. Saying so can't undo the approval."""
    try:
        why = "it's held" if (chief.workers(ws).get(key) or {}).get("held") else chief.refusal(ws, cfg, at)
    except Exception as e:
        _log(ws, "approved %s: %r" % (did, e))
        why = None
    if why:
        return "%s approved, but %s can't run yet (%s). If that outlasts the approval, nothing goes out, and a later " \
               "batch says so." % (did, key, why)
    return "%s approved: %s will make that one call." % (did, key)


def _expire(ws: Path, doc: dict, d: dict, at: datetime) -> None:
    """A pending decision ran out of time. The caller holds `locked(ws)`."""
    did, key = d.get("id"), d.get("key")
    _set_grant(ws, did, None)  # a grant never outlives its decision, so a read problem here is harmless
    d["status"] = "expired"
    doc["confirm"].append("%s expired unanswered." % did)
    save(ws, doc)
    if key in chief.workers(ws):
        chief.enqueue(ws, at, key, "%s expired unanswered. Don't make that call." % did, "decision")
        _resume(ws, key)


def _log(ws: Path, msg: str) -> None:
    """Failures the manager needn't read in the DM: chief/decisions.log. Writing it can't fail a tick."""
    try:
        with open(str(chief.chief_dir(ws) / "decisions.log"), "a") as f:
            f.write("%s %s\n" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg))
    except OSError:
        pass


def _close_one(ws: Path, doc: dict, d: dict, at: datetime, used: set, live: dict, busy: set, queue: list,
               cfg: Optional[dict] = None) -> None:
    """Close one approved decision when what happened to it is known. The ledger proves the guard let the call through,
    not that the service accepted it, so the wording says only that."""
    did, key = d.get("id"), d.get("key")
    w = live.get(key)
    waiting = any(e.get("key") == key and e.get("source") == "decision"
                  and str(e.get("text")).startswith("%s approved by" % did) for e in queue)
    made = "chief:%s" % did in used
    if made and waiting:  # the grant is spent, so "make this call" can only be refused: it mustn't hold this open
        chief.drop_queued(ws, key, "%s approved by" % did)
        waiting = False
    ended = w is None or not (key in busy or w.get("status") == "working" or waiting)
    if made:
        if ended:
            d["status"] = "sent"
            doc["confirm"].append("✓ %s: the call was made (%s on %s)" % (did, d.get("kind"), plain_target(d)))
            save(ws, doc)
        return
    ends = guard.parse_time(d.get("grant_expires"))
    if ends is None:
        said = guard.parse_time((d.get("answer") or {}).get("at"))
        ends = said + GRANT_HOURS * 3600 if said is not None else at.timestamp()
    if at.timestamp() >= ends:
        _set_grant(ws, did, None)
        d["status"] = "expired"
        doc["confirm"].append("%s: approved but not used in time; nothing went out" % did)
        save(ws, doc)
        if w is not None:
            chief.drop_queued(ws, key, "%s approved by" % did)  # a dead instruction: the guard would refuse the call
            chief.enqueue(ws, at, key, "%s's approval ran out before the call was made. Don't make that call; file it "
                                       "again if it's still needed." % did, "decision")
            if w.get("status") != "working":
                _resume(ws, key)
        return
    if not ended:
        return
    _set_grant(ws, did, None)
    d["status"] = "failed"
    why = grant_problem(d.get("kind"), d.get("target"), cfg) if cfg is not None else None
    if w is None:
        doc["confirm"].append("%s: the worker is gone and the call wasn't made; nothing went out" % did)
        note = ""
    elif w.get("status") == "failed":
        doc["confirm"].append("%s: the worker failed after your yes; it's back with the worker" % did)
        note = "%s: your run failed after the manager's yes, so the call wasn't made. File it again if it's still " \
               "needed." % did
    elif why:
        doc["confirm"].append("%s: %s; nothing went out, and it's back with the worker." % (did, why))
        note = "%s: %s, so nothing went out. Filing it again won't help unless that changes." % (did, why)
    else:
        doc["confirm"].append("%s: the worker's turn ended without the call you approved; nothing went out, and it's "
                              "back with the worker." % did)
        note = "%s: your turn ended without the approved call, so nothing went out. The approval covered that exact " \
               "call, once. File it again if it's still needed." % did
    save(ws, doc)
    if w is not None:
        chief.enqueue(ws, at, key, note, "decision")
        _resume(ws, key)


def settle(ws: Path, at: datetime, cfg: Optional[dict] = None) -> None:
    """The tick's pass over the queue: expire what ran out of time, and close each approved decision once its outcome is
    known, so nothing stays approved past its grant. A run has ended when the worker is gone, or its run lock is free,
    it isn't marked working and its approval has left the queue. A ledger that can't be read says nothing, so those
    decisions wait for it. One bad decision is logged and can't stop the others."""
    ws = Path(ws)
    with locked(ws):
        doc = load(ws)
        for d in doc["decisions"]:
            try:
                exp = guard.parse_time(d.get("expires"))
                if d.get("status") == "pending" and exp is not None and at.timestamp() >= exp:
                    _expire(ws, doc, d, at)
            except Exception as e:
                _log(ws, "settle %s: %r" % (d.get("id"), e))
        approved = [d for d in doc["decisions"] if d.get("status") == "approved"]
        if not approved:
            return
        with guard.Ledger(str(ws)) as led:
            book = led.read()
        if book is None:
            return
        used = {r.get("run") for r in book["uses"] if isinstance(r, dict) and isinstance(r.get("run"), str)}
        # the queue first: a run that claims an approval between the reads then shows as running, never as ended
        queue = chief._read_queue(ws)
        live, busy = chief.workers(ws), set(chief.running(ws))
        for d in approved:
            try:
                _close_one(ws, doc, d, at, used, live, busy, queue, cfg)
            except Exception as e:
                _log(ws, "settle %s: %r" % (d.get("id"), e))


# ---- the tick's replies: a manager's note in the self-DM that answers the queue --------------------------------

REPLIED_KEEP = 200
REPLY_TRIES = 3
RETRY_FIRST_SECONDS = 60
RETRY_MAX_SECONDS = 30 * 60
VOID_KEEP = 50
READ_FAILED = "Slack read failed; I'll apply your reply when I can read it"
READ_GAVE_UP = "Slack still can't be read and no decision your message could answer is open, so it wasn't applied."
READ_LATE = "I read your message late: it isn't an answer to a decision, so nothing was applied. Send it again if it " \
            "was meant for me."
COUNTERFEIT = "A message in this DM that looked like a decision batch didn't come from Mother, so replies to %s after " \
              "it were held. Answer %s again from this batch."
_INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff]")
_ANY_ID = re.compile(r"\bD(\d+)\b", re.I)
_RECOMMEND = re.compile(r"\brecommend", re.I)
_FOOTER = re.compile(r"reply in this dm|\bok all\b.*\bd?\d+\s+(?:ok|no)\b|\bd?\d+\s+(?:ok|no)\b.*\bok all\b", re.I | re.S)


def is_reply_line(l: dict) -> bool:
    """A self-DM note whose summary starts like a reply. Only a `chat` line can be re-read from Slack: a `manual`
    source line came from a CLI verb any session can run. This only pre-filters: `answer_reply` reads the message."""
    return l.get("kind") == "manual" and l.get("source") == "chat" and parse_reply(l.get("summary"), set()) is not None


def for_chief(ws: Path, lines: List[dict]) -> List[dict]:
    """The lines the chief may judge, in order: replies are the tick's and are left out, and it all stops at a line
    still waiting on a Slack read. Past that line the chief's decide could move the cursor over it, and a note must
    never vanish. The hold lasts at most REPLY_TRIES ticks."""
    seen = load(ws)["replied"]
    out = []
    for l in lines:
        if is_reply_line(l):
            v = seen.get(l["id"])
            if v is True:
                continue
            if type(v) is int and v >= REPLY_TRIES:  # an outage can last hours: the reply waits, the rest don't
                continue
            if v is not False:
                break
        out.append(l)
    return out


def _record_reply(ws: Path, lid: str, state, lines: List[str]) -> None:
    with locked(ws):
        doc = load(ws)
        doc["replied"][lid] = state
        doc["replied"] = dict(list(doc["replied"].items())[-REPLIED_KEEP:])
        doc["retry_at"].pop(lid, None)
        doc["confirm"].extend(lines)
        save(ws, doc)


def reply_channel(ws: Path, cfg: dict) -> Tuple[Optional[str], Optional[str]]:
    """(the D id a self-DM reply is re-read from, what stops replies from working). An inbox line names no channel, and
    two settings say which one it is: the watcher reads `follow.inbox` in watch.json, and the batch goes to the D id in
    manager.chat_dm. When they differ, every reply fails closed, so the Chief pane says so."""
    ds = sorted(str(x) for x in guard.dm_ids(cfg) if str(x).startswith("D"))
    inbox = hub.merged_watch(cfg, hub.load_watch(ws))["chat"].get("inbox") or ""
    if not ds:
        return None, "config.json has no D… channel in manager.chat_dm, so no batch can be posted and no reply confirmed"
    if not inbox:
        return ds[0], "the watcher has no self-DM inbox, so replies in the DM never arrive: run `mother hub dms inbox %s`" % ds[0]
    if inbox not in ds:
        return None, "the watcher reads %s but manager.chat_dm says %s, so no reply can be confirmed: make them the same" \
            % (chief.safe_line(inbox, 40), ds[0])
    if len(ds) > 1:
        return inbox, "manager.chat_dm lists several D… channels: the batch goes to the first, and replies are read in %s" % inbox
    return inbox, None


def _record_failed_read(ws: Path, at: datetime, lid: str, ts: str, why: str) -> None:
    """One more failed read of a reply. It stays pending while a decision it could answer does: the manager is told once
    at REPLY_TRIES, and the reads back off from there."""
    _log(ws, "reply %s: read failed: %s" % (lid, why))
    with locked(ws):
        doc = load(ws)
        tries = doc["replied"].get(lid, 0) + 1
        doc["replied"][lid] = tries
        doc["replied"] = dict(list(doc["replied"].items())[-REPLIED_KEEP:])
        if tries >= REPLY_TRIES:
            if _reply_open_ids(ws, ts):
                if tries == REPLY_TRIES:
                    doc["confirm"].append(READ_FAILED)
                wait = min(RETRY_MAX_SECONDS, RETRY_FIRST_SECONDS * 2 ** min(tries - REPLY_TRIES, 16))
                doc["retry_at"][lid] = hub._iso(at + timedelta(seconds=wait))
            else:
                doc["replied"][lid] = False
                doc["retry_at"].pop(lid, None)
                doc["confirm"].append(READ_GAVE_UP)
        save(ws, doc)


def _text_of_message(msg: dict) -> str:
    """All a message shows, as plain characters: its text, what Slack escaped undone, invisible characters gone, and any
    blocks or attachments, which is what Slack draws when they are there."""
    parts = [_norm(msg.get("text"))]
    for k in ("blocks", "attachments"):
        if msg.get(k):
            parts.append(json.dumps(msg[k], ensure_ascii=False))
    return _INVISIBLE.sub("", "\n".join(parts))


def rows_named(msg: dict) -> Optional[List[str]]:
    """The ids a message names when it looks like a decision batch (`D<n>` with `recommend`, or the Reply footer), else
    None. A pattern is a net, not a proof: the guard's rule on unattended sessions is the other half."""
    text = _text_of_message(msg)
    ids = ["D%s" % n for n in _ANY_ID.findall(text)]
    if (ids and _RECOMMEND.search(text)) or _FOOTER.search(text):
        return sorted(set(ids), key=lambda i: int(i[1:]))
    return None


def _voided_at(doc: dict, ts: str) -> Set[str]:
    """The ids a reply typed at `ts` can't answer: a counterfeit named them before it, and no real batch came between."""
    at, out = guard.parse_time(ts), set()
    for v in doc["void"]:
        began, ended = guard.parse_time(v.get("from")), guard.parse_time(v.get("until"))
        if at is not None and began is not None and began < at and (ended is None or at < ended):
            out.update(i for i in v["ids"] if isinstance(i, str))
    return out


def _scan_from(doc: dict, first_reply: float) -> Optional[str]:
    """Where a scan starts: the last real batch before the earliest reply, so a counterfeit a late line was typed after
    is still seen. With none recorded, the first announcement. None: nothing is announced, so no reply can apply."""
    posts = [x["ts"] for x in doc["batch"].get("posts") or [] if (guard.parse_time(x["ts"]) or 0) < first_reply]
    if posts:
        return max(posts, key=guard.parse_time)
    open_ = {d.get("id") for d in doc["decisions"] if d.get("status") == "pending"}
    stamps = [guard.parse_time(v) for k, v in doc["batch"]["announced"].items() if k in open_]
    stamps = [t for t in stamps if t is not None]
    return "%.6f" % min(stamps) if stamps else None


def scan_counterfeits(ws: Path, channel: str, history, first_reply: float) -> None:
    """Find app-posted messages in the self-DM that look like a decision batch and aren't one, before any reply is
    applied. Each is recorded once and told once. A normal session keeps its green post to the DM, so a reply can't
    assume what it answers came from `batch`. Raises when the DM can't be read: the replies then wait."""
    with locked(ws):
        doc = load(ws)
        since = _scan_from(doc, first_reply)
    if since is None:
        return
    msgs = history(channel, since)
    with locked(ws):
        doc = load(ws)
        b = doc["batch"]
        known = [t for x in b.get("posts") or [] for t in x["texts"] if isinstance(t, str)]
        known += [o["text"] for o in b.get("offers") or []]
        seen = {v["from"] for v in doc["void"]}
        open_ = {d.get("id") for d in doc["decisions"] if d.get("status") == "pending"}
        changed = False
        for m in sorted((m for m in msgs if isinstance(m.get("ts"), str)), key=lambda m: guard.parse_time(m["ts"]) or 0):
            if m["ts"] in seen or not _app_posted(m) or (_plain(m) and any(_same_text(t, m.get("text")) for t in known)):
                continue
            ids = rows_named(m)
            if ids is None:
                continue
            ids = [i for i in ids if i in open_] or ([] if ids else sorted(open_, key=lambda i: int(i[1:])))
            if not ids:  # it names only closed rows: no reply can reach them
                continue
            start = guard.parse_time(m["ts"]) or 0
            later = [x["ts"] for x in b.get("posts") or [] if (guard.parse_time(x["ts"]) or 0) > start]
            doc["void"].append({"from": m["ts"], "ids": ids,
                                "until": min(later, key=guard.parse_time) if later else None})
            doc["confirm"].append(COUNTERFEIT % ((", ".join(ids),) * 2))
            changed = True
        if changed:
            doc["void"] = doc["void"][-VOID_KEEP:]
            save(ws, doc)


def apply_replies(ws: Path, cfg: dict, at: datetime, lines: List[dict]) -> Set[str]:
    """Apply every reply among `lines`, each once, even one the cursor can't reach yet. Result lines go to `confirm`.
    Returns the ids to log: the replies that reached the queue, or were held. The self-DM is scanned for counterfeits
    first. A failed Slack read is tried again next tick, `for_chief` stopping there, and after REPLY_TRIES the manager is
    told once and the reads back off until no decision it could answer is left; `for_chief` then passes it by. Anything
    else keeps its normal path: a note that isn't a reply, and one that only starts like a reply, which gets the help
    line. One bad line is logged and can't stop the others."""
    ws = Path(ws)
    channel, problem = reply_channel(ws, cfg)
    candidates = [l for l in lines if is_reply_line(l)]
    doc = load(ws)
    seen = doc["replied"]
    now = at.timestamp()
    todo = [l for l in candidates if not isinstance(seen.get(l["id"]), bool)
            and (guard.parse_time(doc["retry_at"].get(l["id"])) or 0) <= now]
    if todo:
        with _slack_session(cfg) as fetch:
            unread = None
            stamps = [guard.parse_time(l["id"].rsplit(":", 1)[-1]) for l in todo]
            stamps = [t for t in stamps if t is not None]
            if channel and stamps:
                try:
                    scan_counterfeits(ws, channel, fetch.history, min(stamps))
                except Exception as e:
                    unread = "Slack couldn't be read: %s" % (str(e) or type(e).__name__)
            for l in todo:
                lid = l["id"]
                ts = lid.rsplit(":", 1)[-1]
                try:
                    if not channel:  # the config is what's wrong, not where the manager typed
                        state, got = "unconfirmed", [UNCONFIRMED % (problem or "no self-DM channel")]
                    elif unread:
                        state, got = "retry", [unread]
                    else:
                        state, got = _reply(ws, cfg, at, channel, ts, fetch)
                    if state == "retry":
                        _record_failed_read(ws, at, lid, ts, got[0])
                        continue
                    if state == "not_reply" and type(seen.get(lid)) is int and seen[lid] >= REPLY_TRIES:
                        got = [READ_LATE]  # the chief was let past it, so it may never see this one
                    _record_reply(ws, lid, state in ("applied", "held"), got)
                except Exception as e:
                    _log(ws, "reply %s: %r" % (lid, e))
                    try:
                        _record_reply(ws, lid, False, [UNREADABLE])
                    except Exception as e2:
                        _log(ws, "reply %s: %r" % (lid, e2))
    seen = load(ws)["replied"]
    return {l["id"] for l in candidates if seen.get(l["id"]) is True}


def waiting_reads(ws: Path) -> Set[str]:
    """Lines with a failed read still to retry, wherever the cursor is now."""
    return {k for k, v in load(ws)["replied"].items() if type(v) is int}


def stray_replies(ws: Path, behind: Set[str]) -> Set[str]:
    """Applied replies among `behind` (lines the cursor has passed): the chief's decide jumped them, so the routed log
    has no entry yet."""
    return {k for k, v in load(ws)["replied"].items() if v is True and k in behind}


def forget_replies(ws: Path, ids: Set[str]) -> None:
    if not ids:
        return
    with locked(ws):
        doc = load(ws)
        doc["replied"] = {k: v for k, v in doc["replied"].items() if k not in ids}
        doc["retry_at"] = {k: v for k, v in doc["retry_at"].items() if k not in ids}
        save(ws, doc)


# ---- the batch: what the manager's DM hears from the queue ------------------------------------------------------

WHY_MAX = 100
CONFIRM_MAX = 400  # a manual result line and its pointer to the pane fit
ALSO_SENDS_MAX = 5
OFFERS_KEEP = 5
POSTS_KEEP = 5
NO_OFFER = "no batch is waiting to be recorded: `mother decide batch` makes one"
_SLACK_TS = re.compile(r"^\d+(?:\.\d+)?$")


def _shown(text, cap: int) -> str:
    """Text a worker or a service wrote, as one line of a Slack message: capped, and it can't start a row or draw a
    link, a `<!here>` or an @mention."""
    return chief.safe_line(text, cap).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") \
        .replace("@", "@\u200b")


def _preview(inp) -> str:
    """The draft as a row shows it: the main text, saying when it is cut, and naming any other field that goes out. A
    reply approves what the manager read, so they must know when they read part of it."""
    fields = draft_fields(inp)
    if not fields:
        return '""'
    names = [p + k for k in TOP_TEXT_KEYS + ("blocks",) + NESTED_TEXT_KEYS for p in ("", "inputs.")]
    main = next((f for n in names for f in fields if f[0] == n), fields[0])
    flat = chief.safe_line(main[1], len(main[1]) + 1)
    out = '"%s"' % _shown(flat, PREVIEW_MAX)
    if len(flat) > PREVIEW_MAX:
        out += " … +%d chars, see the pane" % (len(flat) - PREVIEW_MAX)
    others = [f[0] for f in fields if f is not main]
    if others:
        shown = [_shown(n, 60) for n in others[:ALSO_SENDS_MAX]]
        if len(others) > ALSO_SENDS_MAX:
            shown.append("+%d more" % (len(others) - ALSO_SENDS_MAX))
        out += " · also sends: %s" % ", ".join(shown)
    return out


def _done_without_you(ws: Path, doc: dict, last: Optional[datetime]) -> int:
    """Workers that ran since the last batch and rest done or idle, with no decision open or filed since then."""
    n = 0
    for key, w in chief.workers(ws).items():
        ran = hub._parse_at(w.get("last_run_at"))
        if ran is None or (last is not None and ran <= last) or w.get("status") not in ("done", "idle"):
            continue
        filed = [d for d in doc["decisions"] if d.get("key") == key]
        if any(d.get("status") in ("pending", "approved") or last is None
               or (hub._parse_at(d.get("created")) or last) > last for d in filed):
            continue
        n += 1
    return n


def batch(ws: Path, cfg: dict, at: datetime) -> Optional[dict]:
    """{"text", "ids"} when a message is due for the manager's DM, else None. Due: an unannounced pending decision
    once batch_minutes have passed since the last batch, or at once when one is urgent; or any confirm line. It lists
    every pending decision, already announced or not: `ok all` reaches them all, so the manager sees them all."""
    ws = Path(ws)
    if chief.paused(ws):
        return None
    doc = load(ws)
    open_ = [d for d in doc["decisions"] if d.get("status") == "pending"]
    fresh = [d for d in open_ if d.get("id") not in doc["batch"]["announced"]]
    last = hub._parse_at(doc["batch"]["last_at"])
    wait = chief.settings(cfg)["batch_minutes"] * 60
    due = bool(fresh) and (last is None or (at - last).total_seconds() >= wait or any(d.get("urgent") for d in fresh))
    if not due and not doc["confirm"]:
        return None
    rows = ["%d to decide · %d done without you" % (len(open_), _done_without_you(ws, doc, last))]
    for d in open_:
        why = _shown(d.get("why"), WHY_MAX)
        rows.append("%s · %s · %s on %s: %s%s · recommend: %s" % (
            d.get("id"), _shown(d.get("key"), WHY_MAX), _shown(d.get("kind"), WHY_MAX), _shown(plain_target(d), 200),
            _preview(d.get("input")), " · why: %s" % why if why else "",
            _shown(d.get("recommendation"), WHY_MAX)))
    rows.extend(_shown(c, CONFIRM_MAX) for c in doc["confirm"])
    if open_:
        rows.append("Reply in this DM, not in a thread: 7 ok · 7 no · 7 edit: your text · ok all")
    return {"text": "\n".join(rows), "ids": [d.get("id") for d in open_]}


def batch_key(got: Optional[dict]) -> Optional[str]:
    """What a batch says, as a short hash: `mother chief tick` hands it to the mod, which counts posts per offer content
    and must see a change without making an offer. None when nothing is due."""
    return hashlib.sha256(got["text"].encode("utf-8")).hexdigest()[:16] if got else None


def offer(ws: Path, cfg: dict, at: datetime) -> Optional[dict]:
    """`batch`, recorded as the message about to be posted: `sent` stamps only these ids."""
    ws = Path(ws)
    with locked(ws):
        got = batch(ws, cfg, at)
        if got is not None:
            doc = load(ws)
            # with an offer still live, its post may be out: a later `sent` clears only what both carried. A line shown
            # twice beats a line lost.
            b = doc["batch"]
            b["offered"] = min(b.get("offered", len(doc["confirm"])), len(doc["confirm"]))
            b["offered_ids"] = list(got["ids"])
            b["offered_at"] = hub._iso(at)
            b["offered_text"] = got["text"]
            b["offers"] = (b.get("offers") or [])[-(OFFERS_KEEP - 1):] + [
                {"text": got["text"], "ids": list(got["ids"]), "at": b["offered_at"]}]
            save(ws, doc)
    return got


class SlackReadFailed(Exception):
    """The post couldn't be checked: worth trying again, unlike a post that isn't there."""


EMOJI_SLOT = "\ufffc"  # stands for any emoji, however Slack spelt it, when comparing a post with what was offered
_WIRE_LINK = re.compile(r"<((?:https?|mailto|tel):[^|>\s]*)(?:\|([^>]*))?>")
# Unicode's emoji: everything in the pictograph blocks, and the BMP characters that can be one. Not ✓ or → or the like.
_EMOJI_CHARS = "\U0001F000-\U0001FAFF\u00A9\u00AE\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA\u231A\u231B\u2328\u23CF" \
               "\u23E9-\u23F3\u23F8-\u23FA\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2600-\u2604\u260E\u2611\u2614\u2615" \
               "\u2618\u261D\u2620\u2622\u2623\u2626\u262A\u262E\u262F\u2638-\u263A\u2640\u2642\u2648-\u2653\u265F\u2660" \
               "\u2663\u2665\u2666\u2668\u267B\u267E\u267F\u2692-\u2697\u2699\u269B\u269C\u26A0\u26A1\u26A7\u26AA\u26AB\u26B0" \
               "\u26B1\u26BD\u26BE\u26C4\u26C5\u26C8\u26CE\u26CF\u26D1\u26D3\u26D4\u26E9\u26EA\u26F0-\u26F5\u26F7-\u26FA\u26FD" \
               "\u2702\u2705\u2708-\u270D\u270F\u2712\u2714\u2716\u271D\u2721\u2728\u2733\u2734\u2744\u2747\u274C\u274E" \
               "\u2753-\u2755\u2757\u2763\u2764\u2795-\u2797\u27A1\u27B0\u27BF\u2934\u2935\u2B05-\u2B07\u2B1B\u2B1C\u2B50\u2B55" \
               "\u3030\u303D\u3297\u3299"
_UNI_EMOJI = "(?:[%s]|[0-9#*]\ufe0f?\u20e3)[\U000E0020-\U000E007F]*" % _EMOJI_CHARS
# A name holds a letter, or it's one of these: `:30:` in 10:30:45 is a time, and two times must not compare equal.
_CODE = r":(?:[a-z0-9_+'\-]*[a-z][a-z0-9_+'\-]*|\+1|-1|100|1234):(?::skin-tone-[2-6](?:-[2-6])?:)?"
_JOIN = "[\u200d\ufe0f]*"
_EMOJI_RUN = re.compile(r"(?:(?<![\w:])%s(?!\w)|%s)(?:%s(?:%s(?!\w)|%s))*%s" % (_CODE, _UNI_EMOJI, _JOIN, _CODE, _UNI_EMOJI, _JOIN))
_GLUED_SLOT = "(?:%s|%s%s)+" % (EMOJI_SLOT, _CODE, _JOIN)
_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))


def _unescaped(t: str) -> str:
    for ent, ch in _ENTITIES:
        t = t.replace(ent, ch)
    return t


def _unwired(m) -> str:
    return m.group(2) or m.group(1)


def _norm(text) -> str:
    """A message's text in the one form a post and an offer are compared in, whatever Slack did to it: `<url|label>` as
    its label, `<url>` as the url, entities undone (however often the path to Slack escaped them), every emoji spelling
    as one EMOJI_SLOT, and no trailing spaces on a line. A zero-width space stays: Slack keeps it. For comparing only:
    `slack_text` is what a reader gets."""
    t = text if isinstance(text, str) else ""
    for _ in range(3):
        u = _unescaped(_WIRE_LINK.sub(_unwired, t))
        if u == t:
            break
        t = u
    t = _EMOJI_RUN.sub(EMOJI_SLOT, t)
    return "\n".join(l.rstrip() for l in t.replace("\r\n", "\n").split("\n")).strip()


def _same_text(offered, stored) -> bool:
    """`stored` says what `offered` said. A shortcode touching a word ("Done:white_check_mark:") reads as an emoji only
    where the offer has one: anywhere else `key:one:two` is words, and words must match."""
    a, b = _norm(offered), _norm(stored)
    if a == b:
        return True
    return EMOJI_SLOT in a and re.fullmatch(_GLUED_SLOT.join(map(re.escape, a.split(EMOJI_SLOT))), b) is not None


def _app_posted(msg: dict) -> bool:
    return bool(msg.get("app_id") or msg.get("bot_id") or msg.get("subtype") == "bot_message")


def _bare_url(u: str) -> str:
    return re.sub(r"^(?:https?://|mailto:|tel:)", "", _unescaped(u or ""), flags=re.I).rstrip("/").lower()


def _is_preview(att, text: str) -> bool:
    """A link preview of a link in the message's own text, which Slack adds after the post and nobody wrote. `edited`
    comes with it, so `edited` on a post means nothing."""
    if not isinstance(att, dict) or not (att.get("is_app_unfurl") is True or att.get("from_url")):
        return False
    links = {u.rstrip("/") for u in re.findall(r"(?:https?://|mailto:|tel:)[^\s|<>]+", _unescaped(text))}
    return any(isinstance(att.get(k), str) and _unescaped(att[k]).rstrip("/") in links
               for k in ("app_unfurl_url", "from_url", "original_url"))


def _plain(msg: dict) -> bool:
    """It draws what its `text` says: no attachments but a preview of its own links, no blocks but the rich_text Slack adds
    on its own, and no link labelled as anything but its address (an offer has none, and Slack's auto-links are that)."""
    blocks = msg.get("blocks") or []
    text = msg.get("text") if isinstance(msg.get("text"), str) else ""
    atts = msg.get("attachments") or []
    return isinstance(atts, list) and all(_is_preview(a, text) for a in atts) and isinstance(blocks, list) \
        and all(isinstance(x, dict) and x.get("type") == "rich_text" for x in blocks) \
        and all(not m.group(2) or _bare_url(m.group(2)) == _bare_url(m.group(1)) for m in _WIRE_LINK.finditer(text))


def _batch_channel(cfg: dict) -> Optional[str]:
    """The D… channel a batch goes to: the first in manager.chat_dm, as the Chief pane's mod picks it."""
    m = cfg.get("manager") or {}
    ids = list(guard.as_list(m.get("chat_dm"))) + list(guard.as_list(m.get("chat_user")))
    return next((x for x in ids if isinstance(x, str) and re.match(r"^D[A-Z0-9]+$", x)), None)


def record_post(ws: Path, cfg: dict, at: datetime, ids: List[str], ts: Optional[str], fetch=None) -> None:
    """`mother decide sent`: re-read the message at `ts` in the self-DM and stamp only if it is app-posted and says what
    was offered. A stamp is what lets a reply reach a decision, so it must prove a post. ValueError when the proof fails,
    SlackReadFailed when it can't be looked at. Nothing is written either way, and the offer stays live."""
    if ts is None or not _SLACK_TS.match(str(ts)):
        raise ValueError("--ts must be the Slack ts of the batch you posted: it is re-read before anything is recorded")
    ws = Path(ws)
    with locked(ws):
        if "offered" not in load(ws)["batch"]:
            raise ValueError(NO_OFFER)
    channel = _batch_channel(cfg)
    if not channel:
        raise ValueError("config.json has no D… channel for the manager, so the post can't be checked")
    try:
        msg = (fetch or _fetch_slack)(cfg, channel, str(ts))
    except Exception as e:
        raise SlackReadFailed("couldn't read %s in %s to check the post (%s); nothing was recorded, try again" % (
            ts, channel, str(e) or type(e).__name__))
    if not isinstance(msg, dict) or msg.get("ts") != str(ts):
        raise ValueError("Slack shows no message at %s in %s, so nothing was recorded. Use the ts of the batch you posted"
                         % (ts, channel))
    if not _app_posted(msg):
        raise ValueError("the message at %s wasn't posted by the app, so it isn't the batch; nothing was recorded" % ts)
    if not _plain(msg):
        raise ValueError("the message at %s carries blocks, attachments or a relabelled link, so it isn't the batch; nothing was recorded" % ts)
    sent(ws, at, ids, str(ts), posted=msg.get("text"))


def sent(ws: Path, at: datetime, ids: List[str], ts: Optional[str] = None, posted: Optional[str] = None) -> None:
    """Record a posted batch; the CLI's way here is `record_post`, which proves the post first. `ids` count as announced
    at `ts`, the post's own Slack ts (now when none is given). They must be ids the offer handed out, the one whose text
    is `posted` when given (a late post can prove an earlier offer) and the last one otherwise, and the stamp is never
    earlier than that offer: the manager can only answer what they saw. A decision keeps its first stamp, so a reply to an
    earlier batch stays good. Ends the void of any counterfeit before it. Clears the confirm lines that offer handed
    out; a line that arrived while the post was going out stays. Uses the offer up. ValueError with no live offer, on a
    `ts` that isn't a Slack ts, on text no offer had, or on an id that wasn't offered: nothing is written."""
    if ts is not None and not _SLACK_TS.match(str(ts)):
        raise ValueError("--ts must be the post's Slack ts")
    ws = Path(ws)
    with locked(ws):
        doc = load(ws)
        open_ = {d.get("id") for d in doc["decisions"] if d.get("status") == "pending"}
        b = doc["batch"]
        if "offered" not in b:
            raise ValueError(NO_OFFER)
        offers = b.get("offers") or []
        shown, offered_at = b.get("offered_ids") or [], b.get("offered_at")
        if posted is not None:
            match = next((o for o in reversed(offers) if _same_text(o["text"], posted)), None)
            if match is None:
                raise ValueError("the message at %s isn't the batch `mother decide batch` offered: its text differs. "
                                 "Nothing was recorded" % ts)
            shown, offered_at = match["ids"], match["at"]
        extra = [i for i in ids if i not in shown]
        if extra:
            raise ValueError("%s wasn't in the batch `mother decide batch` offered" % ", ".join(extra))
        stamp = str(ts) if ts is not None else hub._iso(at)
        b.pop("offered_at", None)
        b.pop("offered_text", None)
        b.pop("offers", None)
        floor = guard.parse_time(offered_at)
        if floor is not None and guard.parse_time(stamp) < floor:
            stamp = offered_at
        b.pop("offered_ids", None)
        announced = {k: v for k, v in b["announced"].items() if k in open_}
        for i in ids:
            if i in open_:
                announced.setdefault(i, stamp)
        b["announced"] = announced
        b["last_at"] = hub._iso(at)
        doc["confirm"] = doc["confirm"][b.pop("offered"):]
        if ts is not None:
            b["posts"] = (b.get("posts") or [])[-(POSTS_KEEP - 1):] + [{"ts": str(ts), "texts": [o["text"] for o in offers]}]
            for v in doc["void"]:
                if v.get("until") is None and (guard.parse_time(v.get("from")) or 0) < (guard.parse_time(str(ts)) or 0):
                    v["until"] = str(ts)
        save(ws, doc)


def _resume(ws: Path, key: str) -> None:
    """The worker leaves waiting_decision unless another decision of its is still open; normal dispatch picks up what
    was queued."""
    try:
        chief.update_worker(ws, key, status=chief.rest_status(ws, key))
    except KeyError:
        pass


def _opts(args, valued, flags):
    pos, o, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and a[2:] in valued:
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % a)
            o[a[2:]] = args[i + 1]
            i += 2
        elif a.startswith("--") and a[2:] in flags:
            o[a[2:]] = True
            i += 1
        elif a.startswith("--"):
            raise ValueError("unknown option %s" % a)
        else:
            pos.append(a)
            i += 1
    return pos, o


def main(args, ws, cfg, out=sys.stdout, err=sys.stderr, at: Optional[datetime] = None) -> int:
    verb, rest = (args[0], args[1:]) if args else ("", [])
    if verb in ("", "help", "-h", "--help"):
        print(USAGE, file=out)
        return 0
    if verb == "answer":
        return _main_answer(rest, ws, cfg, out, err, at)
    if verb in ("batch", "sent"):
        return _main_batch(verb, rest, ws, cfg, out, err, at)
    if verb != "propose":
        print("usage:\n" + USAGE, file=err)
        return 2
    try:
        pos, o = _opts(rest, ("tool", "recommend", "why"), ("urgent", "stdin"))
    except ValueError as e:
        print("mother decide: %s" % e, file=err)
        return 2
    if len(pos) != 1 or not pos[0] or not o.get("tool") or not o.get("recommend") or not o.get("stdin"):
        print("usage:\n" + USAGE, file=err)
        return 2
    me = os.environ.get(guard.CHIEF_WORKER_ENV, "").strip()
    if me and me != pos[0]:  # the approval's instruction would land with the other worker
        print("mother decide: you are %s, so file under %s, not %s" % (me, me, pos[0]), file=err)
        return 1
    try:
        inp = json.loads(sys.stdin.read())
    except ValueError:
        print("mother decide: stdin is not JSON", file=err)
        return 2
    if not isinstance(inp, dict):
        print("mother decide: stdin must be a JSON object (the tool's input)", file=err)
        return 2
    try:
        d = propose(Path(ws), cfg or {}, at or datetime.now(timezone.utc), pos[0], o["tool"], inp, o["recommend"],
                    why=o.get("why", ""), urgent=bool(o.get("urgent")))
    except ValueError as e:
        print("mother decide: %s" % e, file=err)
        return 1
    print(json.dumps({"ok": True, "decision": d}), file=out)
    return 0


def _main_batch(verb, rest, ws, cfg, out, err, at) -> int:
    """mother decide batch [--json] | sent --ts TS [--ids D7,D8|'']. Refused in a worker's process: announcing is what
    lets a reply reach a decision, so a worker must not be able to mark its own draft as seen."""
    if os.environ.get(guard.CHIEF_WORKER_ENV, "").strip():
        print(json.dumps({"ok": False, "message": "A chief worker can't announce decisions."}), file=out)
        return 1
    at = at or datetime.now(timezone.utc)
    ws = Path(ws)
    try:
        pos, o = _opts(rest, ("ids", "ts") if verb == "sent" else (), ("json",) if verb == "batch" else ())
    except ValueError as e:
        print("mother decide: %s" % e, file=err)
        return 2
    if pos:
        print("usage:\n" + USAGE, file=err)
        return 2
    if verb == "batch":
        got = offer(ws, cfg or {}, at)
        if o.get("json"):
            print(json.dumps(got, ensure_ascii=False), file=out)
        elif got is not None:
            print(got["text"], file=out)
        return 0
    raw = o.get("ids", "")
    if raw and not re.match(r"^D\d+(?:,D\d+)*$", raw):
        print("mother decide: --ids takes D7,D8, or '' for a batch of confirm lines only", file=err)
        return 2
    try:
        record_post(ws, cfg or {}, at, raw.split(",") if raw else [], o.get("ts"))
    except SlackReadFailed as e:
        print("mother decide: %s" % e, file=err)
        return 3
    except ValueError as e:
        print("mother decide: %s" % e, file=err)
        return 2
    print(json.dumps({"ok": True}), file=out)
    return 0


def _main_answer(rest, ws, cfg, out, err, at) -> int:
    """mother decide answer <n> <ok|no|edit|say> --via pane [-- <text>] — for the Chief pane only. Refused in a
    worker's process, behind the guard's deny. Not on MOTHER_CHIEF_ROLE: the pane's mod runs in the chief's session and
    inherits it (hooks/chief.tsx reads its role the same way)."""
    if os.environ.get(guard.CHIEF_WORKER_ENV, "").strip():
        print(json.dumps({"ok": False, "message": "A chief worker can't answer a decision: only the manager does."}),
              file=out)
        return 1
    payload = ""
    if "--" in rest:
        i = rest.index("--")
        rest, payload = rest[:i], " ".join(rest[i + 1:])
    try:
        pos, o = _opts(rest, ("via",), ())
    except ValueError as e:
        print("mother decide: %s" % e, file=err)
        return 2
    m = _NUM.match(pos[0].lower()) if len(pos) == 2 else None
    if not m or pos[1] not in VERBS or o.get("via") not in VIAS \
            or (pos[1] in ("edit", "say") and not payload.strip()):
        print("usage: mother decide answer <n> <ok|no|edit|say> --via pane [-- <text>]", file=err)
        return 2
    ok, msg = _answer(Path(ws), cfg or {}, at or datetime.now(timezone.utc), int(m.group(1)), pos[1], payload,
                      {"via": o["via"]})
    print(json.dumps({"ok": ok, "message": msg}), file=out)
    return 0 if ok else 1
