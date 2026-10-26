"""mother now — the deterministic core behind the hourly tick and the Now panel (docs/DESIGN.md).

This part: when the manager is "at work", and how long a signal may sit before it counts.

  working hours   hours of a time range that fall inside workdays ∩ active hours, in the manager's
                  timezone. Nights and weekends do not age anything.
  thresholds      per-signal "how long before it shows up", in working hours; "1d" = one active window
                  (16h for the default 06:00-22:00). A signal is off when switched off in
                  routines.tick.signals or when its source connector is "none"/absent.

Python 3.9 stdlib only.
"""
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
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
