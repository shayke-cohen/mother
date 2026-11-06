"""mother floor — one card per session: did its last turn stop on a question for the manager?

Each session's mod writes its own card at turn end (`floor card --stdin`) and clears it when the manager types there;
the Chief pane reads them all (`floor list --json`). Cards live in <workspace>/.mother/floor/<session_id>.json,
machine-local and never synced. Python 3.9, stdlib only.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

USAGE = """usage: mother floor <verb>
  card --stdin     store one session's card: {session_id, asks, question, options, waiting_on, recommendation?,
                   why_manager?, recap?, source?, cwd?, waiting_text?} as JSON on stdin; `waiting_text` is the end of
                   the turn's final message, whose last `Waiting on:` line becomes the card's `waiting`
  list --json      every card not older than 7 days: {version: 1, cards: [...]}
  mark --stdin     {session_id, mark}: checked (a re-check was sent; refused within 24 h of the last), keep (no chase
                   for 3 days), drop (no chase for this wait), done-keep (no Done card until the session moves)
"""
VERBS = ("card", "list", "mark")
SESSION_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
KEEP = timedelta(days=7)
QUESTION_MAX = 200
LABEL_MAX = 24
MESSAGE_MAX = 200
WAITING_MAX = 80
OPTIONS_MAX = 4
WHY_MANAGER_MAX = 100
RECAP_MAX = 200
REPO_MAX = 100
BRANCH_MAX = 100
SOURCES = ("wrap_up",)     # where a card came from, when it is not the model's reading of the final message
WHO_MAX = 80
WHAT_MAX = 200
WAIT_KINDS = ("manager", "other", "nothing")
CHECK_EVERY = timedelta(hours=24)     # waiting on someone else this long: a re-check is due, at most once a day
KEEP_WAITING = timedelta(days=3)      # Keep waiting on a chase card: no chase card for this long
MARKS = ("checked", "keep", "drop", "done-keep")
WAITING_TEXT_MAX = 2000
_WAITING_RE = re.compile(r"^\W{0,3}Waiting on:\s*(.+?)\s*(?:—|–|--|-)\s*(.*?)\W{0,3}$", re.IGNORECASE)
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_REPO_URL = re.compile(r"^https://[\w.-]+/[\w.-]+/[\w.-]+$")
_REMOTE = re.compile(r"^(?:[a-z+]+://)?(?:[^@/\s]+@)?([\w.-]+)[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def floor_dir(ws: Path) -> Path:
    return Path(ws) / ".mother" / "floor"


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text) -> Optional[datetime]:
    try:
        at = datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return at.replace(tzinfo=timezone.utc)


def _line(v, cap: int) -> str:
    return " ".join(str(v).split())[:cap] if isinstance(v, str) else ""


def _safe_line(v, cap: int) -> str:
    return _UNSAFE.sub("", _line(v.replace("\t", " ") if isinstance(v, str) else v, cap))


def waiting_line(text) -> Optional[dict]:
    """The `Waiting on: <who> — <what>` last line of a message, read in code: {state, on, what} with state
    `waiting_manager` (the manager), `done` (nothing) or `waiting_other` (anyone else, named in `on`). None when the
    message's last non-empty line is not one. (lib/chief.py reads a transcript's last message with it too.)"""
    rows = [l.strip() for l in str(text or "").splitlines() if l.strip()]
    m = _WAITING_RE.match(rows[-1]) if rows else None
    if not m:
        return None
    who, what = _safe_line(m.group(1), WHO_MAX), _safe_line(m.group(2), WHAT_MAX)
    low = who.lower()
    if low in ("nothing", "nobody", "no one"):
        return {"state": "done", "on": None, "what": what}
    if low in ("the manager", "manager", "you"):
        return {"state": "waiting_manager", "on": None, "what": what}
    return {"state": "waiting_other", "on": who, "what": what}


def _wait_of(w) -> Optional[dict]:
    """A `waiting_line` reading as the card's `waiting` fields {kind, who, what}; None for none."""
    if not isinstance(w, dict) or w.get("state") not in ("waiting_manager", "waiting_other", "done"):
        return None
    kind = {"waiting_manager": "manager", "waiting_other": "other", "done": "nothing"}[w["state"]]
    who = {"manager": "the manager", "nothing": "nothing"}.get(kind) or _safe_line(w.get("on"), WHO_MAX)
    return {"kind": kind, "who": who or "someone", "what": _safe_line(w.get("what"), WHAT_MAX)}


def _same_wait(a, b) -> bool:
    return isinstance(a, dict) and isinstance(b, dict) and a.get("kind") == b.get("kind") \
        and str(a.get("who", "")).lower() == str(b.get("who", "")).lower() and a.get("what") == b.get("what")


def merged_wait(old, new: Optional[dict], at: datetime) -> Optional[dict]:
    """The card's next `waiting`: `new` (from `_wait_of`) with the old one's `since` and marks while the same who and
    what repeat; a new wait starts now with no marks. None when `new` is None."""
    if new is None:
        return None
    if _same_wait(old, new) and _parse(old.get("since")):
        kept = {k: old[k] for k in ("since", "checked_at", "snooze_until") if _parse(old.get(k))}
        if old.get("dropped") is True:
            kept["dropped"] = True
        return dict(new, **kept)
    return dict(new, since=_iso(at))


def clean_waiting(w) -> Optional[dict]:
    """A stored `waiting` as a reader may trust it, or None."""
    if not isinstance(w, dict) or w.get("kind") not in WAIT_KINDS or not _parse(w.get("since")):
        return None
    out = {"kind": w["kind"], "who": _safe_line(w.get("who"), WHO_MAX) or "someone", "what": _safe_line(w.get("what"), WHAT_MAX),
           "since": w["since"]}
    for k in ("checked_at", "snooze_until"):
        if _parse(w.get(k)):
            out[k] = w[k]
    if w.get("dropped") is True:
        out["dropped"] = True
    return out


def check_due(w: Optional[dict], at: datetime) -> bool:
    """Waiting on someone other than the manager for more than 24 hours, and no re-check sent in the last 24."""
    if not w or w.get("kind") != "other" or w.get("dropped"):
        return False
    since, checked = _parse(w.get("since")), _parse(w.get("checked_at"))
    return since is not None and at - since > CHECK_EVERY and (checked is None or at - checked >= CHECK_EVERY)


def ask_id(question: str, at: str) -> str:
    """Names one question on one card: a button drawn for it can tell it is still the one being asked."""
    return hashlib.sha256((question + "\n" + at).encode("utf-8")).hexdigest()[:16]


def _remote_url(common: Path) -> str:
    in_origin, url = False, ""
    for raw in (common / "config").read_text(errors="replace").splitlines():
        text = raw.strip()
        if text.startswith("["):
            in_origin = text == '[remote "origin"]'
        elif in_origin and re.match(r"^url\s*=", text):
            url = text.split("=", 1)[1].strip()
    return url


def repo_facts(cwd) -> dict:
    """{repo, branch, repo_url} of the git checkout holding `cwd`, each only when known. Read from its files: no
    process and no network. A remote's credentials are never kept, and `repo_url` is set only on a GitHub host, where a
    bare `#123` can be made a link."""
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return {}
    start = Path(cwd)
    top = next((p for p in [start] + list(start.parents) if (p / ".git").exists()), None)
    if top is None:
        return {}
    out = {}
    try:
        git = top / ".git"
        if git.is_file():      # a worktree: `.git` names its own folder, whose `commondir` names the shared one
            found = re.match(r"gitdir:\s*(.+)", git.read_text())
            if not found:
                return {}
            git = (top / found.group(1).strip()).resolve()
        common = (git / (git / "commondir").read_text().strip()).resolve() if (git / "commondir").is_file() else git
        head = re.match(r"ref:\s*refs/heads/(\S+)", (git / "HEAD").read_text().strip())
        if head:
            out["branch"] = head.group(1)[:BRANCH_MAX]
        remote = _REMOTE.match(_remote_url(common))
        if remote and "." in remote.group(1) and not remote.group(1).startswith("."):
            host, owner, name = remote.groups()
            out["repo"] = ("%s/%s" % (owner, name))[:REPO_MAX]
            if host == "github.com" or host.startswith("github."):
                out["repo_url"] = "https://%s/%s/%s" % (host, owner, name)
    except (OSError, ValueError):
        pass
    return out


def clean(doc) -> Optional[dict]:
    """The card's fields from untrusted input, cut to their caps; None when the session id is off-shape. An optional
    field is kept only when it says something."""
    if not isinstance(doc, dict):
        return None
    sid = doc.get("session_id")
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        return None
    options = []
    for o in doc.get("options") if isinstance(doc.get("options"), list) else []:
        if not isinstance(o, dict):
            continue
        label, message = _line(o.get("label"), LABEL_MAX), _line(o.get("message"), MESSAGE_MAX)
        if label and message:
            options.append({"label": label, "message": message})
    question = _line(doc.get("question"), QUESTION_MAX)
    asks = doc.get("asks") is True and bool(question)
    card = {"session_id": sid, "asks": asks, "question": question if asks else "",
            "options": options[:OPTIONS_MAX] if asks else [],
            "waiting_on": (_line(doc.get("waiting_on"), WAITING_MAX) or None) if asks else None}
    extra = {"recap": _line(doc.get("recap"), RECAP_MAX)}
    if asks:
        url = doc.get("repo_url")
        extra.update(recommendation=_line(doc.get("recommendation"), MESSAGE_MAX),
                     why_manager=_line(doc.get("why_manager"), WHY_MANAGER_MAX),
                     repo=_line(doc.get("repo"), REPO_MAX), branch=_line(doc.get("branch"), BRANCH_MAX),
                     repo_url=url if isinstance(url, str) and _REPO_URL.match(url) else "")
    if doc.get("source") in SOURCES:
        extra["source"] = doc["source"]
    extra["waiting"] = clean_waiting(doc.get("waiting"))
    extra["done_kept"] = doc.get("done_kept") if _parse(doc.get("done_kept")) else ""
    card.update({k: v for k, v in extra.items() if v})
    return card


def write_card(ws: Path, at: datetime, doc) -> Optional[dict]:
    """Store the card atomically (temp file, then rename); None when it is refused. `cwd` is not kept: it says which
    checkout the repo and branch are read from. The same question, still asked on the card it replaces, keeps that
    card's `at` and so its `ask_id`: a refresh that writes it again doesn't make it a new question."""
    if isinstance(doc, dict):
        doc = {k: v for k, v in doc.items() if k not in ("waiting", "done_kept")}   # those are the file's, never the caller's
    card = clean(dict(doc, **repo_facts(doc.get("cwd"))) if isinstance(doc, dict) else doc)
    if card is None:
        return None
    path = floor_dir(ws) / (card["session_id"] + ".json")
    since = at
    old = None
    if path.is_file():
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            old = None
    old = old if isinstance(old, dict) else {}
    if card["asks"] and old.get("asks") is True and old.get("question") == card["question"]:
        since = _parse(old.get("at")) or at
    # the wait: a turn's final message with a `Waiting on:` line sets it; anything else keeps the one before
    text = doc.get("waiting_text") if isinstance(doc.get("waiting_text"), str) else None
    new = _wait_of(waiting_line(text[-WAITING_TEXT_MAX:])) if text is not None else None
    wait = merged_wait(old.get("waiting"), new, at) if new is not None else clean_waiting(old.get("waiting"))
    if wait:
        card["waiting"] = wait
    card["at"] = _iso(since)
    if card["asks"]:
        card["ask_id"] = ask_id(card["question"], card["at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / ("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(card, ensure_ascii=False) + "\n")
    os.replace(str(tmp), str(path))
    return card


def list_cards(ws: Path, at: datetime) -> List[dict]:
    """Every readable card, oldest first. A card older than 7 days is deleted; an unreadable file is skipped."""
    cards = []
    folder = floor_dir(ws)
    if not folder.is_dir():
        return cards
    for path in sorted(folder.glob("*.json")):
        try:
            card = clean(json.loads(path.read_text()))
            written = _parse(json.loads(path.read_text()).get("at")) if card else None
        except (OSError, ValueError, AttributeError):
            continue
        if card is None or written is None or card["session_id"] != path.stem:
            continue
        if at - written > KEEP:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        card["at"] = _iso(written)
        if card["asks"]:
            card["ask_id"] = ask_id(card["question"], card["at"])
        if card.get("waiting"):
            card["waiting"]["check_due"] = check_due(card["waiting"], at)
        cards.append(card)
    return sorted(cards, key=lambda c: (c["at"], c["session_id"]))


def _save(path: Path, card: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / ("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(card, ensure_ascii=False) + "\n")
    os.replace(str(tmp), str(path))


def note_waiting(ws: Path, at: datetime, sid: str, w) -> Optional[dict]:
    """A transcript's `Waiting on:` reading (`waiting_line`) into the session's card, kept as `write_card` keeps it;
    a session with no card gets one that doesn't ask. Its `at` is left alone. None when nothing was written."""
    new = _wait_of(w)
    if new is None or not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        return None
    path = floor_dir(ws) / (sid + ".json")
    try:
        old = json.loads(path.read_text()) if path.is_file() else None
    except (OSError, ValueError):
        old = None
    card = clean(old) if isinstance(old, dict) and old.get("session_id") == sid else None
    if card is None:
        card = {"session_id": sid, "asks": False, "question": "", "options": [], "waiting_on": None, "at": _iso(at)}
    else:
        card["at"] = old.get("at") if _parse(old.get("at")) else _iso(at)
    wait = merged_wait(card.get("waiting"), new, at)
    if _same_wait(card.get("waiting"), wait) and card["waiting"].get("since") == wait.get("since"):
        return card
    card["waiting"] = wait
    _save(path, card)
    return card


def mark(ws: Path, at: datetime, doc) -> dict:
    """`mark --stdin` {session_id, mark}: what the manager pressed on a wait or a Done card, kept on the card without
    moving its `at`. ValueError when refused: an off-shape id or mark, no card, no wait to mark, or `checked` within
    24 hours of the last re-check."""
    sid = doc.get("session_id") if isinstance(doc, dict) else None
    what = doc.get("mark") if isinstance(doc, dict) else None
    if not (isinstance(sid, str) and SESSION_ID_RE.match(sid)):
        raise ValueError("session_id must be a 36-character session id")
    if what not in MARKS:
        raise ValueError("mark must be one of %s" % ", ".join(MARKS))
    path = floor_dir(ws) / (sid + ".json")
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError("no card for that session")
    card = clean(old) if isinstance(old, dict) else None
    if card is None or not _parse(old.get("at")):
        raise ValueError("no card for that session")
    card["at"] = old["at"]
    if what == "done-keep":
        card["done_kept"] = card["at"]
    else:
        w = card.get("waiting")
        if not w:
            raise ValueError("that session waits on nothing")
        if what == "checked":
            last = _parse(w.get("checked_at"))
            if last is not None and at - last < CHECK_EVERY:
                raise ValueError("a re-check went to that session in the last 24 hours")
            w["checked_at"] = _iso(at)
        elif what == "keep":
            w["snooze_until"] = _iso(at + KEEP_WAITING)
        else:
            w["dropped"] = True
    _save(path, card)
    return {"ok": True, "session_id": sid, "mark": what}


def main(args, ws, cfg, at=None) -> int:
    at = at or datetime.now(timezone.utc)
    if args[:1] in ([], ["help"], ["-h"], ["--help"]):
        print(USAGE.strip())
        return 0
    if args == ["card", "--stdin"]:
        try:
            doc = json.loads(sys.stdin.read())
        except ValueError:
            print("mother floor: the card on stdin isn't JSON", file=sys.stderr)
            return 2
        card = write_card(ws, at, doc)
        if card is None:
            print("mother floor: session_id must be a 36-character session id; nothing was written", file=sys.stderr)
            return 2
        print(json.dumps({"stored": card["session_id"], "asks": card["asks"]}))
        return 0
    if args == ["mark", "--stdin"]:
        try:
            doc = json.loads(sys.stdin.read())
        except ValueError:
            print("mother floor: the mark on stdin isn't JSON", file=sys.stderr)
            return 2
        try:
            print(json.dumps(mark(ws, at, doc)))
        except ValueError as e:
            print("mother floor mark: %s" % e, file=sys.stderr)
            return 1
        return 0
    if args == ["list", "--json"]:
        print(json.dumps({"version": 1, "cards": list_cards(ws, at)}, ensure_ascii=False))
        return 0
    print(USAGE.strip(), file=sys.stderr)
    return 2
