"""mother now — the deterministic core behind the hourly tick and the Now panel (docs/DESIGN.md).

This part: when the manager is "at work", and how long a signal may sit before it counts.

  working hours   hours of a time range that fall inside workdays ∩ active hours, in the manager's
                  timezone. Nights and weekends do not age anything.
  thresholds      per-signal "how long before it shows up", in working hours; "1d" = one active window
                  (16h for the default 06:00-22:00). A signal is off when switched off in
                  routines.tick.signals or when its source connector is "none"/absent.

Python 3.9 stdlib only.
"""
import copy
import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

# signal -> (section, source, default threshold)
SIGNALS = {
    "review_requested": ("on_you", "code", "4h"),
    "unanswered_mention": ("on_you", "chat", "2h"),
    "decision_open": ("on_you", "workspace", "1d"),
    "ticket_waiting_on_you": ("on_you", "tracker", "0"),
    "mail_needs_reply": ("on_you", "mail", "4h"),
    "invite_unanswered": ("on_you", "calendar", "0"),
    "promise_due": ("on_you", "workspace", "0"),
    "suggestion_waiting": ("on_you", "workspace", "0"),
    "pr_no_reviewer": ("neglected", "code", "1d"),
    "pr_approved_unmerged": ("neglected", "code", "2d"),
    "pr_changes_silent": ("neglected", "code", "3d"),
    "default_branch_red": ("neglected", "code", "2h"),
    "ticket_stalled": ("neglected", "tracker", "3d"),
    "bug_unowned": ("neglected", "tracker", "1d"),
    "blocker_cleared": ("neglected", "tracker", "1d"),
    "sprint_at_risk": ("neglected", "tracker", "0"),
    "outside_question": ("neglected", "chat", "4h"),
    "action_item_no_ticket": ("neglected", "workspace", "1d"),
    "one_on_one_drift": ("neglected", "workspace", "0"),
}
SLOW_SIGNALS = {k for k, v in SIGNALS.items() if v[0] == "neglected" and k != "default_branch_red"}
# Work that hasn't moved in this many days is backlog, not a pulse item. Workspace signals (promises,
# decisions, suggestions) carry their own lifecycle and never age out here.
DEFAULT_MAX_AGE_DAYS = 30
AGELESS = {k for k, v in SIGNALS.items() if v[1] == "workspace"}

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_HOURS = "06:00-22:00"
DEFAULT_EVERY = 60
DEFAULT_WORKDAYS = DAYS[:5]


@dataclass
class Schedule:
    tz: ZoneInfo
    workdays: List[str]
    start: time
    end: time
    every: int

    @classmethod
    def from_config(cls, cfg):
        mgr = cfg.get("manager") or {}
        tick = (cfg.get("routines") or {}).get("tick") or {}
        a, b = (tick.get("active_hours") or DEFAULT_HOURS).split("-")
        return cls(
            tz=ZoneInfo(mgr.get("timezone") or "UTC"),
            workdays=list(mgr.get("workdays") or DEFAULT_WORKDAYS),
            start=time.fromisoformat(a.strip()),
            end=time.fromisoformat(b.strip()),
            every=int(tick.get("every") or DEFAULT_EVERY),
        )


def working_hours(a: datetime, b: datetime, s: Schedule) -> float:
    """Hours of [a, b) inside workdays ∩ active hours, in s.tz. 0 if b <= a."""
    if b <= a:
        return 0
    a, b = a.astimezone(s.tz), b.astimezone(s.tz)
    total = 0.0
    day = a.date()
    while day <= b.date():
        if DAYS[day.weekday()] in s.workdays:
            lo = max(a, datetime.combine(day, s.start, tzinfo=s.tz))
            hi = min(b, datetime.combine(day, s.end, tzinfo=s.tz))
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        day += timedelta(days=1)
    return total


def day_hours(s: Schedule) -> float:
    """Length of the active window in hours; one "1d" threshold."""
    d = datetime(2000, 1, 1)
    return (datetime.combine(d, s.end) - datetime.combine(d, s.start)).total_seconds() / 3600


def _parse(spec, s: Schedule) -> float:
    spec = str(spec).strip()
    if spec in ("0", ""):
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", spec)
    if not m:
        raise ValueError("bad threshold %r (use Nh, Nd or 0)" % spec)
    n = float(m.group(1))
    return n * (day_hours(s) if m.group(2) == "d" else 1.0)


def threshold_hours(signal: str, cfg: dict, s: Schedule) -> Optional[float]:
    """Working hours a signal must age before it shows; None when the signal is off."""
    _section, source, default = SIGNALS[signal]
    tick = (cfg.get("routines") or {}).get("tick") or {}
    if (tick.get("signals") or {}).get(signal) is False:
        return None
    if source != "workspace" and (cfg.get("connectors") or {}).get(source, "none") in ("none", None, ""):
        return None
    return _parse((tick.get("thresholds") or {}).get(signal, default), s)


# ---------------------------------------------------------------- merge

SECTIONS = ["on_you", "neglected", "suggestion"]
SUGGESTION_LAPSE_DAYS = 3
VERSION = 1
LINKS_MAX = 8
LINK_LABEL_MAX = 24
TOP_MAX = 3


def max_age_days(cfg: dict) -> int:
    """`routines.tick.max_age_days`, default 30; 0 turns the cap off."""
    v = ((cfg.get("routines") or {}).get("tick") or {}).get("max_age_days")
    return DEFAULT_MAX_AGE_DAYS if v is None else int(v)


def _too_old(sig: str, since: datetime, cfg: dict, now: datetime) -> bool:
    cap = max_age_days(cfg)
    return bool(cap) and sig not in AGELESS and now - since > timedelta(days=cap)


def _keys(sig: str, link: str, also) -> set:
    """Every (signal, link) pair that names the same work: the item's link and its `also` links."""
    return {(sig, k) for k in [link] + list(also or []) if k}


def _links(raw) -> list:
    """The candidate's labelled source links, cleaned: http(s) url, a label (trimmed), no repeated url, at most
    LINKS_MAX. A bad entry is skipped; it is not a malformed candidate."""
    out, urls = [], set()
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        url, label = e.get("url"), e.get("label")
        if not isinstance(url, str) or not re.match(r"https?://", url) or url in urls:
            continue
        if not isinstance(label, str) or not label.strip():
            continue
        urls.add(url)
        out.append({"label": label.strip()[:LINK_LABEL_MAX].rstrip(), "url": url})
        if len(out) == LINKS_MAX:
            break
    return out


def item_id(c: dict) -> str:
    return "%s:%s:%s" % (c["kind"], c["link"], c["signal"])


def _iso(d: datetime) -> str:
    return d.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(v: str) -> datetime:
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo("UTC"))


def _in_quiet(cfg: dict, now: datetime) -> bool:
    mgr = cfg.get("manager") or {}
    m = re.fullmatch(r"\s*(\d\d):(\d\d)\s*-\s*(\d\d):(\d\d)\s*", str(mgr.get("quiet_hours") or ""))
    if not m:
        return False
    t = now.astimezone(ZoneInfo(mgr.get("timezone") or "UTC"))
    cur = t.hour * 60 + t.minute
    a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
    return (a <= cur < b) if a <= b else (cur >= a or cur < b)


def next_slot(now: datetime, s: Schedule) -> datetime:
    """First tick slot strictly after `now`: minute :05 of the active window, every s.every minutes,
    on a workday, in s.tz."""
    local = now.astimezone(s.tz)
    day = local.date()
    for _ in range(9):
        if DAYS[day.weekday()] in s.workdays:
            slot = datetime.combine(day, s.start, tzinfo=s.tz).replace(minute=5)
            end = datetime.combine(day, s.end, tzinfo=s.tz)
            while slot < end:
                if slot > local:
                    return slot
                slot += timedelta(minutes=s.every)
        day += timedelta(days=1)
    return local + timedelta(minutes=s.every)


def _coverage(inp: dict, prev: Optional[dict], cfg: dict, now: datetime) -> dict:
    conns = cfg.get("connectors") or {}
    prev_cov = (prev or {}).get("coverage") or {}
    out = {}
    for src, row in (inp.get("coverage") or {}).items():
        if src != "workspace" and conns.get(src, "none") in ("none", None, ""):
            continue
        row = {"status": row} if isinstance(row, str) else dict(row or {})
        if not row.get("status"):
            row["status"] = "couldnt_check"
        if row.get("status") == "ok":
            row.pop("since", None)
        else:
            was = prev_cov.get(src) or {}
            row["since"] = was["since"] if was.get("status") not in (None, "ok") and was.get("since") else _iso(now)
        out[src] = row
    return out


def _state_effect(iid: str, since: Optional[datetime], st_items: dict, now: datetime) -> Optional[str]:
    """What the manager's state does to one item: "hide", "new" (a done id re-requested after the
    done), "open" (snooze ran out), or None. Resurfaced / expired entries are removed from `st_items`."""
    e = st_items.get(iid)
    if not isinstance(e, dict):
        return None
    try:
        if e.get("state") == "done":
            if since is not None and since > _parse_iso(e["at"]):
                del st_items[iid]
                return "new"
            return "hide"
        if e.get("state") == "snoozed":
            if e.get("until") and _parse_iso(e["until"]) > now:
                return "hide"
            del st_items[iid]
            return "open"
    except (KeyError, ValueError, TypeError):
        return "hide"
    return None


def _top_entries(raw, set_by: str, set_at: Optional[str] = None) -> list:
    """Normalise raw entries. `set_at` is when the list was set: an entry's own value wins, else this."""
    out = []
    for e in list(raw or [])[:TOP_MAX]:
        if not isinstance(e, dict) or not e.get("title"):
            continue
        out.append({
            "rank": len(out) + 1, "title": e["title"], "why": e.get("why", ""), "link": e.get("link"),
            "item_id": e.get("item_id"), "set_by": e.get("set_by") or set_by,
            "set_at": e.get("set_at") or set_at,
            "state": "done" if e.get("state") == "done" else "open",
        })
    return out


def _carry_done(top: list, old: Optional[list]) -> list:
    """Keep `done` from the previous list (match by item_id, else title) on a list rebuilt from state."""
    done = [(e.get("item_id"), e.get("title")) for e in old or [] if e.get("state") == "done"]
    for e in top:
        if any((e.get("item_id") and e["item_id"] == i) or (not e.get("item_id") and e["title"] == t)
               for i, t in done):
            e["state"] = "done"
    return top


def _mark_top_done(top: list, now_ids: set, prev_ids: set, st_items: dict) -> list:
    """An entry tied to an item is done when the manager marked that id done, or the item was on the
    list before and is gone now (closed). Done is sticky."""
    for e in top:
        iid = e.get("item_id")
        if not iid or e["state"] == "done":
            continue
        marked = (st_items.get(iid) or {}).get("state") == "done"
        snoozed = (st_items.get(iid) or {}).get("state") == "snoozed"
        if marked or (iid in prev_ids and iid not in now_ids and not snoozed):
            e["state"] = "done"
    return top


def merge(inp: dict, prev: Optional[dict], state: dict, cfg: dict, now: datetime) -> dict:
    """Fold this tick's candidates into the previous now.json and apply the manager's `state`
    (done, snooze, Top 3). Expired snoozes and resurfaced done entries are removed from
    `state["items"]` in place; the caller saves it."""
    s = Schedule.from_config(cfg)
    coverage = _coverage(inp, prev, cfg, now)
    checked = set(inp.get("checked_signals") or [])
    prev_items = {i["id"]: i for i in (prev or {}).get("items") or []}
    next_action = max(int((prev or {}).get("next_action") or 1), 1)
    for pi in prev_items.values():
        for a in pi.get("actions") or []:
            m = re.fullmatch(r"S(\d+)", str(a.get("id")))
            if m:
                next_action = max(next_action, int(m.group(1)) + 1)
    lapse = SUGGESTION_LAPSE_DAYS * day_hours(s)
    st_items = state.setdefault("items", {})
    # The same work found under another link (a build page one tick, a commit the next) keeps its old id,
    # so the manager's done and snooze still apply and the list shows it once.
    prev_by_key = {}
    for pi in prev_items.values():
        for k in _keys(pi.get("signal"), pi.get("link"), pi.get("also")):
            prev_by_key.setdefault(k, pi["id"])

    items, seen, taken, dropped = [], set(), set(), 0
    for c in inp.get("candidates") or []:
        try:
            if not isinstance(c, dict) or any(not c.get(k) for k in ("kind", "link", "signal", "since", "title", "why")):
                raise KeyError("candidate")
            sig = c["signal"]
            since = _parse_iso(c["since"])
        except (KeyError, ValueError, TypeError):
            dropped += 1
            continue
        if sig not in SIGNALS:
            continue
        th = threshold_hours(sig, cfg, s)
        if th is None or working_hours(since, now, s) < th or _too_old(sig, since, cfg, now):
            continue
        keys = _keys(sig, c["link"], c.get("also"))
        if keys & taken:
            continue                       # already listed under another of its links
        iid = item_id(c)
        if iid not in prev_items:
            iid = next((prev_by_key[k] for k in sorted(keys) if k in prev_by_key), iid)
        if iid in seen:
            continue
        seen.add(iid)
        taken |= keys
        section = SIGNALS[sig][0]
        if c.get("actions") and sig == "suggestion_waiting":
            section = "suggestion"
        effect = _state_effect(iid, since, st_items, now)
        if effect == "hide":
            continue
        old = prev_items.get(iid)
        old_ids = {a["label"]: a["id"] for a in (old or {}).get("actions") or []}
        actions = []
        for a in c.get("actions") or []:
            if not isinstance(a, dict) or not a.get("label"):
                dropped += 1
                continue
            aid = old_ids.get(a["label"])
            if aid is None:
                aid = "S%d" % next_action
                next_action += 1
            actions.append({"id": aid, "label": a["label"], "prompt": "mother now do %s" % aid,
                            "task": str(a.get("prompt") or a["label"])})
        first_seen = old["first_seen"] if old else _iso(now)
        if section == "suggestion" and working_hours(since, now, s) >= lapse:
            continue
        items.append({
            "id": iid, "section": section, "signal": sig, "title": c["title"], "why": c.get("why", ""),
            "link": c["link"], "also": list(c.get("also") or []), "links": _links(c.get("links")), "team": c.get("team"),
            "since": c["since"], "first_seen": first_seen, "last_seen": _iso(now),
            "state": effect or ("open" if old else "new"),
            "snooze_until": None, "actions": actions,
        })

    for iid, pi in prev_items.items():
        if iid in seen:
            continue
        sig = pi.get("signal")
        if sig not in SIGNALS or threshold_hours(sig, cfg, s) is None:
            continue                       # signal switched off or source gone
        effect = _state_effect(iid, None, st_items, now)
        if effect == "hide":
            continue
        if _keys(sig, pi.get("link"), pi.get("also")) & taken:
            continue                       # this tick lists the same work under another link
        if _too_old(sig, _parse_iso(pi["since"]), cfg, now):
            continue
        row = coverage.get(SIGNALS[sig][1]) if sig in SIGNALS else None
        if sig in checked and row is not None and row["status"] == "ok":
            continue                       # looked, and it is gone: closed
        kept = dict(pi)
        kept["snooze_until"] = None
        if kept.get("state") == "new" or effect == "open":
            kept["state"] = "open"
        if sig in checked and row is not None:
            kept.setdefault("unverified_since", _iso(now))
        if kept["section"] == "suggestion" and working_hours(_parse_iso(kept["since"]), now, s) >= lapse:
            continue
        items.append(kept)

    items.sort(key=lambda i: (SECTIONS.index(i["section"]), _parse_iso(i["since"])))
    statuses = [r["status"] for r in coverage.values()]
    if not statuses or all(x == "ok" for x in statuses):
        status = "ok"
    elif all(x != "ok" for x in statuses):
        status = "failed"
    else:
        status = "partial"
    nxt = next_slot(now, s)
    today = now.astimezone(s.tz).date().isoformat()
    top, top_date, hint = _pick_top(inp, prev, state, today, _iso(now))
    hint = _resolve_hint(hint, items)
    _link_top(top, items)
    _mark_top_done(top, {i["id"] for i in items}, set(prev_items), st_items)
    return {
        "version": VERSION,
        "generated_at": _iso(now),
        "timezone": (cfg.get("manager") or {}).get("timezone"),
        "quiet": _in_quiet(cfg, now),
        "next_at": _iso(nxt),
        "stale_after": _iso(nxt + timedelta(minutes=s.every)),
        "status": status,
        "coverage": coverage,
        "routines": list(inp.get("routines") or []),
        "counts": {k: sum(1 for i in items if i["section"] == k) for k in SECTIONS},
        "next_action": next_action,
        "dropped": dropped,
        "top": top,
        "top_date": top_date,
        "swap_hint": hint,
        "items": items,
    }


def _resolve_hint(hint, items: list):
    """Keep `item_id` on a swap hint only if it names an item in this run; else take the one item whose
    link equals the hint's link (also when the hint came without an `item_id`); else drop it. The hint
    itself is always kept."""
    if not isinstance(hint, dict):
        return hint
    hint = dict(hint)
    iid = hint.get("item_id")
    if iid and any(i["id"] == iid for i in items):
        return hint
    same = [i for i in items if hint.get("link") and i.get("link") == hint["link"]]
    if len(same) == 1:
        hint["item_id"] = same[0]["id"]
    else:
        hint.pop("item_id", None)
    return hint


def _link_top(top: list, items: list) -> list:
    """Give a Top 3 entry without an `item_id` the id of the one item whose link equals its link, so
    the panel can tie them together and closing the item marks the entry done."""
    for e in top:
        if e.get("item_id") or not e.get("link"):
            continue
        same = [i["id"] for i in items if i.get("link") == e["link"]]
        if len(same) == 1:
            e["item_id"] = same[0]
    return top


def _agent_owned(top: list) -> bool:
    """True when no entry was set by the manager: such a list may carry a swap hint."""
    return all(e.get("set_by") != "manager" for e in top or [])


def _pick_top(inp: dict, prev: Optional[dict], state: dict, today: str, stamp: Optional[str] = None):
    """Top 3 today: the list kept in state for today wins (the manager's, or the agent's with the
    manager's done marks); else the list already set today stays as it is; else the first tick of the
    day takes the agent's proposal. A swap hint is kept only while no entry is the manager's."""
    st_top = state.get("top")
    if isinstance(st_top, dict) and st_top.get("date") == today:
        top = _top_entries(st_top.get("entries"), "manager", st_top.get("at"))
        if prev and prev.get("top_date") == today:
            _carry_done(top, prev.get("top"))
        return top, today, (inp.get("swap_hint") if _agent_owned(top) else None)
    if prev and prev.get("top_date") == today and prev.get("top"):
        top = copy.deepcopy(prev["top"])
        return top, today, (inp.get("swap_hint") if _agent_owned(top) else None)
    return _top_entries(inp.get("top"), "agent", stamp), today, inp.get("swap_hint")


def write_now(ws: Path, doc: dict) -> None:
    d = Path(ws) / ".mother"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ("now.json.%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    os.replace(str(tmp), str(d / "now.json"))


def load_now(ws: Path) -> Optional[dict]:
    try:
        doc = json.loads((Path(ws) / ".mother" / "now.json").read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("version") == VERSION else None


# ---------------------------------------------------------------- manager state

def _empty_state() -> dict:
    return {"items": {}, "top": None}


def load_state(ws: Path) -> dict:
    try:
        st = json.loads((Path(ws) / ".mother" / "now-state.json").read_text())
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(st, dict):
        return _empty_state()
    if not isinstance(st.get("items"), dict):
        st["items"] = {}
    if not isinstance(st.get("top"), dict):
        st["top"] = None
    return st


@contextmanager
def locked(ws: Path):
    """Exclusive flock on .mother/now.lock for the with-block. Wrap every load_state -> change ->
    save_state in it, so a tick and a panel edit cannot overwrite each other. Not re-entrant."""
    d = Path(ws) / ".mother"
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / "now.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save_state(ws: Path, st: dict) -> None:
    """Atomic write (temp file + rename). Does not lock: the caller holds `locked(ws)` around its
    load -> change -> save."""
    d = Path(ws) / ".mother"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ("now-state.json.%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(st, indent=2) + "\n")
    os.replace(str(tmp), str(d / "now-state.json"))


def snooze_until(word: str, now: datetime, s: Schedule) -> datetime:
    """`1h` = now + 1h; `tomorrow` = next scheduled workday at active start; `next-week` = first
    workday of next week (weeks start Monday) at active start. Returned in s.tz."""
    local = now.astimezone(s.tz)
    if word == "1h":
        return local + timedelta(hours=1)
    if word == "tomorrow":
        day = local.date() + timedelta(days=1)
    elif word == "next-week":
        day = local.date() + timedelta(days=7 - local.weekday())
    else:
        raise ValueError("bad snooze %r (use 1h, tomorrow or next-week)" % word)
    for _ in range(8):
        if DAYS[day.weekday()] in s.workdays:
            break
        day += timedelta(days=1)
    return datetime.combine(day, s.start, tzinfo=s.tz)


def apply_edit(doc: dict, st: dict) -> dict:
    """Re-apply manager state to an existing now.json so a panel edit shows before the next tick.
    Pure: returns a new doc."""
    out = copy.deepcopy(doc)
    st_items = copy.deepcopy((st or {}).get("items") or {})
    try:
        ref = _parse_iso(doc["generated_at"])
    except (KeyError, ValueError, TypeError):
        ref = datetime.now(ZoneInfo("UTC"))
    before = {i["id"] for i in doc.get("items") or []}
    items = []
    for i in out.get("items") or []:
        if _state_effect(i["id"], None, st_items, ref) == "hide":
            continue
        items.append(i)
    out["items"] = items
    out["counts"] = {k: sum(1 for i in items if i["section"] == k) for k in SECTIONS}
    top = (st or {}).get("top")
    if isinstance(top, dict) and top.get("date") and top["date"] >= str(out.get("top_date") or ""):
        out["top"] = _top_entries(top.get("entries"), "manager", top.get("at"))
        if top["date"] == doc.get("top_date"):
            _carry_done(out["top"], doc.get("top"))
        out["top_date"] = top["date"]
        if not _agent_owned(out["top"]):
            out["swap_hint"] = None
    _mark_top_done(out.get("top") or [], {i["id"] for i in items}, before, (st or {}).get("items") or {})
    return out


# ---------------------------------------------------------------- CLI

USAGE = """mother now <verb>
  merge                       stdin = merge input; writes now.json, prints the runs note
  show [--json]               the list as text, or the raw now.json
  done <id>                   hide an item (panel only)
  snooze <id> <1h|tomorrow|next-week>
  top set <item-id|text>...   set today's Top 3 (1-3 args)
  top swap <rank> <item-id>   put an item at rank 1-3 (appends past the end)
  top done <rank>             mark a Top 3 entry done
  do <S-id>                   print the item and action JSON for the mother:now skill"""


def _fail(msg: str):
    print("mother: now: %s" % msg, file=sys.stderr)
    sys.exit(2)


def _today(cfg: dict, at: datetime) -> str:
    return at.astimezone(Schedule.from_config(cfg).tz).date().isoformat()


def _find_item(doc: Optional[dict], iid: str) -> dict:
    for i in (doc or {}).get("items") or []:
        if i["id"] == iid:
            return i
    _fail("unknown item %r" % iid)


def _note(doc: dict) -> str:
    c = doc["counts"]
    new = sum(1 for i in doc["items"] if i["section"] == "on_you" and i["state"] == "new")
    bad = sorted(k for k, v in doc["coverage"].items() if v.get("status") not in ("ok", "partial"))
    part = sorted(k for k, v in doc["coverage"].items() if v.get("status") == "partial")
    return "%d on you (%d new), %d neglected, %d suggestions; couldn't check: %s%s" % (
        c["on_you"], new, c["neglected"], c["suggestion"], ", ".join(bad) or "none",
        "; partial: %s" % ", ".join(part) if part else "")


def _show(doc: dict) -> str:
    out = ["Top 3 (%s)" % doc.get("top_date")]
    for e in doc.get("top") or []:
        out.append("  %d. [%s] %s%s" % (e["rank"], "x" if e["state"] == "done" else " ", e["title"],
                                         " — " + e["why"] if e.get("why") else ""))
    for sec, label in (("on_you", "On you"), ("neglected", "Neglected"), ("suggestion", "Suggestions")):
        rows = [i for i in doc["items"] if i["section"] == sec]
        out.append("%s (%d)" % (label, len(rows)))
        for i in rows:
            out.append("  %s%s — %s" % ("* " if i["state"] == "new" else "", i["title"], i["why"]))
            out.append("    %s  [%s]" % (i["link"], i["id"]))
            for a in i.get("actions") or []:
                out.append("    %s: %s" % (a["id"], a["label"]))
    if doc.get("status") != "ok":
        bad = sorted(k for k, v in doc["coverage"].items() if v.get("status") != "ok")
        out.append("Couldn't check: %s" % (", ".join(bad) or "none"))
    return "\n".join(out)


TOP_KEYS = ("title", "why", "link", "item_id", "state", "set_by", "set_at")


def _manager_top(doc: Optional[dict], st: dict, today: str) -> list:
    """Today's entries as state should hold them: the list already in state, else the current list.
    Each entry keeps its own `set_by` and `set_at`, so a done mark does not change who set it."""
    top = st.get("top")
    if isinstance(top, dict) and top.get("date") == today:
        return [dict(e, set_by=e.get("set_by") or top.get("set_by") or "manager",
                     set_at=e.get("set_at") or top.get("at")) for e in top.get("entries") or []]
    if doc and doc.get("top_date") == today:
        return [{k: e.get(k) for k in TOP_KEYS if k in e} for e in doc.get("top") or []]
    return []


def main(args, ws: Path, cfg: dict, at: datetime) -> None:
    verb, rest = (args[0], args[1:]) if args else ("", [])
    if verb in ("", "help", "-h", "--help"):
        print(USAGE)
        return
    s = Schedule.from_config(cfg)
    ws = Path(ws)
    if verb == "show":
        doc = load_now(ws)
        if rest == ["--json"] and doc is not None:
            print(json.dumps(doc, indent=2))
        elif doc is None:
            print("Tick hasn't run on this machine yet.")
        else:
            print(_show(doc))
        return
    if verb == "merge":
        try:
            inp = json.loads(sys.stdin.read())
        except ValueError as e:
            _fail("merge input is not JSON: %s" % e)
        if not isinstance(inp, dict):
            _fail("merge input must be a JSON object")
        with locked(ws):
            state = load_state(ws)
            try:
                doc = merge(inp, load_now(ws), state, cfg, at)
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                _fail("merge input is unusable: %s" % e)
            write_now(ws, doc)
            save_state(ws, state)
        print(_note(doc))
        return
    if verb == "do":
        if len(rest) != 1:
            _fail("usage: mother now do <S-id>")
        doc = load_now(ws)
        lapse = SUGGESTION_LAPSE_DAYS * day_hours(s)
        for i in (doc or {}).get("items") or []:
            for a in i.get("actions") or []:
                if a["id"] == rest[0]:
                    if i["section"] == "suggestion" and working_hours(_parse_iso(i["since"]), at, s) >= lapse:
                        _fail("%s has lapsed" % rest[0])
                    print(json.dumps({"item": i, "action": a}, indent=2))
                    return
        _fail("unknown action %r" % rest[0])
    if verb in ("done", "snooze", "top"):
        _edit(verb, rest, ws, cfg, at, s)
        return
    _fail("unknown verb %r — see `mother now help`" % verb)


def _edit(verb: str, rest: list, ws: Path, cfg: dict, at: datetime, s: Schedule) -> None:
    today = _today(cfg, at)
    with locked(ws):
        doc, st = load_now(ws), load_state(ws)
        if doc is None:
            _fail("no now.json yet — the tick has not run")
        if verb == "done":
            if len(rest) != 1:
                _fail("usage: mother now done <id>")
            _find_item(doc, rest[0])
            st["items"][rest[0]] = {"state": "done", "until": None, "at": _iso(at)}
        elif verb == "snooze":
            if len(rest) != 2:
                _fail("usage: mother now snooze <id> <1h|tomorrow|next-week>")
            _find_item(doc, rest[0])
            try:
                until = snooze_until(rest[1], at, s)
            except ValueError as e:
                _fail(str(e))
            st["items"][rest[0]] = {"state": "snoozed", "until": _iso(until), "at": _iso(at)}
        else:
            sub, rest = (rest[0], rest[1:]) if rest else ("", [])
            entries = _manager_top(doc, st, today)
            stamp = _iso(at)
            if sub == "set":
                if not 1 <= len(rest) <= TOP_MAX:
                    _fail("usage: mother now top set <item-id|text>... (1-%d)" % TOP_MAX)
                entries = []
                for a in rest:
                    hit = next((i for i in doc["items"] if i["id"] == a), None)
                    entries.append({"title": hit["title"], "why": hit["why"], "link": hit["link"], "item_id": hit["id"]}
                                   if hit else {"title": a, "why": "", "link": None, "item_id": None})
            elif sub == "swap":
                if len(rest) != 2 or not rest[0].isdigit() or not 1 <= int(rest[0]) <= TOP_MAX:
                    _fail("usage: mother now top swap <rank 1-%d> <item-id>" % TOP_MAX)
                i = _find_item(doc, rest[1])
                e = {"title": i["title"], "why": i["why"], "link": i["link"], "item_id": i["id"]}
                r = int(rest[0])
                if r <= len(entries):
                    entries[r - 1] = e
                else:
                    entries.append(e)
            elif sub == "done":
                if len(rest) != 1 or not rest[0].isdigit() or not 1 <= int(rest[0]) <= len(entries):
                    _fail("usage: mother now top done <rank> (an existing rank)")
                entries[int(rest[0]) - 1]["state"] = "done"
            else:
                _fail("usage: mother now top set|swap|done ...")
            if sub in ("set", "swap"):           # only these make the list the manager's
                for e in entries:
                    e["set_by"], e["set_at"] = "manager", stamp
            owner = "agent" if _agent_owned(entries) else "manager"
            was = st.get("top") if isinstance(st.get("top"), dict) and st["top"].get("date") == today else {}
            set_at = stamp if sub in ("set", "swap") else (was.get("at") or next(
                (e.get("set_at") for e in entries if e.get("set_at")), None))
            st["top"] = {"date": today, "set_by": owner, "at": set_at, "entries": entries}
        write_now(ws, apply_edit(doc, st))
        save_state(ws, st)
