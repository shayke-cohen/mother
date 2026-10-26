"""mother now — the deterministic core behind the hourly tick and the Now panel (docs/DESIGN.md).

This part: when the manager is "at work", and how long a signal may sit before it counts.

  working hours   hours of a time range that fall inside workdays ∩ active hours, in the manager's
                  timezone. Nights and weekends do not age anything.
  thresholds      per-signal "how long before it shows up", in working hours; "1d" = one active window
                  (16h for the default 06:00-22:00). A signal is off when switched off in
                  routines.tick.signals or when its source connector is "none"/absent.

Python 3.9 stdlib only.
"""
import json
import os
import re
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
    "bug_unowned": ("neglected", "tracker", "4h"),
    "blocker_cleared": ("neglected", "tracker", "1d"),
    "sprint_at_risk": ("neglected", "tracker", "0"),
    "outside_question": ("neglected", "chat", "4h"),
    "action_item_no_ticket": ("neglected", "workspace", "1d"),
    "one_on_one_drift": ("neglected", "workspace", "0"),
}
SLOW_SIGNALS = {k for k, v in SIGNALS.items() if v[0] == "neglected" and k != "default_branch_red"}

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


def merge(inp: dict, prev: Optional[dict], state: dict, cfg: dict, now: datetime) -> dict:
    """Fold this tick's candidates into the previous now.json. `state` (manager done/snooze) is not
    applied yet."""
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

    items, seen, dropped = [], set(), 0
    for c in inp.get("candidates") or []:
        try:
            if not isinstance(c, dict) or any(not c.get(k) for k in ("kind", "link", "signal", "since", "title")):
                raise KeyError("candidate")
            sig = c["signal"]
            since = _parse_iso(c["since"])
        except (KeyError, ValueError, TypeError):
            dropped += 1
            continue
        if sig not in SIGNALS:
            continue
        th = threshold_hours(sig, cfg, s)
        if th is None or working_hours(since, now, s) < th:
            continue
        iid = item_id(c)
        if iid in seen:
            continue
        seen.add(iid)
        section = SIGNALS[sig][0]
        if c.get("actions") and sig == "suggestion_waiting":
            section = "suggestion"
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
            actions.append({"id": aid, "label": a["label"], "prompt": "mother now do %s" % aid})
        first_seen = old["first_seen"] if old else _iso(now)
        if section == "suggestion" and working_hours(since, now, s) >= lapse:
            continue
        items.append({
            "id": iid, "section": section, "signal": sig, "title": c["title"], "why": c.get("why", ""),
            "link": c["link"], "also": list(c.get("also") or []), "team": c.get("team"),
            "since": c["since"], "first_seen": first_seen, "last_seen": _iso(now),
            "state": "open" if old else "new",
            "snooze_until": (old or {}).get("snooze_until"), "actions": actions,
        })

    for iid, pi in prev_items.items():
        if iid in seen:
            continue
        sig = pi.get("signal")
        if sig not in SIGNALS or threshold_hours(sig, cfg, s) is None:
            continue                       # signal switched off or source gone
        row = coverage.get(SIGNALS[sig][1]) if sig in SIGNALS else None
        if sig in checked and row is not None and row["status"] == "ok":
            continue                       # looked, and it is gone: closed
        kept = dict(pi)
        if kept.get("state") == "new":
            kept["state"] = "open"
        if sig in checked and row is not None:
            kept.setdefault("unverified_since", _iso(now))
        if kept["section"] == "suggestion" and working_hours(_parse_iso(kept["first_seen"]), now, s) >= lapse:
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
        "items": items,
    }


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
