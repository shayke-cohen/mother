"""mother floor — one card per session: did its last turn stop on a question for the manager?

Each session's mod writes its own card at turn end (`floor card --stdin`) and clears it when the manager types there;
the Chief pane reads them all (`floor list --json`). Cards live in <workspace>/.mother/floor/<session_id>.json,
machine-local and never synced. Python 3.9, stdlib only.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

USAGE = """usage: mother floor <verb>
  card --stdin     store one session's card: {session_id, asks, question, options, waiting_on} as JSON on stdin
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


def clean(doc) -> Optional[dict]:
    """The card's fields from untrusted input, cut to their caps; None when the session id is off-shape."""
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
    return {"session_id": sid, "asks": asks, "question": question if asks else "",
            "options": options[:OPTIONS_MAX] if asks else [],
            "waiting_on": (_line(doc.get("waiting_on"), WAITING_MAX) or None) if asks else None}


def write_card(ws: Path, at: datetime, doc) -> Optional[dict]:
    """Store the card atomically (temp file, then rename); None when it is refused."""
    card = clean(doc)
    if card is None:
        return None
    card["at"] = _iso(at)
    path = floor_dir(ws) / (card["session_id"] + ".json")
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
