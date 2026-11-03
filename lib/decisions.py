"""mother decide — the decision queue. A worker files the exact call it wants to make; the manager answers; code
(never a model) writes a single-use grant bound to that call (`answer`).

  propose <key> --tool T --recommend R [--why W] [--urgent] --stdin     stdin: the tool's input JSON
  answer <n> <ok|no|edit|say> --via pane|typed [-- <text>]              the Chief pane's answer (mod only)

State is <workspace>/.mother/chief/decisions.json, changed only under `locked(ws)` (flock on decisions.lock) and
written atomically. Python 3.9 stdlib only.
"""
import contextlib
import importlib.util
import json
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
    return {"version": 1, "next": 1, "decisions": [], "batch": {"last_at": None, "announced": []}, "confirm": []}


def load(ws: Path) -> dict:
    """decisions.json with every field present; a missing or unreadable file reads as empty."""
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
        if isinstance(b.get("announced"), list):
            doc["batch"]["announced"] = [x for x in b["announced"] if isinstance(x, str)]
    if isinstance(raw.get("confirm"), list):
        doc["confirm"] = [x for x in raw["confirm"] if isinstance(x, str)]
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


def propose(ws: Path, cfg: dict, at: datetime, key: str, tool: str, inp: dict, recommendation: str,
            why: str = "", urgent: bool = False) -> dict:
    if not isinstance(inp, dict):
        raise ValueError("the tool input must be a JSON object")
    sha = guard.draft_sha(tool, inp)
    kind, target, thread = kind_of(tool, inp)
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
    for k in TOP_TEXT_KEYS + ("blocks",):
        if guard.first(out, k) is not None:
            if k == "blocks" or not isinstance(out[k], str):
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
VIAS = ("slack", "pane", "typed")


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


def verify_typed(ws: Path, cfg: dict, channel: str, ts: str, text: str, fetch=None) -> Optional[str]:
    """None when Slack shows the message at (channel, ts) typed by the manager (`hub_sources.typed_by`) in the
    manager's own DM, with the same text after `hub.plain`; otherwise the reason it can't be confirmed."""
    import hub_sources
    if not isinstance(channel, str) or not channel.startswith("D") or channel not in guard.dm_ids(cfg):
        return "it isn't in your own DM"
    mine = guard.owner_id(cfg)
    if not mine:
        return "config.json has no manager chat id"
    if not isinstance(ts, str) or not ts:
        return "the message has no timestamp"
    try:
        msg = (fetch or _fetch_slack)(cfg, channel, ts)
    except Exception as e:
        return "Slack couldn't be read: %s" % (str(e) or type(e).__name__)
    if not isinstance(msg, dict) or msg.get("ts") != ts:
        return "Slack doesn't show that message"
    if not hub_sources.typed_by(msg, mine):
        return "Slack doesn't show it as typed by you"
    if hub.plain(msg.get("text")).strip() != hub.plain(text).strip():
        return "the text in Slack is different"
    return None


def _grant(d: dict, cfg: dict, at: datetime, owner: str, source: dict) -> dict:
    now = at.timestamp()
    if source.get("via") == "slack":
        src = {"channel": source["channel"], "user": owner, "ts": source["ts"]}
    else:
        src = {"channel": "pane", "user": owner, "ts": "%.6f" % now}
    _, max_age = guard.grant_limits(cfg)
    t = guard.parse_time(src["ts"])
    if t is None:
        raise ValueError("the reply's timestamp can't be read")
    ends = [now + GRANT_HOURS * 3600, t + max_age * 3600]
    exp = guard.parse_time(d.get("expires"))
    if exp is not None:
        ends.append(exp)
    return {"scope": d["kind"], "target": d.get("target"), "thread": d.get("thread"), "uses": 1,
            "expires": grants.iso(min(ends)), "run": "chief:%s" % d["id"], "decision": d["id"],
            "draft_sha": d["draft_sha"], "source": src}


def answer(ws: Path, cfg: dict, at: datetime, n: int, verb: str, payload: str, source: dict, fetch=None) -> str:
    """Apply the manager's answer to decision D<n>; the one-line result for the manager. Only a verified yes (a
    Slack reply re-read by `verify_typed`, or a pane/typed answer) writes a grant, and that grant is bound to the
    exact call (`draft_sha`), single-use and short-lived. Anything unverifiable changes nothing.
    Lock order: decisions, then grants, then the hub lock (enqueue, update_worker)."""
    return _answer(ws, cfg, at, n, verb, payload, source, fetch)[1]


def _answer(ws: Path, cfg: dict, at: datetime, n: int, verb: str, payload: str, source: dict,
            fetch=None) -> Tuple[bool, str]:
    ws = Path(ws)
    did = "D%d" % n
    if verb not in VERBS:
        return False, "%s is still open: I don't know the answer %r." % (did, verb)
    via = (source or {}).get("via")
    if via not in VIAS:
        return False, "%s is still open: I couldn't confirm that reply came from you (unknown source)." % did
    if via == "slack":
        why = verify_typed(ws, cfg, source.get("channel"), source.get("ts"), source.get("text"), fetch=fetch)
        if why:
            return False, "%s is still open: I couldn't confirm that reply came from you (%s)." % (did, why)
    with locked(ws):
        doc = load(ws)
        d = next((x for x in doc["decisions"] if x.get("id") == did), None)
        if d is None:
            return False, "%s isn't a decision I know." % did
        if d.get("status") == "pending":
            exp = guard.parse_time(d.get("expires"))
            if exp is not None and at.timestamp() >= exp:
                d["status"] = "expired"
                save(ws, doc)
        if d.get("status") != "pending":
            return False, "%s is already %s." % (did, d.get("status"))
        if chief.paused(ws):
            return False, "%s is still open: outbound is paused." % did
        key = d.get("key")
        if key not in chief.workers(ws):
            return False, "%s is still open: %s is no longer a live worker." % (did, key)
        rec = {"verb": verb, "via": via, "at": hub._iso(at)}
        if via == "slack":
            rec["ts"] = source.get("ts")
        if verb in ("edit", "say"):
            rec["payload"] = payload
        if verb == "edit":
            try:
                inp = edited_input(d["input"], payload)
            except ValueError as e:
                return False, "%s is still open: %s." % (did, e)
            kind, target, thread = kind_of(d["tool"], inp)
            d.update(input=inp, text=guard.text_of(inp), draft_sha=guard.draft_sha(d["tool"], inp), kind=kind,
                     target=target, thread=thread)
        if verb in ("ok", "edit"):
            if d.get("kind") == "manual":
                d.update(status="manual", answer=rec)
                save(ws, doc)
                _resume(ws, key)
                return True, "%s is yours to do by hand (%s isn't something I can send): %s" % (
                    did, d["tool"], d.get("text"))
            owner = guard.owner_id(cfg)
            if not owner:
                return False, "%s is still open: config.json has no manager chat id." % did
            try:
                g = _grant(d, cfg, at, owner, source)
            except ValueError as e:
                return False, "%s is still open: %s." % (did, e)
            with grants.locked(ws):
                gdoc, problem = grants.read(ws)
                if problem:
                    return False, "%s is still open: %s." % (did, problem)
                if not any(isinstance(x, dict) and x.get("decision") == did for x in gdoc["grants"]):
                    gdoc["grants"].append(g)
                    grants.write(ws, gdoc)
            d.update(status="approved", answer=rec)
            save(ws, doc)
            chief.enqueue(ws, at, key, "%s approved by the manager. Make exactly this one call now, with this input "
                                       "unchanged, then end the turn.\nTool: %s\nInput: %s"
                          % (did, d["tool"], canonical(d["input"])), "decision")
            _resume(ws, key)
            return True, "%s approved: %s will make that one call." % (did, key)
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


def _resume(ws: Path, key: str) -> None:
    """The worker leaves waiting_decision; normal dispatch picks up what was queued."""
    try:
        chief.update_worker(ws, key, status="idle")
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


def _main_answer(rest, ws, cfg, out, err, at) -> int:
    """mother decide answer <n> <ok|no|edit|say> --via pane|typed [-- <text>] — for the Chief pane only."""
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
