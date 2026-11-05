"""MOTHER habits — what the manager did with the cards the Chief pane showed, and what that says about the next one.

Three machine-local files under <ws>/.mother/chief/, none of them synced:
  actions.jsonl  one line per action on a card (and one `shown` line per Inbox item triage made): labels and a title
                 only, never a message body or a summary. 90 days, at most 5000 lines.
  quieted.json   the lines the quiet rule logged instead of showing, so each can still be shown.
  habits.json    {exceptions: [bucket ids]}: buckets the manager said never to quiet.
  HABITS_OFF     the switch: while it exists nothing is quieted and no habit goes into a triage prompt.

Everything here is computed in code. lib/chief.py calls in; this module reaches chief's helpers lazily, since chief
imports it. Stdlib only.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import hub

ACTIONS_FILE, QUIETED_FILE, HABITS_FILE, OFF_FLAG = "actions.jsonl", "quieted.json", "habits.json", "HABITS_OFF"
KEEP_DAYS, KEEP_LINES = 90, 5000
HABIT_DAYS = 30
# what the pane may log; `shown` is written only by triage (a line became an Inbox item)
VERBS = ("open_case", "action", "done", "snooze", "ignore", "follow", "dismiss", "answer", "ask")
SHOWN = "shown"
ENGAGED = ("open_case", "action", "answer", "follow", "ask")
SOURCES = ("chat", "mail", "tracker", "code", "calendar", "manual", "error", "session", "report", "other")
TITLE_MAX, LABEL_MAX, WHO_MAX, ID_MAX, KEY_MAX, ACTION_LABEL_MAX, NOTE_MAX = 60, 40, 40, 80, 200, 24, 80
QUIET_MIN_SHOWN, QUIET_MIN_IGNORED = 5, 4      # the quiet rule's floor: items shown, items ignored or closed unread
PREFERS_MIN = 3
QUIETED_KEEP, QUIETED_KEEP_DAYS, QUIETED_RECENT, QUIETED_COUNT_DAYS = 200, 30, 10, 7
EXCEPTIONS_MAX = 200
QUIET_KINDS, QUIET_URGENCIES = ("fyi", "act"), ("today", "later")
BUCKET_RE = re.compile(r"^(%s)\|(label|who)\|(.{1,%d})$" % ("|".join(SOURCES), LABEL_MAX))


def _chief():
    import chief
    return chief


def _dir(ws: Path) -> Path:
    return Path(ws) / ".mother" / "chief"


# ---- the switch and the exceptions --------------------------------------------------------------------

def is_on(ws: Path) -> bool:
    return not (_dir(ws) / OFF_FLAG).exists()


def set_on(ws: Path, on: bool) -> None:
    flag = _dir(ws) / OFF_FLAG
    if on:
        try:
            flag.unlink()
        except FileNotFoundError:
            pass
    else:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("")


def exceptions(ws: Path) -> List[str]:
    got = _chief()._read_obj(_dir(ws) / HABITS_FILE).get("exceptions")
    return [b for b in got if isinstance(b, str) and BUCKET_RE.match(b)] if isinstance(got, list) else []


def add_exception(ws: Path, bucket) -> bool:
    """Never quiet this bucket again. False for anything that is not a `(source, label)` or `(source, who)` bucket id."""
    if not isinstance(bucket, str) or not BUCKET_RE.match(bucket):
        return False
    with hub.locked(ws):
        kept = [b for b in exceptions(ws) if b != bucket] + [bucket]
        _chief()._write_atomic(_dir(ws) / HABITS_FILE,
                               json.dumps({"version": 1, "exceptions": kept[-EXCEPTIONS_MAX:]}, ensure_ascii=False) + "\n")
    return True


# ---- the actions log ----------------------------------------------------------------------------------

def who_name(who) -> str:
    """A person as a bucket names them: the name without a trailing `<id>`."""
    return _chief().safe_line(re.sub(r"\s*<[^<>]*>\s*$", "", who if isinstance(who, str) else ""), WHO_MAX)


def bucket_label(key) -> str:
    """What a key belongs to: a chat channel or group (never a direct message), a tracker project, a repo. '' for
    the rest (mail, calendar, a note): those have no `(source, label)` bucket."""
    k = str(key or "").strip()
    source = _chief().source_of(k)
    if source == "tracker":
        return k.rsplit("-", 1)[0][:LABEL_MAX]
    if source == "code":
        return k.split("#", 1)[0][:LABEL_MAX]
    if source == "chat":
        if k.startswith("wa:"):
            return k[:LABEL_MAX] if k.endswith("@g.us") else ""
        head, _, tail = k.partition("/")
        return "" if tail == "dm" else head[:LABEL_MAX]
    return ""


def clean_action(doc, at: datetime, verbs=VERBS) -> Optional[dict]:
    """One log row from what a caller handed over, or None for an unknown verb. Only the row's own fields are kept
    (an unknown key, so any body or summary, is dropped), each one safe line; `source` and `label` come from the key
    when it has one."""
    c = _chief()
    if not isinstance(doc, dict) or doc.get("verb") not in verbs:
        return None

    def text(name, cap):
        return c.safe_line(doc.get(name) if isinstance(doc.get(name), str) else "", cap)

    def pick(name, allowed):
        return doc[name] if isinstance(doc.get(name), str) and doc[name] in allowed else None

    key = text("key", KEY_MAX)
    source = c.source_of(key) if key else "other"
    if source == "other" and doc.get("source") in SOURCES:
        source = doc["source"]
    row = {"at": hub._iso(at), "verb": doc["verb"], "item_id": text("item_id", ID_MAX), "key": key, "source": source,
           "label": bucket_label(key) or text("label", LABEL_MAX), "who": who_name(doc.get("who")),
           "kind": pick("kind", c.KINDS), "urgency": pick("urgency", c.URGENCIES), "on": pick("on", c.ONS),
           "title": text("title", TITLE_MAX)}
    if text("action_label", ACTION_LABEL_MAX):
        row["action_label"] = text("action_label", ACTION_LABEL_MAX)
    idx = doc.get("action_index")
    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < c.ACTIONS_MAX:
        row["action_index"] = idx     # 0 = the recommended one
    return row


def _read_rows(ws: Path) -> List[dict]:
    try:
        text = (_dir(ws) / ACTIONS_FILE).read_text()
    except (OSError, ValueError):
        return []
    rows = []
    for ln in text.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("verb"), str) and hub._parse_at(d.get("at")) is not None:
            rows.append(d)
    return rows


def append_actions(ws: Path, at: datetime, rows: List[dict]) -> int:
    """Append cleaned rows and prune: nothing older than 90 days, at most the newest 5000 lines."""
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return 0
    with hub.locked(ws):
        floor = at - timedelta(days=KEEP_DAYS)
        kept = [r for r in _read_rows(ws) + rows if hub._parse_at(r["at"]) >= floor][-KEEP_LINES:]
        _chief()._write_atomic(_dir(ws) / ACTIONS_FILE,
                               "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    return len(rows)


def actions(ws: Path, at: datetime, days: int = KEEP_DAYS) -> List[dict]:
    floor = at - timedelta(days=days)
    return [r for r in _read_rows(ws) if hub._parse_at(r["at"]) >= floor]


# ---- habits: the log, counted per bucket --------------------------------------------------------------

def bucket_id(source: str, by: str, value: str) -> str:
    return "%s|%s|%s" % (source, by, value)


def habits(ws: Path, at: datetime, days: int = HABIT_DAYS) -> dict:
    """The last 30 days per bucket: `(source, label)`, `(source, who)`, `(source, kind)`. Each item (its rows share
    an item id) counts once in `shown` and in at most one of: `engaged` (it was opened, acted on, answered, followed
    or asked about), `ignored`, `closed_unread` (done with no engagement), `snoozed`; in that order of precedence.
    `engage_rate` is engaged / shown. A `(source, kind)` bucket carries `prefers` when the manager picked one
    non-recommended action label at least 3 times and more often than the recommended one."""
    items, order = {}, []
    for n, r in enumerate(actions(ws, at, days)):
        iid = r.get("item_id") if isinstance(r.get("item_id"), str) and r["item_id"] else "row:%d" % n
        if iid not in items:
            items[iid] = {"verbs": set(), "source": "", "label": "", "who": "", "kind": "", "picks": []}
            order.append(iid)
        it = items[iid]
        it["verbs"].add(r["verb"])
        for f in ("source", "label", "who", "kind"):
            if not it[f] and isinstance(r.get(f), str) and r[f]:
                it[f] = r[f]
        if r["verb"] == "action" and isinstance(r.get("action_index"), int):
            it["picks"].append((r["action_index"], str(r.get("action_label") or "")))
    buckets = {}
    for iid in order:
        it = items[iid]
        v = it["verbs"]
        state = ("engaged" if v & set(ENGAGED) else "ignored" if "ignore" in v else "closed_unread" if "done" in v
                 else "snoozed" if "snooze" in v else None)
        for by in ("label", "who", "kind"):
            if not it["source"] or not it[by]:
                continue
            bid = bucket_id(it["source"], by, it[by])
            b = buckets.setdefault(bid, {"id": bid, "by": by, "source": it["source"], "value": it[by], "shown": 0,
                                         "engaged": 0, "closed_unread": 0, "ignored": 0, "snoozed": 0, "_picks": []})
            b["shown"] += 1
            if state:
                b[state] += 1
            if by == "kind":
                b["_picks"].extend(it["picks"])
    out = []
    for bid in sorted(buckets):
        b = buckets[bid]
        picks = b.pop("_picks")
        b["engage_rate"] = round(b["engaged"] / b["shown"], 3) if b["shown"] else 0.0
        first, others = sum(1 for i, _ in picks if i == 0), {}
        for i, label in picks:
            if i > 0 and label:
                others[label.lower()] = (others.get(label.lower(), (0, label))[0] + 1, label)
        best = max(sorted(others.values(), key=lambda x: x[1]), key=lambda x: x[0]) if others else None
        if best and best[0] >= PREFERS_MIN and best[0] > first:
            b["prefers"] = best[1]
        out.append(b)
    return {"version": 1, "days": days, "on": is_on(ws), "buckets": out, "exceptions": exceptions(ws)}


def _line_buckets(doc: dict, source: str, key, who) -> List[dict]:
    """The line's own `(source, label)` then `(source, who)` buckets that the log knows."""
    by_id = {b["id"]: b for b in (doc or {}).get("buckets") or []}
    out = []
    for by, value in (("label", bucket_label(key)), ("who", who_name(who))):
        b = by_id.get(bucket_id(source, by, value)) if value else None
        if b:
            out.append(b)
    return out


LABEL_NAME = {"chat": "this_channel", "tracker": "this_project", "code": "this_repo"}


def for_line(doc: Optional[dict], source: str, key, who) -> Optional[dict]:
    """What a triage prompt carries for one line: counts for its own buckets only, plus `prefers` ({kind: action
    label}) for its source. None when habits are off or the log says nothing about it."""
    if not doc or not doc.get("on"):
        return None
    out = {}
    for b in _line_buckets(doc, source, key, who):
        name = "this_person" if b["by"] == "who" else LABEL_NAME.get(source, "this_label")
        out[name] = {k: b[k] for k in ("shown", "engaged", "ignored", "closed_unread")}
    prefers = {b["value"]: b["prefers"] for b in doc.get("buckets") or []
               if b["by"] == "kind" and b["source"] == source and b.get("prefers")}
    if prefers:
        out["prefers"] = prefers
    return out or None


def bucket_text(b: dict, channels: Optional[dict] = None) -> str:
    """A bucket in words: `chat #platform`, `tracker PLAT`, `chat Sam`."""
    value = b["value"]
    name = (channels or {}).get(value) if b["by"] == "label" and b["source"] == "chat" else None
    if name:
        value = "#" + str(name).lstrip("#")
    return _chief().safe_line("%s %s" % (b["source"], value), LABEL_MAX + 12)


def quiet(doc: Optional[dict], line: dict, verdict, to_manager, has_session, folds: bool,
          channels: Optional[dict] = None) -> Optional[dict]:
    """The quiet rule: {bucket, why} when this line is logged instead of shown, else None. ALL must hold:
    habits are on; the line has a well-formed verdict that itself says kind `fyi` or `act` and urgency `today` or
    `later`; the line is not `manual` or `error` (by kind or by key), not addressed to or mentioning the manager
    (so never a direct message), its key has no live session, and deciding it folds no other line; and one of its
    own `(source, label)` / `(source, who)` buckets, not on the exceptions list, has at least 5 items shown in 30
    days, none engaged with, and at least 4 ignored or closed unread."""
    c = _chief()
    if not doc or not doc.get("on") or not isinstance(verdict, dict):
        return None
    if verdict.get("kind") not in QUIET_KINDS or verdict.get("urgency") not in QUIET_URGENCIES:
        return None
    source = c.source_of(line.get("key"))
    if line.get("kind") in c.TRIAGE_NEVER_LOG or source in c.TRIAGE_NEVER_LOG:
        return None
    key = str(line.get("key") or "")
    if to_manager or key.endswith("/dm") or (key.startswith("wa:") and not key.endswith("@g.us")):
        return None
    if has_session or folds:
        return None
    skip = set(doc.get("exceptions") or [])
    for b in _line_buckets(doc, source, key, line.get("who")):
        unread = b["ignored"] + b["closed_unread"]
        if b["id"] not in skip and b["shown"] >= QUIET_MIN_SHOWN and b["engaged"] == 0 and unread >= QUIET_MIN_IGNORED:
            return {"bucket": b["id"],
                    "why": "habit: %s ignored %d of %d" % (bucket_text(b, channels), unread, b["shown"])}
    return None


def habit_note(v) -> Optional[str]:
    """A verdict's (or a stored extra's) `habit_note` as one safe line of at most 80 characters, else None."""
    note = v.get("habit_note") if isinstance(v, dict) else None
    return (_chief().safe_line(note, NOTE_MAX) or None) if isinstance(note, str) else None


def shown_row(at: datetime, item_id: str, line: dict, verdict) -> Optional[dict]:
    """The `shown` row for a line that became Inbox item `item_id`."""
    v = verdict if isinstance(verdict, dict) else {}
    return clean_action({"verb": SHOWN, "item_id": item_id, "key": line.get("key"), "who": line.get("who"),
                         "kind": v.get("kind"), "urgency": v.get("urgency"), "on": v.get("on"),
                         "title": v.get("title") if isinstance(v.get("title"), str) else line.get("title")},
                        at, verbs=(SHOWN,))


# ---- quieted lines: kept so each can still be shown ---------------------------------------------------

def _quieted(ws: Path) -> List[dict]:
    rows = _chief()._read_obj(_dir(ws) / QUIETED_FILE).get("items")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)] if isinstance(rows, list) else []


def _save_quieted(ws: Path, at: datetime, rows: List[dict]) -> None:
    floor = at - timedelta(days=QUIETED_KEEP_DAYS)
    kept = [r for r in rows if (hub._parse_at(r.get("at")) or at) >= floor][-QUIETED_KEEP:]
    _chief()._write_atomic(_dir(ws) / QUIETED_FILE, json.dumps({"version": 1, "items": kept}, ensure_ascii=False) + "\n")


def record_quieted(ws: Path, at: datetime, rows: List[dict]) -> None:
    """Remember quieted lines: [{line, verdict, bucket, why, label}] as triage_decide logged them."""
    c = _chief()
    new = []
    for r in rows:
        l, v = r["line"], r["verdict"]
        extra = c.verdict_extra(v)
        new.append({"id": l["id"], "key": c.safe_line(l.get("key"), KEY_MAX), "title": c.safe_line(v.get("title"), TITLE_MAX),
                    "tldr": c.safe_line(v.get("tldr"), c.TLDR_MAX), "label": c.safe_line(r.get("label"), LABEL_MAX),
                    "who": who_name(l.get("who")), "link": l.get("link") if isinstance(l.get("link"), str) else "",
                    "bucket": r["bucket"], "why": c.safe_line(r["why"], 120), "at": hub._iso(at), "extra": extra})
    if not new:
        return
    with hub.locked(ws):
        ids = {r["id"] for r in new}
        _save_quieted(ws, at, [r for r in _quieted(ws) if r["id"] not in ids] + new)


def quieted_doc(ws: Path, at: datetime) -> dict:
    """ChiefStatus.quieted: {count_7d, recent: [{id, key, title, label, bucket, at, why}]} (the last 10, newest first)."""
    rows = _quieted(ws)
    floor = at - timedelta(days=QUIETED_COUNT_DAYS)
    recent = [{k: r.get(k) or "" for k in ("id", "key", "title", "label", "bucket", "at", "why")}
              for r in reversed(rows[-QUIETED_RECENT:])]
    return {"count_7d": sum(1 for r in rows if (hub._parse_at(r.get("at")) or floor) > floor), "recent": recent}


def show_quieted(ws: Path, cfg: dict, at: datetime, line_id: str) -> Optional[str]:
    """Show: a quieted line becomes a normal open Inbox item (its key's open item is refreshed, else a new one),
    with its verdict's extras, and leaves the quieted list. Returns the item id, or None when it is not listed."""
    c = _chief()
    with hub.locked(ws):
        rows = _quieted(ws)
        row = next((r for r in rows if r["id"] == line_id), None)
        if row is None:
            return None
        h = hub._hub_doc(ws)
        stamp = hub._iso(at)
        old = [u for u in h["updates"] if isinstance(u, dict) and u.get("key") == row["key"] and u.get("state") != "done"]
        if old:
            u = old[0]
            u.update({"title": row["title"], "why": row.get("tldr") or "", "link": row.get("link") or "", "at": stamp,
                      "state": "open"})
            u.pop("snooze_until", None)
        else:
            n = int(h.get("next_update") or 0)
            for x in h["updates"]:
                m = re.match(r"U(\d+)$", str(x.get("id")) if isinstance(x, dict) else "")
                n = max(n, int(m.group(1)) if m else 0)
            h["next_update"] = n + 1
            u = {"id": "U%d" % (n + 1), "key": row["key"], "title": row["title"], "why": row.get("tldr") or "",
                 "link": row.get("link") or "", "at": stamp, "state": "open"}
            h["updates"].append(u)
        uid = u["id"]
        hub._save_hub(ws, h, hub._sessions_doc(ws), cfg or {}, at)
        _save_quieted(ws, at, [r for r in rows if r["id"] != line_id])
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else None
    c._store_extras(ws, {uid: extra})
    x = extra or {}
    append_actions(ws, at, [clean_action({"verb": SHOWN, "item_id": uid, "key": row["key"], "who": row.get("who"),
                                          "kind": x.get("kind"), "urgency": x.get("urgency"), "on": x.get("on"),
                                          "title": row["title"]}, at, verbs=(SHOWN,))])
    return uid
