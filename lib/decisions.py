"""mother decide — the decision queue. A worker files the exact call it wants to make; the manager answers; code
(never a model) writes a single-use grant bound to that call (`answer`).

  propose <key> --tool T --recommend R [--why W] [--urgent] --stdin     stdin: the tool's input JSON
  answer <n> <ok|no|edit|say> --via pane|typed [-- <text>]              the Chief pane's answer (mod only)
  batch [--json]                                                        the message due for the manager's DM, if any
  sent [--ids D7,D8] [--ts TS]                                          that message went out (TS: its Slack ts)

State is <workspace>/.mother/chief/decisions.json, changed only under `locked(ws)` (flock on decisions.lock) and
written atomically. Python 3.9 stdlib only.
"""
import contextlib
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
            "replied": {}}


def load(ws: Path) -> dict:
    """decisions.json with every field present; a missing or unreadable file reads as empty. `batch.announced` maps a
    decision id to the stamp of the batch that showed it to the manager; any other shape reads as no announcements.
    `batch.offered` is how many confirm lines the last `batch` command handed out. `replied` maps an inbox line that
    looked like a reply to whether its re-read message was one, so a line behind the cursor is applied once."""
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
    b = raw.get("batch")
    if isinstance(b, dict):
        doc["batch"]["last_at"] = b.get("last_at") if isinstance(b.get("last_at"), str) else None
        if isinstance(b.get("announced"), dict):
            doc["batch"]["announced"] = {k: v for k, v in b["announced"].items() if isinstance(v, str)}
        if isinstance(b.get("offered"), int) and not isinstance(b["offered"], bool) and b["offered"] >= 0:
            doc["batch"]["offered"] = b["offered"]
    if isinstance(raw.get("confirm"), list):
        doc["confirm"] = [x for x in raw["confirm"] if isinstance(x, str)]
    if isinstance(raw.get("replied"), dict):
        doc["replied"] = {k: v for k, v in raw["replied"].items() if isinstance(v, bool)}
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
        raise ValueError("this %s isn't signed, so the guard would refuse it even with the manager's yes. Sign it: put "
                         "%s in the text%s, then file it again" % (
                             kind, marks[0], " and the blocks" if guard.classify_mcp(tool, inp).get("blocks") else ""))
    hours = chief.settings(cfg)["decision_hours"]
    with locked(ws):
        if key not in chief.workers(ws):
            raise ValueError("%s is not a live CLI worker" % key)
        doc = load(ws)
        for d in doc["decisions"]:
            if d.get("status") == "pending" and d.get("key") == key and d.get("draft_sha") == sha:
                raise ValueError("%s already has this call pending as %s" % (key, d.get("id")))
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


# what routes a call rather than what it says: `target` shows these, and an id in a text field would only be noise
ROUTING_KEYS = {"channel", "channel_id", "channelId", "thread_ts", "threadTs", "ts", "to", "issueKey", "issueIdOrKey",
                "issue_key", "issueId", "projectKey", "project_key", "project", "issueType", "issueTypeId",
                "issuetype", "issue_type", "transitionId", "transition", "transition_id", "inputs"}


def draft_fields(inp) -> List[Tuple[str, str]]:
    """(field, what it says) for everything a call would send: every field but ROUTING_KEYS, at the top and under
    `inputs`, in the input's order. A list or object (blocks) reads as JSON. Booleans and numbers are settings."""
    out = []
    for prefix, d in (("", inp), ("inputs.", inp.get("inputs") if isinstance(inp, dict) else None)):
        for k, v in (d.items() if isinstance(d, dict) else ()):
            if k in ROUTING_KEYS or v is None or v == "" or isinstance(v, (bool, int, float)):
                continue
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
    """Where the call goes, as one safe line: the tracker issue, the channel, the grant's target, or the recipient."""
    tool, inp = d.get("tool"), d.get("input")
    if not isinstance(inp, dict) or not isinstance(tool, str):
        return ""
    nested = inp.get("inputs") if isinstance(inp.get("inputs"), dict) else {}
    a = guard.classify_mcp(tool, inp) or {}
    found = guard.first(inp, "issueKey", "issueIdOrKey", "issue_key", "issueId") \
        or guard.first(nested, "issueKey", "issueIdOrKey", "issue_key", "issueId") \
        or a.get("channel") or d.get("target") or guard.first(inp, "to") or guard.first(nested, "to") or ""
    return chief.safe_line(", ".join(map(str, found)) if isinstance(found, list) else found, 200)


def signed_edit(d: dict, cfg: dict, text: str) -> dict:
    """`d`'s input with the manager's `text`, still carrying the signature the guard wants, or the grant could never
    match. Their words stay as typed; the signature the draft carried goes on a line after them when they left it
    out. ValueError when that isn't possible."""
    tool, inp = d["tool"], edited_input(d["input"], text)
    fields = [f for f, _ in draft_fields(d["input"])]
    if len(fields) > 1:  # new text in one would send the others unread: the manager can say what to change instead
        raise ValueError("this call sends %s, and an edit can only replace one. Tell the worker what to change: "
                         "%s <your words>" % (" and ".join(fields), str(d.get("id"))[1:]))
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


_NUM = re.compile(r"^d?(\d+)$")
_START_NUM = re.compile(r"^d?\d+(?!\w)", re.I)
_START_WORD = re.compile(r"^(\S+)\s+(?:d?\d+|all)(?!\w)", re.I)
_EDIT = re.compile(r"^\s*d?(\d+)\s+edit(?!\w)(.*)$", re.I | re.S)


def _starts_like_reply(t: str) -> bool:
    if _START_NUM.match(t):
        return True
    m = _START_WORD.match(t)
    return bool(m) and m.group(1).lower() in (channels.SEND_WORDS | channels.NO_WORDS)


def _clause(c: str, open_ids: Set[int]) -> Optional[list]:
    """Items for one non-edit clause, or None when it doesn't parse."""
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
    return [(n, "say", " ".join(toks[1:]))]


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
        parts = re.split(r"[,;\n]", rest, maxsplit=1)
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
VERBS = ("ok", "no", "edit", "say")
VIAS = ("pane", "typed")  # a Slack reply goes through answer_reply, which reads what the manager wrote
UNREADABLE = "I couldn't read that reply, so nothing was applied. Use: 7 ok · 7 no · 7 edit: <text> · ok all"
UNCONFIRMED = "I couldn't confirm that reply came from you (%s)."
_SLACK_LINK = re.compile(r"<(https?:[^|>\s]*)(?:\|([^>]*))?>")


def canonical(inp) -> str:
    return json.dumps(inp, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fetch_slack(cfg: dict, channel: str, ts: str) -> Optional[dict]:
    """The one message at `ts` in `channel`, read through the hub's read-only chat client; None when Slack has no
    such message. Raises when the chat connection can't be made or read."""
    from hub_mcp import McpError
    h = hub._hub_cfg(cfg)
    conn = (h.get("connections") or {}).get("chat")
    if not isinstance(conn, dict):
        raise McpError("other", "no hub chat connection in config")
    only_chat = dict(cfg, hub=dict(h, connections={"chat": conn}))  # start the chat server only
    clients = hub.build_clients(only_chat, {})
    try:
        c = clients.get("chat")
        if c is None or isinstance(c, Exception):
            raise c if isinstance(c, Exception) else McpError("other", "no chat client")
        res = c.call("get_channel_history", {"channel_id": channel, "oldest": ts, "latest": ts, "inclusive": True,
                                             "limit": 1})
        for m in (res or {}).get("messages") or []:
            if isinstance(m, dict) and m.get("ts") == ts:
                return m
        return None
    finally:
        for c in clients.values():
            if not isinstance(c, Exception):
                hub._close(c)


def _typed_message(cfg: dict, channel: str, ts: str, fetch=None) -> Tuple[Optional[dict], Optional[str]]:
    """(message, None) when Slack shows the message at (channel, ts) typed by the manager (`hub_sources.typed_by`) in
    the manager's own DM; otherwise (None, the reason it can't be confirmed). Reads Slack once."""
    import hub_sources
    if not isinstance(channel, str) or not channel.startswith("D") or channel not in guard.dm_ids(cfg):
        return None, "it isn't in your own DM"
    mine = guard.owner_id(cfg)
    if not mine:
        return None, "config.json has no manager chat id"
    if not isinstance(ts, str) or guard.parse_time(ts) is None:
        return None, "the message has no timestamp"
    try:
        msg = (fetch or _fetch_slack)(cfg, channel, ts)
    except Exception as e:
        return None, "Slack couldn't be read: %s" % (str(e) or type(e).__name__)
    if not isinstance(msg, dict) or msg.get("ts") != ts:
        return None, "Slack doesn't show that message"
    if not hub_sources.typed_by(msg, mine):
        return None, "Slack doesn't show it as typed by you"
    return msg, None


def slack_text(raw: str) -> str:
    """Slack's wire form as the words the manager wrote: links unwrapped and &amp; &lt; &gt; unescaped. Mentions,
    channel references, newlines, tabs and everything else stay. &amp; goes last, so it can't make a new entity."""
    def link(m):
        url, label = m.group(1), m.group(2)
        return url if not label or label == url else "%s (%s)" % (label, url)
    return _SLACK_LINK.sub(link, raw).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _reply_open_ids(ws: Path, ts: str) -> Set[int]:
    """What a Slack reply can answer: pending decisions announced to the manager before the reply was typed. Anything
    else is a call the manager hadn't seen. A decision is filed before it is announced, so this also covers `created`."""
    sent = guard.parse_time(ts)
    doc = load(ws)
    out = set()
    for d in doc["decisions"]:
        shown = guard.parse_time(doc["batch"]["announced"].get(d.get("id")))
        if d.get("status") == "pending" and shown is not None and shown < sent:
            m = _NUM.match(str(d["id"]).lower())
            if m:
                out.add(int(m.group(1)))
    return out


def answer_reply(ws: Path, cfg: dict, at: datetime, channel: str, ts: str, fetch=None) -> Optional[List[str]]:
    """Apply a reply the manager typed in the self-DM. The verb and any edit text come from the message re-read from
    Slack here, never from a caller or the watcher's flattened summary. None: the message isn't a reply. Otherwise
    the result lines: one for an unconfirmed or unreadable message (nothing changes), else one per parsed item."""
    ws = Path(ws)
    msg, why = _typed_message(cfg, channel, ts, fetch)
    if why:
        return [UNCONFIRMED % why]
    with locked(ws):
        open_ids = _reply_open_ids(ws, ts)
    items = parse_reply(msg.get("text"), open_ids)
    if items is None:
        return None
    if not items:
        return [UNREADABLE]
    source = {"via": "slack", "channel": channel, "ts": ts}
    lines, refused = [], set()
    for n, verb, payload in items:
        # `8 no <words>` is a no and then the words: they still reach the worker after the no has landed
        after_no = verb == "say" and n in refused
        ok, line = _answer(ws, cfg, at, n, verb, slack_text(payload) if verb == "edit" else payload, source,
                           after_no=after_no)
        if ok and verb == "no":
            refused.add(n)
        lines.append(line)
    return lines


def _grant(d: dict, cfg: dict, at: datetime, owner: str, source: dict) -> dict:
    """The grant for an approved call. ValueError with the reason when the guard could never honour it: nothing is
    written, and the decision stays open rather than being reported as approved."""
    now = at.timestamp()
    if source.get("via") == "slack":
        src = {"channel": source["channel"], "user": owner, "ts": source["ts"]}
    else:
        src = {"channel": "pane", "user": owner, "ts": "%.6f" % now}
    scopes, max_age = guard.grant_limits(cfg)
    if d["kind"] not in scopes:
        raise ValueError("policy.grants turns %s off" % d["kind"])
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
    """Apply a Chief pane (or typed) answer to decision D<n>; the one-line result for the manager. A Slack reply goes
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
            d.update(status="approved", answer=rec)
            save(ws, doc)
            chief.enqueue(ws, at, key, "%s approved by the manager. Make exactly this one call now, with this input "
                                       "unchanged, then end the turn.\nTool: %s\nInput: %s"
                          % (did, d["tool"], canonical(d["input"])), "decision")
            _resume(ws, key)
            return True, "%s approved: %s will make that one call." % (did, key)
        problem = _set_grant(ws, did, None)  # no live grant survives an answer that doesn't send
        if problem:
            return False, "%s is still open: %s." % (did, problem)
        if verb in ("ok", "edit"):
            d.update(status="manual", answer=rec)
            save(ws, doc)
            _resume(ws, key)
            return True, "%s is yours to do by hand (%s isn't something I can send): %s" % (
                did, d["tool"], d.get("text"))
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


def settle(ws: Path, at: datetime) -> None:
    """The tick's pass over the queue: expire what ran out of time, and close each approved decision whose worker
    run has ended. A run has ended when the worker is gone, or its run lock is free, it isn't marked working and its
    approval has left the queue. The guard's ledger then says whether the grant was used; a ledger that can't be read
    says nothing, so those decisions stay approved until it can."""
    ws = Path(ws)
    with locked(ws):
        doc = load(ws)
        for d in doc["decisions"]:
            exp = guard.parse_time(d.get("expires"))
            if d.get("status") == "pending" and exp is not None and at.timestamp() >= exp:
                _expire(ws, doc, d, at)
        approved = [d for d in doc["decisions"] if d.get("status") == "approved"]
        if not approved:
            return
        with guard.Ledger(str(ws)) as led:
            book = led.read()
        if book is None:
            return
        used = {r.get("run") for r in book["uses"] if isinstance(r, dict)}
        live, busy, queue = chief.workers(ws), set(chief.running(ws)), chief._read_queue(ws)
        for d in approved:
            did, key = d.get("id"), d.get("key")
            w = live.get(key)
            waiting = any(e.get("key") == key and e.get("source") == "decision"
                          and str(e.get("text")).startswith("%s approved by" % did) for e in queue)
            if w is not None and (key in busy or w.get("status") == "working" or waiting):
                continue
            if "chief:%s" % did in used:
                d["status"] = "sent"
                doc["confirm"].append("✓ %s done: %s on %s" % (did, d.get("kind"), d.get("target")))
                save(ws, doc)
                continue
            _set_grant(ws, did, None)
            text = "%s didn't go out: the call didn't match what you approved. It's back with the worker." % did
            d["status"] = "failed"
            doc["confirm"].append(text)
            save(ws, doc)
            if w is not None:
                chief.enqueue(ws, at, key, text, "decision")


# ---- the tick's replies: a manager's note in the self-DM that answers the queue --------------------------------

REPLIED_KEEP = 200


def is_reply_line(l: dict) -> bool:
    """A self-DM note whose summary starts like a reply. Only a `chat` line can be re-read from Slack: a `manual`
    source line came from a CLI verb any session can run. This only pre-filters: `answer_reply` reads the message."""
    return l.get("kind") == "manual" and l.get("source") == "chat" and parse_reply(l.get("summary"), set()) is not None


def held_replies(ws: Path, lines: List[dict]) -> Set[str]:
    """The lines that are the tick's, not the chief's: replies, and any not yet read that may be one."""
    seen = load(ws)["replied"]
    return {l["id"] for l in lines if is_reply_line(l) and seen.get(l["id"]) is not False}


def apply_replies(ws: Path, cfg: dict, at: datetime, lines: List[dict]) -> Set[str]:
    """Apply every reply among `lines`, each once, even one the cursor can't reach yet. Result lines go to `confirm`.
    Returns the ids of the lines that are replies; a line whose re-read message isn't one keeps its normal path."""
    ws = Path(ws)
    channel = next((x for x in sorted(guard.dm_ids(cfg), key=str) if str(x).startswith("D")), None)
    out = set()
    for l in lines:
        if not is_reply_line(l):
            continue
        lid = l["id"]
        known = load(ws)["replied"].get(lid)
        if known is None:
            got = answer_reply(ws, cfg, at, channel, lid.rsplit(":", 1)[-1])
            known = got is not None
            with locked(ws):
                doc = load(ws)
                doc["replied"][lid] = known
                doc["replied"] = dict(list(doc["replied"].items())[-REPLIED_KEEP:])
                if known:
                    doc["confirm"].extend(got or [UNREADABLE])
                save(ws, doc)
        if known:
            out.add(lid)
    return out


# ---- the batch: what the manager's DM hears from the queue ------------------------------------------------------

WHY_MAX = 100
_SLACK_TS = re.compile(r"^\d+(?:\.\d+)?$")


def _shown(text, cap: int) -> str:
    """Worker-written text as one line of a Slack message: capped, and it can't draw a link or a mention."""
    return chief.safe_line(text, cap).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        rows.append("%s · %s: %s · recommend: %s" % (d.get("id"), _shown(d.get("key"), WHY_MAX),
                                                     _shown(d.get("why"), WHY_MAX),
                                                     _shown(d.get("recommendation"), WHY_MAX)))
    rows.extend(doc["confirm"])
    if open_:
        rows.append("Reply: 7 ok · 7 no · 7 edit: <text> · ok all")
    return {"text": "\n".join(rows), "ids": [d.get("id") for d in open_]}


def sent(ws: Path, at: datetime, ids: List[str], ts: Optional[str] = None) -> None:
    """The batch was posted. `ids` count as announced at `ts`, the post's own Slack ts (now when none is given). A
    decision keeps its first stamp, so a reply to an earlier batch stays good. Clears the confirm lines the last
    `batch` command handed out, or all of them; a line that arrived while the post was going out stays. ValueError on
    a `ts` that isn't a Slack ts: nothing is written."""
    if ts is not None and not _SLACK_TS.match(str(ts)):
        raise ValueError("--ts must be the post's Slack ts")
    ws = Path(ws)
    with locked(ws):
        doc = load(ws)
        open_ = {d.get("id") for d in doc["decisions"] if d.get("status") == "pending"}
        b = doc["batch"]
        announced = {k: v for k, v in b["announced"].items() if k in open_}
        for i in ids:
            if i in open_:
                announced.setdefault(i, str(ts) if ts is not None else hub._iso(at))
        b["announced"] = announced
        b["last_at"] = hub._iso(at)
        offered = b.pop("offered", None)
        doc["confirm"] = doc["confirm"][offered:] if offered is not None else []
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
    """mother decide batch [--json] | sent [--ids D7,D8] [--ts TS]. Refused in a worker's process: announcing is what
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
        with locked(ws):
            got = batch(ws, cfg or {}, at)
            if got is not None:
                doc = load(ws)
                doc["batch"]["offered"] = len(doc["confirm"])
                save(ws, doc)
        if o.get("json"):
            print(json.dumps(got, ensure_ascii=False), file=out)
        elif got is not None:
            print(got["text"], file=out)
        return 0
    raw = o.get("ids", "")
    if raw and not re.match(r"^D\d+(?:,D\d+)*$", raw):
        print("mother decide: --ids takes D7,D8, or nothing for a batch of confirm lines only", file=err)
        return 2
    try:
        sent(ws, at, raw.split(",") if raw else [], o.get("ts"))
    except ValueError as e:
        print("mother decide: %s" % e, file=err)
        return 2
    print(json.dumps({"ok": True}), file=out)
    return 0


def _main_answer(rest, ws, cfg, out, err, at) -> int:
    """mother decide answer <n> <ok|no|edit|say> --via pane|typed [-- <text>] — for the Chief pane only. Refused in a
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
    if not m or pos[1] not in VERBS or o.get("via") not in ("pane", "typed") \
            or (pos[1] in ("edit", "say") and not payload.strip()):
        print("usage: mother decide answer <n> <ok|no|edit|say> --via pane|typed [-- <text>]", file=err)
        return 2
    ok, msg = _answer(Path(ws), cfg or {}, at or datetime.now(timezone.utc), int(m.group(1)), pos[1], payload,
                      {"via": o["via"]})
    print(json.dumps({"ok": ok, "message": msg}), file=out)
    return 0 if ok else 1
