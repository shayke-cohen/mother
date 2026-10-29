"""mother hub — state, thing keys and the inbox behind the watcher and the hub session (docs/CONTRACT.md §5e).

This part: the machine-local files under <workspace>/.mother/hub/ and the vocabulary they share.

  thing key   one string per piece of work, the same wherever it shows up: tracker `PLAT-123`, code
              `owner/repo#45`, chat `C00000001/<thread ts>`, mail `mail:<thread id>`, calendar `cal:<event id>`.
  inbox line  one JSON object per source update, appended by the watcher; summary at most 300 characters.
  state files written atomically (temp + os.replace) under the flock `hub.lock`; `inbox.jsonl` is append-only.

Python 3.9 stdlib only.
"""
import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

SUMMARY_MAX = 300
KEEP_DAYS = 7
KINDS = ("update", "resolved", "error", "sweep")

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


def make_line(source: str, kind: str, key: str, at: str, title: str = "", who: str = "", link: str = "",
              summary: str = "", also=None) -> dict:
    if kind not in KINDS:
        raise ValueError("unknown kind: %r" % (kind,))
    return {"id": "%s:%s:%s" % (source, key, at), "at": at, "source": source, "kind": kind, "key": key,
            "also": list(also or []), "title": title, "who": who, "link": link,
            "summary": (summary or "")[:SUMMARY_MAX]}


def coalesce(lines: List[dict]) -> List[dict]:
    """One line per key: the latest `at` wins, `also` is the union (order kept). First-seen key order."""
    best = {}
    also = {}
    for l in lines:
        k = l["key"]
        also.setdefault(k, [])
        for a in l.get("also") or []:
            if a not in also[k]:
                also[k].append(a)
        if k not in best or l["at"] >= best[k]["at"]:
            best[k] = l
    out = []
    for k, l in best.items():
        l = dict(l)
        l["also"] = also[k]
        out.append(l)
    return out


def append_inbox(ws: Path, lines: List[dict]) -> None:
    """Append one JSON object per line to inbox.jsonl, flushed and fsynced."""
    if not lines:
        return
    d = hub_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / "inbox.jsonl"), "a") as f:
        for l in lines:
            f.write(json.dumps(l, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def rotate_inbox(ws: Path, at: datetime) -> None:
    """Move inbox.jsonl aside as inbox-YYYY-MM-DD.jsonl (the date of `at`, appended to if it exists) and
    delete dated files 7 or more days older than `at`. Hold `locked(ws)` if a reader could be running."""
    d = hub_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    today = at.date()
    cur = d / "inbox.jsonl"
    dest = d / ("inbox-%s.jsonl" % today.isoformat())
    # Never copy-then-delete the live file: a line appended in between would be lost. Rename it away first
    # (atomic), so later appends start a fresh inbox.jsonl, then fold the renamed file into the dated one.
    if cur.exists():
        if not dest.exists():
            if cur.stat().st_size:
                os.replace(str(cur), str(dest))
            else:
                cur.unlink()
        else:
            aside = d / ("inbox.jsonl.%d.rotating" % os.getpid())
            os.replace(str(cur), str(aside))
            with open(str(aside), "rb") as src, open(str(dest), "ab") as out:
                out.write(src.read())
                out.flush()
                os.fsync(out.fileno())
            aside.unlink()
    for p in d.glob("inbox-*.jsonl"):
        m = re.match(r"inbox-(\d{4}-\d{2}-\d{2})\.jsonl$", p.name)
        if m and (today - datetime.strptime(m.group(1), "%Y-%m-%d").date()).days >= KEEP_DAYS:
            p.unlink()


def main(args, ws, cfg, at) -> int:
    print("mother hub: state core only (watch, status and the rest land with later tasks)")
    return 0
