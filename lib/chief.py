"""MOTHER chief — what a leading desktop session and the Chief pane need: the tick (heartbeat, leader rule, the
lines that need no judgment), triage decisions, the pane's status, pinned desktop sessions, TL;DRs, the spend
ledger and the pause switch under <ws>/.mother/chief/.

spend.json, desktop.json and tldr.json are written inside hub.locked(ws). Until 0.18.0 this module also ran a chief
session and headless workers; what they left on disk (worker rows in sessions.json, queue.jsonl, decisions.json,
chief.json, wake.json) is ignored. Stdlib only.
"""
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import floor as floormod
import habits
import hub
import now as nowmod

DEFAULTS = {"daily_budget_usd": 20, "tldr_model": "sonnet", "triage_model": "sonnet", "refresh_minutes": 5,
            "ignore_folders": [], "mention_search_seconds": 180}
REFRESH_MINUTES = (1, 60)   # chief.refresh_minutes: how often the leading session re-reads each session for its card
MENTION_SECONDS = (60, 1800)   # chief.mention_search_seconds: how often the leader runs the desktop mention search
TLDR_MODELS = ("sonnet", "opus")   # the Chief pane's TL;DR model; anything else reads as the default
SPEND_KEEP_DAYS = 14


def chief_dir(ws: Path) -> Path:
    return Path(ws) / ".mother" / "chief"


def settings(cfg: dict) -> dict:
    """The chief settings from config.json -> chief, defaults merged in. Unknown keys are dropped."""
    given = (cfg or {}).get("chief")
    given = given if isinstance(given, dict) else {}
    out = {k: given.get(k, v) for k, v in DEFAULTS.items()}
    if out["triage_model"] not in TLDR_MODELS:
        out["triage_model"] = DEFAULTS["triage_model"]
    r = out["refresh_minutes"]
    if not (isinstance(r, int) and not isinstance(r, bool) and REFRESH_MINUTES[0] <= r <= REFRESH_MINUTES[1]):
        out["refresh_minutes"] = DEFAULTS["refresh_minutes"]
    m = out["mention_search_seconds"]
    if not (isinstance(m, int) and not isinstance(m, bool) and MENTION_SECONDS[0] <= m <= MENTION_SECONDS[1]):
        out["mention_search_seconds"] = DEFAULTS["mention_search_seconds"]
    out["ignore_folders"] = ignore_folders(out["ignore_folders"])
    return out


IGNORE_FOLDERS_MAX = 50


def ignore_folders(v, home: Optional[str] = None) -> List[str]:
    """chief.ignore_folders as absolute paths: each an absolute or `~/` path, `~` expanded, no trailing slash;
    anything else is dropped. A pinned session under one gets no `Waiting on:` instruction (CONTRACT §5g)."""
    home = home if home is not None else os.path.expanduser("~")
    out = []
    for f in v if isinstance(v, list) else []:
        if not isinstance(f, str):
            continue
        f = f.strip()
        if f == "~" or f.startswith("~/"):
            f = home + f[1:]
        if f.startswith("/") and len(f) > 1:
            f = f.rstrip("/") or "/"
            if f not in out:
                out.append(f)
    return out[:IGNORE_FOLDERS_MAX]


# ---- pause ------------------------------------------------------------------------------------------

def paused(ws: Path) -> bool:
    """True when the guard's global switch (.mother/PAUSED) or the chief-only one exists."""
    return (Path(ws) / ".mother" / "PAUSED").exists() or (chief_dir(ws) / "PAUSED").exists()


def pause(ws: Path) -> None:
    chief_dir(ws).mkdir(parents=True, exist_ok=True)
    (chief_dir(ws) / "PAUSED").write_text("")


def resume(ws: Path) -> None:
    """Removes only the chief switch; the global .mother/PAUSED is never touched."""
    try:
        (chief_dir(ws) / "PAUSED").unlink()
    except FileNotFoundError:
        pass


# ---- chief-only files (same atomic temp-file + rename as hub.save_json; caller holds hub.locked) ------

def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / ("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(text)
    os.replace(str(tmp), str(path))


def _is_cli(s) -> bool:
    """A headless worker's row in sessions.json, left over from before 0.18.0: never a session to route to."""
    return isinstance(s, dict) and s.get("kind") == "cli"


def _retire_workers(ws: Path) -> None:
    """Mark leftover worker rows that still read `live` as dropped, so hub.decide never routes a line to a worker
    that no longer runs. Nothing is written when there is none."""
    if not any(_is_cli(v) and v.get("state") == "live" for v in hub._sessions_doc(ws)["sessions"].values()):
        return
    with hub.locked(ws):
        doc = hub._sessions_doc(ws)
        for v in doc["sessions"].values():
            if _is_cli(v) and v.get("state") == "live":
                v["state"] = "dropped"
        hub.save_json(ws, "sessions.json", doc)



# ---- spend ------------------------------------------------------------------------------------------

def _day(at: datetime, tz) -> str:
    return at.astimezone(tz).date().isoformat()


def _read_spend(ws: Path) -> dict:
    try:
        d = json.loads((chief_dir(ws) / "spend.json").read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def spent_today(ws: Path, at: datetime, tz) -> float:
    """USD spent on the local (`tz`, a tzinfo) date of `at`."""
    with hub.locked(ws):
        v = _read_spend(ws).get(_day(at, tz), 0.0)
    return float(v) if isinstance(v, (int, float)) else 0.0


def add_spend(ws: Path, at: datetime, tz, usd: float) -> None:
    """Add to today's total in chief/spend.json ({"YYYY-MM-DD": usd}); keeps the last 14 days."""
    with hub.locked(ws):
        doc = _read_spend(ws)
        day = _day(at, tz)
        doc[day] = round(float(doc.get(day, 0.0)) + float(usd), 6)
        keep = sorted(doc)[-SPEND_KEEP_DAYS:]
        _write_atomic(chief_dir(ws) / "spend.json", json.dumps({k: doc[k] for k in keep}, indent=2) + "\n")


# USD per million input and output tokens, for the pane's own model calls (triage, TL;DR). `spend` estimates from these.
MODEL_PRICES = {"sonnet": (3, 15), "opus": (15, 75)}
TLDR_MAX_TOKENS = 1024  # hooks/chief.tsx's cap on a TL;DR call: what a call with no usage counts as


def model_cost(model, input_tokens, output_tokens) -> float:
    """The estimate for one call. Usage that is missing or not a count reads as TLDR_MAX_TOKENS of output, and a model
    with no price as the dearest: this feeds a cap, so it errs high."""
    def count(v):
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0
    if not (count(input_tokens) and count(output_tokens)):
        input_tokens, output_tokens = 0, TLDR_MAX_TOKENS
    per_in, per_out = (MODEL_PRICES.get(model) if isinstance(model, str) else None) or max(MODEL_PRICES.values())
    return (input_tokens * per_in + output_tokens * per_out) / 1e6

def _spend_unreadable(ws: Path) -> bool:
    """True when spend.json exists but cannot be read as an object. A missing file is $0, not unreadable."""
    try:
        text = (chief_dir(ws) / "spend.json").read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        return not isinstance(json.loads(text), dict)
    except ValueError:
        return True


# ---- tick: the heartbeat, the leader rule, and the lines that need no judgment ---------------------------

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
PROGRESS_MAX = 200
JUDGE_MAX = 20


def _pending_lines(ws: Path) -> List[dict]:
    """The digest of the inbox lines after hub.json's cursor (what the hub tail would print)."""
    cursor = hub._hub_doc(ws).get("inbox_cursor")
    batch, _missing = hub.after_cursor(hub.read_inbox(ws), cursor)
    return hub.digest(ws, batch)


def _session_kind(sessions: dict, line: dict) -> Optional[str]:
    """"desktop" when the line's key has a live hub session, else None. A leftover worker row is no session."""
    hit = hub.find_session(sessions, [line["key"]] + list(line.get("also") or []))
    return "desktop" if hit is not None and not _is_cli(hit[1]) else None


def _manager_names(cfg: dict) -> Tuple[str, str]:
    """(name, short name) of the manager from config.json -> agent; '' for one that is not set."""
    a = (cfg or {}).get("agent")
    a = a if isinstance(a, dict) else {}
    name = safe_line(a.get("manager") if isinstance(a.get("manager"), str) else "", NAME_MAX)
    short = safe_line(a.get("manager_short") if isinstance(a.get("manager_short"), str) else "", NAME_MAX)
    return name, short


def _to_manager(cfg: dict, l: dict) -> Optional[str]:
    """`addressed` for a direct message, `mentioned` when the line's text names the manager (their chat id as a
    mention, or their name or short name as a word), else None: only what the line itself shows."""
    key = str(l.get("key") or "")
    if key.endswith("/dm") or (key.startswith("wa:") and not key.endswith("@g.us")):
        return "addressed"
    if l.get("to_manager") is True:  # a desktop search hit for a mention of the manager (`mother hub ingest`)
        return "mentioned"
    text = "%s %s" % (l.get("title") or "", l.get("summary") or "")
    me = hub.build_me(cfg or {})
    if me.get("chat") and "<@%s" % me["chat"] in text:
        return "mentioned"
    for n in _manager_names(cfg):
        if len(n) >= 2 and re.search(r"(?<!\w)@?%s(?!\w)" % re.escape(n), text, re.IGNORECASE):
            return "mentioned"
    return None


_THREAD_KEY = re.compile(r"^[A-Z0-9]+/(\d+\.\d+)$")
THREAD_FIRST_MAX = 300


def _thread_first(stored: dict, line: dict) -> Optional[dict]:
    """A chat reply's thread opener, as {who, text}, when the inbox already holds it: the watcher writes a post under
    `chat:<channel>/<ts>:<ts>`. Never a read of the chat: an opener the watcher didn't see gives nothing."""
    m = _THREAD_KEY.match(str(line.get("key") or ""))
    if not m:
        return None
    root = stored.get("chat:%s:%s" % (line["key"], m.group(1)))
    if not root or root.get("id") == line.get("id"):
        return None
    text = safe_line(str(root.get("summary") or root.get("title") or ""), THREAD_FIRST_MAX)
    return {"who": safe_line(str(root.get("who") or ""), LABEL_MAX), "text": text} if text else None


def lines(ws: Path, cfg: Optional[dict] = None) -> List[dict]:
    """The lines waiting for a decision: the digest after the cursor, trimmed to the fields triage needs, plus
    `session` ("desktop" for a live hub session, else None), `source` (the key's kind), `channel` (a chat channel's
    configured name, else None), `to_manager` (`addressed`, `mentioned` or None, as `_to_manager`) and, when the inbox
    holds it, `thread_first` (a reply's thread opener, as `_thread_first`)."""
    ws = Path(ws)
    sessions = hub._sessions_doc(ws)
    channels = _channel_names(cfg or {})
    hab = habits.habits(ws, datetime.now(timezone.utc)) if habits.is_on(ws) else None
    out = []
    pending = _pending_lines(ws)
    stored = {r["id"]: r for r in hub.read_inbox(ws)} if any(_THREAD_KEY.match(str(l.get("key") or "")) for l in pending) else {}
    for l in pending:
        d = {k: l.get(k) for k in ("id", "key", "kind", "title", "who", "link", "summary")}
        d["also"] = list(l.get("also") or [])
        d["session"] = _session_kind(sessions, l)
        key = str(l.get("key") or "")
        d["source"] = source_of(key)
        name = channels.get(key.split("/", 1)[0]) if d["source"] == "chat" and "/" in key else None
        d["channel"] = safe_line(name, LABEL_MAX) if name else None
        d["to_manager"] = _to_manager(cfg or {}, l)
        d["habits"] = habits.for_line(hab, d["source"], key, l.get("who"))   # the manager's past handling of its own buckets
        first = _thread_first(stored, l)
        if first:
            d["thread_first"] = first
        out.append(d)
    return out


# ---- triage context: who the manager is and what they run, names only, for the triage prompt ----

NAME_MAX = 40
CONTEXT_MAX = 1500          # characters of the context as compact JSON
CONTEXT_TEAMS, CONTEXT_PEOPLE, CONTEXT_LIST = 8, 12, 12
CONTEXT_PEOPLE_IDS = 60     # chat ids the `people` map names


def triage_context(ws: Path, cfg: dict) -> dict:
    """`context --json`: {version, manager: {name, short}, teams: [{name, people: [names]}], projects, repos, people}.
    `people` maps each configured person's chat id to their name (and the manager's own), so the pane can say who
    an item is from; everything else is names only: no ids, no emails. `projects` and `repos` are what the hub
    follows (config and the watch list) plus each team's own. Trimmed from the end of its longest list until the
    compact JSON is at most CONTEXT_MAX characters (`people` last, whole)."""
    cfg = cfg or {}
    name, short = _manager_names(cfg)
    teams, projects, repos = [], [], []
    by_chat: Dict[str, str] = {}
    me = cfg.get("manager") if isinstance(cfg.get("manager"), dict) else {}
    for key in ("chat_user", "chat_dm"):   # a `U…` user id; a `D…` DM channel id is not one (lib/hub.py manager_ids)
        if isinstance(me.get(key), str) and re.match(r"^U[A-Z0-9]+$", me[key].strip()) and (short or name):
            by_chat[me[key].strip()] = short or name

    def add(dst, v):
        v = safe_line(v, NAME_MAX) if isinstance(v, str) else ""
        if v and v not in dst and len(dst) < CONTEXT_LIST:
            dst.append(v)

    for t in cfg.get("teams") if isinstance(cfg.get("teams"), list) else []:
        if not isinstance(t, dict):
            continue
        people = []
        for p in t.get("people") if isinstance(t.get("people"), list) else []:
            add(people, p.get("name") if isinstance(p, dict) else None)
            pname = safe_line(p.get("name"), NAME_MAX) if isinstance(p, dict) and isinstance(p.get("name"), str) else ""
            if pname and isinstance(p.get("chat"), str) and p["chat"].strip() and len(by_chat) < CONTEXT_PEOPLE_IDS:
                by_chat.setdefault(p["chat"].strip(), pname)
        tname = safe_line(t.get("name") if isinstance(t.get("name"), str) else "", NAME_MAX) \
            or safe_line(t.get("slug") if isinstance(t.get("slug"), str) else "", NAME_MAX)
        if tname and len(teams) < CONTEXT_TEAMS:
            teams.append({"name": tname, "people": people[:CONTEXT_PEOPLE]})
        tr = t.get("tracker")
        add(projects, tr.get("project") if isinstance(tr, dict) else None)
        for r in t.get("repos") if isinstance(t.get("repos"), list) else []:
            add(repos, r)
    try:
        watch = hub.merged_watch(cfg, hub.load_watch(ws))
    except Exception:  # an unreadable watch list: the config's own teams still say enough
        watch = {}
    for k in (watch.get("tracker") or {}).get("projects") or []:
        add(projects, k)
    for r in (watch.get("code") or {}).get("repos") or []:
        add(repos, r)
    doc = {"version": 1, "manager": {"name": name, "short": short}, "teams": teams, "projects": projects, "repos": repos,
           "people": by_chat}

    def size():
        return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))

    while size() > CONTEXT_MAX:
        lists = [repos, projects] + [t["people"] for t in teams]
        longest = max(lists, key=len)
        if longest:
            longest.pop()
        elif teams:
            teams.pop()
        elif by_chat:
            by_chat.clear()
        else:
            break
    return doc


def _read_obj(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _landing(raw: List[dict], order: dict, start: int, l: dict) -> int:
    """Where hub.decide would leave the cursor for `l`: an update stands for every same-key update after the
    cursor with no later `at`, so it lands on the furthest of them."""
    far = order[l["id"]]
    if l.get("kind") == "update":
        for x in raw[start:]:
            if x["key"] == l["key"] and x.get("kind") == "update" and x["at"] <= l["at"]:
                far = max(far, order[x["id"]])
    return far


def _beat(at: datetime, session: Optional[str]) -> str:
    doc = {"at": hub._iso(at), "pid": os.getpid()}
    if session:
        doc["session_id"] = session  # the leading session's id (`tick --as`)
    return json.dumps(doc) + "\n"


def lead(ws: Path, at: datetime, session: str) -> Optional[str]:
    """The leader rule for `tick --as <session>`: None when this session leads (the heartbeat is then written under
    its id), else why it does not: the heartbeat is fresh (ALIVE_SECONDS) and another session wrote it. A leader
    that stops ticking is replaced once its heartbeat is stale. Check and write happen under the hub lock."""
    ws = Path(ws)
    path = chief_dir(ws) / "heartbeat.json"
    with hub.locked(ws):
        last = _read_obj(path)
        beat = hub._parse_at(last.get("at"))
        if beat is not None and abs((at - beat).total_seconds()) < ALIVE_SECONDS and last.get("session_id") != session:
            return "another session leads"
        _write_atomic(path, _beat(at, session))
    return None


def tick(ws: Path, cfg: dict, at: datetime, session: Optional[str] = None) -> dict:
    """One pass: write the heartbeat, log the leading muted lines, and name the lines that need a decision (`judge`,
    for `triage --stdin --decide`). Paused: the heartbeat only. `session` is the leading session's id (`tick --as`),
    kept in the heartbeat. Not atomic as a whole (hub.locked is not re-entrant); every step is safe to re-run."""
    ws = Path(ws)
    _write_atomic(chief_dir(ws) / "heartbeat.json", _beat(at, session))
    out = {"paused": False, "decided": [], "judge": []}
    if paused(ws):
        out["paused"] = True
        return out
    _retire_workers(ws)
    mute = hub.live_mute(hub.merged_watch(cfg, hub.load_watch(ws))["mute"] or {}, at)
    pending = _pending_lines(ws)
    raw = hub.read_inbox(ws)
    order = {l["id"]: i for i, l in enumerate(raw)}
    cursor = hub._hub_doc(ws).get("inbox_cursor")
    start = order[cursor] + 1 if cursor in order else 0

    def muted(l):
        return l.get("kind") != "manual" and hub.is_muted(l, mute)

    first = next((l for l in pending if not muted(l)), None)
    limit = order[first["id"]] if first is not None else None
    items = []
    for l in pending:
        if l is first or (limit is not None and _landing(raw, order, start, l) > limit):
            break
        items.append({"line_id": l["id"], "key": l["key"], "decision": "log"})
    if items:
        out["decided"] = hub.decide(ws, cfg, at, items)
    out["judge"] = [l["id"] for l in pending[len(items):]][:JUDGE_MAX]
    return out


# ---- triage: a cheap model's verdicts, made by the mod, decide every judge line in code ----

TITLE_MAX, TLDR_MAX = 60, 300
WHY_MAX = 300   # a `now` item's why: a verdict's tldr or the line's own summary, which the hub stores up to 300
TRIAGE_NEVER_LOG = ("manual", "error")  # a person's own line, and a source failure: never logged on a model's say-so

def _verdict_ok(v) -> bool:
    return (isinstance(v, dict) and isinstance(v.get("line_id"), str) and isinstance(v.get("key"), str)
            and isinstance(v.get("is_work"), bool) and isinstance(v.get("needs_attention"), bool)
            and isinstance(v.get("title"), str) and isinstance(v.get("tldr"), str))


KINDS, ONS, URGENCIES = ("decide", "reply", "act", "fyi"), ("manager", "team", "other"), ("now", "today", "later")
KIND_DEFAULT, ON_DEFAULT, URGENCY_DEFAULT = "act", "manager", "today"
ACTIONS_MAX, ACTION_LABEL_MAX, ACTION_PROMPT_MAX = 3, 24, 300
TRIAGE_FILE = "triage.json"   # .mother/chief/triage.json: {version, items: {<update id>: {kind, on, urgency, actions}}}


def clean_actions(actions) -> List[dict]:
    """A verdict's actions as a card may show them: at most 3 of {label, prompt}, each one safe line (24 and 300
    characters), both non-empty. Anything off-shape is dropped."""
    out = []
    for a in actions if isinstance(actions, list) else []:
        if not isinstance(a, dict) or not isinstance(a.get("label"), str) or not isinstance(a.get("prompt"), str):
            continue
        label, prompt = safe_line(a["label"], ACTION_LABEL_MAX), safe_line(a["prompt"], ACTION_PROMPT_MAX)
        if label and prompt:
            out.append({"label": label, "prompt": prompt})
        if len(out) == ACTIONS_MAX:
            break
    return out


def verdict_extra(v) -> Optional[dict]:
    """{kind, on, urgency, actions} from a verdict that carries any of them, else None (the older four-field
    verdict). An unknown kind reads `act`, an unknown `on` `manager`, an unknown urgency `today`."""
    if not isinstance(v, dict) or not any(k in v for k in ("kind", "on", "urgency", "actions")):
        return None

    def pick(name, allowed, default):
        return v[name] if isinstance(v.get(name), str) and v[name] in allowed else default

    out = {"kind": pick("kind", KINDS, KIND_DEFAULT), "on": pick("on", ONS, ON_DEFAULT),
           "urgency": pick("urgency", URGENCIES, URGENCY_DEFAULT), "actions": clean_actions(v.get("actions"))}
    reason = _reason(v)
    return dict(out, reason=reason) if reason else out


VERDICT_REASON_MAX = 100
VERDICTS_FILE = "verdicts.jsonl"   # .mother/chief/verdicts.jsonl: one row per decided line, no bodies
VERDICTS_KEEP_DAYS = 30
VERDICTS_SHOWN = 10


def _reason(v) -> str:
    r = v.get("reason") if isinstance(v, dict) else None
    return safe_line(r, VERDICT_REASON_MAX) if isinstance(r, str) else ""


def _read_verdicts(ws: Path) -> List[dict]:
    try:
        text = (chief_dir(ws) / VERDICTS_FILE).read_text()
    except OSError:
        return []
    rows = []
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and hub._parse_at(row.get("at")) is not None and isinstance(row.get("key"), str):
            rows.append(row)
    return rows


def log_verdicts(ws: Path, at: datetime, rows: List[dict]) -> None:
    """Append the rows and drop what is older than 30 days. It only explains a decision already made, so a file that
    can't be written costs the row and nothing else."""
    if not rows:
        return
    floor = at - timedelta(days=VERDICTS_KEEP_DAYS)
    try:
        with hub.locked(ws):
            kept = [r for r in _read_verdicts(ws) if hub._parse_at(r["at"]) >= floor] + rows
            _write_atomic(chief_dir(ws) / VERDICTS_FILE, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    except OSError:
        pass


def verdicts_for(ws: Path, key: str) -> List[dict]:
    """The last verdicts of one key, newest first."""
    return [r for r in _read_verdicts(ws) if r["key"] == key][-VERDICTS_SHOWN:][::-1]


def _triage_items(ws: Path) -> dict:
    items = _read_obj(chief_dir(ws) / TRIAGE_FILE).get("items")
    return items if isinstance(items, dict) else {}


def _store_extras(ws: Path, extras: dict) -> None:
    """Keep each `now` item's verdict extras by update id (None drops one: the item was decided again with no
    verdict), and drop the entries whose update the hub no longer holds."""
    with hub.locked(ws):
        items = _triage_items(ws)
        for uid, extra in extras.items():
            if extra is None:
                items.pop(uid, None)
            else:
                items[uid] = extra
        held = {u.get("id") for u in hub._hub_doc(ws)["updates"] if isinstance(u, dict)}
        items = {k: v for k, v in items.items() if k in held}
        chief_dir(ws).mkdir(parents=True, exist_ok=True)
        _write_atomic(chief_dir(ws) / TRIAGE_FILE, json.dumps({"version": 1, "items": items}, ensure_ascii=False) + "\n")


def _folds_others(raw: List[dict], start: int, l: dict) -> bool:
    """True when deciding `l` would also settle another line: an update stands for every same-key update after the
    cursor with no later `at` (hub.decide moves the cursor past them all; the digest showed only the newest)."""
    if l.get("kind") != "update":
        return False
    return any(x["id"] != l["id"] and x["key"] == l["key"] and x.get("kind") == "update" and x["at"] <= l["at"]
               for x in raw[start:])


def _one_line(text: str, cap: int) -> str:
    """Whitespace (newlines included) collapsed to single spaces, then cut to `cap` characters."""
    return " ".join(text.split())[:cap]


def triage_decide(ws: Path, cfg: dict, at: datetime, judge: List[str], verdicts: List[dict]) -> dict:
    """Decide every `judge` line in code, for the leading session (no model turn).

    `verdicts` is [{line_id, key, is_work, needs_attention, title, tldr, kind?, on?, urgency?, actions?}]; the last
    four (`verdict_extra`) are kept for a `now` item in .mother/chief/triage.json by its update id. A line is logged when it has a well-formed
    verdict for its key that says is_work false or needs_attention false (never a `manual` or `error` line, one whose
    key has a live session, or an update whose decide would also fold other same-key updates), with
    `why: "triage: <title>"`, and a `sweep` line is logged too: it asks a hub session to look again, and there is none. Every
    other line, so every line with no verdict (a failed model call, no budget), is recorded as `now`: title and why
    from the verdict's title and tldr, else from the line, with the line's link. hub.decide stores a `now` whose key
    has a live session as a route to it. Returns {paused, logged, now}: the line ids."""
    ws = Path(ws)
    out = {"paused": False, "logged": [], "now": []}
    if paused(ws):
        out["paused"] = True
        return out
    sessions = hub._sessions_doc(ws)
    pending = _pending_lines(ws)
    raw = hub.read_inbox(ws)
    order = {l["id"]: i for i, l in enumerate(raw)}
    cursor = hub._hub_doc(ws).get("inbox_cursor")
    start = order[cursor] + 1 if cursor in order else 0
    keys = {l["id"]: l["key"] for l in pending}
    by_id = {v["line_id"]: v for v in (verdicts or [])
             if _verdict_ok(v) and keys.get(v["line_id"]) == v["key"]}
    wanted = set(judge or [])
    hab = habits.habits(ws, at) if habits.is_on(ws) else None
    channels = _channel_names(cfg or {})
    items, quieted, lines_by_id = [], {}, {l["id"]: l for l in pending}
    for l in pending:
        if l["id"] not in wanted:
            continue
        v = by_id.get(l["id"])
        if l.get("kind") == "sweep":
            items.append({"line_id": l["id"], "key": l["key"], "decision": "log", "why": "sweep: no hub session"})
            continue
        if (v is not None and l.get("kind") not in TRIAGE_NEVER_LOG and _session_kind(sessions, l) is None
                and not _folds_others(raw, start, l) and (not v["is_work"] or not v["needs_attention"])):
            items.append({"line_id": l["id"], "key": l["key"], "decision": "log",
                          "why": "triage: " + _one_line(v["title"], TITLE_MAX)})
            continue
        # the quiet rule (lib/habits.py `quiet`): the manager never engaged with this line's channel, project, repo or person
        q = habits.quiet(hab, l, v, _to_manager(cfg or {}, l), _session_kind(sessions, l), _folds_others(raw, start, l), channels)
        if q:
            items.append({"line_id": l["id"], "key": l["key"], "decision": "log", "why": q["why"]})
            quieted[l["id"]] = {"line": l, "verdict": v, "bucket": q["bucket"], "why": q["why"],
                                "label": item_label(l["key"], habits.who_name(l.get("who")), channels)}
        else:
            title = _one_line(v["title"] if v else str(l.get("title") or ""), TITLE_MAX) or _one_line(str(l["key"]), TITLE_MAX)
            why = _one_line(v["tldr"] if v else str(l.get("summary") or ""), WHY_MAX)
            items.append({"line_id": l["id"], "key": l["key"], "decision": "now", "title": title, "why": why,
                          "link": l.get("link") or ""})
    if items:
        extras, shown, hushed, explained = {}, [], [], []
        for res in hub.decide(ws, cfg, at, items):
            v = by_id.get(res["line_id"])
            l = lines_by_id[res["line_id"]]
            if l.get("kind") != "sweep":      # a sweep line was never judged
                hush = quieted.get(res["line_id"])
                explained.append({"at": hub._iso(at), "line_id": res["line_id"], "key": l["key"],
                                  "label": item_label(l["key"], habits.who_name(l.get("who")), channels),
                                  "decision": res.get("decision"), "kind": v.get("kind") if v else None,
                                  "urgency": v.get("urgency") if v else None,
                                  "reason": _reason(v) or (safe_line(hush["why"], VERDICT_REASON_MAX) if hush else ""),
                                  "title": _one_line(v["title"] if v else str(l.get("title") or ""), TITLE_MAX)})
            if res.get("decision") == "now" and res.get("update_id"):
                extra, note = verdict_extra(v), habits.habit_note(v)
                extras[res["update_id"]] = dict(extra, habit_note=note) if extra and note else extra
                shown.append(habits.shown_row(at, res["update_id"], lines_by_id[res["line_id"]], v))
            elif res.get("decision") == "log" and res["line_id"] in quieted:
                hushed.append(quieted[res["line_id"]])
        if extras:
            _store_extras(ws, extras)
        habits.append_actions(ws, at, shown)     # what was shown: the denominator of every habit
        habits.record_quieted(ws, at, hushed)
        log_verdicts(ws, at, explained)
        sync_relays(ws)   # a line routed to a case session waits in relays.json for that session to deliver
    out["logged"] = [it["line_id"] for it in items if it["decision"] == "log"]
    out["now"] = [it["line_id"] for it in items if it["decision"] == "now"]
    return out


INBOX_MAX = 10


def source_of(key) -> str:
    """An item's source from its key's shape (docs/CONTRACT.md §5e "Formats"), as hooks/pane_ui.tsx `sourceOf`."""
    k = str(key or "").strip()
    if k.startswith("watcher:"):
        return "error"
    if k.startswith("mail:"):
        return "mail"
    if k.startswith("cal:"):
        return "calendar"
    if k.startswith("manual:"):
        return "manual"
    if k.startswith("wa:"):
        return "chat"
    if re.match(r"^[A-Z][A-Z0-9_]*-\d+$", k):
        return "tracker"
    if re.match(r"^[\w.-]+/[\w.-]+#\d+$", k):
        return "code"
    if re.match(r"^[A-Z0-9]+/(dm|\d+(\.\d+)?)$", k):
        return "chat"
    return "other"


LABEL_MAX = 40
_FIXED_LABELS = {"mail": "mail thread", "calendar": "calendar", "manual": "note", "error": "source error", "other": "update"}


def _channel_names(cfg: dict) -> dict:
    """{chat channel id: its configured name} across the config's teams."""
    names = {}
    for t in (cfg or {}).get("teams") or []:
        for ch in (t.get("channels") or []) if isinstance(t, dict) else []:
            if isinstance(ch, dict) and ch.get("id") and isinstance(ch.get("name"), str) and ch["name"].strip():
                names[str(ch["id"])] = ch["name"].strip()
    return names


def item_label(key, who: str, channels: dict) -> str:
    """What a card says in place of a raw key: a tracker key or `owner/repo#N` as it is; for chat the channel's
    configured name, else the person, else `chat thread`; `mail thread`; `calendar`. Never a raw chat or mail id."""
    k, source = str(key or "").strip(), source_of(key)
    if source in ("tracker", "code"):
        return safe_line(k, LABEL_MAX)
    if source == "chat":
        name = channels.get(k.split("/", 1)[0]) if "/" in k else None
        if name:
            return safe_line("#" + name.lstrip("#"), LABEL_MAX)
        return safe_line(who, LABEL_MAX) or "chat thread"
    return _FIXED_LABELS[source]


def _who_by_key(ws: Path) -> dict:
    """{key: who} from the newest inbox line of each key that names someone; a trailing `<id>` is left out."""
    out = {}
    for l in hub.read_inbox(ws):
        who = l.get("who")
        if isinstance(who, str) and who.strip():
            out[l["key"]] = re.sub(r"\s*<[^<>]*>\s*$", "", who).strip()
    return out


# ---- where an item came from: one link builder for the Inbox, the cases and the pane ----
LINKS_MAX = 8
LINK_LABEL_MAX = 24
LINK_KIND = {"chat": "thread", "tracker": "issue", "code": "PR", "mail": "mail", "calendar": "event"}
_HTTP = re.compile(r"^https?://\S+$")
_CHAT_BASE = re.compile(r"^(https://[\w.-]+)/archives/[A-Z0-9]+(?:/|$)")
_TRACKER_BASE = re.compile(r"^(https?://[\w.:-]+(?:/[\w.-]+)*?)/browse/[A-Z][A-Z0-9_]*-\d+")
_CODE_HOST = "https://github.com"
_CHAT_TS = re.compile(r"^\d{9,11}\.\d{6}$")


def link_bases(ws: Path, cfg: Optional[dict], lines: Optional[List[dict]] = None) -> dict:
    """{chat, tracker, code}: the URL base of each source, or None when nothing reveals it. Never a guess: the chat
    base is the host of a stored chat message link, the tracker base a team's `tracker.url` in the config else the
    part before `/browse/` in a stored issue link, the code host only when the config's code connector is GitHub
    (`connectors.code`, or `hub.connections.code.cli: gh`) or a stored link is already on it."""
    cfg = cfg if isinstance(cfg, dict) else {}
    bases = {"chat": None, "tracker": None, "code": None}
    for t in cfg.get("teams") or []:
        tr = t.get("tracker") if isinstance(t, dict) else None
        url = tr.get("url") if isinstance(tr, dict) else None
        if isinstance(url, str) and _HTTP.match(url) and not bases["tracker"]:
            bases["tracker"] = re.sub(r"/browse(/.*)?$", "", url.rstrip("/"))
    conn = ((cfg.get("hub") or {}).get("connections") or {}).get("code") if isinstance(cfg.get("hub"), dict) else None
    if (cfg.get("connectors") or {}).get("code") == "github" or (isinstance(conn, dict) and conn.get("cli") == "gh"):
        bases["code"] = _CODE_HOST
    stored = [l.get("link") for l in (hub.read_inbox(ws) if lines is None else lines)]
    stored += [u.get("link") for u in hub._hub_doc(ws)["updates"] if isinstance(u, dict)]
    for link in stored:
        if not isinstance(link, str):
            continue
        m = _CHAT_BASE.match(link)
        if m and not bases["chat"]:
            bases["chat"] = m.group(1)
        m = _TRACKER_BASE.match(link)
        if m and not bases["tracker"]:
            bases["tracker"] = m.group(1)
        if link.startswith(_CODE_HOST + "/") and not bases["code"]:
            bases["code"] = _CODE_HOST
    return bases


def source_link(key, bases: dict) -> str:
    """The URL of the thing a key names, built from a known base; '' when the base is not known or the key has no
    address (mail, calendar, a note, a WhatsApp chat: those keep the watcher's own link)."""
    k, src = str(key or "").strip(), source_of(key)
    if src == "chat" and bases.get("chat") and "/" in k and not k.startswith("wa:"):
        ch, ts = k.split("/", 1)
        if ts == "dm":
            return "%s/archives/%s" % (bases["chat"], ch)
        if _CHAT_TS.match(ts):
            return "%s/archives/%s/p%s" % (bases["chat"], ch, ts.replace(".", ""))
    if src == "tracker" and bases.get("tracker"):
        return "%s/browse/%s" % (bases["tracker"], k)
    if src == "code" and bases.get("code"):
        repo, n = k.rsplit("#", 1)
        return "%s/%s/pull/%s" % (bases["code"], repo, n)
    return ""


def _url_label(url: str) -> str:
    m = re.search(r"/browse/([A-Z][A-Z0-9_]*-\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/([\w.-]+)/(pull|issues)/(\d+)", url)
    if m:
        return "%s #%s" % ("PR" if m.group(2) == "pull" else "issue", m.group(3))
    if "/archives/" in url:
        return "thread"
    host = re.match(r"https?://([^/?#]+)", url)
    return host.group(1) if host else "link"


def item_links(key, link, also, text, bases: dict) -> Tuple[str, List[dict]]:
    """(link, links) for one item: `link` is its own stored link when that is a URL, else the one built from its key
    (`source_link`), else ''. `links` is [{label, url}], the source first (labelled by kind: thread, issue, PR, mail,
    event, else source), then the things its `also` keys name and the URLs in `text`; no URL twice, at most 8."""
    own = link if isinstance(link, str) and _HTTP.match(link) else source_link(key, bases)
    out, seen = [], set()

    def add(label, url):
        if url and _HTTP.match(url) and url not in seen and len(out) < LINKS_MAX:
            seen.add(url)
            out.append({"label": safe_line(label, LINK_LABEL_MAX) or "link", "url": url})

    add(LINK_KIND.get(source_of(key), "source"), own)
    for a in also if isinstance(also, list) else []:
        if isinstance(a, str):
            add(a, source_link(a, bases))
    for m in hub._URL.finditer(text if isinstance(text, str) else ""):
        url = m.group(0).rstrip(".,;:!?)>]\"'")
        add(_url_label(url), url)
    return own, out


# ---- the ask box: a case started from the manager's own words (hooks/chief.tsx startAsk) ----
# `ask` appends the manual line and holds its key in chief/asks.json. While the hold is fresh and unclaimed the
# item it makes is not an Inbox card; the session that starts from the brief claims the key (a hold stands in for
# the open item a claim needs), which closes that item, and the line itself is never relayed back into the case.
ASK_HOLD_MINUTES = 10
ASK_KEEP_DAYS = 7
ASK_TEXT_MAX = 2000


def _asks(ws: Path) -> dict:
    d = _read_obj(chief_dir(ws) / "asks.json").get("asks")
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def _save_asks(ws: Path, asks: dict, at: datetime) -> None:
    def fresh(v):
        t = hub._parse_at(v.get("at"))
        return t is not None and (at - t).total_seconds() < ASK_KEEP_DAYS * 86400
    kept = {k: v for k, v in asks.items() if fresh(v)}
    _write_atomic(chief_dir(ws) / "asks.json", json.dumps({"version": 1, "asks": kept}, ensure_ascii=False) + "\n")


def _ask_held(asks: dict, key, at: Optional[datetime]) -> bool:
    """Is `key` held by an ask that no session claimed yet and that is under ASK_HOLD_MINUTES old?"""
    a = asks.get(key)
    if a is None or at is None or a.get("claimed"):
        return False
    t = hub._parse_at(a.get("at"))
    return t is not None and (at - t).total_seconds() < ASK_HOLD_MINUTES * 60


def ask(ws: Path, cfg: dict, at: datetime, text) -> dict:
    """The ask box: append the manager's text as a `manual` line (`hub.add_line`, as `mother hub add`) and say what
    the pane needs to start a case: {ok, key, line_id, link, update_id, owned_by}. `owned_by` is the session id of
    the live case that already holds the key (the hub routes the line there; no new case); `update_id` the open
    inbox item the key already has (the new session claims that one). With neither, the key is held."""
    text = hub.plain(text if isinstance(text, str) else "").strip()[:ASK_TEXT_MAX]
    if not text:
        raise ValueError("no text")
    ws = Path(ws)
    ln = hub.add_line(ws, at, text)
    key = ln["key"]
    out = {"ok": True, "key": key, "line_id": ln["id"], "link": ln["link"], "update_id": None, "owned_by": None}
    with hub.locked(ws):
        hit = hub.find_session(hub._sessions_doc(ws), [key] + list(ln.get("also") or []))
        if hit is not None and not _is_cli(hit[1]):
            out["owned_by"] = hit[1].get("cli_session_id") or hit[1].get("session_id")
            return out
        upd = next((u for u in hub._hub_doc(ws)["updates"] if isinstance(u, dict) and u.get("key") == key
                    and u.get("state") != "done"), None)
        if upd is not None:
            out["update_id"] = upd.get("id")
            return out
        asks = _asks(ws)
        asks[key] = {"at": hub._iso(at), "line_id": ln["id"], "claimed": None}
        _save_asks(ws, asks, at)
    return out


def _item_session(hit) -> dict:
    """An inbox item's live session: {key, title}, plus kind and cli_session_id when it is a case."""
    d = {"key": hit[0], "title": safe_line(hit[1].get("title") or hit[0], TITLE_MAX)}
    if hit[1].get("kind") == "case":
        d.update({"kind": "case", "cli_session_id": hit[1].get("cli_session_id")})
    return d


def inbox_items(ws: Path, cfg: Optional[dict] = None, at: Optional[datetime] = None) -> List[dict]:
    """The hub's open updates (what a `now` decision makes), by urgency (`now`, `today`, `later`; none reads
    `today`) then newest first, each with its triage `kind`, `on`, `urgency` and `actions` when it has them, for the Chief pane's Inbox section. The
    text is line- or model-written, so each field is one safe line. `session` is the live hub session for the key.
    `source` is the key's kind, `who` the person its newest inbox line names ('' when none) and `label` what a card
    shows in place of the raw key (`item_label`). `link` is the item's own link, else one built from its key
    (`item_links`; '' when no base is known), and `links` every labelled source, that one first. With `at`, an item
    whose key the ask box holds (`_ask_held`) is left out."""
    h, sessions = hub._hub_doc(ws), hub._sessions_doc(ws)
    whos, channels, extras = _who_by_key(ws), _channel_names(cfg or {}), _triage_items(ws)
    raw = hub.read_inbox(ws)
    newest = {l["key"]: l for l in raw}
    bases, asks = link_bases(ws, cfg, raw), _asks(ws)
    rows = []
    for u in h["updates"]:
        if not isinstance(u, dict) or u.get("state") != "open" or not isinstance(u.get("id"), str):
            continue
        if _ask_held(asks, u.get("key"), at):
            continue
        line = newest.get(u.get("key")) or {}
        link, links = item_links(u.get("key"), u.get("link"), line.get("also"), line.get("summary"), bases)
        hit = hub.find_session(sessions, [u.get("key")])
        who = safe_line(whos.get(u.get("key")) or "", LABEL_MAX)
        rows.append({"id": u["id"], "key": safe_line(u.get("key"), PROGRESS_MAX),
                     "title": safe_line(u.get("title"), TITLE_MAX), "why": safe_line(u.get("why"), WHY_MAX),
                     "link": link or (u.get("link") if isinstance(u.get("link"), str) else ""), "links": links,
                     "at": u.get("at"),
                     "source": source_of(u.get("key")), "who": who, "label": item_label(u.get("key"), who, channels),
                     "session": (_item_session(hit) if hit and not _is_cli(hit[1]) else None)})
        # the triage verdict's extras, cleaned again on the way out (the file is only data); None / [] when none
        x = verdict_extra(extras.get(u["id"])) or {"kind": None, "on": None, "urgency": None, "actions": []}
        rows[-1].update(x)
        rows[-1]["habit_note"] = habits.habit_note(extras.get(u["id"]))   # set when habits changed the verdict
    rows.sort(key=lambda r: str(r["at"] or ""), reverse=True)
    rows.sort(key=lambda r: URGENCIES.index(r["urgency"] or URGENCY_DEFAULT))   # stable: newest first inside one urgency
    return rows



# ---- cases: a desktop session that owns one inbox item until it is closed (skills/cos, hooks/cos.ts) ----------
# A case is a row in the hub's sessions.json (`kind: "case"`, `session_id` = the session's CLI id), so hub.decide
# routes later lines of the key to it. What was routed and not yet delivered into the session waits in
# chief/relays.json; the owning session's own mod reads it (`relays`), submits it there, then acknowledges it (`relays --ack`).

CASE_STALE_DAYS = 7
RELAY_DONE_KEEP = 200      # delivered line ids remembered per key
RELAY_WAIT_MAX = 100       # waiting lines kept per key, newest
RELAY_WHO_MAX, RELAY_SUMMARY_MAX = 80, 300


def relay_on(ws: Path) -> bool:
    """Relay is on unless the machine-local switch .mother/chief/RELAY_OFF exists."""
    return not (chief_dir(ws) / "RELAY_OFF").exists()


def set_relay(ws: Path, on: bool) -> None:
    path = chief_dir(ws) / "RELAY_OFF"
    if on:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


def _deliver_flag(ws: Path, cli: str) -> Path:
    return chief_dir(ws) / "relay-now" / cli


def relay_deliver(ws: Path, cli: str) -> None:
    """Ask the session `cli` to deliver what waits once, on its next pass, though relay is off."""
    path = _deliver_flag(ws, cli)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def _is_case(s) -> bool:
    return isinstance(s, dict) and s.get("kind") == "case" and s.get("state") == "live"


def _row_age_days(row: dict, at: datetime) -> float:
    """Days since the row was opened or last routed to, whichever is later; unreadable stamps read as stale."""
    stamps = [hub._parse_at(row.get(k)) for k in ("opened_at", "last_routed_at")]
    stamps = [t for t in stamps if t is not None]
    return (at - max(stamps)).total_seconds() / 86400.0 if stamps else float("inf")


def claim(ws: Path, cfg: dict, at: datetime, doc) -> dict:
    """A session claims a key (`claim --stdin` {key, session_id, title, link, also?}): its sessions.json row becomes
    a live case of that session, and the key's open update is linked to it. `also` is more keys (threads, tickets,
    PRs) the same case owns, so their lines route to it too. ValueError when refused: an off-shape key or session
    id, no open inbox item for the key and no ask-box hold on it (a claim never makes a route for a key nobody
    raised), or a key (the main one or one of `also`) live in another session whose row is younger than
    CASE_STALE_DAYS. A key the ask box holds: its item, if the leader already made one, is closed here (the case
    replaces the card), and the hold is marked claimed."""
    if not isinstance(doc, dict):
        raise ValueError("stdin must be a JSON object {key, session_id, title, link}")
    key, cli = doc.get("key"), doc.get("session_id")
    if not (isinstance(key, str) and hub._KEY_TOKEN.fullmatch(key)):
        raise ValueError("key is not a hub key")
    if not (isinstance(cli, str) and CLI_ID_RE.fullmatch(cli)):
        raise ValueError("session_id is not a session id")
    link = doc.get("link")
    link = link if isinstance(link, str) and re.match(r"https?://\S+$", link) else None
    extra = doc.get("also") if doc.get("also") is not None else []
    if not (isinstance(extra, list) and all(isinstance(a, str) and hub._KEY_TOKEN.fullmatch(a) for a in extra)):
        raise ValueError("also must be a list of hub keys")
    stamp = hub._iso(at)
    with hub.locked(ws):
        sess, h = hub._sessions_doc(ws), hub._hub_doc(ws)
        asks = _asks(ws)
        held = asks.get(key)
        items = sess["sessions"]
        upd = next((u for u in h["updates"] if isinstance(u, dict) and u.get("key") == key
                    and u.get("state") != "done"), None)
        hit = hub.find_session(sess, [key])
        mine = None
        if hit is not None and not _is_cli(hit[1]):
            if str(hit[1].get("session_id")) == cli:
                mine = hit
            elif _row_age_days(hit[1], at) <= CASE_STALE_DAYS:
                raise ValueError("%s is live in another session; refusing to repoint it" % key)
            else:
                hit[1]["state"] = "dropped"   # a stale owner: the key moves to the claiming session
        if mine is None and upd is None and held is None:
            raise ValueError("%s has no open inbox item; nothing to claim" % key)
        for a in extra:
            _refuse_owned(sess, a, cli, at)
        sk = mine[0] if mine else key
        prev = mine[1] if mine else {}
        title = safe_line(doc.get("title") or (upd or {}).get("title") or prev.get("title") or key, TITLE_MAX)
        items[sk] = {"session_id": cli, "cli_session_id": cli, "kind": "case",
                     "name": prev.get("name") or "case-" + re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")[:40],
                     "title": title, "link": link or (upd or {}).get("link") or prev.get("link") or None,
                     "also": _merged_also(prev.get("also"), [a for a in extra if a != sk]),
                     "opened_at": prev.get("opened_at") or stamp, "last_routed_at": prev.get("last_routed_at"),
                     "unseen": int(prev.get("unseen") or 0), "state": "live"}
        if held is not None:
            if upd is not None:
                upd["state"] = "done"       # the ask's own item: the case stands in its place
                upd.pop("snooze_until", None)
            held["claimed"] = cli
            _save_asks(ws, asks, at)
        elif upd is not None:
            upd["session_id"] = cli
        h["pending"] = [p for p in h["pending"] if p.get("key") not in [key] + extra]
        hub.save_json(ws, "sessions.json", sess)
        hub._save_hub(ws, h, sess, cfg, at)
    return {"ok": True, "key": sk, "session_id": cli, "update_id": upd.get("id") if upd else None,
            "also": items[sk]["also"]}


def _refuse_owned(sess: dict, key: str, cli: str, at: datetime) -> None:
    """ValueError when `key` is live in another session whose row is younger than CASE_STALE_DAYS."""
    hit = hub.find_session(sess, [key])
    if hit is not None and not _is_cli(hit[1]) and str(hit[1].get("session_id")) != cli \
            and _row_age_days(hit[1], at) <= CASE_STALE_DAYS:
        raise ValueError("%s is live in another session; refusing to repoint it" % key)


def _merged_also(prev, more) -> List[str]:
    keep = [a for a in (prev or []) if isinstance(a, str)]
    for a in more:
        if a not in keep:
            keep.append(a)
    return keep[-hub.ALSO_MAX:]


def watch_key(ws: Path, cfg: dict, at: datetime, key, cli) -> dict:
    """`watch-key <key> --for <session id>`: add one more key (a related thread, ticket or PR) to a live case that
    session holds, so its lines route there too. Refused as a claim is: an off-shape key or session id, a key live
    in another session whose row is under CASE_STALE_DAYS old; and when that session holds no live case (a key is
    only ever added to a case that an inbox item or an ask opened)."""
    if not (isinstance(key, str) and hub._KEY_TOKEN.fullmatch(key)):
        raise ValueError("key is not a hub key")
    if not (isinstance(cli, str) and CLI_ID_RE.fullmatch(cli)):
        raise ValueError("--for is not a session id")
    with hub.locked(ws):
        sess, h = hub._sessions_doc(ws), hub._hub_doc(ws)
        mine = sorted(((sk, r) for sk, r in sess["sessions"].items() if _is_case(r) and r.get("cli_session_id") == cli),
                      key=lambda x: str(x[1].get("opened_at") or ""))
        if not mine:
            raise ValueError("that session holds no live case; nothing to add %s to" % key)
        _refuse_owned(sess, key, cli, at)
        hit = hub.find_session(sess, [key])
        sk, row = hit if hit is not None and str(hit[1].get("session_id")) == cli else mine[0]
        if key != sk:
            row["also"] = _merged_also(row.get("also"), [key])
        for u in h["updates"]:
            if isinstance(u, dict) and u.get("key") == key and u.get("state") != "done":
                u["session_id"] = cli
        h["pending"] = [p for p in h["pending"] if p.get("key") != key]
        hub.save_json(ws, "sessions.json", sess)
        hub._save_hub(ws, h, sess, cfg, at)
    return {"ok": True, "key": sk, "session_id": cli, "also": [a for a in row.get("also") or [] if isinstance(a, str)]}


MENTION_KEYS_MAX = 5
MENTION_TEXT_MAX = 8000


def mention_keys(text) -> List[str]:
    """The keys a prompt names, in order, at most 5: each chat message link's thread, then tracker keys and PRs."""
    text = str(text or "")[:MENTION_TEXT_MAX]
    out = []
    for m in hub._SLACK_LINK.finditer(text):
        k = hub.thing_key("chat", {"channel": m.group("c"), "ts": "%s.%s" % (m.group("a"), m.group("b")),
                                   "thread_ts": m.group("t")})
        if k not in out:
            out.append(k)
    for k in hub.extract_also(text):
        if k not in out:
            out.append(k)
    return [k for k in out if hub._KEY_TOKEN.fullmatch(k)][:MENTION_KEYS_MAX]


def mention(ws: Path, cfg: dict, at: datetime, doc) -> dict:
    """`mention --stdin` {session_id, text?, keys?, title?}: the keys the manager named to a session (the chat links,
    tracker keys and PRs of a prompt they typed there, or an Inbox item they sent there) join that session's case, so
    their lines route to it; a session with no live case gets one (`origin: "mention"`), its first key the row's.
    At most 5 keys. A key live in another session whose row is under 7 days old is refused, and so is a key already
    this session's (nothing to add). Prints {added, refused}; ValueError for an off-shape id."""
    if not isinstance(doc, dict):
        raise ValueError("stdin must be a JSON object {session_id, text?, keys?, title?}")
    cli = doc.get("session_id")
    if not (isinstance(cli, str) and CLI_ID_RE.fullmatch(cli)):
        raise ValueError("session_id is not a session id")
    keys = mention_keys(doc.get("text"))
    for k in doc.get("keys") if isinstance(doc.get("keys"), list) else []:
        if isinstance(k, str) and hub._KEY_TOKEN.fullmatch(k) and k not in keys:
            keys.append(k)
    keys = keys[:MENTION_KEYS_MAX]
    added, refused = [], []
    stamp = hub._iso(at)
    with hub.locked(ws):
        sess, h = hub._sessions_doc(ws), hub._hub_doc(ws)
        items = sess["sessions"]
        for key in keys:
            hit = hub.find_session(sess, [key])
            if hit is not None and str(hit[1].get("session_id")) == cli and _is_case(hit[1]):
                continue   # already this session's
            try:
                _refuse_owned(sess, key, cli, at)
            except ValueError:
                refused.append(key)
                continue
            if hit is not None and not _is_cli(hit[1]) and str(hit[1].get("session_id")) != cli:
                hit[1]["state"] = "dropped"   # a stale owner: the key moves
            mine = sorted(((sk, r) for sk, r in items.items() if _is_case(r) and r.get("cli_session_id") == cli),
                          key=lambda x: str(x[1].get("opened_at") or ""))
            if mine:
                row = mine[0][1]
                if key != mine[0][0]:
                    row["also"] = _merged_also(row.get("also"), [key])
            else:
                title = safe_line(doc.get("title") or key, TITLE_MAX)
                items[key] = {"session_id": cli, "cli_session_id": cli, "kind": "case", "origin": "mention",
                              "name": "case-" + re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")[:40], "title": title,
                              "link": None, "also": [], "opened_at": stamp, "last_routed_at": None, "unseen": 0,
                              "state": "live"}
            for u in h["updates"]:
                if isinstance(u, dict) and u.get("key") == key and u.get("state") != "done":
                    u["session_id"] = cli
            h["pending"] = [p for p in h["pending"] if p.get("key") != key]
            added.append(key)
        if added:
            hub.save_json(ws, "sessions.json", sess)
            hub._save_hub(ws, h, sess, cfg, at)
    return {"ok": True, "session_id": cli, "added": added, "refused": refused}


def close_case(ws: Path, cfg: dict, at: datetime, key) -> dict:
    """`close-case <key>`: the live case that holds `key` is done, and so is every open inbox item of its keys; later
    activity on them raises a new item. ValueError when no live case holds the key."""
    with hub.locked(ws):
        sess, h = hub._sessions_doc(ws), hub._hub_doc(ws)
        hit = hub.find_session(sess, [key]) if isinstance(key, str) else None
        if hit is None or not _is_case(hit[1]):
            raise ValueError("no live case holds %s" % key)
        sk, row = hit
        row["state"] = "done"
        keys = [sk] + [a for a in row.get("also") or [] if isinstance(a, str)]
        closed = []
        for u in h["updates"]:
            if isinstance(u, dict) and u.get("key") in keys and u.get("state") != "done":
                u["state"] = "done"
                u.pop("snooze_until", None)
                closed.append(u.get("id"))
        hub.save_json(ws, "sessions.json", sess)
        hub._save_hub(ws, h, sess, cfg, at)
    return {"ok": True, "key": sk, "session_id": row.get("cli_session_id"), "updates": closed}


def _relays_doc(ws: Path) -> dict:
    d = _read_obj(chief_dir(ws) / "relays.json")
    if not isinstance(d.get("keys"), dict):
        d = {"version": 1, "keys": {}}
    return d


def _merge_routed(doc: dict, h: dict, sess: dict, inbox: Optional[dict], skip=()) -> bool:
    """Copy hub.json's routes to live case sessions into `doc` (relays.json) as waiting lines, once each; forget the
    keys that are no longer a live case. `inbox` is {line id: line} (None = counts only, no text). `skip` is line
    ids never relayed: the ask box's own lines, which the case already started from. True when `doc` changed."""
    rows = {sk: r for sk, r in sess["sessions"].items() if _is_case(r)}
    changed = False
    for sk in [k for k in doc["keys"] if k not in rows]:
        del doc["keys"][sk]
        changed = True
    for e in h["routed"]:
        if not isinstance(e, dict) or e.get("decision") != "route" or not isinstance(e.get("line_id"), str) \
                or e["line_id"] in skip:
            continue
        hit = hub.find_session({"sessions": rows}, [e.get("key")])
        if hit is None or hit[1].get("session_id") != e.get("session_id"):
            continue
        ent = doc["keys"].setdefault(hit[0], {"waiting": [], "done": []})
        if e["line_id"] in ent["done"] or any(w.get("line_id") == e["line_id"] for w in ent["waiting"]):
            continue
        l = (inbox or {}).get(e["line_id"]) or {}
        link = l.get("link")
        ent["waiting"].append({
            "line_id": e["line_id"], "key": safe_line(e.get("key"), PROGRESS_MAX), "at": e.get("at"),
            "who": safe_line(re.sub(r"\s*<[^<>]*>\s*$", "", str(l.get("who") or "")), RELAY_WHO_MAX),
            "title": safe_line(l.get("title"), TITLE_MAX), "summary": safe_line(l.get("summary"), RELAY_SUMMARY_MAX),
            "link": link if isinstance(link, str) and re.match(r"https?://\S+$", link) else ""})
        ent["waiting"] = ent["waiting"][-RELAY_WAIT_MAX:]
        changed = True
    return changed


def _ask_lines(ws: Path) -> set:
    return {a.get("line_id") for a in _asks(ws).values() if isinstance(a.get("line_id"), str)}


def sync_relays(ws: Path) -> None:
    """Move new routes to case sessions from hub.json (which keeps only its last 50 decisions) into relays.json."""
    ws = Path(ws)
    inbox = {l["id"]: l for l in hub.read_inbox(ws)}
    with hub.locked(ws):
        doc = _relays_doc(ws)
        if _merge_routed(doc, hub._hub_doc(ws), hub._sessions_doc(ws), inbox, _ask_lines(ws)):
            _write_atomic(chief_dir(ws) / "relays.json", json.dumps(doc, ensure_ascii=False) + "\n")


def relays(ws: Path, at: datetime, cli: str) -> List[dict]:
    """The routed lines not yet delivered into session `cli`, for the live cases it holds, oldest first:
    [{id, key, at, who, title, summary, link}]. Nothing is removed: the session submits them, then acknowledges the
    ids it delivered (`relay_ack`). New routes are copied in from hub.json on the way."""
    ws = Path(ws)
    inbox = {l["id"]: l for l in hub.read_inbox(ws)}
    with hub.locked(ws):
        sess = hub._sessions_doc(ws)
        doc = _relays_doc(ws)
        if _merge_routed(doc, hub._hub_doc(ws), sess, inbox, _ask_lines(ws)):
            _write_atomic(chief_dir(ws) / "relays.json", json.dumps(doc, ensure_ascii=False) + "\n")
        mine = [sk for sk, r in sess["sessions"].items() if _is_case(r) and r.get("cli_session_id") == cli]
        rows = sorted((w for sk in mine for w in (doc["keys"].get(sk) or {}).get("waiting", [])),
                      key=lambda w: str(w.get("at") or ""))
    return [dict({"id": w.get("line_id")}, **{k: w.get(k) for k in ("key", "at", "who", "title", "summary", "link")})
            for w in rows]


def relay_ack(ws: Path, at: datetime, cli: str, ids) -> int:
    """Session `cli` delivered the lines `ids` (as `relays` named them): they stop waiting, each of its rows'
    `unseen` becomes what still waits, and its deliver-once flag is removed. Under the hub lock; an id that is not
    waiting for this session is ignored. Returns how many were acknowledged."""
    ws = Path(ws)
    ids = {i for i in (ids or []) if isinstance(i, str)}
    n = 0
    with hub.locked(ws):
        sess = hub._sessions_doc(ws)
        doc = _relays_doc(ws)
        mine = [sk for sk, r in sess["sessions"].items() if _is_case(r) and r.get("cli_session_id") == cli]
        for sk in mine:
            ent = doc["keys"].get(sk) or {"waiting": [], "done": []}
            hit = [w.get("line_id") for w in ent["waiting"] if w.get("line_id") in ids]
            if hit:
                ent["waiting"] = [w for w in ent["waiting"] if w.get("line_id") not in ids]
                ent["done"] = (ent["done"] + hit)[-RELAY_DONE_KEEP:]
                doc["keys"][sk] = ent
                n += len(hit)
            sess["sessions"][sk]["unseen"] = len(ent["waiting"])
        if mine:
            hub.save_json(ws, "sessions.json", sess)
        if n:
            _write_atomic(chief_dir(ws) / "relays.json", json.dumps(doc, ensure_ascii=False) + "\n")
        if CLI_ID_RE.fullmatch(cli or ""):
            try:
                _deliver_flag(ws, cli).unlink()
            except OSError:
                pass
    return n


def cases(ws: Path, cfg: Optional[dict] = None) -> List[dict]:
    """The live case rows for the pane and for each session's own mod: [{key, cli_session_id, title, link, links, also, label,
    unseen, last_routed_at, opened_at, deliver_now}], oldest first. `unseen` is how many routed lines wait to be
    delivered. Reads only."""
    sess, doc = hub._sessions_doc(ws), _relays_doc(ws)
    _merge_routed(doc, hub._hub_doc(ws), sess, None, _ask_lines(ws))
    whos, channels = _who_by_key(ws), _channel_names(cfg or {})
    bases = link_bases(ws, cfg)
    out = []
    for sk, r in sess["sessions"].items():
        cli = r.get("cli_session_id") if _is_case(r) else None
        if not (isinstance(cli, str) and CLI_ID_RE.fullmatch(cli)):
            continue
        also = [a for a in r.get("also") or [] if isinstance(a, str)]
        link, links = item_links(sk, r.get("link"), also, "", bases)
        out.append({"key": safe_line(sk, PROGRESS_MAX), "cli_session_id": cli,
                    "title": safe_line(r.get("title") or sk, TITLE_MAX),
                    "link": link or None, "links": links, "also": [safe_line(a, PROGRESS_MAX) for a in also],
                    "label": item_label(sk, safe_line(whos.get(sk) or "", LABEL_MAX), channels),
                    "unseen": len((doc["keys"].get(sk) or {}).get("waiting", [])),
                    "last_routed_at": r.get("last_routed_at"), "opened_at": r.get("opened_at"),
                    "deliver_now": _deliver_flag(ws, cli).exists(),
                    **({"origin": "mention"} if r.get("origin") == "mention" else {})})
    out.sort(key=lambda c: str(c["opened_at"] or ""))
    return out


# ---- pinned desktop sessions: a snapshot the console writes from list_sessions, read back for the pane ----------

REPORT_MAX = 200
APP_FILE_MAX = 2 * 1024 * 1024    # an app session file past this is skipped, never parsed
APP_FIELDS = ("cliSessionId", "isArchived", "cwd", "lastActivityAt", "lastTurnReport")
LOCAL_ID_RE = re.compile(r"local_[0-9A-Za-z-]{1,80}")
CLI_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def default_app_dir() -> Path:
    """Where the desktop app keeps one local_<id>.json per Claude Code session (under account and org dirs)."""
    return Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"


def _app_fields(path: Path, cache: Optional[dict] = None) -> Optional[dict]:
    """The few fields the pane needs from one app session file (APP_FIELDS), or None when it is missing, over 2 MB
    or not a JSON object. `cache` maps str(path) -> {mtime_ns, size, fields}: an unchanged file is not re-read."""
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > APP_FILE_MAX:
        return None
    key = str(path)
    hit = (cache or {}).get(key)
    if isinstance(hit, dict) and hit.get("mtime_ns") == st.st_mtime_ns and hit.get("size") == st.st_size \
            and isinstance(hit.get("fields"), dict):
        return hit["fields"]
    doc = _read_obj(path)
    if not doc:
        return None
    fields = {k: doc[k] for k in APP_FIELDS if k in doc}
    if isinstance(fields.get("lastTurnReport"), str):
        fields["lastTurnReport"] = fields["lastTurnReport"][:REPORT_MAX]
    if cache is not None:
        cache[key] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "fields": fields}
    return fields


def _session_file(app_dir: Path, sid: str, cache: Optional[dict] = None) -> Optional[dict]:
    """The app's own record for `sid` (local_…): app_dir/, app_dir/*/ or app_dir/*/*/local_<id>.json, as its
    APP_FIELDS; None when the id is malformed, no file is found, or it cannot be read (see _app_fields)."""
    if not (isinstance(sid, str) and LOCAL_ID_RE.fullmatch(sid)):
        return None
    name = sid + ".json"
    base = Path(app_dir)
    for pattern in (name, "*/" + name, "*/*/" + name):
        for path in sorted(base.glob(pattern)):
            doc = _app_fields(path, cache)
            if doc:
                return doc
    return None


def _cli_id(doc: Optional[dict]) -> Optional[str]:
    """The session's CLI id (what $.session.send reaches it by), from a live, unarchived app record."""
    if not doc or doc.get("isArchived") is True:
        return None
    cli = doc.get("cliSessionId")
    return cli if isinstance(cli, str) and CLI_ID_RE.fullmatch(cli) else None


def pinned_snapshot(ws: Path, at: datetime, rows: List[dict], app_dir: Optional[Path] = None) -> int:
    """Write chief/desktop.json {at, sessions:[{session_id, cli_session_id, title, cwd, link}]} from the
    list_sessions rows with `pinned: true`. The CLI id comes from the app's local_<id>.json, never from the row;
    a row whose file is missing or archived (or the row itself archived) is dropped. Returns how many were kept."""
    app_dir = Path(app_dir or default_app_dir())
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("pinned") is not True or r.get("isArchived") is True:
            continue
        sid = r.get("sessionId")
        cli = _cli_id(_session_file(app_dir, sid))
        if cli is None:
            continue
        link = r.get("link")
        out.append({"session_id": sid, "cli_session_id": cli, "title": str(r.get("title") or sid),
                    "cwd": str(r.get("cwd") or ""), "link": link if isinstance(link, str) and link else None})
    with hub.locked(ws):
        _write_atomic(chief_dir(ws) / "desktop.json",
                      json.dumps({"at": hub._iso(at), "sessions": out}, indent=2) + "\n")
    return len(out)


def _iso_ms(ms) -> Optional[str]:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    try:
        return hub._iso(datetime.fromtimestamp(ms / 1000.0, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def desktop_status(ws: Path, app_dir: Optional[Path] = None) -> List[dict]:
    """The pane's Pinned rows: each snapshot row still live in the app, with its repo (cwd basename), last activity
    (iso) and the first 200 characters of its last turn report. The fields read from each app file are cached in
    chief/desktop-cache.json by mtime and size, so an unchanged file (often hundreds of KB) is not parsed again."""
    app_dir = Path(app_dir or default_app_dir())
    snap = _read_obj(chief_dir(ws) / "desktop.json").get("sessions")
    if not isinstance(snap, list) or not snap:
        return []
    cache_path = chief_dir(ws) / "desktop-cache.json"
    before = _read_obj(cache_path)
    cache = dict(before)
    out = []
    for s in snap:
        if not isinstance(s, dict):
            continue
        doc = _session_file(app_dir, s.get("session_id"), cache)
        cli = _cli_id(doc)
        if cli is None or cli != s.get("cli_session_id"):
            continue
        cwd = str(s.get("cwd") or doc.get("cwd") or "")
        report = doc.get("lastTurnReport")
        out.append({"session_id": s["session_id"], "cli_session_id": cli, "title": str(s.get("title") or ""),
                    "cwd": cwd, "repo": os.path.basename(cwd.rstrip("/")),
                    "last_activity_at": _iso_ms(doc.get("lastActivityAt")),
                    "last_report": report[:REPORT_MAX] if isinstance(report, str) else "",
                    "open_link": s.get("link") if isinstance(s.get("link"), str) else None})
    names = {"%s.json" % s.get("session_id") for s in snap if isinstance(s, dict)}
    cache = {k: v for k, v in cache.items() if os.path.basename(k) in names}  # only the pinned sessions' files
    if cache != before:
        try:
            _write_atomic(cache_path, json.dumps(cache) + "\n")
        except OSError:
            pass  # a cache that cannot be written only costs a re-read
    return out


# ---- the Chief pane's TL;DR: each session's recent tail, and a cache of what the model said ----------------

TAIL_MAX = 6000            # characters of tail text per session that the pane hands the model
TAIL_READ_BYTES = 512 * 1024
TAIL_MESSAGES = 12
TAIL_MESSAGE_MAX = 1500
TLDR_LINES, TLDR_LINE_MAX = 2, 160
CHIP_KINDS = ("reply", "open")
CHIPS_MAX, CHIP_TEXT_MAX, CHIP_LABEL_MAX = 6, 200, 24   # stored; the pane shows at most 3 that apply to the card now


def chip_label(text: str, label=None) -> str:
    """A reply chip's button label: the model's own (one line, at most 24 characters) when it gave one, else the text's
    first words that fit 24 characters, cut at a word boundary with `…` when the text is longer."""
    own = safe_line(label, CHIP_LABEL_MAX) if isinstance(label, str) else ""
    if own and len(safe_line(label, 500)) <= CHIP_LABEL_MAX:
        return own
    text = " ".join(_safe(text).split())
    if len(text) <= CHIP_LABEL_MAX:
        return text
    head = text[:CHIP_LABEL_MAX - 1]
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else head).rstrip(" .,;:!?-") + "…"


def project_slug(path: str) -> str:
    """Claude Code's folder name under ~/.claude/projects for a working directory: every non-alphanumeric
    character becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _block_text(content) -> str:
    """One message's text: a string as is; from a block list the text blocks, plus `[ran <tool>]` per tool call.
    Tool results and thinking are left out (noise, and tool output is the least trusted part)."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
        elif b.get("type") == "tool_use":
            parts.append("[ran %s]" % str(b.get("name") or "a tool")[:40])
    return " ".join(p.strip() for p in parts if p.strip())


RUNNING_SECONDS = 120          # a transcript that moved this recently is running
STUCK_SECONDS = 30 * 60        # a card that said `working` with no movement for this long is stuck ...
STUCK_MAX_SECONDS = 2 * 60 * 60   # ... until this long: past it the session was interrupted or closed, and reads idle
STATES = ("working", "waiting_manager", "waiting_other", "done", "stuck", "idle")
PROGRESS_MAX_CHARS = 160
_ERROR_END_RE = re.compile(r"\b(api error|rate limit|usage limit|limit reached|overloaded|credit balance is too low|"
                           r"request timed out|connection error|prompt is too long)\b", re.IGNORECASE)
ERROR_END_MAX = 400

# The cos skill's (and every pinned session's) last line `Waiting on: <who> — <what>`, read in code: lib/floor.py's
# parser, so the turn-end card and the refresh read it the same way.
waiting_line = floormod.waiting_line


def _transcript_read(path: Path) -> Optional[dict]:
    """The end of a session transcript: {fingerprint, text, mtime, last_from, open_turn, waiting, error_end}. `text`
    is the last TAIL_MESSAGES main-thread user and assistant messages, `role: text` per line, each capped, the
    whole cut to TAIL_MAX keeping the newest; the fingerprint is `t:<size>:<mtime_ns>`. `last_from` is who wrote
    the last message with text; `open_turn` whether the last main-thread entry is a prompt, a tool result or a tool
    call (a turn that has not ended); `waiting` the last assistant message's `Waiting on:` line (`waiting_line`),
    only when that message is the last one; `error_end` whether the tail ends in an API error or limit message.
    None when the file is missing or holds no such message."""
    try:
        st = path.stat()
        with open(str(path), "rb") as f:
            if st.st_size > TAIL_READ_BYTES:
                f.seek(st.st_size - TAIL_READ_BYTES)
                f.readline()  # a partial first line
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    msgs, last, last_text = [], None, ""
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") not in ("user", "assistant") or d.get("isSidechain") \
                or d.get("isMeta"):
            continue
        m = d.get("message")
        content = m.get("content") if isinstance(m, dict) else None
        full = _block_text(content) if isinstance(content, list) else (content if isinstance(content, str) else "")
        if isinstance(content, list):   # keep the line breaks of the text blocks: the last line matters
            full = "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
                             and isinstance(b.get("text"), str)) or full
        calls = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
        last = {"role": d["type"], "calls": calls, "api_error": d.get("isApiErrorMessage") is True}
        text = " ".join(_block_text(content).split())
        if text:
            msgs.append("%s: %s" % (d["type"], text[:TAIL_MESSAGE_MAX]))
            last_text = full
            last["said"] = True
    if not msgs:
        return None
    out = "\n".join(msgs[-TAIL_MESSAGES:])
    if len(out) > TAIL_MAX:
        out = out[-TAIL_MAX:]
    role = msgs[-1].split(":", 1)[0]
    ended_on_text = bool(last.get("said")) and last["role"] == "assistant" and not last["calls"]
    return {"fingerprint": "t:%d:%d" % (st.st_size, st.st_mtime_ns), "text": out, "mtime": st.st_mtime,
            "last_from": "assistant" if role == "assistant" else "manager",
            "open_turn": last["role"] == "user" or last["calls"],
            "waiting": waiting_line(last_text) if ended_on_text else None,
            "error_end": last["api_error"] or (ended_on_text and len(last_text) <= ERROR_END_MAX
                                               and bool(_ERROR_END_RE.search(last_text)))}


def _transcript_tail(path: Path) -> Optional[Tuple[str, str]]:
    """(fingerprint, text) of `_transcript_read`, or None."""
    got = _transcript_read(path)
    return (got["fingerprint"], got["text"]) if got else None


def _short_fp(prefix: str, text: str) -> str:
    return "%s:%s" % (prefix, hashlib.sha1(text.encode("utf-8")).hexdigest()[:16])


def _tail_for(home: Path, cwds: List[str], sid: str) -> Optional[dict]:
    for cwd in cwds:
        if cwd:
            got = _transcript_read(home / ".claude" / "projects" / project_slug(cwd) / (sid + ".jsonl"))
            if got:
                return got
    return None


def _live_sessions(ws: Path, home: Path, app_dir: Optional[Path], at: Optional[datetime] = None) -> List[dict]:
    """Every session the pane draws a card for, with its tail: the pinned desktop sessions, then the live case
    sessions that are not pinned (their transcript is looked for under the workspace folder, where a case starts),
    asleep or not. One with neither a transcript nor a last report has nothing to summarise. Each row is {id, kind
    (pinned | case), fingerprint, text} plus what the transcript says without a model: `running` (it moved in the
    last 2 minutes, or its turn is open and it moved in the last 30), `idle_for` (seconds since it last moved;
    None without a transcript), `last_from` (assistant | manager), `waiting` (`waiting_line`) and `error_end`."""
    out, seen = [], set()
    now_s = at.timestamp() if at is not None else None

    def row(sid, kind, got):
        idle = max(0, int(now_s - got["mtime"])) if now_s is not None and got.get("mtime") is not None else None
        open_turn = bool(got.get("open_turn"))
        seen.add(sid)
        out.append({"id": sid, "kind": kind, "fingerprint": got["fingerprint"], "text": got["text"],
                    "running": idle is not None and (idle < RUNNING_SECONDS or (open_turn and idle < STUCK_SECONDS)),
                    "idle_for": idle, "last_from": got.get("last_from"), "open_turn": open_turn,
                    "waiting": got.get("waiting"), "error_end": bool(got.get("error_end"))})

    for d in desktop_status(ws, app_dir):
        got = _tail_for(home, [d["cwd"]], d["cli_session_id"])
        if not got and d["last_report"].strip():
            got = {"fingerprint": _short_fp("r", d["last_report"]), "text": d["last_report"]}
        if got:
            row(d["cli_session_id"], "pinned", got)
    for r in hub._sessions_doc(ws)["sessions"].values():
        cli = r.get("cli_session_id") if _is_case(r) else None
        if isinstance(cli, str) and CLI_ID_RE.fullmatch(cli) and cli not in seen:
            got = _tail_for(home, [str(ws)], cli)
            if got:
                row(cli, "case", got)
    return out


# C0 controls but newline and tab, DEL, C1 controls, the line and paragraph separators, and the bidi marks, embeddings,
# overrides and isolates: what a model could copy from a transcript to make a card misread, or that a surface refuses to
# draw. A tab becomes a space, so it can't join two words.
_UNSAFE_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _safe(text: str) -> str:
    return _UNSAFE_RE.sub("", text.replace("\t", " "))


def safe_line(text, cap: int) -> str:
    """Untrusted text (a line's, a model's), as one line a card or a message can show: whitespace flattened, capped, nothing unsafe."""
    return _safe(_one_line(str(text or ""), cap))


def clean_tldr(tldr, chips) -> Optional[dict]:
    """{tldr, chips} as the pane may show them, or None: tldr at most 2 non-empty lines of 160 characters; chips at
    most 6 of CHIP_KINDS, each kind once, a `reply` with a one-line text of 1..200 characters and a `label` of at
    most 24 (the model's, else the text's first words: `chip_label`). Anything else is dropped."""
    if not isinstance(tldr, str):
        return None
    lines = [" ".join(_safe(l).split())[:TLDR_LINE_MAX] for l in tldr.split("\n")]
    lines = [l for l in lines if l][:TLDR_LINES]
    if not lines:
        return None
    out, seen = [], set()
    for c in chips if isinstance(chips, list) else []:
        if not isinstance(c, dict) or c.get("kind") not in CHIP_KINDS:
            continue
        kind = c["kind"]
        if kind == "reply":
            text = c.get("text")
            text = _safe(text) if isinstance(text, str) else None
            if text is None or "\n" in text.strip() or not text.strip() or len(text.strip()) > CHIP_TEXT_MAX:
                continue
            ident = ("reply", text.strip())
            chip = {"kind": "reply", "text": text.strip(), "label": chip_label(text.strip(), c.get("label"))}
        else:
            ident, chip = (kind,), {"kind": kind}
        if ident in seen:
            continue
        seen.add(ident)
        out.append(chip)
        if len(out) == CHIPS_MAX:
            break
    return {"tldr": "\n".join(lines), "chips": out}


def clean_card(r) -> Optional[dict]:
    """A session card as the pane may show it, or None: `clean_tldr`'s {tldr, chips}, and, when the row carries a
    `state`, {state (STATES; anything else reads `idle`), progress (one line, 160), asks, question, options,
    waiting_on} with the floor card's own caps (lib/floor.py)."""
    if not isinstance(r, dict):
        return None
    c = clean_tldr(r.get("tldr"), r.get("chips"))
    if not c or "state" not in r:
        return c
    f = floormod.clean({"session_id": "0" * 36, "asks": r.get("asks"), "question": r.get("question"),
                        "options": r.get("options"), "waiting_on": None}) or {}
    c.update({"state": r["state"] if r.get("state") in STATES else "idle",
              "progress": safe_line(r.get("progress") if isinstance(r.get("progress"), str) else "", PROGRESS_MAX_CHARS),
              "asks": bool(f.get("asks")), "question": _safe(f.get("question") or ""),
              "options": [{"label": _safe(o["label"]), "message": _safe(o["message"])} for o in f.get("options") or []],
              "waiting_on": safe_line(r.get("waiting_on") if isinstance(r.get("waiting_on"), str) else "", 80) or None})
    return c


def _stored_card(hit) -> Optional[dict]:
    c = clean_card(hit) if isinstance(hit, dict) else None
    if c is not None:
        c["at"] = hit.get("at") if isinstance(hit.get("at"), str) else None
        if "state" in c:
            c["prev_state"] = hit.get("prev_state") if hit.get("prev_state") in STATES else None
    return c


def _is_stuck(row: dict, card: Optional[dict]) -> bool:
    """The code rule: the tail ends in an error or limit message; or the card said `working` and nothing moved for
    between 30 minutes and 2 hours. Longer than that with no error is not stuck: the turn was interrupted or the
    app closed, and the session reads idle (an open turn alone says nothing: it stays open when the app closes)."""
    idle = row.get("idle_for")
    still = isinstance(idle, int) and STUCK_SECONDS <= idle < STUCK_MAX_SECONDS
    working = bool(card) and card.get("state") == "working"
    return bool(row.get("error_end")) or (still and working)


def session_tails(ws: Path, cfg: dict, home: Optional[Path] = None, app_dir: Optional[Path] = None,
                  at: Optional[datetime] = None) -> dict:
    """`tail --json`: {version, model, refresh_minutes, sessions: [{id, kind, fingerprint, text, running, idle_for,
    last_from, open_turn, waiting, error_end, stuck, cached?, prev?}]}. `cached` is the stored card when tldr.json
    holds one for the same id, fingerprint and model; `prev` the stored card whatever it was made from (its tldr,
    state and `at`, for the next card's delta and the refresh interval). `stuck` is `_is_stuck`."""
    home = Path(home or Path.home())
    conf = settings(cfg)
    model = conf.get("tldr_model")
    model = model if model in TLDR_MODELS else DEFAULTS["tldr_model"]
    store = _read_obj(chief_dir(ws) / "tldr.json")
    sessions = _live_sessions(Path(ws), home, app_dir if app_dir is not None else default_app_dir(), at)
    for s in sessions:
        hit = store.get(s["id"])
        card = _stored_card(hit)
        if card:
            s["prev"] = dict(card, fingerprint=hit.get("fingerprint"))
            if hit.get("fingerprint") == s["fingerprint"] and hit.get("model") == model:
                s["cached"] = {k: v for k, v in card.items() if k != "at"}
        s["stuck"] = _is_stuck(s, card)
    return {"version": 1, "model": model, "refresh_minutes": conf["refresh_minutes"], "sessions": sessions}


def _fill_floor(ws: Path, at: datetime, sid: str, card: dict, row: dict) -> None:
    """A refreshed card's ask, as the session's floor card (lib/floor.py), so Needs you covers a session whose own
    mod is not running. A floor card written since the transcript last moved is that session's own word on its
    final message and wins: it is left alone. Otherwise the refresh's ask is written, or a stale ask cleared."""
    path = floormod.floor_dir(ws) / (sid + ".json")
    cur = _read_obj(path)
    cur_at = floormod._parse(cur.get("at")) if cur else None
    moved = row.get("idle_for")
    if cur_at is not None and (moved is None or (at - cur_at).total_seconds() <= moved + 1):
        return
    if not card.get("asks") and not cur.get("asks"):
        return
    floormod.write_card(ws, at, {"session_id": sid, "asks": card.get("asks"), "question": card.get("question"),
                                 "options": card.get("options"), "waiting_on": card.get("waiting_on") or "the manager"})


def tldr_store(ws: Path, at: datetime, rows: List[dict], home: Optional[Path] = None,
               app_dir: Optional[Path] = None, cards: Optional[dict] = None) -> int:
    """`tldr --stdin`: merge [{id, fingerprint, model, tldr, chips, state?, progress?, asks?, question?, options?,
    waiting_on?}] into .mother/chief/tldr.json, for live sessions only and cleaned as `clean_card`; entries for
    sessions no longer live are pruned. For a card with a `state`, what the transcript says in code wins over the
    model: a `Waiting on:` last line sets the state and `waiting_on`, and a running session is `working`; the
    card it replaces gives `prev_state`; its ask fills the session's floor card (`_fill_floor`). Returns how many
    were stored; `cards`, when given, receives {id: the stored card}."""
    ws = Path(ws)
    live = {s["id"]: s for s in _live_sessions(ws, Path(home or Path.home()),
                                               app_dir if app_dir is not None else default_app_dir(), at)}
    n = 0
    with hub.locked(ws):
        path = chief_dir(ws) / "tldr.json"
        doc = {k: v for k, v in _read_obj(path).items() if k in live}
        for r in rows or []:
            if not isinstance(r, dict) or r.get("id") not in live or not isinstance(r.get("fingerprint"), str):
                continue
            c = clean_card(r)
            if not c:
                continue
            row = live[r["id"]]
            if "state" in c:
                w = row.get("waiting")
                if w:
                    c["state"], c["waiting_on"] = w["state"], w["on"]
                    if w["state"] != "waiting_manager":
                        c.update({"asks": False, "question": "", "options": []})
                elif row.get("running"):
                    c["state"] = "working"
                old = doc.get(r["id"])
                c["prev_state"] = old.get("state") if isinstance(old, dict) and old.get("state") in STATES else None
                try:
                    _fill_floor(ws, at, r["id"], c, row)
                    if w:
                        floormod.note_waiting(ws, at, r["id"], w)   # the floor card's wait, for a session whose mod is not running
                except OSError:
                    pass   # the card is stored all the same; Needs you misses this ask until the next refresh
            doc[r["id"]] = dict(c, fingerprint=r["fingerprint"], at=hub._iso(at),
                                model=r.get("model") if r.get("model") in TLDR_MODELS else None)
            if cards is not None:
                cards[r["id"]] = dict(c, at=hub._iso(at))
            n += 1
        _write_atomic(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return n


# ---- what a dots action would do: its own text, from the newest dots output on disk -----------------------------
DOTS_ID_RE = re.compile(r"^[AM]\d{1,3}$")
DOTS_TEXT_MAX = 1500
DOTS_FILE_MAX = 256 * 1024
DOTS_FILES = 7
DOTS_LABEL_MAX = 24        # a button's width: a longer clause is no label
_DOTS_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")


def _dots_files(ws: Path) -> List[Tuple[str, Path]]:
    """(date, path) of the dots outputs on disk, newest date first; inside one date the machine-local full copy
    (.mother/reports/dots-<date>.md), then the journal file the routine always writes (journal/dots/<date>.md),
    then the undelivered copy (.mother/dots-unsent-<date>.md)."""
    found = []
    for order, (folder, pattern) in enumerate(((Path(ws) / ".mother" / "reports", r"^dots-\d{4}-\d{2}-\d{2}\.md$"),
                                               (Path(ws) / "journal" / "dots", r"^\d{4}-\d{2}-\d{2}\.md$"),
                                               (Path(ws) / ".mother", r"^dots-unsent-\d{4}-\d{2}-\d{2}\.md$"))):
        try:
            names = os.listdir(str(folder))
        except OSError:
            continue
        for name in names:
            path = folder / name
            if re.match(pattern, name) and not path.is_symlink() and path.is_file():
                found.append((_DOTS_DATE.search(name).group(1), order, path))
    found.sort(key=lambda f: f[1])
    found.sort(key=lambda f: f[0], reverse=True)
    return [(d, p) for d, _o, p in found[:DOTS_FILES]]


def _dots_block(lines: List[str], ident: str) -> Optional[List[str]]:
    """The action's own block in a dots file: the line that starts with its id (after a list marker), or for `M<n>`
    with no such line the `<n>.` entry under the "For you" heading, with the indented lines under it."""
    start = None
    own = re.compile(r"^\s*(?:[-*]\s+|\d+[.)]\s+)?[*_`]{0,2}%s[*_`]{0,2}(?=[\s:.)—-]|$)" % re.escape(ident))
    for i, l in enumerate(lines):
        if own.match(l):
            start = i
            break
    if start is None and ident.startswith("M"):
        in_you = False
        for i, l in enumerate(lines):
            if l.startswith("#"):
                in_you = bool(re.search(r"for you", l, re.IGNORECASE))
            elif in_you and re.match(r"^%s[.)]\s" % ident[1:], l):
                start = i
                break
    if start is None:
        return None
    block = [lines[start]]
    for l in lines[start + 1:]:
        if not l.strip() or not l[:1].isspace():
            break
        block.append(l)
    return block


def dots_label(line: str, ident: str) -> Optional[str]:
    """A short verb phrase for the action's button ("Annotate 7 todos"), from its own line: the first clause after
    the id, when it is 2 or more words and at most 24 characters. None otherwise."""
    text = re.sub(r"^\s*(?:[-*]\s+|\d+[.)]\s+)?[*_`]{0,2}(?:%s)?[*_`]{0,2}[\s:.)—-]*" % re.escape(ident), "", line)
    clause = re.split(r"\s+[—–]\s+|\s+--?\s+|\s+because\s+|[(:;]", text, 1)[0].strip(" .*`_")
    clause = safe_line(clause, 200)
    if not clause or len(clause) > DOTS_LABEL_MAX or len(clause.split()) < 2 or not clause[0].isalpha():
        return None
    return clause[0].upper() + clause[1:]


def dots_detail(ws: Path, ident) -> Optional[dict]:
    """`dots-detail <id>`: {id, text, date, source_file, label} for a dots action (`A<n>`) or manager item (`M<n>`),
    from the newest dots output on disk that has it (`_dots_files`): its own line or paragraph, then the other
    lines of that file that name it, at most 1500 characters. `source_file` is relative to the workspace; `label`
    is `dots_label`. None when the id is off-shape or no file has it."""
    if not isinstance(ident, str) or not DOTS_ID_RE.match(ident):
        return None
    for date, path in _dots_files(ws):
        try:
            if path.stat().st_size > DOTS_FILE_MAX:
                continue
            lines = path.read_bytes().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        block = _dots_block(lines, ident)
        if block is None:
            continue
        named = re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(ident))
        more = [l for l in lines if l not in block and named.search(l) and not l.startswith("#")]
        text = "\n".join(_safe(l.rstrip()) for l in block + more).strip()[:DOTS_TEXT_MAX]
        return {"id": ident, "text": text, "date": date, "source_file": str(path.relative_to(Path(ws))),
                "label": dots_label(block[0], ident)}
    return None


# ---- health: what the Chief pane's Health tab reads, so the mod opens no more files -----------------------------

WATCHER_KEY_PREFIX = "watcher:"          # an Inbox item about a source the watcher could not read: not work
WATCHER_ERRORS_MAX = 20
UNSENT_RE = re.compile(r"^([a-z0-9-]+)-unsent-(\d{4}-\d{2}-\d{2})\.md$")
UNSENT_MAX_BYTES = 64 * 1024
UNDELIVERED_MAX = 20
REASON_MAX = 300


def split_watcher_errors(rows: List[dict]) -> Tuple[List[dict], List[dict]]:
    """(watcher errors, the rest) of inbox_items rows, each in its given order."""
    errors = [r for r in rows if str(r.get("key") or "").startswith(WATCHER_KEY_PREFIX)]
    return errors, [r for r in rows if r not in errors]


def guard_state(ws: Path) -> str:
    """`paused` (.mother/PAUSED, which wins), `off` (.mother/GUARD_OFF) or `on`: the one read behind both the
    status' top-level `guard` and `health.guard`."""
    state = Path(ws) / ".mother"
    return "paused" if (state / "PAUSED").exists() else "off" if (state / "GUARD_OFF").exists() else "on"


def undelivered(ws: Path) -> List[dict]:
    """Routine reports that could not be delivered to the manager's DM: <ws>/.mother/<routine>-unsent-<date>.md,
    newest first, at most UNDELIVERED_MAX. Names only; `mother chief report <name>` prints one."""
    rows = []
    try:
        names = sorted(os.listdir(str(Path(ws) / ".mother")))
    except OSError:
        return rows
    for name in names:
        m = UNSENT_RE.fullmatch(name)
        path = Path(ws) / ".mother" / name
        if not m or path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append({"name": name, "routine": m.group(1), "date": m.group(2), "bytes": size})
    rows.sort(key=lambda r: (r["date"], r["name"]), reverse=True)
    return rows[:UNDELIVERED_MAX]


def report_text(ws: Path, name: str) -> Optional[str]:
    """One undelivered report's text, or None: a name off the pattern, a file that is missing, a link, outside
    .mother/ or past 64 KB."""
    if not isinstance(name, str) or not UNSENT_RE.fullmatch(name):
        return None
    base = Path(ws) / ".mother"
    path = base / name
    try:
        if path.is_symlink() or not path.is_file() or path.resolve().parent != base.resolve():
            return None
        if path.stat().st_size > UNSENT_MAX_BYTES:
            return None
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def watcher_health(ws: Path, at: datetime) -> Optional[dict]:
    """The watcher's heartbeat (.mother/hub/watcher.json) as the pane draws it, or None when it never ran. `stale`
    as `mother hub status` judges it: inside active hours and older than twice its interval."""
    w = hub.load_json(ws, "watcher.json", {})
    w = w if isinstance(w, dict) else {}
    if not isinstance(w.get("at"), str) or not w["at"]:
        return None
    try:
        every = int(w.get("every_seconds") or hub.DEFAULTS["every_seconds"])
    except (TypeError, ValueError):
        every = hub.DEFAULTS["every_seconds"]
    active, age = bool(w.get("active")), hub._age(w.get("at"), at)
    sources = {}
    for cap, ent in sorted((w.get("sources") or {}).items() if isinstance(w.get("sources"), dict) else []):
        if not isinstance(ent, dict):
            continue
        sources[safe_line(cap, 40)] = {
            "status": safe_line(ent.get("status"), 40) or "unknown", "reason": safe_line(ent.get("reason"), REASON_MAX),
            "since": ent.get("since") if isinstance(ent.get("since"), str) else None,
            "at": ent.get("at") if isinstance(ent.get("at"), str) else None}
        if ent.get("degraded"):  # reading less than usual (the chat source, rate limited)
            sources[safe_line(cap, 40)]["degraded"] = safe_line(ent.get("degraded"), REASON_MAX)
    return {"at": w["at"], "every_seconds": every, "active": active,
            "stale": active and (age is None or age > 2 * every), "sources": sources}


# What a source's error says, when only the manager can fix it: a sign-in that expired or a refused token
# (`signin`), or the network, the VPN or a timeout (`network`). The one list both the watcher's source status and its
# `watcher:` error lines are matched against; the first pattern that matches names the kind. Rate limits are neither.
ACCESS_PATTERNS = (
    ("signin", r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid[_ ]auth|needs?[_ ]auth|not authenticated|"
               r"(?:token|sign[- ]?in|log[- ]?in|session|credentials?|auth\w*)\W+(?:\w+\W+){0,3}?"
               r"(?:expired|invalid|revoked|refused|rejected|missing)|expired\W+(?:\w+\W+){0,2}?(?:token|session)|"
               r"invalidated|re-?authenticate|reconnect it from connector settings|please sign ?in|sign ?in again"),
    ("network", r"\bvpn\b|econnrefused|enotfound|econnreset|ehostunreach|enetunreach|getaddrinfo|"
                r"connection (?:refused|reset)|timed? ?out|timeout|no reply to [\w/-]+ within|unreachable|"
                r"fetch failed|network|\bdns\b|could not resolve"),
)
ACCESS_HELP = {
    "signin": "Sign in to this connector again (its sign-in or token expired or was refused), then Retry.",
    "network": "Reconnect the VPN or the network this connector needs (its server could not be reached), then Retry.",
}


def access_kind(text) -> Optional[str]:
    """`signin`, `network` or None for one error text (ACCESS_PATTERNS)."""
    low = str(text or "").lower()
    if not low or _rate_limited(low):
        return None
    for kind, pat in ACCESS_PATTERNS:
        if re.search(pat, low):
            return kind
    return None


def _rate_limited(low: str) -> bool:
    return bool(re.search(r"\b429\b|ratelimited|rate[ _-]?limit", low))


def access_problems(watcher: Optional[dict], errors: List[dict]) -> List[dict]:
    """ChiefStatus.health.access: one row per source whose failure only the manager can fix, `{source, kind,
    reason, help, since}`. From the watcher's source status when the source is down (`couldnt_check`), else from an
    open `watcher:<source>` error line; a source the watcher reads as ok or partial has none."""
    out, seen = [], set()
    sources = (watcher or {}).get("sources") if isinstance((watcher or {}).get("sources"), dict) else {}
    for cap, ent in sorted(sources.items()):
        if not isinstance(ent, dict) or ent.get("status") != "couldnt_check":
            continue
        kind = access_kind(ent.get("reason"))
        if kind:
            seen.add(cap)
            out.append({"source": cap, "kind": kind, "reason": safe_line(ent.get("reason"), 120),
                        "help": ACCESS_HELP[kind], "since": ent.get("since")})
    for e in errors:
        cap = str(e.get("key") or "")[len(WATCHER_KEY_PREFIX):]
        if not cap or cap in seen or (isinstance(sources.get(cap), dict) and sources[cap].get("status") in ("ok", "partial")):
            continue
        reason = e.get("why") or e.get("title") or ""
        kind = access_kind(reason)
        if kind:
            seen.add(cap)
            out.append({"source": safe_line(cap, 40), "kind": kind, "reason": safe_line(reason, 120),
                        "help": ACCESS_HELP[kind], "since": e.get("at")})
    return out


def health_doc(ws: Path, at: datetime, errors: List[dict]) -> dict:
    """ChiefStatus.health: the watcher, its errors (Inbox items keyed `watcher:`), undelivered reports, the guard,
    and the sources only the manager can fix (`access`)."""
    watcher = watcher_health(ws, at)
    return {"guard": guard_state(ws), "watcher": watcher,
            "watcher_errors": errors[:WATCHER_ERRORS_MAX], "watcher_error_count": len(errors),
            "undelivered": undelivered(ws), "access": access_problems(watcher, errors),
            "mention_search": mention_health(ws)}


# ---- mention search: the leading desktop session's chat connector finds mentions and DMs (CONTRACT §5g) ----
# The watcher's own token may not search (missing scope), so mentions in channels it does not poll, group DMs and
# DMs from people not in config would never reach the inbox. The leader runs two searches on its own connector
# (`plan` says when and from what time), and hands the raw answers to `record`, which parses them and writes the
# hits through `hub.ingest_chat`. No model call, no post; the state is .mother/chief/mention_search.json.
MENTION_FILE = "mention_search.json"
MENTION_FIRST_HOURS = 6
MENTION_OVERLAP = 600
MENTION_PAGES = 3
MENTION_BACKOFF_MAX = 1800
MENTION_STATUSES = ("ok", "error", "rate_limited", "unavailable")
MENTION_UNAVAILABLE = "not available (no Slack search connector in this session)"


def mention_state(ws: Path) -> dict:
    return _read_obj(chief_dir(ws) / MENTION_FILE)


def mention_plan(ws: Path, cfg: dict, at: datetime, session: str = "", force: bool = False) -> dict:
    """`mention-search plan`: {due, why?, every_seconds, next_at?} and, when due, {after (Unix seconds, a string),
    started_at, max_pages, queries: [{label, query}]}. Only the session that wrote the fresh heartbeat (`--as`) is
    due; `--force` (`/chief refresh`) skips the interval and any backoff, never the leader rule. `after` is the last
    fully successful run less a 10-minute overlap; the first run reaches back 6 hours."""
    ws = Path(ws)
    st = mention_state(ws)
    every = settings(cfg)["mention_search_seconds"]
    out = {"version": 1, "due": False, "every_seconds": every}
    mine = hub.build_me(cfg).get("chat") or ""
    now = at.timestamp()
    if not mine:
        out["why"] = "no manager chat user id (manager.chat_user)"
        return out
    if paused(ws):
        out["why"] = "paused"
        return out
    if session:
        beat = _read_obj(chief_dir(ws) / "heartbeat.json")
        when = hub._parse_at(beat.get("at"))
        if beat.get("session_id") != session or when is None or abs((at - when).total_seconds()) >= ALIVE_SECONDS:
            out["why"] = "another session leads"
            return out
    else:
        out["why"] = "only a leading desktop session searches (--as)"
        return out
    nxt = _num(st.get("next_at"))
    if not force and nxt and now < nxt:
        out.update(why="not due", next_at=hub._iso(datetime.fromtimestamp(nxt, timezone.utc)))
        return out
    wm = _num(st.get("watermark"))
    after = wm - MENTION_OVERLAP if wm else now - MENTION_FIRST_HOURS * 3600
    out.update(due=True, after=str(int(after)), started_at=now, max_pages=MENTION_PAGES,
               queries=[{"label": "mentions", "query": "<@%s>" % mine}, {"label": "DMs", "query": "to:me"}])
    return out


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f > 0 and f == f and f != float("inf") else 0.0


def mention_record(ws: Path, cfg: dict, at: datetime, doc) -> dict:
    """`mention-search record --stdin`: {started_at, status: ok|error|rate_limited|unavailable, reason?, calls?,
    pages: [{label, results (the tool's markdown), truncated?}]}. Every page's hits are written (the dedup makes a
    retry safe); only an `ok` run moves the watermark: to `started_at`, or, when a query stopped at its page cap, to the
    newest hit it reached (the pages run oldest first). A failure doubles the wait, up to 30 minutes."""
    import hub_sources
    ws = Path(ws)
    if not isinstance(doc, dict):
        raise ValueError("stdin must be an object")
    status = doc.get("status")
    if status not in MENTION_STATUSES:
        raise ValueError("status must be one of %s" % ", ".join(MENTION_STATUSES))
    now = at.timestamp()
    started = min(_num(doc.get("started_at")) or now, now)
    hits, cut = [], []
    for pg in doc.get("pages") if isinstance(doc.get("pages"), list) else []:
        if not isinstance(pg, dict) or not isinstance(pg.get("results"), str):
            continue
        got = hub_sources.parse_search_results(pg["results"])
        mention = pg.get("label") == "mentions"
        hits += [dict(h, is_mention=mention and not h["is_dm"]) for h in got]
        if pg.get("truncated") is True:
            cut.append(max([float(h["ts"]) for h in got] or [0.0]))
    res = hub.ingest_chat(ws, cfg, at, hits)
    st = mention_state(ws)
    every = settings(cfg)["mention_search_seconds"]
    wait = every
    if status == "ok":
        if not cut:
            st["watermark"] = started
        elif any(c > 0 for c in cut):  # a cut page with no readable hit says nothing: the watermark stays
            st["watermark"] = min([c for c in cut if c > 0] + [started])
        st.pop("backoff_seconds", None)
        reason = ""
    elif status == "unavailable":
        reason = MENTION_UNAVAILABLE
    else:
        wait = min(max(int(_num(st.get("backoff_seconds"))), every) * 2, MENTION_BACKOFF_MAX)
        st["backoff_seconds"] = wait
        reason = safe_line(doc.get("reason") or ("rate limited (429)" if status == "rate_limited" else "search failed"), 160)
    calls = doc.get("calls")
    st.update(version=1, status=status, reason=reason, last_run_at=hub._iso(at), next_at=now + wait,
              every_seconds=every, found=res["added"], results=len(hits),
              calls=calls if isinstance(calls, int) and not isinstance(calls, bool) and 0 <= calls <= 6 else None)
    if status == "ok":
        st["last_ok_at"] = hub._iso(at)
    _write_atomic(chief_dir(ws) / MENTION_FILE, json.dumps(st, indent=2) + "\n")
    return {"version": 1, "status": status, "added": res["added"], "skipped": res["skipped"],
            "next_at": hub._iso(datetime.fromtimestamp(now + wait, timezone.utc))}


def mention_health(ws: Path) -> dict:
    """ChiefStatus.health.mention_search: {status (never when it never ran), reason, last_run_at, last_ok_at, found,
    every_seconds, next_at}, for the Health tab's chat row."""
    st = mention_state(ws)
    status = st.get("status") if st.get("status") in MENTION_STATUSES else "never"
    nxt = _num(st.get("next_at"))
    found = st.get("found")
    return {"status": status, "reason": safe_line(st.get("reason"), 160),
            "last_run_at": st.get("last_run_at") if isinstance(st.get("last_run_at"), str) else None,
            "last_ok_at": st.get("last_ok_at") if isinstance(st.get("last_ok_at"), str) else None,
            "found": found if isinstance(found, int) and not isinstance(found, bool) else 0,
            "every_seconds": st.get("every_seconds") if isinstance(st.get("every_seconds"), int) else None,
            "next_at": hub._iso(datetime.fromtimestamp(nxt, timezone.utc)) if nxt else None}


# ---- status and the `mother chief` CLI ----------------------------------------------------------------

ALIVE_SECONDS = 120
USAGE = """usage: mother chief <verb> ...

  tick [--json] [--as <session_id>]
                        one pass: write the heartbeat, log the muted lines, name the lines that need a decision
                        (`judge`). --as: a desktop session's pass. It leads only when no other session wrote a
                        heartbeat in the last 120 s; otherwise nothing runs and it prints {"skipped": "<why>"}
  triage --stdin --decide
                        the leading session's: stdin is {judge: [line ids], verdicts: [...]}; every judge line is
                        decided in code, `log` when its verdict says it needs no attention and `now` for the rest;
                        prints {paused, logged, now}
  why <key> [--json]    the last triage verdicts of one key, newest first: {at, line_id, key, label, decision, kind,
                        urgency, reason, title}, from .mother/chief/verdicts.jsonl (no bodies, 30 days)
  mention-search plan [--as <session_id>] [--force]
                        whether the leading desktop session runs the mention and DM search now, and its queries and
                        `after`; prints {due, why?, after, started_at, max_pages, queries}
  mention-search record --stdin
                        the run's raw search answers ({started_at, status, pages: [{label, results, truncated}]}):
                        hits into the inbox through `mother hub ingest`, the watermark and backoff into
                        .mother/chief/mention_search.json; prints {status, added, skipped, next_at}
  lines [--json]        the inbox lines waiting for a decision
  context --json        what the triage prompt says about the manager: their name, the teams with their people's
                        names, the followed tracker projects and repos, names only; plus `people`, the configured
                        chat ids to names, for the pane's `from` (never sent); at most 1500 characters
  status [--json] [--save]
                        paused, who leads (the heartbeat), today's budget, pinned sessions and the inbox; --save also
                        writes the JSON to .mother/chief/status.json (atomically) for the pane's fast refresh
  pinned --stdin        snapshot the pinned desktop sessions from list_sessions rows (JSON array on stdin) into
                        .mother/chief/desktop.json; prints {pinned: N}
  tail --json           each pinned or case session's recent tail (at most 6000 characters) for its card, what
                        its transcript says in code (running, idle_for, last_from, waiting, error_end, stuck), the
                        cached card when the session has not moved and the previous one; prints {version, model,
                        refresh_minutes, sessions}
  tldr --stdin          store session cards (JSON array on stdin: tldr and chips, plus state, progress and the ask)
                        in .mother/chief/tldr.json; prints {stored: N, cards: {id: card}}
  spend --stdin         add one pane model call ({model, input_tokens, output_tokens} on stdin) to today's spend;
                        prints {added: usd}
  portrait --json       the Chief pane's picture: .mother/portrait.png when it is a PNG of at most 2 MiB, else the
                        bundled one; prints {version, source, width, height, png (base64)}, plus jpeg, jpeg_width and
                        jpeg_height for a small .mother/portrait.jpg, and svg (the bundled vector)
  report <name>         print one undelivered routine report (.mother/<routine>-unsent-<YYYY-MM-DD>.md, at most
                        64 KB); exit 2 for any other name or file
  claim --stdin         a session claims an inbox item's key ({key, session_id, title, link, also?} on stdin): its
                        row in the hub's sessions.json becomes a live case of that session, so later lines of the
                        key (and of each key in `also`) route to it. Refused (exit 1) with no open inbox item and
                        no ask-box hold for the key, or when a key is live in another session whose row is under
                        7 days old
  watch-key <key> --for <session_id>
                        add one more key (a related thread, ticket or PR) to a live case that session holds;
                        refused (exit 1) as a claim is, and when that session holds no live case
  close-case <key>      the live case that holds the key is done, with every open inbox item of its keys
  mention --stdin       the keys the manager named to a session ({session_id, text?, keys?, title?}: chat message
                        links, tracker keys and PRs in `text`, plus `keys`; at most 5) join its case, which is made
                        (`origin: "mention"`) when it has none; a key live in another session is refused; prints
                        {ok, session_id, added, refused}
  ask --stdin           the ask box: {text} on stdin becomes a `manual` inbox line (as `mother hub add`) whose key
                        is held for the case the pane starts; prints {ok, key, line_id, link, update_id, owned_by}
  dots-detail <id> --json
                        what a dots action (A<n>) or manager item (M<n>) says, from the newest dots output on disk:
                        {id, text, date, source_file, label}, or null
  relays --for <session_id> [--ack --stdin]
                        the routed lines not yet delivered into that session, for the cases it holds: a JSON
                        array of {id, key, at, who, title, summary, link}; nothing is removed. --ack --stdin: the
                        session delivered the lines whose ids are the JSON array on stdin; they stop waiting;
                        prints {acked: N}
  relay on | off | status | deliver --for <session_id>
                        the machine-local relay switch (.mother/chief/RELAY_OFF); deliver: that session delivers
                        what waits once, on its next pass, though relay is off
  act-log --stdin | --json [--days N]
                        --stdin: append one action the manager took on a card ({verb, item_id, key, who, kind,
                        urgency, on, title, action_label, action_index}; any other field is dropped) to
                        .mother/chief/actions.jsonl (90 days, at most 5000 lines); --json prints the log
  habits [--json] | on | off | status | show <line id> | except <bucket>
                        --json: the log counted per bucket over 30 days; on | off: the machine-local switch
                        (.mother/chief/HABITS_OFF); show: a line quieted by habit becomes an Inbox item;
                        except: never quiet that bucket again
  pause | resume        the chief-only switch (.mother/chief/PAUSED); never the global one

A valued option takes --opt=V or --opt V."""

# verb: (boolean flags, valued options); only `report`, `relay`, `habits`, `watch-key`, `close-case`, `dots-detail` and `why` take a positional
VERBS = {
    "lines": (("--json",), ()), "context": (("--json",), ()), "status": (("--json", "--save"), ()), "tick": (("--json",), ("--as",)),
    "pause": ((), ()), "resume": ((), ()),
    "triage": (("--stdin", "--decide"), ()), "pinned": (("--stdin",), ()),
    "tail": (("--json",), ()), "tldr": (("--stdin",), ()), "spend": (("--stdin",), ()),
    "portrait": (("--json",), ()), "report": ((), ()),
    "act-log": (("--stdin", "--json"), ("--days",)), "habits": (("--json",), ()),
    "claim": (("--stdin",), ()), "relays": (("--ack", "--stdin", "--json"), ("--for",)), "relay": ((), ("--for",)),
    "watch-key": ((), ("--for",)), "close-case": ((), ()), "ask": (("--stdin",), ()), "dots-detail": (("--json",), ()),
    "why": (("--json",), ()), "mention": (("--stdin",), ()),
    "mention-search": (("--json", "--stdin", "--force"), ("--as",)),
}

# ---- the pane's picture: the manager's own file in the workspace, else the one the plugin ships ------------------

PORTRAIT_MAX = 2 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
BUNDLED_PORTRAIT = Path(__file__).resolve().parent.parent / "assets" / "art" / "mother-cos.png"


def _read_png(path: Path) -> Optional[Tuple[bytes, int, int]]:
    """(bytes, width, height) of a PNG of at most 2 MiB, read from its IHDR; None for anything else."""
    try:
        if not path.is_file() or path.stat().st_size > PORTRAIT_MAX:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 24 or len(data) > PORTRAIT_MAX or data[:8] != PNG_MAGIC or data[12:16] != b"IHDR":
        return None
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return (data, width, height) if width > 0 and height > 0 else None


JPEG_B64_MAX = 120_000      # what fits an Svg's 131072 characters with its wrapper
BUNDLED_SVG = BUNDLED_PORTRAIT.with_suffix(".svg")
SVG_MAX = 131_072
_SOF = (0xC0, 0xC1, 0xC2)   # baseline, extended and progressive frames carry the size


def _read_jpeg(path: Path) -> Optional[Tuple[bytes, int, int]]:
    """(bytes, width, height) of a JPEG whose base64 is at most 120000 characters, the size read from its frame
    header (SOF0/SOF2); None for anything else."""
    try:
        if not path.is_file() or path.stat().st_size > JPEG_B64_MAX:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if data[:2] != b"\xff\xd8" or len(base64.b64encode(data)) > JPEG_B64_MAX:
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:                       # fill byte
            i += 1
            continue
        if marker in (0x01,) or 0xD0 <= marker <= 0xD8:   # no length
            i += 2
            continue
        size = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in _SOF:
            height, width = int.from_bytes(data[i + 5:i + 7], "big"), int.from_bytes(data[i + 7:i + 9], "big")
            return (data, width, height) if width > 0 and height > 0 else None
        if size < 2 or marker == 0xDA:           # malformed, or the scan began with no frame header
            return None
        i += 2 + size
    return None


def _read_svg(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return text if text.lstrip().startswith("<svg") and len(text) <= SVG_MAX else None


def portrait_doc(ws: Path, bundled: Path = BUNDLED_PORTRAIT, bundled_svg: Path = BUNDLED_SVG) -> Optional[dict]:
    """The Chief pane's picture. `png` (with `width`, `height`) is <workspace>/.mother/portrait.png when it is a PNG
    of at most 2 MiB, else the bundled one: what a terminal draws. For a surface that draws SVG only: `jpeg` (with
    `jpeg_width`, `jpeg_height`) is <workspace>/.mother/portrait.jpg when it is a JPEG of at most 120000 base64
    characters, and `svg` is the bundled vector's markup, the fallback. `source` is `custom` when either workspace
    file is used. Both workspace files are machine-local and never synced. None when no PNG reads."""
    home = Path(ws) / ".mother"
    custom, png = True, _read_png(home / "portrait.png")
    if not png:
        custom, png = False, _read_png(bundled)
    if not png:
        return None
    jpeg = _read_jpeg(home / "portrait.jpg")
    doc = {"version": 1, "source": "custom" if custom or jpeg else "bundled", "width": png[1], "height": png[2],
           "png": base64.b64encode(png[0]).decode("ascii")}
    if jpeg:
        doc.update({"jpeg": base64.b64encode(jpeg[0]).decode("ascii"), "jpeg_width": jpeg[1], "jpeg_height": jpeg[2]})
    svg = _read_svg(bundled_svg)
    if svg:
        doc["svg"] = svg
    return doc


def status_doc(ws: Path, cfg: dict, at: datetime) -> dict:
    """The ChiefStatus document the mod reads (version 1). `led_by` is the session id that wrote the heartbeat
    (`tick --as`) while that heartbeat is fresh (ALIVE_SECONDS), else None. `guard` is `guard_state`."""
    ws = Path(ws)
    beat_doc = _read_obj(chief_dir(ws) / "heartbeat.json")
    beat = beat_doc.get("at")
    beat_at = hub._parse_at(beat)
    alive = beat_at is not None and (at - beat_at).total_seconds() < ALIVE_SECONDS
    leader = beat_doc.get("session_id")
    errors, inbox = split_watcher_errors(inbox_items(ws, cfg, at))   # a `watcher:` item is not work: it goes to health
    tz = nowmod.Schedule.from_config(cfg or {}).tz
    spent, limit = round(spent_today(ws, at, tz), 6), float(settings(cfg)["daily_budget_usd"])
    unreadable = _spend_unreadable(ws)
    return {
        "version": 1, "at": hub._iso(at), "paused": paused(ws),
        "chief": {"alive": alive, "heartbeat_at": beat if beat_at is not None else None},
        "budget": {"spent": spent, "limit": limit, "unreadable": unreadable, "over": unreadable or spent >= limit},
        "desktop": desktop_status(ws, default_app_dir()),
        "led_by": leader if alive and isinstance(leader, str) and leader else None,
        "inbox": inbox[:INBOX_MAX],
        "inbox_count": len(inbox),
        "cases": cases(ws, cfg),
        "relay": {"on": relay_on(ws)},
        "guard": guard_state(ws),
        "health": health_doc(ws, at, errors),
        "habits": {"on": habits.is_on(ws)},
        "quieted": habits.quieted_doc(ws, at),
        "floor": {"ignore_folders": settings(cfg)["ignore_folders"]},
        "quiet": quiet_now(cfg, at),
    }


def quiet_now(cfg: dict, at: datetime) -> bool:
    """Inside manager.quiet_hours ("HH:MM-HH:MM" in manager.timezone): the pane notifies only urgent things."""
    import channels
    try:
        return bool(channels.quiet_now(cfg or {}, now=at.timestamp()))
    except Exception:
        return False


STATUS_SNAPSHOT = "status.json"


def save_status(ws: Path, doc: dict) -> None:
    """`status --json --save`: the same JSON also at .mother/chief/status.json, written atomically, for the pane's
    fast refresh (it re-reads the file every few seconds, the CLI only every minute)."""
    _write_atomic(chief_dir(ws) / STATUS_SNAPSHOT, json.dumps(doc, indent=2, ensure_ascii=False))


def _usage(msg: str) -> int:
    print("mother chief: %s" % msg, file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return EXIT_USAGE


def _parse(rest: List[str], flags, valued) -> Tuple[dict, List[str]]:
    """(options, positionals). A valued option is --opt=V or --opt V. ValueError on an unknown option or a valueless
    one."""
    opts, pos, i = {}, [], 0
    while i < len(rest):
        a = rest[i]
        name = a.split("=", 1)[0]
        if name in valued:
            if "=" in a:
                opts[name[2:]] = a.split("=", 1)[1]
            elif i + 1 < len(rest):
                i += 1
                opts[name[2:]] = rest[i]
            else:
                raise ValueError("%s needs a value" % a)
        elif a in flags:
            opts[a[2:]] = True
        elif a.startswith("-"):
            raise ValueError("unknown option %s" % a)
        else:
            pos.append(a)
        i += 1
    return opts, pos


def _print_status(doc: dict) -> None:
    c = doc["chief"]
    state = "led by a desktop session" if doc.get("led_by") else "no session is leading"
    print("chief: %s%s (heartbeat %s)" % (state, ", paused" if doc["paused"] else "", c["heartbeat_at"] or "never"))
    b = doc["budget"]
    print("budget: %s" % ("spend file unreadable: no model call is made" if b.get("unreadable") else
                          "$%.2f of $%.2f today%s" % (b["spent"] or 0, b["limit"] or 0,
                                                       ", spent: no model call is made" if b.get("over") else "")))
    print("pinned: %d   inbox: %d" % (len(doc["desktop"]), doc["inbox_count"]))


def _case_verb(ws: Path, cfg: dict, at: datetime, verb: str, opts: dict, pos: List[str]) -> int:
    """`claim --stdin`, `relays --for`, `relay on|off|status|deliver`."""
    who = opts.get("for")
    if verb == "claim":
        if not opts.get("stdin"):
            return _usage("claim reads {key, session_id, title, link} from stdin: pass --stdin")
        try:
            doc = json.loads(sys.stdin.read())
        except ValueError as e:
            return _usage("claim: stdin is not JSON (%s)" % e)
        try:
            print(json.dumps(claim(ws, cfg, at, doc)))
        except ValueError as e:
            print("mother chief claim: %s" % e, file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK
    if verb == "relays":
        if not (isinstance(who, str) and who.strip()):
            return _usage("relays needs --for <session_id>")
        if opts.get("ack"):
            if not opts.get("stdin"):
                return _usage("relays --ack reads the delivered line ids from stdin: pass --stdin")
            try:
                ids = json.loads(sys.stdin.read())
            except ValueError as e:
                return _usage("relays --ack: stdin is not JSON (%s)" % e)
            if not isinstance(ids, list):
                return _usage("relays --ack: stdin must be a JSON array of line ids")
            print(json.dumps({"acked": relay_ack(ws, at, who, ids)}))
            return EXIT_OK
        print(json.dumps(relays(ws, at, who), indent=2, ensure_ascii=False))
        return EXIT_OK
    sub = pos[0] if len(pos) == 1 else ""
    if sub == "deliver":
        if not (isinstance(who, str) and CLI_ID_RE.fullmatch(who)):
            return _usage("relay deliver needs --for <session_id>")
        relay_deliver(ws, who)
        print("relay: that session delivers what waits on its next pass")
        return EXIT_OK
    if sub not in ("on", "off", "status"):
        return _usage("relay takes on, off, status or deliver --for <session_id>")
    if sub != "status":
        set_relay(ws, sub == "on")
    waiting = sum(c["unseen"] for c in cases(ws, cfg))
    print("relay %s%s" % ("on" if relay_on(ws) else "off", ": %d line(s) waiting" % waiting if waiting else ""))
    return EXIT_OK


def _habit_verb(ws: Path, cfg: dict, at: datetime, verb: str, opts: dict, pos: List[str]) -> int:
    """`act-log --stdin | --json [--days N]`, `habits [--json] | on | off | status | show <id> | except <bucket>`."""
    if verb == "act-log":
        if opts.get("stdin"):
            try:
                row = habits.clean_action(json.loads(sys.stdin.read()), at)
            except ValueError as e:
                return _usage("act-log: stdin is not JSON (%s)" % e)
            if row is None:
                return _usage("act-log: stdin must be a JSON object whose verb is one of %s" % ", ".join(habits.VERBS))
            print(json.dumps({"logged": habits.append_actions(ws, at, [row])}))
            return EXIT_OK
        if not opts.get("json"):
            return _usage("act-log takes --stdin or --json")
        try:
            days = int(opts.get("days", habits.KEEP_DAYS))
        except ValueError:
            return _usage("act-log --days needs a number")
        print(json.dumps(habits.actions(ws, at, max(0, days)), indent=2, ensure_ascii=False))
        return EXIT_OK
    sub = pos[0] if pos else ""
    if sub in ("show", "except") and len(pos) == 2:
        if sub == "show":
            uid = habits.show_quieted(ws, cfg, at, pos[1])
            if uid is None:
                print("mother chief habits show: no quieted line %s" % pos[1], file=sys.stderr)
                return EXIT_FAILED
            print("Shown in the Inbox as %s" % uid)
        elif not habits.add_exception(ws, pos[1]):
            print("mother chief habits except: not a bucket (source|label|value or source|who|value)", file=sys.stderr)
            return EXIT_FAILED
        else:
            print("Never quieted again: %s" % safe_line(pos[1].replace("|label|", " ").replace("|who|", " "), 80))
        return EXIT_OK
    if len(pos) > 1 or sub not in ("", "on", "off", "status"):
        return _usage("habits takes --json, on, off, status, show <line id> or except <bucket>")
    if sub in ("on", "off"):
        habits.set_on(ws, sub == "on")
    if opts.get("json") and not sub:
        print(json.dumps(habits.habits(ws, at), indent=2, ensure_ascii=False))
        return EXIT_OK
    print("habits %s: %d quieted in 7 days" % ("on" if habits.is_on(ws) else "off", habits.quieted_doc(ws, at)["count_7d"]))
    return EXIT_OK


def main(args, ws, cfg, at) -> int:
    """`mother chief <verb>`."""
    args = list(args)
    verb, rest = (args[0], args[1:]) if args else ("", [])
    if verb in ("help", "-h", "--help") and not rest:
        print(USAGE)
        return EXIT_OK
    spec = VERBS.get(verb)
    if spec is None:
        return _usage("unknown verb %r" % verb if verb else "no verb given")
    flags, valued = spec
    try:
        opts, pos = _parse(rest, flags, valued)
    except ValueError as e:
        return _usage(str(e))
    if pos and verb not in ("report", "relay", "habits", "watch-key", "close-case", "dots-detail", "why", "mention-search"):
        return _usage("%s takes no arguments" % verb)
    ws = Path(ws)
    if verb == "why":
        if len(pos) != 1:
            return _usage("why takes one key")
        rows = verdicts_for(ws, pos[0])
        if opts.get("json"):
            print(json.dumps({"version": 1, "key": pos[0], "verdicts": rows}, ensure_ascii=False))
        else:
            for r in rows:
                print("%s  %s  %s" % (r.get("at"), r.get("decision"), r.get("reason") or "no reason given"))
            if not rows:
                print("No verdicts for %s in the last %d days." % (pos[0], VERDICTS_KEEP_DAYS))
        return EXIT_OK
    if verb == "report":
        text = report_text(ws, pos[0]) if len(pos) == 1 else None
        if text is None:
            print("mother chief report: not an undelivered report (a name like brief-unsent-2026-01-31.md, in "
                  ".mother/, at most 64 KB)", file=sys.stderr)
            return EXIT_USAGE
        sys.stdout.write(text)
        return EXIT_OK
    as_json = bool(opts.get("json"))
    if verb in ("claim", "relays", "relay"):
        return _case_verb(ws, cfg, at, verb, opts, pos)
    if verb in ("watch-key", "close-case"):
        if len(pos) != 1:
            return _usage("%s takes one key" % verb)
        try:
            print(json.dumps(watch_key(ws, cfg, at, pos[0], opts.get("for")) if verb == "watch-key"
                             else close_case(ws, cfg, at, pos[0])))
        except ValueError as e:
            print("mother chief %s: %s" % (verb, e), file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK
    if verb == "ask":
        if not opts.get("stdin"):
            return _usage("ask reads {text} from stdin: pass --stdin")
        try:
            doc = json.loads(sys.stdin.read())
        except ValueError as e:
            return _usage("ask: stdin is not JSON (%s)" % e)
        try:
            print(json.dumps(ask(ws, cfg, at, doc.get("text") if isinstance(doc, dict) else None), ensure_ascii=False))
        except ValueError as e:
            print("mother chief ask: %s" % e, file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK
    if verb == "mention":
        if not opts.get("stdin"):
            return _usage("mention reads {session_id, text, keys, title} from stdin: pass --stdin")
        try:
            doc = json.loads(sys.stdin.read())
        except ValueError as e:
            return _usage("mention: stdin is not JSON (%s)" % e)
        try:
            print(json.dumps(mention(ws, cfg, at, doc)))
        except ValueError as e:
            print("mother chief mention: %s" % e, file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK
    if verb == "mention-search":
        if pos not in (["plan"], ["record"]):
            return _usage("mention-search takes plan [--as <session_id>] [--force] or record --stdin")
        if pos == ["plan"]:
            print(json.dumps(mention_plan(ws, cfg, at, opts.get("as") or "", bool(opts.get("force")))))
            return EXIT_OK
        if not opts.get("stdin"):
            return _usage("mention-search record reads the run from stdin: pass --stdin")
        try:
            print(json.dumps(mention_record(ws, cfg, at, json.loads(sys.stdin.read()))))
        except ValueError as e:
            print("mother chief mention-search: %s" % e, file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK
    if verb == "dots-detail":
        if len(pos) != 1:
            return _usage("dots-detail takes one id (A<n> or M<n>)")
        print(json.dumps(dots_detail(ws, pos[0]), ensure_ascii=False))
        return EXIT_OK
    if verb in ("act-log", "habits"):
        return _habit_verb(ws, cfg, at, verb, opts, pos)
    if verb in ("pause", "resume"):
        (pause if verb == "pause" else resume)(ws)
        print("chief %s" % ("paused" if verb == "pause" else "resumed"))
        return EXIT_OK
    if verb == "lines":
        got = lines(ws, cfg)
        if as_json:
            print(json.dumps(got, indent=2, ensure_ascii=False))
        else:
            for l in got:
                print("%s  %s  [%s]  %s: %s" % (l["id"], l["key"], l["session"] or "-", l["who"], l["title"]))
        return EXIT_OK
    if verb == "context":
        print(json.dumps(triage_context(ws, cfg), ensure_ascii=False))
        return EXIT_OK
    if verb == "triage":
        if not (opts.get("stdin") and opts.get("decide")):
            return _usage("triage decides the judge lines from stdin: pass --stdin --decide")
        try:
            verdicts = json.loads(sys.stdin.read())
        except ValueError as e:
            return _usage("triage: stdin is not JSON (%s)" % e)
        if not (isinstance(verdicts, dict) and isinstance(verdicts.get("judge"), list)
                and isinstance(verdicts.get("verdicts"), list)):
            return _usage("triage --decide: stdin must be a JSON object {judge: [...], verdicts: [...]}")
        judge = [j for j in verdicts["judge"] if isinstance(j, str)]
        print(json.dumps(triage_decide(ws, cfg, at, judge, verdicts["verdicts"]), indent=2, ensure_ascii=False))
        return EXIT_OK
    if verb == "pinned":
        if not opts.get("stdin"):
            return _usage("pinned reads list_sessions rows from stdin: pass --stdin")
        try:
            rows = json.loads(sys.stdin.read())
        except ValueError as e:
            return _usage("pinned: stdin is not JSON (%s)" % e)
        if not isinstance(rows, list):
            return _usage("pinned: stdin must be a JSON array of list_sessions rows")
        print(json.dumps({"pinned": pinned_snapshot(ws, at, rows, default_app_dir())}))
        return EXIT_OK
    if verb == "portrait":
        doc = portrait_doc(ws)
        if doc is None:
            print("mother chief portrait: no readable PNG", file=sys.stderr)
            return 1
        print(json.dumps(doc))
        return EXIT_OK
    if verb == "tail":
        print(json.dumps(session_tails(ws, cfg, Path.home(), default_app_dir(), at), indent=2, ensure_ascii=False))
        return EXIT_OK
    if verb == "tldr":
        if not opts.get("stdin"):
            return _usage("tldr reads its entries from stdin: pass --stdin")
        try:
            rows = json.loads(sys.stdin.read())
        except ValueError as e:
            return _usage("tldr: stdin is not JSON (%s)" % e)
        if not isinstance(rows, list):
            return _usage("tldr: stdin must be a JSON array of {id, fingerprint, tldr, chips}")
        cards = {}
        n = tldr_store(ws, at, rows, Path.home(), default_app_dir(), cards)
        print(json.dumps({"stored": n, "cards": cards}, ensure_ascii=False))
        return EXIT_OK
    if verb == "spend":
        if not opts.get("stdin"):
            return _usage("spend reads one model call from stdin: pass --stdin")
        try:
            call = json.loads(sys.stdin.read())
        except ValueError as e:
            return _usage("spend: stdin is not JSON (%s)" % e)
        if not isinstance(call, dict):
            return _usage("spend: stdin must be a JSON object {model, input_tokens, output_tokens}")
        if _spend_unreadable(ws):  # add_spend would read it as $0 and overwrite it, lifting the refusal
            print("spend file is unreadable (%s); nothing added" % (chief_dir(ws) / "spend.json"), file=sys.stderr)
            return EXIT_FAILED
        usd = model_cost(call.get("model"), call.get("input_tokens"), call.get("output_tokens"))
        add_spend(ws, at, nowmod.Schedule.from_config(cfg or {}).tz, usd)
        print(json.dumps({"added": round(usd, 6)}))
        return EXIT_OK
    if verb == "status":
        doc = status_doc(ws, cfg, at)
        if opts.get("save"):
            try:
                save_status(ws, doc)
            except OSError as e:
                print("status snapshot not written: %s" % e, file=sys.stderr)
        if as_json:
            print(json.dumps(doc, indent=2, ensure_ascii=False))
        else:
            _print_status(doc)
        return EXIT_OK
    who = opts.get("as")  # the only verb left: tick
    if who is not None:
        if not who.strip():
            return _usage("tick --as needs a session id")
        why = lead(ws, at, who)
        if why:
            print(json.dumps({"skipped": why}))
            return EXIT_OK
    r = tick(ws, cfg, at, session=who)
    if as_json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif r["paused"]:
        print("paused: heartbeat only")
    else:
        print("decided %d, judge %d" % (len(r["decided"]), len(r["judge"])))
    return EXIT_OK
