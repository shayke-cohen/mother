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
                   why_manager?, recap?, source?, cwd?} as JSON on stdin
  list --json      every card not older than 7 days: {version: 1, cards: [...]}
"""
VERBS = ("card", "list")
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
    card.update({k: v for k, v in extra.items() if v})
    return card


def write_card(ws: Path, at: datetime, doc) -> Optional[dict]:
    """Store the card atomically (temp file, then rename); None when it is refused. `cwd` is not kept: it says which
    checkout the repo and branch are read from. The same question, still asked on the card it replaces, keeps that
    card's `at` and so its `ask_id`: a refresh that writes it again doesn't make it a new question."""
    card = clean(dict(doc, **repo_facts(doc.get("cwd"))) if isinstance(doc, dict) else doc)
    if card is None:
        return None
    path = floor_dir(ws) / (card["session_id"] + ".json")
    since = at
    if card["asks"] and path.is_file():
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            old = None
        if isinstance(old, dict) and old.get("asks") is True and old.get("question") == card["question"]:
            since = _parse(old.get("at")) or at
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
        cards.append(card)
    return sorted(cards, key=lambda c: (c["at"], c["session_id"]))


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
    if args == ["list", "--json"]:
        print(json.dumps({"version": 1, "cards": list_cards(ws, at)}, ensure_ascii=False))
        return 0
    print(USAGE.strip(), file=sys.stderr)
    return 2
