"""MOTHER chief — workers (headless Claude sessions) kept in the hub's sessions.json, plus the chief-only
say queue, spend ledger and pause switch under <ws>/.mother/chief/.

Every sessions.json write runs inside hub.locked(ws). queue.jsonl and spend.json use the same lock, so
take_queue cannot race enqueue. Stdlib only.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import hub

DEFAULTS = {"max_workers": 3, "run_minutes": 20, "daily_budget_usd": 20,
            "chief_model": "sonnet", "worker_model": "", "tick_seconds": 30}
SOURCES = ("say", "route", "release")
NAME_MAX = 24
SPEND_KEEP_DAYS = 14
RELEASE_NOTE = "Shay worked in this session directly; read the latest turns before continuing."


def chief_dir(ws: Path) -> Path:
    return Path(ws) / ".mother" / "chief"


def settings(cfg: dict) -> dict:
    """The chief caps from config.json -> chief, defaults merged in. Unknown keys are dropped."""
    given = (cfg or {}).get("chief")
    given = given if isinstance(given, dict) else {}
    return {k: given.get(k, v) for k, v in DEFAULTS.items()}


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


def _read_queue(ws: Path) -> List[dict]:
    out = []
    try:
        text = (chief_dir(ws) / "queue.jsonl").read_text()
    except OSError:
        return out
    for ln in text.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _write_queue(ws: Path, entries: List[dict]) -> None:
    _write_atomic(chief_dir(ws) / "queue.jsonl", "".join(json.dumps(e) + "\n" for e in entries))


# ---- workers ----------------------------------------------------------------------------------------

def _slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:NAME_MAX].strip("-") or "worker"


def _is_cli(s) -> bool:
    return isinstance(s, dict) and s.get("kind") == "cli"


def register_worker(ws: Path, at: datetime, key: str, title: str, also: List[str] = ()) -> dict:
    """Create the worker for `key`, or return the existing live CLI one. A live entry without kind is a
    desktop session, which a CLI process cannot write to: ValueError("desktop session")."""
    with hub.locked(ws):
        doc = hub._sessions_doc(ws)
        cur = doc["sessions"].get(key)
        if isinstance(cur, dict) and cur.get("state") == "live":
            if _is_cli(cur):
                return cur
            raise ValueError("desktop session")
        entry = {
            "session_id": str(uuid.uuid4()), "name": _slug(key), "title": title, "also": list(also),
            "link": None, "opened_at": hub._iso(at), "last_routed_at": None, "unseen": 0,
            "state": "live", "kind": "cli", "status": "idle", "held": False, "progress": "",
            "cost": 0.0, "runs": 0, "last_run_at": None,
        }
        doc["sessions"][key] = entry
        hub.save_json(ws, "sessions.json", doc)
        return entry


def _live_cli(doc: dict, key: str) -> dict:
    s = doc["sessions"].get(key)
    if not (_is_cli(s) and s.get("state") == "live"):
        raise KeyError(key)
    return s


def update_worker(ws: Path, key: str, **fields) -> dict:
    with hub.locked(ws):
        doc = hub._sessions_doc(ws)
        s = _live_cli(doc, key)
        s.update(fields)
        hub.save_json(ws, "sessions.json", doc)
        return s


def workers(ws: Path) -> Dict[str, dict]:
    """Live kind:"cli" entries only."""
    return {k: s for k, s in hub._sessions_doc(ws)["sessions"].items()
            if _is_cli(s) and s.get("state") == "live"}


def hold(ws: Path, key: str) -> dict:
    return update_worker(ws, key, held=True)


def release(ws: Path, key: str, at: Optional[datetime] = None) -> dict:
    """Hand the worker back: clear held, reset failed to idle, queue a note to re-read the transcript."""
    at = at or datetime.now(timezone.utc)
    with hub.locked(ws):
        doc = hub._sessions_doc(ws)
        s = _live_cli(doc, key)
        s["held"] = False
        if s.get("status") == "failed":
            s["status"] = "idle"
        hub.save_json(ws, "sessions.json", doc)
        q = _read_queue(ws)
        q.append({"key": key, "text": RELEASE_NOTE, "source": "release", "at": hub._iso(at)})
        _write_queue(ws, q)
        return s


# ---- say queue --------------------------------------------------------------------------------------

def enqueue(ws: Path, at: datetime, key: str, text: str, source: str) -> None:
    """Append {key, text, source, at}. `text` is stored byte for byte. A "say" gives a failed worker another try."""
    if source not in SOURCES:
        raise ValueError("source must be one of %s" % ", ".join(SOURCES))
    with hub.locked(ws):
        q = _read_queue(ws)
        q.append({"key": key, "text": text, "source": source, "at": hub._iso(at)})
        _write_queue(ws, q)
        if source == "say":
            doc = hub._sessions_doc(ws)
            s = doc["sessions"].get(key)
            if _is_cli(s) and s.get("state") == "live" and s.get("status") == "failed":
                s["status"] = "idle"
                hub.save_json(ws, "sessions.json", doc)


def take_queue(ws: Path, key: str) -> List[dict]:
    """Atomically remove and return `key`'s entries, oldest first."""
    with hub.locked(ws):
        q = _read_queue(ws)
        mine = [e for e in q if e.get("key") == key]
        if mine:
            _write_queue(ws, [e for e in q if e.get("key") != key])
        return mine


def queued(ws: Path) -> Dict[str, int]:
    out = {}  # type: Dict[str, int]
    with hub.locked(ws):
        for e in _read_queue(ws):
            k = e.get("key")
            if isinstance(k, str):
                out[k] = out.get(k, 0) + 1
    return out


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
