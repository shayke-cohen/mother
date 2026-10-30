"""mother hub — state, thing keys and the inbox behind the watcher and the hub session (docs/CONTRACT.md §5e).

This part: the machine-local files under <workspace>/.mother/hub/ and the vocabulary they share.

  thing key   one string per piece of work, the same wherever it shows up: tracker `PLAT-123`, code
              `owner/repo#45`, chat `C00000001/<thread ts>`, mail `mail:<thread id>`, calendar `cal:<event id>`.
  inbox line  one JSON object per source update, appended by the watcher; summary at most 300 characters.
  state files written atomically (temp + os.replace) under the flock `hub.lock`; `inbox.jsonl` is only appended to
              (also under `hub.lock`, so a rotation never races an append).

Python 3.9 stdlib only.
"""
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import sys
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SUMMARY_MAX = 300
KEEP_DAYS = 7
KINDS = ("update", "resolved", "error", "sweep", "manual")

_TRACKER = re.compile(r"(?<![A-Za-z0-9_-])[A-Z][A-Z0-9]+-\d+(?![A-Za-z0-9_-])")
_PR_URL = r"https?://[\w.-]+/(?P<u>[\w.-]+/[\w.-]+)/pull/(?P<un>\d+)"
_PR_REF = r"(?<![\w./#-])(?P<r>[\w.-]+/[\w.-]+)#(?P<rn>\d+)(?![\w-])"
_CODE = re.compile(_PR_URL + "|" + _PR_REF)


def hub_dir(ws: Path) -> Path:
    return Path(ws) / ".mother" / "hub"


@contextmanager
def locked(ws: Path):
    """Exclusive flock on .mother/hub/hub.lock for the with-block. Wrap every load_json -> change ->
    save_json in it. Not re-entrant."""
    d = hub_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / "hub.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_json(ws: Path, name: str, default):
    """The parsed file, or `default` when it is missing or unreadable (a missing file means nothing pending)."""
    try:
        return json.loads((hub_dir(ws) / name).read_text())
    except (OSError, ValueError):
        return default


def save_json(ws: Path, name: str, doc) -> None:
    """Atomic write (temp file + rename). Does not lock: the caller holds `locked(ws)`."""
    d = hub_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ("%s.%d.tmp" % (name, os.getpid()))
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(str(tmp), str(d / name))


def thing_key(source: str, ref: dict) -> str:
    if source == "tracker":
        return str(ref["key"])
    if source == "code":
        return "%s#%s" % (ref["repo"], ref["number"])
    if source == "chat":
        return "%s/%s" % (ref["channel"], ref.get("thread_ts") or ref["ts"])
    if source == "mail":
        return "mail:%s" % ref["thread_id"]
    if source == "calendar":
        return "cal:%s" % ref["event_id"]
    if source == "whatsapp":
        return "wa:%s" % ref["jid"]
    raise ValueError("unknown source: %r" % (source,))


def extract_also(text: str) -> List[str]:
    """Tracker keys and PR references (URL or `owner/repo#N`) in `text`, de-duplicated, in order."""
    found = []
    for m in re.finditer(_TRACKER.pattern + "|" + _CODE.pattern, text or ""):
        if m.group("u"):
            k = "%s#%s" % (m.group("u"), m.group("un"))
        elif m.group("r"):
            k = "%s#%s" % (m.group("r"), m.group("rn"))
        else:
            k = m.group(0)
        if k not in found:
            found.append(k)
    return found


def plain(text) -> str:
    """Source text with C0/C1 control characters and Unicode format characters (bidi controls, zero-width
    characters, tags) removed; tabs and newlines become spaces. `title` and `who` are written by whoever sent the
    message, and are shown as one line of plain text."""
    out = []
    for ch in str(text or ""):
        o = ord(ch)
        if ch in "\t\n\r":
            out.append(" ")
        elif o < 32 or 0x7f <= o <= 0x9f or unicodedata.category(ch) in ("Cf", "Zl", "Zp"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def make_line(source: str, kind: str, key: str, at: str, title: str = "", who: str = "", link: str = "",
              summary: str = "", also=None, uid: str = "") -> dict:
    """`uid` replaces `at` in the id when the source has a finer stamp (a chat message's raw ts), so two updates in
    one second do not share an id."""
    if kind not in KINDS:
        raise ValueError("unknown kind: %r" % (kind,))
    return {"id": "%s:%s:%s" % (source, key, uid or at), "at": at, "source": source, "kind": kind, "key": key,
            "also": list(also or []), "title": plain(title), "who": plain(who), "link": link,
            "summary": (summary or "")[:SUMMARY_MAX]}


def _wins(new: dict, cur: dict) -> bool:
    """`new` replaces `cur` for the same key: a later `at`, or the same `at` unless that would let a `resolved` line
    swallow an `update` (a reply never hides a message that arrived with or after it)."""
    if new["at"] != cur["at"]:
        return new["at"] > cur["at"]
    return not (new.get("kind") == "resolved" and cur.get("kind") == "update")


def _must_decide(lines: List[dict]) -> bool:
    """A pass's lines include one `tail` will print: a sweep or error line, or a key whose winning line (`_wins`,
    as `digest` picks it) is not `resolved`. An update and its resolution in one pass print nothing."""
    latest = {}
    for l in lines:
        if l.get("kind") in ("sweep", "error", "manual"):
            return True
        k = l.get("key")
        if k not in latest or _wins(l, latest[k]):
            latest[k] = l
    return any(l.get("kind") != "resolved" for l in latest.values())


def coalesce(lines: List[dict]) -> List[dict]:
    """One line per key: the latest `at` wins (`_wins`), `also` is the union (order kept). First-seen key order.
    A `manual` line is never collapsed: it stays, as it is, where it came."""
    best = {}
    also = {}
    order = []  # type: list
    for l in lines:
        if l.get("kind") == "manual":
            order.append((None, l))
            continue
        k = l["key"]
        also.setdefault(k, [])
        for a in l.get("also") or []:
            if a not in also[k]:
                also[k].append(a)
        if k not in best:
            order.append((k, None))
        if k not in best or _wins(l, best[k]):
            best[k] = l
    out = []
    for k, m in order:
        if k is None:
            out.append(m)
            continue
        l = dict(best[k])
        l["also"] = also[k]
        out.append(l)
    return out


def append_inbox(ws: Path, lines: List[dict]) -> None:
    """Append one JSON object per line to inbox.jsonl, flushed and fsynced. Takes `locked(ws)` itself (so it
    never interleaves with rotate_inbox); the caller must not already hold it."""
    if not lines:
        return
    with locked(ws):
        d = hub_dir(ws)
        with open(str(d / "inbox.jsonl"), "a") as f:
            for l in lines:
                f.write(json.dumps(l, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _fold(src: Path, dest: Path) -> None:
    """Append src to dest (fsynced), then delete src."""
    with open(str(src), "rb") as r, open(str(dest), "ab") as out:
        out.write(r.read())
        out.flush()
        os.fsync(out.fileno())
    src.unlink()


def rotate_inbox(ws: Path, at: datetime) -> None:
    """Move inbox.jsonl aside as inbox-YYYY-MM-DD.jsonl (the date of `at`, appended to if it exists) and
    delete dated files 7 or more days older than `at`. Takes `locked(ws)` itself, so no append can land
    mid-rotation; the caller must not already hold it. Leftover `inbox.jsonl.*.rotating` files from a crash
    are folded into the dated file first, so no line is stranded."""
    with locked(ws):
        d = hub_dir(ws)
        today = at.date()
        cur = d / "inbox.jsonl"
        dest = d / ("inbox-%s.jsonl" % today.isoformat())
        for old in sorted(d.glob("inbox.jsonl.*.rotating")):
            _fold(old, dest)
        if cur.exists():
            if not cur.stat().st_size:
                cur.unlink()
            elif not dest.exists():
                os.replace(str(cur), str(dest))
            else:
                aside = d / ("inbox.jsonl.%d.rotating" % os.getpid())
                os.replace(str(cur), str(aside))
                _fold(aside, dest)
        for p in d.glob("inbox-*.jsonl"):
            m = re.match(r"inbox-(\d{4}-\d{2}-\d{2})\.jsonl$", p.name)
            if m and (today - datetime.strptime(m.group(1), "%Y-%m-%d").date()).days >= KEEP_DAYS:
                p.unlink()


# ------------------------------------------------------------------------------------------------- watcher
CAPS = ("chat", "mail", "tracker", "calendar", "code", "whatsapp")
DEFAULTS = {"every_seconds": 120, "idle_every_seconds": 600, "sweep_minutes": 30}


def connector_of(cfg: dict, cap: str):
    """`connectors.<cap>`; for `whatsapp`, which needs no MCP connector, also the `local` connection."""
    v = ((cfg or {}).get("connectors") or {}).get(cap)
    if isinstance(v, str) and v:
        return v
    conn = (_hub_cfg(cfg).get("connections") or {}).get(cap)
    if cap == "whatsapp" and isinstance(conn, dict) and conn.get("local") == "whatsapp":
        return "whatsapp"
    return None


def _hub_cfg(cfg: dict) -> dict:
    h = (cfg or {}).get("hub")
    return h if isinstance(h, dict) else {}


def _iso(at: datetime) -> str:
    import hub_sources
    return hub_sources.iso(at)


def _parse_at(stamp) -> Optional[datetime]:
    """An ISO time, or None when it is not one."""
    import hub_sources
    try:
        return hub_sources.parse_iso(stamp)
    except (TypeError, ValueError, AttributeError):
        return None


def is_active(cfg: dict, at: datetime) -> bool:
    """Inside the manager's active hours on a workday (routines.tick.active_hours, manager.workdays/timezone)."""
    import now as nowmod
    s = nowmod.Schedule.from_config(cfg)
    local = at.astimezone(s.tz)
    return nowmod.DAYS[local.weekday()] in s.workdays and s.start <= local.time().replace(tzinfo=None) < s.end


def next_active_start(cfg: dict, at: datetime) -> datetime:
    import now as nowmod
    s = nowmod.Schedule.from_config(cfg)
    local = at.astimezone(s.tz)
    day = local.date()
    for _ in range(9):
        start = datetime.combine(day, s.start, tzinfo=s.tz)
        if nowmod.DAYS[day.weekday()] in s.workdays and start > local:
            return start
        day += timedelta(days=1)
    return local + timedelta(hours=1)


def interval(cfg: dict, at: datetime) -> Tuple[bool, int]:
    """(inside active hours, seconds to sleep before the next poll). Outside active hours it is
    `idle_every_seconds`, or, when that is 0, the time until the next active window opens."""
    h = _hub_cfg(cfg)
    if is_active(cfg, at):
        return True, int(h.get("every_seconds", DEFAULTS["every_seconds"]))
    idle = int(h.get("idle_every_seconds", DEFAULTS["idle_every_seconds"]))
    if idle > 0:
        return False, idle
    return False, max(1, int((next_active_start(cfg, at) - at).total_seconds()))


def _as_list(v) -> List[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


_CHAT_USER = re.compile(r"^U[A-Z0-9]{2,}$")


def build_me(cfg: dict) -> dict:
    """What the adapters need to recognise the manager: email, chat user id (manager.chat_user, else the first
    `U…` id in manager.chat_dm, else the matching people entry's `chat`; a `D…` DM channel id is not a user), code
    login, `marks`, the text MOTHER signs its own posts with (agent name `(AI)` and any configured signature), and
    `people`, the config's chat user ids with names (for `who`)."""
    m = (cfg or {}).get("manager") or {}
    users = [x for x in _as_list(m.get("chat_user")) + _as_list(m.get("chat_dm")) if _CHAT_USER.match(x)]
    me = {"email": m.get("email") or "", "chat": users[0] if users else "", "code": m.get("code") or "",
          "tracker_account": m.get("tracker_account") if isinstance(m.get("tracker_account"), str) else ""}
    a = (cfg or {}).get("agent") or {}
    me["marks"] = ["%s (AI)" % a["name"]] if isinstance(a.get("name"), str) and a["name"] else []
    if isinstance(a.get("signature"), str) and a["signature"].strip():
        me["marks"].append(a["signature"].strip())
    for team in (cfg or {}).get("teams") or []:
        for p in (team or {}).get("people") or []:
            if isinstance(p, dict) and me["email"] and (p.get("email") or "").lower() == me["email"].lower():
                me["chat"] = me["chat"] or p.get("chat") or ""
                me["code"] = me["code"] or p.get("code") or ""
    me["people"] = config_people(cfg)
    return me


def config_people(cfg: dict) -> Dict[str, str]:
    """`{chat user id: name}` for every `teams[].people[].chat` in config (the name may be "")."""
    found = {}  # type: Dict[str, str]
    teams = (cfg or {}).get("teams")
    for team in teams if isinstance(teams, list) else []:
        people = team.get("people") if isinstance(team, dict) else None
        for p in people if isinstance(people, list) else []:
            if isinstance(p, dict) and isinstance(p.get("chat"), str) and _CHAT_USER.match(p["chat"]):
                found.setdefault(p["chat"], str(p.get("name") or "").strip())
    return found


def dm_people(cfg: dict) -> Dict[str, str]:
    """The user ids a DM may be mapped for: people in config, never the manager (their self-DM is the inbox)."""
    mine = build_me(cfg).get("chat") or ""
    return {u: n for u, n in config_people(cfg).items() if u != mine}


def _ttl(cfg: dict) -> float:
    try:
        return float(_hub_cfg(cfg).get("update_ttl_hours", 24))
    except (TypeError, ValueError):
        return 24.0


def _older_than_ttl(stamp, cfg: dict, at: datetime) -> bool:
    """True when `stamp` is more than hub.update_ttl_hours working hours before `at`; False when unreadable."""
    import now as nowmod
    import hub_sources
    try:
        return nowmod.working_hours(hub_sources.parse_iso(stamp), at, _schedule(cfg)) > _ttl(cfg)
    except (TypeError, ValueError, AttributeError):
        return False


def pending_work(ws: Path, cfg: dict, at: datetime) -> bool:
    """Something is waiting on the hub: any `pending` proposal in hub.json, or a live session with unseen > 0 that
    was routed to within hub.update_ttl_hours working hours (older unseen news no longer wakes the hub).
    Both files are read defensively; a missing or odd one counts as nothing pending."""
    doc = load_json(ws, "hub.json", {})
    if isinstance(doc, dict) and doc.get("pending"):
        return True
    ses = load_json(ws, "sessions.json", {})
    items = ses.get("sessions") if isinstance(ses, dict) else None
    for s in (items.values() if isinstance(items, dict) else []):
        if isinstance(s, dict) and s.get("state") == "live":
            try:
                unseen = int(s.get("unseen") or 0)
            except (TypeError, ValueError):
                continue
            if unseen > 0 and s.get("last_routed_at") and not _older_than_ttl(s["last_routed_at"], cfg, at):
                return True
    return False


_EMPTY_WATCH = {"version": 1, "follow": {"projects": [], "channels": [], "threads": [], "repos": [], "whatsapp": [],
                                         "dms": {}, "inbox": ""},
                "mute": {"keys": [], "people": [], "channels": [], "topics": [], "until": {}}, "auto": {"projects": []},
                "unfollowed": []}
_DM_CHANNEL = re.compile(r"^D[A-Z0-9]{2,}$")


def load_watch(ws: Path) -> dict:
    """`watch.json` with every field present and well-typed (a missing or damaged file is an empty list)."""
    doc = load_json(ws, "watch.json", {})
    doc = doc if isinstance(doc, dict) else {}
    out = json.loads(json.dumps(_EMPTY_WATCH))
    for part in ("follow", "mute"):
        got = doc.get(part) if isinstance(doc.get(part), dict) else {}
        for k in out[part]:
            if part == "mute" and k == "until":  # {<muted value>: <ISO UTC>}, filled in below once the lists are read
                continue
            if part == "follow" and k == "dms":  # {<user id>: <DM channel id>}; a malformed pair is dropped
                d = got.get(k) if isinstance(got.get(k), dict) else {}
                out[part][k] = {u: c for u, c in d.items() if isinstance(u, str) and _CHAT_USER.match(u)
                                and isinstance(c, str) and _DM_CHANNEL.match(c)}
                continue
            if part == "follow" and k == "inbox":  # the manager's self-DM channel id, or ""
                v = got.get(k)
                out[part][k] = v if isinstance(v, str) and _DM_CHANNEL.match(v) else ""
                continue
            out[part][k] = list(dict.fromkeys(x for x in (got.get(k) or []) if isinstance(x, str) and x)) \
                if isinstance(got.get(k), list) else []
    got = (doc.get("mute") or {}).get("until") if isinstance(doc.get("mute"), dict) else None
    out["mute"]["topics"] = [t for t in out["mute"]["topics"] if _TOPIC.match(t)]
    listed = {x for k in MUTE_LISTS for x in out["mute"][k]}
    out["mute"]["until"] = {v: t for v, t in got.items() if v in listed and isinstance(t, str) and _parse_at(t)} \
        if isinstance(got, dict) else {}
    auto = doc.get("auto") if isinstance(doc.get("auto"), dict) else {}
    out["auto"]["projects"] = [a for a in (auto.get("projects") or []) if isinstance(a, dict)
                               and isinstance(a.get("key"), str) and a["key"]] \
        if isinstance(auto.get("projects"), list) else []
    out["unfollowed"] = [x for x in doc.get("unfollowed") or [] if isinstance(x, str) and x] \
        if isinstance(doc.get("unfollowed"), list) else []
    return out


def save_watch(ws: Path, doc: dict) -> None:
    """Atomic write of `watch.json`; the caller holds `locked(ws)`."""
    save_json(ws, "watch.json", doc)


def merged_watch(cfg: dict, watch: dict) -> dict:
    """`hub.watch` from config plus the manager's watch list, in the shape adapters read (`chat.channels`,
    `chat.follow` = thread keys, `tracker.projects`, `code.repos`), plus a `mute` block."""
    w = _hub_cfg(cfg).get("watch")
    out = json.loads(json.dumps(w)) if isinstance(w, dict) else {}
    for cap in ("chat", "tracker", "code", "whatsapp"):
        if not isinstance(out.get(cap), dict):
            out[cap] = {}
    f = watch.get("follow") or {}

    def join(sec, field, extra):
        base = out[sec].get(field)
        out[sec][field] = list(dict.fromkeys([x for x in (base if isinstance(base, list) else []) if x] + list(extra)))

    join("chat", "channels", f.get("channels") or [])
    join("chat", "follow", f.get("threads") or [])
    autos = [a["key"] for a in (watch.get("auto") or {}).get("projects") or []]
    join("tracker", "projects", list(f.get("projects") or []) + autos)
    sure = set(f.get("projects") or []) | set((_hub_cfg(cfg).get("watch") or {}).get("tracker", {}).get("projects") or [])
    out["tracker"]["auto"] = [k for k in autos if k not in sure]  # only these are dropped on repeated failure
    join("code", "repos", f.get("repos") or [])
    join("whatsapp", "groups", f.get("whatsapp") or [])
    # only watch.json holds the DM map and the inbox; a DM is read only for a person still in config
    inbox = f.get("inbox") or ""
    ok = dm_people(cfg)
    out["chat"]["dms"] = {u: d for u, d in (f.get("dms") or {}).items() if u in ok and d != inbox}
    out["chat"]["inbox"] = inbox
    out["mute"] = {k: list((watch.get("mute") or {}).get(k) or []) for k in MUTE_LISTS}
    out["mute"]["until"] = dict((watch.get("mute") or {}).get("until") or {})
    return out


MUTE_LISTS = ("keys", "people", "channels", "topics")
# A "similar messages" rule: 1-5 words (any of them, whole words, any case) in the title or summary, optionally
# only in one chat channel. Stored as one string so `until`, `unmute` and the Ignored list treat it like any value.
_TOPIC = re.compile(r"^topic:([a-z0-9][\w-]{2,29}(?:\|[a-z0-9][\w-]{2,29}){0,4})(?:@([CG][A-Z0-9]{6,}))?$")


def topic_value(words, channel: str = "") -> Optional[str]:
    """`topic:<w>|<w>[@<channel>]` from a word list, or None when a word or the channel is not acceptable."""
    ws = list(dict.fromkeys(w.strip().lower() for w in words if w.strip()))
    v = "topic:" + "|".join(ws) + ("@" + channel if channel else "")
    return v if ws and _TOPIC.match(v) else None


def _topic_hit(value: str, text: str, channels) -> bool:
    m = _TOPIC.match(value)
    if not m or (m.group(2) and m.group(2) not in channels):
        return False
    return any(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(w), text or "", re.I) for w in m.group(1).split("|"))


def live_mute(mute: dict, at: datetime) -> dict:
    """The mutes still in force at `at`: a value muted `--for` a while drops out once its `until` has passed."""
    until = mute.get("until") or {}
    gone = {v for v, t in until.items() if (_parse_at(t) or at) <= at}
    out = {k: [x for x in mute.get(k) or [] if x not in gone] for k in MUTE_LISTS}
    out["until"] = {v: t for v, t in until.items() if v not in gone}
    return out


def item_keys(item: dict) -> set:
    """Every key a Now item names: its link, `also` and `links` urls as they are, plus the thing each url is about
    (a chat link's thread key, a tracker or PR key)."""
    urls = [item.get("link")] + list(item.get("also") or []) + [l.get("url") for l in item.get("links") or []
                                                                 if isinstance(l, dict)]
    keys = {u for u in urls if isinstance(u, str) and u}
    for u in list(keys):
        if _URL.match(u):
            k = manual_key(u)
            if not k.startswith("manual:"):
                keys.add(k)
    return keys


def item_muted(item: dict, mute: dict) -> bool:
    """A Now item the manager ignored: any of its keys is muted, its sender (`from`) is a muted person, a chat
    thread it names is in a muted channel, or its title matches a topic rule."""
    keys = item_keys(item)
    if keys & set(mute.get("keys") or []):
        return True
    sender = str(item.get("from") or "").strip().lower()
    if sender and sender in [p.lower() for p in mute.get("people") or []]:
        return True
    chans = {k.split("/", 1)[0] for k in keys if _THREAD.match(k)}
    if chans & set(mute.get("channels") or []):
        return True
    return any(_topic_hit(t, item.get("title") or "", chans) for t in mute.get("topics") or [])


def effective_cfg(cfg: dict, watch: dict) -> dict:
    """A copy of `cfg` whose `hub.watch` is the merged list: what one polling pass hands to the adapters."""
    eff = dict(cfg or {})
    h = dict(_hub_cfg(cfg))
    h["watch"] = merged_watch(cfg, watch)
    eff["hub"] = h
    return eff


def is_muted(line: dict, mute: dict) -> bool:
    """A watcher line the manager muted: its key or an `also` key, its author (chat user id or email), or the
    channel of a chat key. A `manual` line is never muted."""
    if line.get("kind") == "manual":
        return False
    keys = set(mute.get("keys") or [])
    if line.get("key") in keys or any(a in keys for a in line.get("also") or []):
        return True
    who = (line.get("who") or "").strip().lower()
    inner = re.search(r"<([^<>]+)>\s*$", who)
    for p in mute.get("people") or []:
        p = p.lower()
        if who and (who == p or (inner and inner.group(1).strip() == p)):
            return True
    if line.get("source") == "whatsapp":  # a chat JID is a key, a person or (as `mute <jid>` stores it) `wa:<jid>`
        jid = str(line.get("key"))[3:]
        if jid in keys or jid.lower() in [p.lower() for p in mute.get("people") or []]:
            return True
    chan = line["key"].split("/", 1)[0] if line.get("source") == "chat" and "/" in str(line.get("key")) else ""
    if chan and chan in (mute.get("channels") or []):
        return True
    text = "%s %s" % (line.get("title") or "", line.get("summary") or "")
    return any(_topic_hit(t, text, {chan} if chan else set()) for t in mute.get("topics") or [])


def _watch_cfg(cfg: dict, h: dict, cap: str) -> dict:
    """`hub.watch.<cap>`; for the tracker, `projects` is that list plus every team's tracker project, de-duplicated."""
    w = (h.get("watch") or {}).get(cap)
    w = dict(w) if isinstance(w, dict) else {}
    if cap == "tracker":
        keys = list(w.get("projects") or [])
        for t in (cfg or {}).get("teams") or []:
            p = ((t or {}).get("tracker") or {}).get("project") if isinstance(t, dict) else None
            if p:
                keys.append(p)
        w["projects"] = list(dict.fromkeys(k for k in keys if isinstance(k, str) and k))
    if cap == "whatsapp":  # the adapter has no client config of its own: it needs the `whatsapp` block
        wa = (cfg or {}).get("whatsapp")
        wa = wa if isinstance(wa, dict) else {}
        w["_local"] = {"enabled": wa.get("enabled") is True, "read": wa.get("read", True) is True,
                       "self_jid": wa.get("self_jid") if isinstance(wa.get("self_jid"), str) else "",
                       "home": home_status(cfg) == 0}
    return w


def poll_once(ws: Path, cfg: dict, at: datetime, clients: dict, poll: bool = True) -> dict:
    """One pass over the sources; returns the heartbeat it wrote to watcher.json.

    For each capability in `connectors` that has an adapter: poll, coalesce, append to the inbox, and only then
    save the cursor (a crash between the two repeats lines rather than losing them). A failing source keeps its
    cursor; on the first failing pass one `error` line is appended (the health records are not touched). `clients` maps capability -> ReadOnlyClient (or the exception that building it raised).
    With `poll=False` only the heartbeat is refreshed (idle hours with no polling)."""
    import hub_sources
    from hub_mcp import ForbiddenTool
    h = _hub_cfg(cfg)
    stamp = _iso(at)
    old = load_json(ws, "watcher.json", {})
    old_src = old.get("sources") if isinstance(old, dict) and isinstance(old.get("sources"), dict) else {}
    active, every = interval(cfg, at)
    sources = {}  # type: Dict[str, dict]
    errors = []
    woke = False  # a line the hub must decide (anything but `resolved`) was appended this pass
    if not poll:
        sources = dict(old_src)
        errors = [str(e) for e in (old.get("errors") or [])] if isinstance(old, dict) else []
    connectors = (cfg or {}).get("connectors") or {}
    me = build_me(cfg)
    eff = effective_cfg(cfg, load_watch(ws))
    h = _hub_cfg(eff)
    mute = live_mute(h["watch"].get("mute") or {}, at)
    passed = []  # every line of this pass, for auto-follow
    known = None  # the tracker's real project keys, when its adapter could read them this pass
    for cap in CAPS if poll else ():
        connector = connector_of(cfg, cap)
        if not connector:
            continue
        conns = h.get("connections") if isinstance(h.get("connections"), dict) else {}
        adapter = hub_sources.ADAPTERS.get("%s:%s" % (cap, connector))
        client = clients.get(cap)
        prev = old_src.get(cap) or {}
        if not isinstance(prev, dict):
            prev = {}
        since = prev.get("since", stamp) if prev.get("status") == "couldnt_check" else stamp
        if adapter is None or cap not in conns or client is None or isinstance(client, Exception):
            if adapter is None:
                reason = "no adapter for %s" % connector
            elif cap not in conns or client is None:
                reason = "no connection"
            else:
                reason = str(getattr(client, "message", None) or client)[:200]
            ent = {"status": "couldnt_check", "reason": reason, "since": since}
            if isinstance(client, Exception) and cap in conns:  # the server would not start: a source failure
                woke = _failed(ws, cap, stamp, ent) or woke
            sources[cap] = ent
            errors.append("%s: %s" % (cap, reason))
            continue
        cursors = load_json(ws, "cursors.json", {})
        cursors = cursors if isinstance(cursors, dict) else {}
        wcfg = _watch_cfg(cfg, h, cap)
        gap = getattr(adapter, "gap", lambda c: None)(wcfg)
        if gap:  # a config gap, not a failure: nothing to alert on
            sources[cap] = {"status": "couldnt_check", "reason": gap, "since": since}
            errors.append("%s: %s" % (cap, gap))
            continue
        try:
            res = adapter.poll(client, cursors.get(cap), wcfg, me, at)
        except ForbiddenTool:
            raise  # a programming error: stop the watcher rather than swallow it
        except Exception as e:
            ent = {"status": "couldnt_check", "reason": str(getattr(e, "message", None) or e)[:200], "since": since}
            woke = _failed(ws, cap, stamp, ent) or woke
            sources[cap] = ent
            errors.append("%s: %s" % (cap, ent["reason"]))
            continue
        res["lines"] = [l for l in res["lines"] if not is_muted(l, mute)]
        append_inbox(ws, res["lines"])
        woke = _must_decide(res["lines"]) or woke
        passed.extend(res["lines"])
        if cap == "tracker" and isinstance(res.get("known_projects"), list):
            known = res["known_projects"]
        if res.get("drop_auto"):
            drop_auto(ws, res["drop_auto"])
        with locked(ws):
            cursors = load_json(ws, "cursors.json", {})
            cursors = cursors if isinstance(cursors, dict) else {}
            cursors[cap] = res["cursor"]
            save_json(ws, "cursors.json", cursors)
        if res.get("partial"):
            sources[cap] = {"status": "partial", "reason": "; ".join(res["partial"])[:300],
                            "since": prev.get("since", stamp) if prev.get("status") == "partial" else stamp}
            errors.append("%s: %s" % (cap, sources[cap]["reason"]))
        else:
            sources[cap] = {"status": "ok"}
        notes = [str(n)[:200] for n in res.get("notes") or []][:20]
        if notes:  # expected limits (a busy channel), not failures: the status is unchanged
            sources[cap]["notes"] = notes
        _set_errored(ws, cap, False)
    if poll and passed and isinstance(connectors.get("tracker"), str) and connectors.get("tracker"):
        auto_follow(ws, passed, at, _watch_cfg(cfg, h, "tracker").get("projects") or [], known)
    if poll:
        woke = _maybe_sweep(ws, cfg, at, stamp) or woke
    beat = {"at": stamp, "every_seconds": every, "active": active, "sources": sources, "errors": errors}
    last = stamp if woke else (old.get("last_append_at") if isinstance(old, dict) else None)
    if last:
        beat["last_append_at"] = last
    with locked(ws):
        save_json(ws, "watcher.json", beat)
    return beat


AUTO_MAX = 20
_PROJECT_OF = re.compile(r"^([A-Z][A-Z0-9]+)-\d+$")


def auto_follow(ws: Path, lines: List[dict], at: datetime, watched=(), real=None) -> List[str]:
    """Tracker projects that came up in a pass's lines (`key` and `also`), are real projects (in `real`, the
    tracker's project keys; None, when they could not be read, follows nothing) and are not watched, muted or
    unfollowed join `watch.json` `auto.projects` (newest last; past 20 the oldest go). Returns the keys added. Runs
    after the lines are appended and takes `hub.lock` itself."""
    if real is None:
        return []
    real = set(real)
    found = {}  # type: Dict[str, str]
    for l in lines:
        if l.get("kind") == "manual" or l.get("source") == "whatsapp":
            continue  # a person's WhatsApp text never makes the watcher follow anything
        for k in [l.get("key")] + list(l.get("also") or []):
            m = _PROJECT_OF.match(k) if isinstance(k, str) else None
            if m and m.group(1) in real:  # `UTF-8`, `ISO-8601` look like keys but name no project
                found.setdefault(m.group(1), k)
    if not found:
        return []
    added = []
    with locked(ws):
        w = load_watch(ws)
        known = set(watched) | set(w["follow"]["projects"]) | {a["key"] for a in w["auto"]["projects"]}
        skip = known | set(w["mute"]["keys"]) | set(w["unfollowed"])
        for proj, src in found.items():
            if proj not in skip:
                w["auto"]["projects"].append({"key": proj, "from": src, "at": _iso(at)})
                added.append(proj)
        if added:
            w["auto"]["projects"] = w["auto"]["projects"][-AUTO_MAX:]
            save_watch(ws, w)
    return added


def drop_auto(ws: Path, projects) -> None:
    """Stop following auto projects (they failed repeatedly) and remember it so they are not re-followed."""
    with locked(ws):
        w = load_watch(ws)
        keep = [a for a in w["auto"]["projects"] if a["key"] not in projects]
        if len(keep) != len(w["auto"]["projects"]):
            w["auto"]["projects"] = keep
            w["unfollowed"] = list(dict.fromkeys(w["unfollowed"] + list(projects)))
            save_watch(ws, w)


def _failed(ws, cap, stamp, ent) -> bool:
    """The first failing pass of a source since it last worked: one `error` inbox line. `cursors.json` `_errored`
    remembers that it was emitted (not the status, so a source that was `couldnt_check` for a missing connection
    still gets its line when it later fails). Nothing is written to capabilities.json."""
    cursors = load_json(ws, "cursors.json", {})
    cursors = cursors if isinstance(cursors, dict) else {}
    if cap in (cursors.get("_errored") or []):
        return False
    append_inbox(ws, [make_line(cap, "error", "watcher:%s" % cap, stamp, title="%s source unreachable" % cap,
                                summary=ent["reason"])])
    _set_errored(ws, cap, True)
    return True


def _set_errored(ws, cap, on: bool) -> None:
    with locked(ws):
        cursors = load_json(ws, "cursors.json", {})
        cursors = cursors if isinstance(cursors, dict) else {}
        cur = [c for c in (cursors.get("_errored") or []) if c != cap]
        if on:
            cur.append(cap)
        if cur != list(cursors.get("_errored") or []):
            cursors["_errored"] = cur
            save_json(ws, "cursors.json", cursors)


def _maybe_sweep(ws: Path, cfg: dict, at: datetime, stamp: str) -> bool:
    """Append one `sweep` line when sweep_minutes have passed since the last one and something is pending.
    True when it appended one."""
    h = _hub_cfg(cfg)
    minutes = int(h.get("sweep_minutes", DEFAULTS["sweep_minutes"]))
    cursors = load_json(ws, "cursors.json", {})
    last = cursors.get("_last_sweep") if isinstance(cursors, dict) else None
    if last:
        import hub_sources
        try:
            if at - hub_sources.parse_iso(last) < timedelta(minutes=minutes):
                return False
        except (ValueError, TypeError):
            pass
    if not pending_work(ws, cfg, at):
        return False
    append_inbox(ws, [make_line("hub", "sweep", "sweep", stamp, title="sweep",
                                summary="pending proposals or unseen session updates")])
    with locked(ws):
        cursors = load_json(ws, "cursors.json", {})
        cursors = cursors if isinstance(cursors, dict) else {}
        cursors["_last_sweep"] = stamp
        save_json(ws, "cursors.json", cursors)
    return True


def build_clients(cfg: dict, have: dict) -> dict:
    """Clients for every `hub.connections` entry that has no live one yet. A client that cannot start is stored as
    the exception, so poll_once reports that source as couldnt_check and the next pass retries."""
    import hub_mcp
    import hub_sources
    from hub_mcp import McpError
    out = dict(have)
    conns = _hub_cfg(cfg).get("connections") or {}
    for cap in CAPS:
        conn = conns.get(cap)
        if not isinstance(conn, dict) or (cap in out and not isinstance(out[cap], Exception)):
            continue
        adapter = hub_sources.ADAPTERS.get("%s:%s" % (cap, connector_of(cfg, cap)))
        if adapter is None:
            continue
        try:
            if conn.get("local"):  # the local WhatsApp database: a reader, no server and no tools
                if conn["local"] != "whatsapp":
                    raise McpError("other", "unsupported local source: %s" % conn["local"])
                out[cap] = hub_sources.LocalWhatsApp()
                continue
            if conn.get("cli"):  # cli wins over command
                if conn["cli"] != "gh":
                    raise McpError("other", "unsupported cli: %s" % conn["cli"])
                raw = hub_sources.GhClient()
            else:
                raw = hub_mcp.Client(conn.get("command") or "", list(conn.get("args") or []),
                                     dict(conn.get("env") or {}))
                raw.start()
            out[cap] = hub_mcp.ReadOnlyClient(raw, hub_mcp.resolve_tools(raw.tools, adapter.READ_TOOLS),
                                              adapter.READ_TOOLS)
        except McpError as e:
            out[cap] = e
        except Exception as e:
            out[cap] = McpError("other", str(e))
    return out


def home_status(cfg: dict) -> int:
    """0 on the home machine; 2 on another machine; 3 when no home machine is set (as `mother home check`)."""
    home = ((cfg or {}).get("workspace") or {}).get("home_machine")
    if not home:
        return 3
    here = (os.environ.get("MOTHER_HOSTNAME") or socket.gethostname()).split(".")[0].lower()
    return 0 if home == here else 2


LOG_MAX = 1024 * 1024


def _log(msg: str) -> None:
    print("%s mother hub: %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), file=sys.stderr)
    try:
        sys.stderr.flush()
    except Exception:
        pass


def cap_log(ws: Path) -> None:
    """Keep watcher.log under LOG_MAX: copy it to watcher.log.1 (replacing it) and empty it in place, so launchd's
    open handle keeps writing to watcher.log."""
    import shutil
    p = hub_dir(ws) / "watcher.log"
    try:
        if p.stat().st_size > LOG_MAX:
            shutil.copyfile(str(p), str(p) + ".1")
            os.truncate(str(p), 0)
    except OSError:
        pass


def config_problem(cfg: dict) -> Optional[str]:
    """Why the watcher cannot run on this config (one line), or None. Checked once at start."""
    h = _hub_cfg(cfg)
    if h.get("enabled") is not True:
        return "hub.enabled is not true in config.json"
    for k, low in (("every_seconds", 1), ("idle_every_seconds", 0), ("sweep_minutes", 0), ("update_ttl_hours", 0)):
        if k not in h:
            continue
        v = h[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < low:
            return "hub.%s must be a number >= %d, not %r" % (k, low, v)
    try:
        import now as nowmod
        nowmod.Schedule.from_config(cfg)
        is_active(cfg, datetime.now().astimezone())
    except Exception as e:
        return "active hours or timezone in config.json: %s" % e
    return None


def watch(ws: Path, cfg: dict, clock=None, sleep=None, factory=None, stop=None) -> None:
    """Run the watcher until SIGTERM. Returns (so the process exits 0 and launchd, `KeepAlive.SuccessfulExit` false,
    does not respawn it) with one log line when this is not the home machine, `hub.enabled` is not true, or a config
    number is bad. Polls every `every_seconds` in active hours, `idle_every_seconds` outside
    (0: not at all, sleeping until the next window); rotates the inbox when the local day changes.
    `clock() -> aware datetime`, `sleep(seconds)`, `factory(cfg, have) -> clients` and `stop` (a threading.Event)
    are injectable for tests."""
    import threading
    import now as nowmod
    if home_status(cfg):
        _log("not the home machine (see `mother home check`); not watching")
        return
    why = config_problem(cfg)
    if why:
        _log("%s; not watching" % why)
        return
    tz = nowmod.Schedule.from_config(cfg).tz
    clock = clock or (lambda: datetime.now(tz))
    sleep = sleep or time.sleep
    factory = factory or build_clients
    stop = stop or threading.Event()
    try:
        old = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except ValueError:  # not the main thread: the caller stops us through `stop`
        old = None
    clients = {}  # type: dict
    prev_at = clock()
    try:
        while not stop.is_set():
            at = clock()
            if prev_at.astimezone(tz).date() != at.astimezone(tz).date():
                try:
                    rotate_inbox(ws, prev_at.astimezone(tz))
                except OSError as e:  # the next day change tries again; never crash-loop on it
                    _log("inbox rotation failed: %s" % e)
            cap_log(ws)
            active, every = interval(cfg, at)
            idle0 = not active and int(_hub_cfg(cfg).get("idle_every_seconds", DEFAULTS["idle_every_seconds"])) == 0
            if not idle0:
                clients = factory(cfg, clients)
            beat = poll_once(ws, cfg, at, clients, poll=not idle0)
            for cap, ent in beat["sources"].items():  # a failed source gets a fresh client next pass
                c = clients.get(cap)
                if ent.get("status") == "couldnt_check" and c is not None:
                    clients.pop(cap, None)
                    if not isinstance(c, Exception):
                        _close(c)
            prev_at = at
            waited = 0
            while not stop.is_set() and waited < beat["every_seconds"]:
                step = min(1, beat["every_seconds"] - waited)
                sleep(step)
                waited += step
    finally:
        if old is not None:
            signal.signal(signal.SIGTERM, old)
        for c in clients.values():
            if not isinstance(c, Exception):
                _close(c)


def _close(c) -> None:
    try:
        c.close()
    except Exception:
        pass


LABEL = "com.mother.hub"
USAGE = """mother hub <verb>
  watch       run the watcher in the foreground (what the launchd job runs); home machine only
  install     write ~/Library/LaunchAgents/com.mother.hub.plist and load it (home machine only)
  uninstall   unload the job and remove the plist
  check       one read per hub.connections entry, under the plist's environment: cap | ok/✗ | reason; exit 1 if any fails
  status      watcher and hub heartbeats; exit 2 if either is stale or the watcher never ran
  smoke       check, then two polling passes 20 s apart into a throwaway copy of the state (never the real inbox);
              prints source | status | lines | reason, no content; exit 2 if a source cannot be read
  tail        [--once] [--follow]  inbox lines after hub.json inbox_cursor, one JSON line per key (the hub's Monitor runs --follow)
  decide      stdin: one decision object or a list {line_id, key, decision: log|now|route|propose|error, ...}
  pending     raised <key> --name <slug> [--task <task_id>] | drop <key>
  session     add <key> --id ID --name N --title T [--link URL] [--also K,K] | drop <key> | seen <key> | for <key>
  done        <U-id>  mark an update done
  snooze      <U-id> 1h|tomorrow|next-week
  show        [--json]  pending proposals, updates and sessions
  add         <link-or-text> [--key K]  hand the hub a thing by hand (a `manual` line it always routes or proposes)
  follow      [--project|--channel|--thread|--repo|--whatsapp] <PROJECT | channel id | channel/ts | owner/repo | WhatsApp JID>  watch it too (`unfollow` stops)
  mute        [--for Nd|Nw] [--scope item|channel] <key | link | U-id | email | channel id | WhatsApp JID>
              drop its watcher lines and its Now items (`unmute` undoes); --scope channel mutes a thread's channel
              mute --words w1,w2 [--channel C]: a rule for similar messages (any word, whole word, any case)
  watching    [--json]  the merged watch list (config plus follows) and the mutes
  dms         set <user id>=<DM id> [...] | drop <user id> | inbox <self-DM id>|none | show [--json]
              DMs with people in config the watcher reads, and the self-DM it reads as your inbox"""


def default_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def _conns(cfg: dict) -> dict:
    c = _hub_cfg(cfg).get("connections")
    return {k: v for k, v in (c if isinstance(c, dict) else {}).items() if k in CAPS and isinstance(v, dict)}


def _program(conn: dict) -> str:
    """The executable a connection needs: `gh` when `cli` is set (it wins over `command`), else `command`."""
    return conn.get("cli") or conn.get("command") or ""


def resolve_programs(cfg: dict, which=None) -> Tuple[Dict[str, str], List[str]]:
    """({cap: absolute path}, [names that were not found])."""
    import shutil
    which = which or shutil.which
    found, missing = {}, []
    for cap, conn in _conns(cfg).items():
        if conn.get("local"):
            continue  # nothing to run
        name = _program(conn)
        path = which(name) if name else None
        if path:
            found[cap] = os.path.abspath(path)
        else:
            missing.append(name or "(%s: no command)" % cap)
    return found, missing


def launch_path(found: Dict[str, str]) -> str:
    dirs = []  # type: List[str]
    for p in found.values():
        d = os.path.dirname(p)
        if d not in dirs:
            dirs.append(d)
    for d in ("/usr/bin", "/bin"):
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def plist(ws: Path, cfg: dict, python: str, mother: str, found: Optional[Dict[str, str]] = None) -> str:
    """The launchd job: absolute python and bin/mother, MOTHER_WORKSPACE and a PATH from the connection commands."""
    import plistlib
    if found is None:
        found, _ = resolve_programs(cfg)
    doc = {"Label": LABEL,
           "ProgramArguments": [python, mother, "hub", "watch"],
           "EnvironmentVariables": {"MOTHER_WORKSPACE": str(ws), "PATH": launch_path(found)},
           "KeepAlive": {"SuccessfulExit": False},  # a clean exit (off home, disabled, bad config) stays down
           "ThrottleInterval": 60,
           "RunAtLoad": True,
           "StandardOutPath": str(hub_dir(ws) / "watcher.log"),
           "StandardErrorPath": str(hub_dir(ws) / "watcher.log")}
    return plistlib.dumps(doc).decode("utf-8")


def launchctl(*args) -> Tuple[int, str]:
    import subprocess
    try:
        p = subprocess.run(["launchctl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "").strip()


def install(ws: Path, cfg: dict, plist_path=None, ctl=None, which=None, python=None, mother=None) -> int:
    ctl = ctl or launchctl
    plist_path = Path(plist_path or default_plist_path())
    code = home_status(cfg)
    if code:
        print("mother hub install: not the home machine (see `mother home check`)", file=sys.stderr)
        return code
    if _hub_cfg(cfg).get("enabled") is not True:
        print("mother hub install: hub.enabled is not true in config.json; set it first", file=sys.stderr)
        return 1
    if not _conns(cfg):
        print("mother hub install: hub.connections is empty; nothing to watch", file=sys.stderr)
        return 1
    found, missing = resolve_programs(cfg, which)
    if missing:
        print("mother hub install: not found on PATH: %s" % ", ".join(missing), file=sys.stderr)
        return 1
    python = python or os.path.abspath(sys.executable)
    mother = mother or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "mother"))
    hub_dir(ws).mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist(ws, cfg, python, mother, found))
    domain = "gui/%d" % os.getuid()
    ctl("bootout", "%s/%s" % (domain, LABEL))  # a reinstall replaces the loaded job; not loaded is fine
    rc, out = ctl("bootstrap", domain, str(plist_path))
    if rc:
        print("mother hub install: launchctl bootstrap failed: %s" % out, file=sys.stderr)
        return 1
    print("installed %s (logs: %s)" % (plist_path, hub_dir(ws) / "watcher.log"))
    return 0


def uninstall(plist_path=None, ctl=None) -> int:
    ctl = ctl or launchctl
    plist_path = Path(plist_path or default_plist_path())
    ctl("bootout", "gui/%d/%s" % (os.getuid(), LABEL))
    existed = plist_path.exists()
    if existed:
        plist_path.unlink()
    print("removed %s" % plist_path if existed else "not installed")
    return 0


def _gh_user() -> None:
    import subprocess
    try:
        p = subprocess.run(["gh", "api", "user"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(str(e))
    if p.returncode:
        raise RuntimeError((p.stderr or "gh api user failed").strip()[:200])


def check(cfg: dict, factory=None, gh_user=None, which=None, out=None) -> int:
    """One read per connection, under the PATH the plist sets: an MCP server must start and answer `tools/list`
    with every tool on its adapter's read allow-list (each exactly once); `gh` must answer `gh api user`. Never calls a write
    tool. Prints `cap | ok/✗ | reason`; exit 1 if any fails."""
    import hub_mcp
    import hub_sources
    out = out or sys.stdout
    factory = factory or build_clients
    gh_user = gh_user or _gh_user
    conns = _conns(cfg)
    if not conns:
        print("mother hub check: hub.connections is empty", file=sys.stderr)
        return 1
    found, _ = resolve_programs(cfg, which)
    saved = os.environ.get("PATH")
    os.environ["PATH"] = launch_path(found)
    rows = []
    try:
        for cap in CAPS:
            conn = conns.get(cap)
            if conn is None:
                continue
            adapter = hub_sources.ADAPTERS.get("%s:%s" % (cap, connector_of(cfg, cap)))
            if adapter is None:
                rows.append((cap, False, "no adapter for connectors.%s" % cap))
                continue
            if conn.get("local"):  # the WhatsApp database: readable, and switched on in config
                why = adapter.gap(_watch_cfg(cfg, _hub_cfg(cfg), cap))
                if why:
                    rows.append((cap, False, why))
                    continue
                got = factory({"hub": {"connections": {cap: conn}}, "connectors": (cfg or {}).get("connectors")}, {})
                c = got.get(cap)
                try:
                    if c is None or isinstance(c, Exception):
                        raise RuntimeError(str(c) if c is not None else "no client")
                    c.ping()
                    rows.append((cap, True, "local database readable"))
                except Exception as e:
                    rows.append((cap, False, hub_sources.wa_problem(e.reason) if hasattr(e, "reason") else str(e)))
                continue
            if conn.get("cli"):
                try:
                    if conn["cli"] != "gh":
                        raise RuntimeError("unsupported cli: %s" % conn["cli"])
                    gh_user()
                    rows.append((cap, True, "gh api user"))
                except Exception as e:
                    rows.append((cap, False, str(e)))
                continue
            got = factory({"hub": {"connections": {cap: conn}}, "connectors": (cfg or {}).get("connectors")}, {})
            c = got.get(cap)
            try:
                if c is None or isinstance(c, Exception):
                    rows.append((cap, False, str(c) if c is not None else "no client"))
                elif _missing_reads(c.tools, adapter.READ_TOOLS):
                    rows.append((cap, False, "missing read tools: %s" % ", ".join(
                        _missing_reads(c.tools, adapter.READ_TOOLS))))
                else:
                    rows.append((cap, True, "tools/list: %d tools" % len(c.tools)))
            finally:
                if c is not None and not isinstance(c, Exception):
                    _close(c)
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved
    for cap, ok, why in rows:
        print("%s | %s | %s" % (cap, "ok" if ok else "\u2717", why), file=out)
    return 0 if all(r[1] for r in rows) else 1


def smoke(cfg: dict, at: datetime, out=None, factory=None, sleep=time.sleep) -> int:
    """A read-only live check of the whole watcher path: `check`, then two `poll_once` passes 20 seconds apart into
    a throwaway workspace (only `config.json` is copied; the real inbox, cursors and watch list are never read or
    written). Prints `cap | status | lines | reason` per enabled source and never a line's content. Exit 0 when
    every source is ok or partial, 2 when any is not (or the check failed or there is nothing to poll)."""
    import tempfile
    out = out or sys.stdout
    factory = factory or build_clients
    if check(cfg, factory=factory, out=out) != 0:
        print("smoke: check failed; no polling pass was made", file=out)
        return 2
    found, _ = resolve_programs(cfg)
    saved = os.environ.get("PATH")
    os.environ["PATH"] = launch_path(found)
    clients = {}  # type: dict
    try:
        with tempfile.TemporaryDirectory(prefix="mother-hub-smoke-") as tmp:
            ws = Path(tmp)
            (ws / "config.json").write_text(json.dumps(cfg))
            clients = factory(cfg, {})
            poll_once(ws, cfg, at, clients)
            sleep(20)
            clients = factory(cfg, clients)  # a server that died is started again, as the watcher does
            beat = poll_once(ws, cfg, at + timedelta(seconds=20), clients)
            counts = {}  # type: Dict[str, int]
            for l in read_inbox(ws):
                if l.get("kind") != "error":
                    counts[l.get("source")] = counts.get(l.get("source"), 0) + 1
    finally:
        for c in clients.values():
            if c is not None and not isinstance(c, Exception):
                _close(c)
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved
    sources = beat.get("sources") or {}
    for cap in CAPS:
        ent = sources.get(cap)
        if ent is None:
            continue
        reason = " ".join(str(ent.get("reason") or "").split())[:120]
        print("%s | %s | %d | %s" % (cap, ent.get("status"), counts.get(cap, 0), reason or "-"), file=out)
    ok = bool(sources) and all((e or {}).get("status") in ("ok", "partial") for e in sources.values())
    return 0 if ok else 2


def _missing_reads(tools, suffixes) -> List[str]:
    """READ_TOOLS suffixes the server does not offer exactly once."""
    return sorted(s for s in suffixes if len([t for t in tools or [] if t.rsplit("__", 1)[-1] == s]) != 1)


def _age(stamp, at: datetime):
    import hub_sources
    try:
        return (at - hub_sources.parse_iso(stamp)).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return None


def status(ws: Path, at: datetime, out=None, cfg=None) -> int:
    """Heartbeats of the watcher and the hub. Exit 2 only when the watcher never ran or is stale (`at` older than
    2x `every_seconds` while `active`). The hub is printed STALE (exit unchanged) when `hub_lagging` while active."""
    out = out or sys.stdout
    w = load_json(ws, "watcher.json", {})
    h = load_json(ws, "hub.json", {})
    w = w if isinstance(w, dict) else {}
    h = h if isinstance(h, dict) else {}
    if not w.get("at"):
        print("watcher: never ran (no watcher.json)", file=out)
        print("hub: never woke" if not h.get("last_wake") else "hub: last wake %s" % h["last_wake"], file=out)
        return 2
    try:
        every = int(w.get("every_seconds") or DEFAULTS["every_seconds"])
    except (TypeError, ValueError):
        every = DEFAULTS["every_seconds"]
    active = bool(w.get("active"))
    stale = False
    age = _age(w.get("at"), at)
    wstale = active and (age is None or age > 2 * every)
    stale = stale or wstale
    print("watcher: last poll %s (%s), every %ds, %s%s" % (
        w["at"], "unreadable time" if age is None else "%ds ago" % age, every,
        "active hours" if active else "outside active hours", ", STALE" if wstale else ""), file=out)
    if cfg is not None:  # judged now, from config: the heartbeat's `active` can be a pass old
        try:
            now_active = is_active(cfg, at)
            nxt = None if now_active else next_active_start(cfg, at)
        except Exception:
            now_active, nxt = None, None
        if now_active is not None:
            print("now: %s" % ("active hours" if now_active else "outside active hours"), file=out)
        if nxt is not None:  # what the hub skill schedules its next wake for (a one-shot `/mother:hub`)
            loc = nxt.astimezone()  # cron runs on this machine's clock
            print("next active window: %s (one-shot cron: %d %d %d %d *)"
                  % (nxt.isoformat(), loc.minute, loc.hour, loc.day, loc.month), file=out)
    for cap, ent in sorted((w.get("sources") or {}).items()):
        why = ent.get("reason")
        print("  %s: %s%s" % (cap, ent.get("status"), " (%s)" % why if why else ""), file=out)
        for n in ent.get("notes") or []:
            print("    note: %s" % n, file=out)
    lagging = hub_lagging(w, h, at)
    if h.get("last_wake"):
        hage = _age(h["last_wake"], at)
        hstale = active and lagging
        print("hub: last wake %s (%s)%s" % (h["last_wake"], "unreadable time" if hage is None else "%ds ago" % hage,
                                           ", STALE" if hstale else ""), file=out)
    else:
        print("hub: never woke%s" % (", STALE" if active and lagging else ""), file=out)
    return 2 if stale else 0


def hub_lagging(w: dict, h: dict, at: datetime) -> bool:
    """The hub has not caught up: the watcher appended a line it must decide (`last_append_at`) after the hub last
    woke (or it never woke), more than 2x `every_seconds` ago. A quiet day's old `last_wake` is not lagging."""
    import hub_sources
    try:
        appended = hub_sources.parse_iso(w.get("last_append_at"))
        every = int(w.get("every_seconds") or DEFAULTS["every_seconds"])
    except (TypeError, ValueError, AttributeError):
        return False
    if h.get("last_wake"):
        try:
            if hub_sources.parse_iso(h["last_wake"]) >= appended:
                return False
        except (TypeError, ValueError, AttributeError):
            pass
    return (at - appended).total_seconds() > 2 * every


# ------------------------------------------------------------------------------------------- hub verbs
ROUTED_MAX = 50
ALSO_MAX = 50
DECISIONS = ("log", "now", "route", "propose", "error")


def _inbox_files(ws: Path) -> List[Path]:
    d = hub_dir(ws)
    files = sorted(d.glob("inbox-*.jsonl"))
    cur = d / "inbox.jsonl"
    if cur.exists():
        files.append(cur)
    return files


def read_inbox(ws: Path) -> List[dict]:
    """Every readable line of the rotated files then inbox.jsonl, oldest first. A torn or odd line is skipped."""
    out = []
    for f in _inbox_files(ws):
        try:
            text = f.read_text()
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                l = json.loads(raw)
            except ValueError:
                continue
            if isinstance(l, dict) and isinstance(l.get("id"), str) and isinstance(l.get("key"), str):
                out.append(l)
    return out


def after_cursor(lines: List[dict], cursor) -> Tuple[List[dict], bool]:
    """(lines after the cursor line, cursor_missing). No cursor = everything; a cursor that is no longer in the
    files (pruned) = everything kept, with cursor_missing True."""
    if not cursor:
        return lines, False
    idx = [i for i, l in enumerate(lines) if l["id"] == cursor]
    if not idx:
        return lines, True
    return lines[idx[-1] + 1:], False


def _hub_doc(ws: Path) -> dict:
    h = load_json(ws, "hub.json", {})
    h = h if isinstance(h, dict) else {}
    h.setdefault("version", 1)
    for k in ("routed", "pending", "updates"):
        if not isinstance(h.get(k), list):
            h[k] = []
    return h


def _sessions_doc(ws: Path) -> dict:
    d = load_json(ws, "sessions.json", {})
    d = d if isinstance(d, dict) else {}
    d.setdefault("version", 1)
    if not isinstance(d.get("sessions"), dict):
        d["sessions"] = {}
    return d


def find_session(sessions: dict, keys, live_only: bool = True):
    """(session key, session) for the first of `keys` that is a session key or in a session's `also`; live
    sessions only unless live_only is False. None when there is no such session."""
    items = sessions.get("sessions") or {}
    for k in keys:
        cands = [(sk, s) for sk, s in items.items() if isinstance(s, dict)
                 and (sk == k or k in (s.get("also") or []))]
        for sk, s in cands:
            if not live_only or s.get("state") == "live":
                return sk, s
    return None


def _apply_resolved(ws: Path, keys) -> None:
    keys = set(keys)
    if not keys:
        return
    with locked(ws):
        h = _hub_doc(ws)
        pend = [p for p in h["pending"] if p.get("key") not in keys]
        upd = [u for u in h["updates"] if u.get("key") not in keys]
        if len(pend) != len(h["pending"]) or len(upd) != len(h["updates"]):
            h["pending"], h["updates"] = pend, upd
            save_json(ws, "hub.json", h)


def digest(ws: Path, batch: List[dict]) -> List[dict]:
    """What tail prints for a batch: sweep, error and manual lines as they are, update lines collapsed to one per key,
    and no output for a key whose latest line is `resolved` (that resolution is applied to hub.json)."""
    pos = {}
    for i, l in enumerate(batch):
        pos[l["id"]] = i
    by_key = {}
    keep = []
    asked = set()  # keys the manager handed over by hand in this batch: no resolution applies to them
    for l in batch:
        if l.get("kind") in ("sweep", "error", "manual"):
            keep.append(l)
            if l.get("kind") == "manual":
                asked.add(l["key"])
        else:
            by_key.setdefault(l["key"], []).append(l)
    resolved = []
    for k, ls in by_key.items():
        latest = ls[0]
        for l in ls[1:]:
            if _wins(l, latest):
                latest = l
        if latest.get("kind") == "resolved":
            if k not in asked:
                resolved.append(k)
            continue
        keep.extend(coalesce([l for l in ls if l.get("kind") == "update"]))
    _apply_resolved(ws, resolved)
    keep.sort(key=lambda l: pos.get(l["id"], 0))
    return keep


def tail(ws: Path, follow: bool = False, out=None, err=None, sleep=time.sleep, stop=None,
         every: float = 1.5) -> int:
    """Print the inbox lines after hub.json `inbox_cursor`, one JSON line per collapsed item, flushed. Never moves
    the cursor (`decide` does). With `follow`, keep polling the files: a rotation needs no special handling because
    the rotated and current files are read together and lines already printed are remembered by id."""
    out = out or sys.stdout
    err = err or sys.stderr
    seen = set()
    warned = False
    sig = None
    while True:
        files = _inbox_files(ws)
        try:
            now_sig = [(f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in files]
        except OSError:
            now_sig = None
        if now_sig is None or now_sig != sig:
            sig = now_sig
            cursor = _hub_doc(ws).get("inbox_cursor")
            lines, missing = after_cursor(read_inbox(ws), cursor)
            if missing and not warned:
                warned = True
                print("mother hub tail: cursor line %s is no longer in the inbox (pruned after %d days); "
                      "starting from the oldest kept line" % (cursor, KEEP_DAYS), file=err)
                err.flush()
            batch = [l for l in lines if l["id"] not in seen]
            seen.update(l["id"] for l in batch)
            for l in digest(ws, batch):
                out.write(json.dumps(l, sort_keys=True) + "\n")
            out.flush()
        if not follow or (stop is not None and stop()):
            return 0
        sleep(every)


def _schedule(cfg: dict):
    import now as nowmod
    return nowmod.Schedule.from_config(cfg or {})


def _prune_updates(h: dict, sessions: dict, cfg: dict, at: datetime) -> None:
    """Drop updates older than hub.update_ttl_hours working hours (counted from the later of `at` and a snooze end)
    and those whose key has a live session; snoozes that have ended become open again."""
    import now as nowmod
    import hub_sources
    try:
        ttl = float(_hub_cfg(cfg).get("update_ttl_hours", 24))
    except (TypeError, ValueError):
        ttl = 24.0
    sched = _schedule(cfg)
    keep = []
    for u in h["updates"]:
        if find_session(sessions, [u.get("key")]):
            continue
        try:
            ref = hub_sources.parse_iso(u.get("at"))
            if u.get("snooze_until"):
                ref = max(ref, hub_sources.parse_iso(u["snooze_until"]))
                if u.get("state") == "snoozed" and hub_sources.parse_iso(u["snooze_until"]) <= at:
                    u["state"] = "open"
            if nowmod.working_hours(ref, at, sched) > ttl:
                continue
        except (TypeError, ValueError, AttributeError):
            pass
        keep.append(u)
    h["updates"] = keep


def _prune_pending(h: dict, cfg: dict, at: datetime) -> None:
    """Drop proposals older than hub.update_ttl_hours working hours, counted from `raised_at` (else
    `proposed_at`)."""
    h["pending"] = [p for p in h["pending"] if not (isinstance(p, dict) and _older_than_ttl(
        p.get("raised_at") or p.get("proposed_at"), cfg, at))]


def _save_hub(ws: Path, h: dict, sessions: dict, cfg: dict, at: datetime) -> None:
    _prune_updates(h, sessions, cfg, at)
    _prune_pending(h, cfg, at)
    save_json(ws, "hub.json", h)


def decide(ws: Path, cfg: dict, at: datetime, items: List[dict]) -> List[dict]:
    """Record the hub's decisions. Returns one result per item: {line_id, key, decision (as stored), session_id?,
    note?}. A `route` to a key with no live session (directly or through `also`) is stored as `propose`."""
    stamp = _iso(at)
    lines = read_inbox(ws)
    by_id = {l["id"]: l for l in lines}
    order = {}
    for i, l in enumerate(lines):
        order[l["id"]] = i
    cursor0 = _hub_doc(ws).get("inbox_cursor")
    start = order[cursor0] + 1 if cursor0 in order else 0
    unknown = [it["line_id"] for it in items if it["line_id"] not in order]
    if unknown:
        raise ValueError("line_id not in the kept inbox: %s" % ", ".join(unknown))
    wrong = [it["line_id"] for it in items if by_id[it["line_id"]].get("key") != it["key"]]
    if wrong:
        raise ValueError("line_id whose inbox line has a different key: %s" % ", ".join(wrong))
    items = sorted(items, key=lambda it: order[it["line_id"]])
    results = []
    with locked(ws):
        h = _hub_doc(ws)
        sess = _sessions_doc(ws)
        done = {r.get("line_id") for r in h["routed"] if isinstance(r, dict)}
        for it in items:
            if it["line_id"] in done:  # a second decide for the same line (a replayed backlog) changes nothing
                results.append({"line_id": it["line_id"], "key": it["key"], "already_decided": True})
                continue
            done.add(it["line_id"])
            line = by_id.get(it["line_id"]) or {}
            key, dec = it["key"], it["decision"]
            res = {"line_id": it["line_id"], "key": key, "decision": dec}
            also = list(it.get("also") or line.get("also") or [])
            entry = {"line_id": it["line_id"], "key": key, "decision": dec, "at": stamp}
            if dec in ("route", "now"):
                hit = find_session(sess, [key] + also)
                if hit and dec == "now":
                    dec = res["decision"] = entry["decision"] = "route"
                    res["note"] = "live session for %s; recorded as a route, not an update" % key
                if hit:
                    sk, s = hit
                    s["unseen"] = int(s.get("unseen") or 0) + 1
                    s["last_routed_at"] = stamp
                    # the line's own key and the keys it mentions now belong to this session too, so the next
                    # reply in the thread finds it even when it no longer names the key it was routed by
                    keep = [a for a in (s.get("also") or []) if isinstance(a, str)]
                    for k in [key] + also:
                        if k != sk and k not in keep:
                            keep.append(k)
                    s["also"] = keep[-ALSO_MAX:]
                    entry["session_id"] = res["session_id"] = s.get("session_id")
                elif dec == "route":
                    dec = res["decision"] = entry["decision"] = "propose"
                    res["note"] = "no live session for %s; stored as a proposal" % key
            title = it.get("title") or line.get("title") or key
            link = it.get("link") or line.get("link") or ""
            if dec == "propose":
                prev = next((p for p in h["pending"] if p.get("key") == key), {})
                h["pending"] = [p for p in h["pending"] if p.get("key") != key]
                keep = [a for a in (prev.get("also") or []) if isinstance(a, str)]
                for k in also:
                    if k != key and k not in keep:
                        keep.append(k)
                h["pending"].append({"key": key, "title": title, "link": link, "read": it.get("read") or "",
                                     "next_step": it.get("next_step") or "",
                                     "proposed_at": prev.get("proposed_at") or stamp,
                                     "raised_at": prev.get("raised_at"),
                                     "task_id": prev.get("task_id"),
                                     "last_update_at": stamp,
                                     "also": keep[-ALSO_MAX:],
                                     "name": it.get("name") or prev.get("name") or ""})
            elif dec == "now":
                old = [u for u in h["updates"] if u.get("key") == key and u.get("state") != "done"]
                if old:
                    u = old[0]
                    u.update({"title": title, "why": it.get("why") or u.get("why") or "", "link": link,
                              "at": stamp, "state": "open"})
                    u.pop("snooze_until", None)
                    res["update_id"] = u["id"]
                else:
                    n = int(h.get("next_update") or 0)
                    for u in h["updates"]:
                        m = re.match(r"U(\d+)$", str(u.get("id")))
                        n = max(n, int(m.group(1)) if m else 0)
                    n += 1
                    h["next_update"] = n
                    h["updates"].append({"id": "U%d" % n, "key": key, "title": title,
                                         "why": it.get("why") or "", "link": link, "at": stamp, "state": "open"})
                    res["update_id"] = "U%d" % n
            h["routed"].append(entry)
            # the printed line stands for every update line of its key that tail collapsed into it (same key,
            # after the cursor, no later `at`), wherever they sit in the inbox: move past the furthest of them
            far = it["line_id"]
            if line.get("kind") == "update":
                for l in lines[start:]:
                    if l["key"] == line["key"] and l.get("kind") == "update" and l["at"] <= line["at"] \
                            and order[l["id"]] > order[far]:
                        far = l["id"]
            cur = h.get("inbox_cursor")
            if cur not in order or order[far] > order[cur]:
                h["inbox_cursor"] = far
            results.append(res)
        if all(r.get("already_decided") for r in results):
            return results  # nothing new: no write at all
        h["routed"] = h["routed"][-ROUTED_MAX:]
        h["last_wake"] = stamp
        save_json(ws, "sessions.json", sess)
        _save_hub(ws, h, sess, cfg, at)
    return results


def _parse_flags(args, valued) -> Tuple[List[str], dict]:
    pos, opts = [], {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":  # everything after it is positional
            pos.extend(args[i + 1:])
            break
        if a.startswith("--") and a[2:] in valued:
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % a)
            opts[a[2:]] = args[i + 1]
            i += 2
        elif a.startswith("--"):
            opts[a[2:]] = True
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, opts


def session_cmd(ws: Path, cfg: dict, at: datetime, args, as_json: bool, out) -> int:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    try:
        pos, o = _parse_flags(rest, ("id", "name", "title", "link", "also"))
    except ValueError as e:
        print("mother hub session: %s" % e, file=sys.stderr)
        return 2
    if sub not in ("add", "drop", "seen", "for") or len(pos) != 1:
        print("usage: mother hub session add <key> --id ID --name N --title T [--link URL] [--also K,K] | "
              "drop <key> | seen <key> | for <key>", file=sys.stderr)
        return 2
    key = pos[0]
    stamp = _iso(at)
    catch_up = None
    if sub == "for":
        hit = find_session(_sessions_doc(ws), [key])
        if not hit:
            print("no live session for %s" % key, file=sys.stderr)
            return 1
        print(json.dumps({"key": hit[0], "session_id": hit[1].get("session_id")}) if as_json
              else hit[1].get("session_id"), file=out)
        return 0
    with locked(ws):
        sess = _sessions_doc(ws)
        h = _hub_doc(ws)
        items = sess["sessions"]
        if sub == "add":
            if not o.get("id") or not o.get("name") or not o.get("title"):
                print("mother hub session add needs --id, --name and --title", file=sys.stderr)
                return 2
            also = [a for a in str(o.get("also") or "").split(",") if a]
            prev = items.get(key) or {}
            was = next((p for p in h["pending"] if p.get("key") == key), None)
            raised = bool(was and was.get("raised_at"))
            if prev.get("state") == "live" and str(prev.get("session_id")) != str(o["id"]) and not raised:
                # a live key keeps its session: only a newly started card (pending, raised) may repoint it
                print("mother hub session add: %s is live in session %s; refusing to repoint it to %s"
                      % (key, prev.get("session_id"), o["id"]), file=sys.stderr)
                return 1
            if key not in items and not raised:
                # only a card the hub raised (or a session it already follows) becomes a hub session: otherwise a
                # line could have the hub route source text into any session by its id
                print("mother hub session add: %s has no raised card in pending and no session; raise a card first"
                      % key, file=sys.stderr)
                return 1
            if was and was.get("raised_at") and (was.get("last_update_at") or "") > was["raised_at"]:
                catch_up = {"key": key, "read": was.get("read") or "", "next_step": was.get("next_step") or "",
                            "link": was.get("link") or "", "at": was["last_update_at"]}
            items[key] = {"session_id": o["id"], "name": o["name"], "title": o["title"],
                          "link": o.get("link") or None, "also": also,
                          "opened_at": prev.get("opened_at") or stamp, "last_routed_at": prev.get("last_routed_at"),
                          "unseen": 0, "state": "live"}
            gone = set([key] + also)
            h["pending"] = [p for p in h["pending"] if p.get("key") not in gone]
        else:
            if key not in items:
                print("no session for %s" % key, file=sys.stderr)
                return 1
            if sub == "drop":
                items[key]["state"] = "dropped"
            else:
                items[key]["unseen"] = 0
        save_json(ws, "sessions.json", sess)
        _save_hub(ws, h, sess, cfg, at)
    res = {"ok": True, "key": key, "action": sub}
    if catch_up:
        res["catch_up"] = catch_up
    print(json.dumps(res) if as_json else "%s: %s%s" % (
        sub, key, "; catch up: %s" % json.dumps(catch_up) if catch_up else ""), file=out)
    return 0


def pending_cmd(ws: Path, cfg: dict, at: datetime, args, as_json: bool, out) -> int:
    try:
        pos, o = _parse_flags(args, ("name", "task"))
    except ValueError as e:
        print("mother hub pending: %s" % e, file=sys.stderr)
        return 2
    raised = len(pos) == 2 and pos[0] == "raised" and isinstance(o.get("name"), str) and o["name"]
    if not raised and not (len(pos) == 2 and pos[0] == "drop"):
        print("usage: mother hub pending raised <key> --name <slug> [--task <task_id>] | drop <key>",
              file=sys.stderr)
        return 2
    key, stamp = pos[1], _iso(at)
    with locked(ws):
        h, sess = _hub_doc(ws), _sessions_doc(ws)
        hit = next((p for p in h["pending"] if p.get("key") == key), None)
        if hit is None:
            print("no pending proposal for %s" % key, file=sys.stderr)
            return 1
        if not raised:
            h["pending"] = [p for p in h["pending"] if p.get("key") != key]
            _save_hub(ws, h, sess, cfg, at)
            print(json.dumps({"ok": True, "key": key, "action": "drop", "task_id": hit.get("task_id")}) if as_json
                  else "dropped: %s%s" % (key, " (card %s)" % hit["task_id"] if hit.get("task_id") else ""), file=out)
            return 0
        hit["raised_at"], hit["name"] = stamp, o["name"]
        if isinstance(o.get("task"), str) and o["task"]:
            hit["task_id"] = o["task"]
        _save_hub(ws, h, sess, cfg, at)
    print(json.dumps({"ok": True, "key": key, "name": o["name"], "raised_at": stamp}) if as_json
          else "raised: %s as %s" % (key, o["name"]), file=out)
    return 0


def update_cmd(ws: Path, cfg: dict, at: datetime, verb: str, args, as_json: bool, out) -> int:
    need = 2 if verb == "snooze" else 1
    if len(args) != need:
        print("usage: mother hub %s" % ("snooze <U-id> 1h|tomorrow|next-week" if verb == "snooze" else "done <U-id>"),
              file=sys.stderr)
        return 2
    uid = args[0]
    until = None
    if verb == "snooze":
        import now as nowmod
        try:
            until = _iso(nowmod.snooze_until(args[1], at, _schedule(cfg)))
        except ValueError as e:
            print("mother hub snooze: %s" % e, file=sys.stderr)
            return 2
    with locked(ws):
        h = _hub_doc(ws)
        sess = _sessions_doc(ws)
        u = next((u for u in h["updates"] if u.get("id") == uid), None)
        if not u:
            print("no update %s" % uid, file=sys.stderr)
            return 1
        if verb == "done":
            u["state"] = "done"
            u.pop("snooze_until", None)
        else:
            u["state"] = "snoozed"
            u["snooze_until"] = until
        _save_hub(ws, h, sess, cfg, at)
    print(json.dumps({"ok": True, "id": uid, "state": u["state"], "snooze_until": u.get("snooze_until")})
          if as_json else "%s: %s%s" % (uid, u["state"], " until %s" % until if until else ""), file=out)
    return 0


def show(ws: Path, as_json: bool, out) -> int:
    h, s = _hub_doc(ws), _sessions_doc(ws)
    if as_json:
        print(json.dumps({"hub": h, "sessions": s["sessions"]}, indent=2, sort_keys=True), file=out)
        return 0
    print("last wake: %s" % (h.get("last_wake") or "never"), file=out)
    print("pending proposals: %d" % len(h["pending"]), file=out)
    for p in h["pending"]:
        print("  %s | %s | %s" % (p.get("key"), p.get("title"), p.get("next_step") or ""), file=out)
    print("updates: %d" % len(h["updates"]), file=out)
    for u in h["updates"]:
        print("  %s | %s | %s | %s%s" % (u.get("id"), u.get("state"), u.get("key"), u.get("title"),
                                       " (until %s)" % u["snooze_until"] if u.get("snooze_until") else ""), file=out)
    live = [(k, v) for k, v in s["sessions"].items() if isinstance(v, dict)]
    print("sessions: %d" % len(live), file=out)
    for k, v in sorted(live):
        print("  %s | %s | %s | unseen %s | %s" % (k, v.get("name"), v.get("state"), v.get("unseen"),
                                                  v.get("link") or "no link"), file=out)
    return 0


def _read_decisions(stdin) -> List[dict]:
    try:
        doc = json.loads(stdin.read())
    except ValueError as e:
        raise ValueError("stdin is not JSON: %s" % e)
    items = doc if isinstance(doc, list) else [doc]
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("line_id"), str) or not isinstance(it.get("key"), str):
            raise ValueError("each decision needs a string line_id and key")
        if it.get("decision") not in DECISIONS:
            raise ValueError("decision must be one of %s" % ", ".join(DECISIONS))
        also = it.get("also")
        if isinstance(also, str):
            it["also"] = [also] if also else []  # one key, not its characters
        elif also is not None and not (isinstance(also, list) and all(isinstance(a, str) for a in also)):
            raise ValueError("also must be a key or a list of keys")
    return items


_SLACK_LINK = re.compile(r"/archives/(?P<c>[CG][A-Z0-9]+)/p(?P<a>\d{10})(?P<b>\d{6})(?:\?[^\s]*thread_ts=(?P<t>\d+\.\d+))?")
_URL = re.compile(r"https?://\S+")
_CHANNEL = re.compile(r"^[CG][A-Z0-9]{6,}$")
_PROJECT = re.compile(r"^[A-Z][A-Z0-9]+$")
_THREAD = re.compile(r"^[CG][A-Z0-9]+/\d+\.\d+$")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


def manual_key(text: str) -> str:
    """The thing a link or note is about: a chat message link -> its thread key, else the first tracker or PR key
    in it, else `manual:<hash of the text>`."""
    m = _SLACK_LINK.search(text)
    if m:
        return thing_key("chat", {"channel": m.group("c"), "ts": "%s.%s" % (m.group("a"), m.group("b")),
                                  "thread_ts": m.group("t")})
    found = extract_also(text)
    if found:
        return found[0]
    return "manual:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


_KEY_TOKEN = re.compile(r"[A-Za-z0-9_.:/@=,+-][A-Za-z0-9_.:/@=,+#-]{0,199}")  # the hub role's plain token


def add_cmd(ws: Path, at: datetime, args, out) -> int:
    try:
        pos, opts = _parse_flags(args, {"key"})
    except ValueError as e:
        print("mother hub add: %s" % e, file=sys.stderr)
        return 2
    text = " ".join(pos).strip()
    if not text:
        print("usage: mother hub add <link-or-text> [--key K]", file=sys.stderr)
        return 2
    text = plain(text)
    key = plain(opts.get("key")).strip() if isinstance(opts.get("key"), str) else ""
    if key and not _KEY_TOKEN.fullmatch(key):  # the hub can only name a key in a plain command token
        print("mother hub add: --key takes letters, digits and _.:/@=,+-# only (no space or quote)", file=sys.stderr)
        return 2
    key = key or manual_key(text)
    stamp = _iso(at)
    url = _URL.search(text)
    link = url.group(0) if url and re.match(r"^https?://\S+$", url.group(0)) else ""
    ln = make_line("manual", "manual", key, stamp, title=text[:80], link=link,
                   summary=text, also=[a for a in extract_also(text) if a != key],
                   uid="%s-%s" % (stamp, os.urandom(3).hex()))
    append_inbox(ws, [ln])
    print("added %s" % key, file=out)
    return 0


_WA_JID = re.compile(r"^(\d{6,15}@s\.whatsapp\.net|\d{6,30}(-\d+)?@g\.us|\d{6,30}@lid)$", re.ASCII)
_FORCE = {"project": ("projects", _PROJECT), "channel": ("channels", _CHANNEL), "thread": ("threads", _THREAD),
          "repo": ("repos", _REPO), "whatsapp": ("whatsapp", _WA_JID)}
_STRICT_CHANNEL = re.compile(r"^[CG][A-Z0-9]{8,}$")


def classify_follow(x: str, force: Optional[str] = None, known_channels=()):
    """(list name in watch.json `follow`, value) or None. A bare uppercase token is a project; it is a channel
    only when it looks like a channel id (`C`/`G` + 8 or more) AND is already a known channel, or `force` says so."""
    if force:
        name, pat = _FORCE[force]
        return (name, x) if pat.match(x) else None
    if _WA_JID.match(x):
        return "whatsapp", x
    if _THREAD.match(x):
        return "threads", x
    if _STRICT_CHANNEL.match(x) and x in known_channels:
        return "channels", x
    if _PROJECT.match(x):
        return "projects", x
    if _REPO.match(x):
        return "repos", x
    return None


def classify_mute(x: str):
    if x.startswith("topic:"):
        return ("topics", x) if _TOPIC.match(x) else None
    if _WA_JID.match(x):
        return "keys", "wa:" + x
    if x.startswith("wa:"):
        return "keys", x
    if _CHAT_USER.match(x) or ("@" in x and "/" not in x):
        return "people", x
    if _CHANNEL.match(x) and any(c.isdigit() for c in x):
        return "channels", x
    return "keys", x


def watch_cmd(ws: Path, cfg: dict, verb: str, args, as_json: bool, out, at: datetime) -> int:
    if verb == "watching":
        w = load_watch(ws)
        merged = merged_watch(cfg, w)
        merged["tracker"]["projects"] = _watch_cfg(cfg, {"watch": merged}, "tracker")["projects"]  # + teams'
        if as_json:
            print(json.dumps({"watch": merged, "follow": w["follow"], "mute": w["mute"], "auto": w["auto"]},
                             indent=2, sort_keys=True), file=out)
            return 0
        for cap, fld in (("chat", "channels"), ("chat", "follow"), ("tracker", "projects"), ("code", "repos"),
                         ("whatsapp", "groups")):
            print("%s %s: %s" % (cap, "threads" if fld == "follow" else fld,
                                 ", ".join(merged.get(cap, {}).get(fld) or []) or "-"), file=out)
        dms = merged["chat"].get("dms") or {}
        print("chat dms: %s" % (", ".join("%s=%s" % kv for kv in sorted(dms.items())) or "- (%s)" % DMS_HINT), file=out)
        print("chat inbox: %s" % (merged["chat"].get("inbox") or "-"), file=out)
        until = merged["mute"].get("until") or {}
        for k in MUTE_LISTS:
            print("mute %s: %s" % (k, ", ".join(v + (" (until %s)" % until[v] if v in until else "")
                                                for v in merged["mute"][k]) or "-"), file=out)
        return 0
    force = None
    tail_args = []
    if "--" in args:  # `--` ends the flags: what follows is the value, even one that starts with `-`
        cut = args.index("--")
        args, tail_args = args[:cut], args[cut + 1:]
    if verb in ("follow", "unfollow"):
        flags = [a for a in args if a in ("--project", "--channel", "--thread", "--repo", "--whatsapp")]
        args = [a for a in args if a not in flags]
        if len(flags) > 1:
            print("mother hub %s: give at most one of --project --channel --thread --repo --whatsapp" % verb, file=sys.stderr)
            return 2
        force = flags[0][2:] if flags else None
    span, scope = None, "item"
    if verb == "mute":  # `--for 1d|2w` ends the mute then; `--scope channel` mutes the channel a chat key or link is in
        try:
            pos, opts = _parse_flags(args, {"for", "scope", "words", "channel"})
        except ValueError as e:
            print("mother hub mute: %s" % e, file=sys.stderr)
            return 2
        if set(opts) - {"for", "scope", "words", "channel"}:
            print("mother hub mute: unknown flag --%s" % sorted(set(opts) - {"for", "scope", "words", "channel"})[0],
                  file=sys.stderr)
            return 2
        args = pos
        if "words" in opts or "channel" in opts:  # a "similar messages" rule takes no value of its own
            topic = topic_value(str(opts.get("words") or "").split(","), str(opts.get("channel") or ""))
            if pos + tail_args or "words" not in opts or topic is None or len(str(opts["words"]).split(",")) > 5:
                print("mother hub mute: --words takes 1-5 comma-separated words of 3-30 letters, digits, - or _ "
                      "(and --channel a channel id), and no other value", file=sys.stderr)
                return 2
            args, tail_args = [], [topic]
        if "for" in opts:
            m = re.fullmatch(r"([1-9]\d?)([dw])", str(opts["for"]))
            if not m or int(m.group(1)) * (7 if m.group(2) == "w" else 1) > 90:
                print("mother hub mute: --for takes Nd or Nw, at most 90 days (e.g. 1d, 1w)", file=sys.stderr)
                return 2
            span = timedelta(days=int(m.group(1)) * (7 if m.group(2) == "w" else 1))
        scope = opts.get("scope", "item")
        if scope not in ("item", "channel"):
            print("mother hub mute: --scope is item or channel", file=sys.stderr)
            return 2
    args = args + tail_args
    if len(args) != 1 or (not tail_args and args[0].startswith("--")):
        print("usage: mother hub %s <%s>" % (verb, "key|person|channel" if "mute" in verb
                                             else "[--project|--channel|--thread|--repo|--whatsapp] "
                                                  "project|channel-id|channel/ts|owner/repo|whatsapp-jid"), file=sys.stderr)
        return 2
    x = args[0]
    if verb in ("mute", "unmute") and _URL.fullmatch(x):  # a link names the thing it is about, if it names one
        k = manual_key(x)
        x = x if k.startswith("manual:") else k
    if scope == "channel":
        if not _THREAD.match(x):
            print("mother hub mute: --scope channel needs a chat thread key or link, not %r" % x, file=sys.stderr)
            return 2
        x = x.split("/", 1)[0]
    known = set(merged_watch(cfg, load_watch(ws)).get("chat", {}).get("channels") or [])
    mute = verb in ("mute", "unmute")
    found = classify_mute(x) if mute else classify_follow(x, force, known)
    if found is None:
        print("mother hub %s: cannot tell what %r is (a project key, channel id, channel/ts, owner/repo or WhatsApp JID)"
              % (verb, x), file=sys.stderr)
        return 2
    part, name = "mute" if mute else "follow", found[0]
    with locked(ws):
        w = load_watch(ws)
        lst = w[part][name]
        if verb in ("follow", "mute"):
            changed = found[1] not in lst
            if changed:
                lst.append(found[1])
            if verb == "mute":
                until = _iso(at + span) if span else None
                if w["mute"]["until"].get(found[1]) != until:
                    changed = True
                    if until:
                        w["mute"]["until"][found[1]] = until
                    else:
                        w["mute"]["until"].pop(found[1], None)
            if verb == "follow" and name == "projects" and found[1] in w["unfollowed"]:
                w["unfollowed"].remove(found[1])
                changed = True
        else:
            changed = found[1] in lst
            if changed:
                lst.remove(found[1])
            if verb == "unmute":
                w["mute"]["until"].pop(found[1], None)
            if verb == "unfollow" and name == "projects":  # an auto-followed project is dropped too
                auto = w["auto"]["projects"]
                keep = [a for a in auto if a.get("key") != found[1]]
                changed = changed or len(keep) != len(auto)
                w["auto"]["projects"] = keep
                if found[1] not in w["unfollowed"]:
                    w["unfollowed"].append(found[1])  # so it is not auto-followed again
                    changed = True
        if changed:
            save_watch(ws, w)
    still = ""
    if verb == "unfollow":  # config.json lists are never rewritten: say why it is still polled
        base = merged_watch(cfg, {})
        conf = {"projects": _watch_cfg(cfg, _hub_cfg(cfg), "tracker").get("projects") or [],
                "channels": base["chat"].get("channels") or [], "threads": base["chat"].get("follow") or [],
                "repos": base["code"].get("repos") or [], "whatsapp": base["whatsapp"].get("groups") or []}
        if found[1] in conf.get(name, []):
            still = "; still watched from config.json"
    if verb == "mute" and span:
        still = " until %s" % _iso(at + span)
    print("%s %s %s%s%s" % (verb, name, found[1], "" if changed else " (no change)", still), file=out)
    return 0


DMS_HINT = ("to fill the DM map, list your DMs with the chat connector's DM listing (types im) in a "
            "normal MOTHER session, match user ids to the people in config, then run "
            "`mother hub dms set <user id>=<DM id> …` (e.g. `mother hub dms set U00000001=D00000001`)")
_DM_PAIR = re.compile(r"^(U[A-Z0-9]{2,})=(D[A-Z0-9]{2,})$")




def dms_cmd(ws: Path, cfg: dict, args, as_json: bool, out) -> int:
    """`dms set <U>=<D> …`, `dms drop <U>`, `dms inbox <D>|none`, `dms show [--json]` (CONTRACT §5e)."""
    sub, rest = (args[0], args[1:]) if args else ("", [])
    usage = "usage: mother hub dms set <user id>=<DM id> [...] | drop <user id> | inbox <DM id>|none | show [--json]"
    if sub == "show" and not rest:
        w = load_watch(ws)["follow"]
        if as_json:
            print(json.dumps({"dms": w["dms"], "inbox": w["inbox"]}, indent=2, sort_keys=True), file=out)
            return 0
        names = config_people(cfg)
        for u, d in sorted(w["dms"].items()):
            print("%s%s = %s" % (u, " (%s)" % names[u] if names.get(u) else "", d), file=out)
        if not w["dms"]:
            print("no DMs mapped: %s" % DMS_HINT, file=out)
        print("inbox: %s" % (w["inbox"] or "- (set it with `mother hub dms inbox <your self-DM id>`)"), file=out)
        return 0
    if sub == "set" and rest:
        pairs, bad = [], []
        for a in rest:
            m = _DM_PAIR.match(a)
            if not m:
                print("mother hub dms set: %r is not <user id>=<DM id> (U… = D…)" % a, file=sys.stderr)
                return 2
            pairs.append((m.group(1), m.group(2)))
        mine = build_me(cfg).get("chat") or ""
        if mine in [u for u, _ in pairs]:
            print("mother hub dms set: %s is you; your self-DM is the inbox (`mother hub dms inbox <D id>`)" % mine,
                  file=sys.stderr)
            return 2
        known = dm_people(cfg)
        bad = [u for u, _ in pairs if u not in known]
        if bad:
            print("mother hub dms set: not a person in config.json (teams[].people[].chat): %s"
                  % ", ".join(dict.fromkeys(bad)), file=sys.stderr)
            return 2
        with locked(ws):
            w = load_watch(ws)
            clash = [d for _, d in pairs if d == w["follow"]["inbox"]]
            if clash:
                print("mother hub dms set: %s is the inbox (your self-DM), not a DM with someone" % clash[0],
                      file=sys.stderr)
                return 2
            changed = any(w["follow"]["dms"].get(u) != d for u, d in pairs)
            w["follow"]["dms"].update(dict(pairs))
            if changed:
                save_watch(ws, w)
        for u, d in pairs:
            print("dms set %s=%s%s" % (u, d, "" if changed else " (no change)"), file=out)
        return 0
    if sub == "drop" and len(rest) == 1 and _CHAT_USER.match(rest[0]):
        with locked(ws):
            w = load_watch(ws)
            changed = w["follow"]["dms"].pop(rest[0], None) is not None
            if changed:
                save_watch(ws, w)
        print("dms drop %s%s" % (rest[0], "" if changed else " (no change)"), file=out)
        return 0
    if sub == "inbox" and len(rest) == 1 and (rest[0] == "none" or _DM_CHANNEL.match(rest[0])):
        val = "" if rest[0] == "none" else rest[0]
        with locked(ws):
            w = load_watch(ws)
            if val and val in w["follow"]["dms"].values():
                print("mother hub dms inbox: %s is mapped as a DM with someone; drop that first" % val,
                      file=sys.stderr)
                return 2
            changed = w["follow"]["inbox"] != val
            w["follow"]["inbox"] = val
            if changed:
                save_watch(ws, w)
        print("dms inbox %s%s" % (val or "none", "" if changed else " (no change)"), file=out)
        return 0
    print(usage, file=sys.stderr)
    return 2


def main(args, ws, cfg, at) -> int:
    verb, rest = (args[0], args[1:]) if args else ("", [])
    if verb in ("", "help", "-h", "--help"):
        print(USAGE)
        return 0
    ws = Path(ws)
    if verb == "watch":
        watch(ws, cfg)
        return 0
    if verb == "install":
        return install(ws, cfg)
    if verb == "uninstall":
        return uninstall()
    if verb == "check":
        return check(cfg)
    if verb == "status":
        return status(ws, at, cfg=cfg)
    if verb == "smoke":
        return smoke(cfg, at)
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    if verb == "tail":
        try:
            return tail(ws, follow="--follow" in rest)
        except (KeyboardInterrupt, BrokenPipeError):
            try:
                sys.stdout = open(os.devnull, "w")
            except OSError:
                pass
            return 0
    if verb == "decide":
        try:
            items = _read_decisions(sys.stdin)
        except ValueError as e:
            print("mother hub decide: %s" % e, file=sys.stderr)
            return 2
        try:
            res = decide(ws, cfg, at, items)
        except ValueError as e:
            print("mother hub decide: %s" % e, file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            for r in res:
                print("%s -> %s%s" % (r["key"], r["decision"], " (%s)" % r["note"] if r.get("note") else ""))
        return 0
    if verb == "session":
        return session_cmd(ws, cfg, at, rest, as_json, sys.stdout)
    if verb == "pending":
        return pending_cmd(ws, cfg, at, rest, as_json, sys.stdout)
    if verb in ("done", "snooze"):
        return update_cmd(ws, cfg, at, verb, rest, as_json, sys.stdout)
    if verb == "show":
        return show(ws, as_json, sys.stdout)
    if verb == "add":
        return add_cmd(ws, at, rest, sys.stdout)
    if verb == "dms":
        return dms_cmd(ws, cfg, rest, as_json, sys.stdout)
    if verb in ("follow", "unfollow", "mute", "unmute", "watching"):
        return watch_cmd(ws, cfg, verb, rest, as_json, sys.stdout, at)
    print("mother hub: unknown verb %r; see `mother hub help`" % verb, file=sys.stderr)
    return 1
