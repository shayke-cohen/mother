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
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import hub
import now as nowmod

DEFAULTS = {"daily_budget_usd": 20, "tldr_model": "sonnet", "triage_model": "sonnet"}
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
    return out


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


def lines(ws: Path) -> List[dict]:
    """The lines waiting for a decision: the digest after the cursor, trimmed to the fields triage needs, plus
    `session` ("desktop" for a live hub session, else None)."""
    ws = Path(ws)
    sessions = hub._sessions_doc(ws)
    out = []
    for l in _pending_lines(ws):
        d = {k: l.get(k) for k in ("id", "key", "kind", "title", "who", "link", "summary")}
        d["also"] = list(l.get("also") or [])
        d["session"] = _session_kind(sessions, l)
        out.append(d)
    return out


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

TITLE_MAX, TLDR_MAX = 60, 200
WHY_MAX = 300   # a `now` item's why: a verdict's tldr (200) or the line's own summary, which the hub stores up to 300
TRIAGE_NEVER_LOG = ("manual", "error")  # a person's own line, and a source failure: never logged on a model's say-so

def _verdict_ok(v) -> bool:
    return (isinstance(v, dict) and isinstance(v.get("line_id"), str) and isinstance(v.get("key"), str)
            and isinstance(v.get("is_work"), bool) and isinstance(v.get("needs_attention"), bool)
            and isinstance(v.get("title"), str) and isinstance(v.get("tldr"), str))


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

    `verdicts` is [{line_id, key, is_work, needs_attention, title, tldr}]. A line is logged when it has a well-formed
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
    items = []
    for l in pending:
        if l["id"] not in wanted:
            continue
        v = by_id.get(l["id"])
        if l.get("kind") == "sweep":
            items.append({"line_id": l["id"], "key": l["key"], "decision": "log", "why": "sweep: no hub session"})
        elif (v is not None and l.get("kind") not in TRIAGE_NEVER_LOG and _session_kind(sessions, l) is None
                and not _folds_others(raw, start, l) and (not v["is_work"] or not v["needs_attention"])):
            items.append({"line_id": l["id"], "key": l["key"], "decision": "log",
                          "why": "triage: " + _one_line(v["title"], TITLE_MAX)})
        else:
            title = _one_line(v["title"] if v else str(l.get("title") or ""), TITLE_MAX) or _one_line(str(l["key"]), TITLE_MAX)
            why = _one_line(v["tldr"] if v else str(l.get("summary") or ""), WHY_MAX)
            items.append({"line_id": l["id"], "key": l["key"], "decision": "now", "title": title, "why": why,
                          "link": l.get("link") or ""})
    if items:
        hub.decide(ws, cfg, at, items)
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


def inbox_items(ws: Path, cfg: Optional[dict] = None) -> List[dict]:
    """The hub's open updates (what a `now` decision makes), newest first, for the Chief pane's Inbox section. The
    text is line- or model-written, so each field is one safe line. `session` is the live hub session for the key.
    `source` is the key's kind, `who` the person its newest inbox line names ('' when none) and `label` what a card
    shows in place of the raw key (`item_label`)."""
    h, sessions = hub._hub_doc(ws), hub._sessions_doc(ws)
    whos, channels = _who_by_key(ws), _channel_names(cfg or {})
    rows = []
    for u in h["updates"]:
        if not isinstance(u, dict) or u.get("state") != "open" or not isinstance(u.get("id"), str):
            continue
        hit = hub.find_session(sessions, [u.get("key")])
        who = safe_line(whos.get(u.get("key")) or "", LABEL_MAX)
        rows.append({"id": u["id"], "key": safe_line(u.get("key"), PROGRESS_MAX),
                     "title": safe_line(u.get("title"), TITLE_MAX), "why": safe_line(u.get("why"), WHY_MAX),
                     "link": u.get("link") if isinstance(u.get("link"), str) else "", "at": u.get("at"),
                     "source": source_of(u.get("key")), "who": who, "label": item_label(u.get("key"), who, channels),
                     "session": ({"key": hit[0], "title": safe_line(hit[1].get("title") or hit[0], TITLE_MAX)}
                                 if hit and not _is_cli(hit[1]) else None)})
    rows.sort(key=lambda r: str(r["at"] or ""), reverse=True)
    return rows


def guard_state(ws: Path) -> str:
    """`paused` while .mother/PAUSED exists (it wins), `off` while .mother/GUARD_OFF does, else `on`."""
    m = Path(ws) / ".mother"
    return "paused" if (m / "PAUSED").exists() else "off" if (m / "GUARD_OFF").exists() else "on"


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
CHIPS_MAX, CHIP_TEXT_MAX = 6, 200   # stored; the pane shows at most 3 that apply to the card now


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


def _transcript_tail(path: Path) -> Optional[Tuple[str, str]]:
    """(fingerprint, text) from the end of a session transcript: the last TAIL_MESSAGES main-thread user and
    assistant messages, `role: text` per line, each capped, the whole cut to TAIL_MAX keeping the newest. The
    fingerprint is `t:<size>:<mtime_ns>`. None when the file is missing or holds no such message."""
    try:
        st = path.stat()
        with open(str(path), "rb") as f:
            if st.st_size > TAIL_READ_BYTES:
                f.seek(st.st_size - TAIL_READ_BYTES)
                f.readline()  # a partial first line
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    msgs = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") not in ("user", "assistant") or d.get("isSidechain") \
                or d.get("isMeta"):
            continue
        m = d.get("message")
        text = _block_text(m.get("content")) if isinstance(m, dict) else ""
        text = " ".join(text.split())
        if text:
            msgs.append("%s: %s" % (d["type"], text[:TAIL_MESSAGE_MAX]))
    if not msgs:
        return None
    out = "\n".join(msgs[-TAIL_MESSAGES:])
    if len(out) > TAIL_MAX:
        out = out[-TAIL_MAX:]
    return "t:%d:%d" % (st.st_size, st.st_mtime_ns), out


def _short_fp(prefix: str, text: str) -> str:
    return "%s:%s" % (prefix, hashlib.sha1(text.encode("utf-8")).hexdigest()[:16])


def _tail_for(home: Path, cwds: List[str], sid: str) -> Optional[Tuple[str, str]]:
    for cwd in cwds:
        if cwd:
            got = _transcript_tail(home / ".claude" / "projects" / project_slug(cwd) / (sid + ".jsonl"))
            if got:
                return got
    return None


def _live_sessions(ws: Path, home: Path, app_dir: Optional[Path]) -> List[dict]:
    """Every session the pane draws a TL;DR for, with its tail: the pinned desktop sessions (by CLI id). One with
    neither a transcript nor a last report has nothing to summarise."""
    out = []
    for d in desktop_status(ws, app_dir):
        got = _tail_for(home, [d["cwd"]], d["cli_session_id"])
        if not got and d["last_report"].strip():
            got = (_short_fp("r", d["last_report"]), d["last_report"])
        if got:
            out.append({"id": d["cli_session_id"], "kind": "pinned", "fingerprint": got[0], "text": got[1]})
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
    most 6 of CHIP_KINDS, each kind once, a `reply` with a one-line text of 1..200 characters. Anything else is
    dropped."""
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
            chip = {"kind": "reply", "text": text.strip()}
        else:
            ident, chip = (kind,), {"kind": kind}
        if ident in seen:
            continue
        seen.add(ident)
        out.append(chip)
        if len(out) == CHIPS_MAX:
            break
    return {"tldr": "\n".join(lines), "chips": out}


def session_tails(ws: Path, cfg: dict, home: Optional[Path] = None, app_dir: Optional[Path] = None) -> dict:
    """`tail --json`: {version, model, sessions: [{id, kind, fingerprint, text, cached?}]}. `cached` is the
    stored {tldr, chips} when tldr.json holds one for the same id, fingerprint and model."""
    home = Path(home or Path.home())
    model = settings(cfg).get("tldr_model")
    model = model if model in TLDR_MODELS else DEFAULTS["tldr_model"]
    store = _read_obj(chief_dir(ws) / "tldr.json")
    sessions = _live_sessions(Path(ws), home, app_dir if app_dir is not None else default_app_dir())
    for s in sessions:
        hit = store.get(s["id"])
        if isinstance(hit, dict) and hit.get("fingerprint") == s["fingerprint"] and hit.get("model") == model:
            c = clean_tldr(hit.get("tldr"), hit.get("chips"))
            if c:
                s["cached"] = c
    return {"version": 1, "model": model, "sessions": sessions}


def tldr_store(ws: Path, at: datetime, rows: List[dict], home: Optional[Path] = None,
               app_dir: Optional[Path] = None) -> int:
    """`tldr --stdin`: merge [{id, fingerprint, model, tldr, chips}] into .mother/chief/tldr.json, for live sessions only
    and cleaned as clean_tldr; entries for sessions no longer live are pruned. Returns how many were stored."""
    ws = Path(ws)
    live = {s["id"] for s in _live_sessions(ws, Path(home or Path.home()),
                                             app_dir if app_dir is not None else default_app_dir())}
    n = 0
    with hub.locked(ws):
        path = chief_dir(ws) / "tldr.json"
        doc = {k: v for k, v in _read_obj(path).items() if k in live}
        for r in rows or []:
            if not isinstance(r, dict) or r.get("id") not in live or not isinstance(r.get("fingerprint"), str):
                continue
            c = clean_tldr(r.get("tldr"), r.get("chips"))
            if not c:
                continue
            doc[r["id"]] = {"fingerprint": r["fingerprint"], "model": r.get("model") if r.get("model") in TLDR_MODELS else None,
                            "tldr": c["tldr"], "chips": c["chips"], "at": hub._iso(at)}
            n += 1
        _write_atomic(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return n




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
  lines [--json]        the inbox lines waiting for a decision
  status [--json]       paused, who leads (the heartbeat), today's budget, pinned sessions and the inbox
  pinned --stdin        snapshot the pinned desktop sessions from list_sessions rows (JSON array on stdin) into
                        .mother/chief/desktop.json; prints {pinned: N}
  tail --json           each pinned session's recent tail (at most 6000 characters) for the Chief pane's TL;DR,
                        with the cached TL;DR when the session has not moved; prints {version, model, sessions}
  tldr --stdin          store the pane's TL;DRs and chips (JSON array on stdin) in .mother/chief/tldr.json;
                        prints {stored: N}
  spend --stdin         add one pane model call ({model, input_tokens, output_tokens} on stdin) to today's spend;
                        prints {added: usd}
  portrait --json       the Chief pane's picture: .mother/portrait.png when it is a PNG of at most 2 MiB, else the
                        bundled one; prints {version, source, width, height, png (base64)}, plus jpeg, jpeg_width and
                        jpeg_height for a small .mother/portrait.jpg, and svg (the bundled vector)
  pause | resume        the chief-only switch (.mother/chief/PAUSED); never the global one

A valued option takes --opt=V or --opt V."""

# verb: (boolean flags, valued options); no verb takes a positional
VERBS = {
    "lines": (("--json",), ()), "status": (("--json",), ()), "tick": (("--json",), ("--as",)),
    "pause": ((), ()), "resume": ((), ()),
    "triage": (("--stdin", "--decide"), ()), "pinned": (("--stdin",), ()),
    "tail": (("--json",), ()), "tldr": (("--stdin",), ()), "spend": (("--stdin",), ()),
    "portrait": (("--json",), ()),
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
    inbox = inbox_items(ws, cfg)
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
        "guard": guard_state(ws),
    }


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
    if pos:
        return _usage("%s takes no arguments" % verb)
    ws = Path(ws)
    as_json = bool(opts.get("json"))
    if verb in ("pause", "resume"):
        (pause if verb == "pause" else resume)(ws)
        print("chief %s" % ("paused" if verb == "pause" else "resumed"))
        return EXIT_OK
    if verb == "lines":
        got = lines(ws)
        if as_json:
            print(json.dumps(got, indent=2, ensure_ascii=False))
        else:
            for l in got:
                print("%s  %s  [%s]  %s: %s" % (l["id"], l["key"], l["session"] or "-", l["who"], l["title"]))
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
        print(json.dumps(session_tails(ws, cfg, Path.home(), default_app_dir()), indent=2, ensure_ascii=False))
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
        print(json.dumps({"stored": tldr_store(ws, at, rows, Path.home(), default_app_dir())}))
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
