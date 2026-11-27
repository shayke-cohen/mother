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
  codex-hook       a Codex hook event on stdin (SessionStart, UserPromptSubmit, PermissionRequest, PostToolUse, Stop,
                   SessionEnd): the Codex session's card. Prints nothing and always exits 0, so it never blocks Codex
  codex-setup [--print]   add those hooks to ~/.codex/config.toml (idempotent); --print shows them instead
  send --stdin     {session_id, text}: type the text into the session's tmux pane and press Enter. For sessions the
                   desktop app can't message (Codex, a CLI in tmux, dev3); refused when the card names no live pane

A card may also carry who the session is: `identify: true` on `card --stdin` reads it from the environment the
session runs in (TMUX_PANE and TMUX, DEV3_TASK_ID with that task's title and overview from `dev3 task show`), and
`agent` (claude, codex) and `surface` (desktop, cli, tmux, dev3) say what it is. `ask_from_waiting: true` makes a
`Waiting on: the manager — <what>` line the card's question, for sessions no model call reads.
"""
VERBS = ("card", "list", "mark", "codex-hook", "codex-setup", "send")
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
AGENTS = ("claude", "codex")
SURFACES = ("desktop", "cli", "tmux", "dev3")
TITLE_MAX = 100
TEXT_MAX = 2000
_PANE = re.compile(r"^%\d{1,6}$")
_TASK = re.compile(r"^[\w-]{1,64}$")
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
    if low in ("the manager", "manager", "you", "the user", "user"):
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
    elif doc.get("agent") in AGENTS:   # an identified session's card shows on the overview, asking or not
        extra.update(repo=_line(doc.get("repo"), REPO_MAX), branch=_line(doc.get("branch"), BRANCH_MAX))
    extra.update(identity(doc))
    if doc.get("source") in SOURCES:
        extra["source"] = doc["source"]
    extra["waiting"] = clean_waiting(doc.get("waiting"))
    extra["done_kept"] = doc.get("done_kept") if _parse(doc.get("done_kept")) else ""
    card.update({k: v for k, v in extra.items() if v})
    return card


def identity(doc) -> dict:
    """Who the session is, from untrusted input: {agent, surface, title, tmux, dev3_task, cwd}, each only when well-formed."""
    out = {}
    if doc.get("agent") in AGENTS:
        out["agent"] = doc["agent"]
    if doc.get("surface") in SURFACES:
        out["surface"] = doc["surface"]
    title = _safe_line(doc.get("title"), TITLE_MAX)
    if title:
        out["title"] = title
    t = doc.get("tmux")
    if isinstance(t, dict) and isinstance(t.get("pane"), str) and _PANE.match(t["pane"]):
        sock = t.get("socket")
        out["tmux"] = dict({"pane": t["pane"]}, **({"socket": sock} if isinstance(sock, str) and sock.startswith("/")
                                                   and len(sock) <= 300 and not _UNSAFE.search(sock) else {}))
    task = doc.get("dev3_task")
    if isinstance(task, str) and _TASK.match(task):
        out["dev3_task"] = task
    cwd = doc.get("cwd")
    if out.get("agent") and isinstance(cwd, str) and cwd.startswith("/") and len(cwd) <= 500 and not _UNSAFE.search(cwd):
        out["cwd"] = cwd   # where its transcript lives (lib/chief.py agent_status)
    return out


def _dev3_task(task_id: str, cwd) -> dict:
    """{title, overview} of a dev3 task from `dev3 task show --json`; {} when dev3 is missing, slow or says nothing."""
    import shutil
    import subprocess
    exe = shutil.which("dev3") or os.path.expanduser("~/.dev3.0/bin/dev3")
    if not os.path.isfile(exe):
        return {}
    try:
        ran = subprocess.run([exe, "task", "show", "--task", task_id, "--json"], capture_output=True, text=True, timeout=4,
                             cwd=cwd if isinstance(cwd, str) and os.path.isdir(cwd) else None)
        d = json.loads(ran.stdout) if ran.returncode == 0 else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    if not isinstance(d, dict):
        return {}
    seq = d.get("seq")
    title = _safe_line(d.get("title"), TITLE_MAX - 6)
    return {"title": ("#%s %s" % (seq, title) if isinstance(seq, (int, str)) and str(seq).isdigit() and title else title),
            "overview": _safe_line(d.get("overview"), RECAP_MAX)}


def identify_env(env=None, cwd=None) -> dict:
    """Who the calling session is, read from the environment it runs in: a tmux pane and socket, a dev3 task (with its
    title, and its overview as the recap). `surface` is dev3, tmux or cli; the desktop app says desktop itself."""
    env = os.environ if env is None else env
    out = {"surface": "cli"}
    pane = env.get("TMUX_PANE", "")
    if _PANE.match(pane):
        out["tmux"] = {"pane": pane}
        sock = env.get("TMUX", "").split(",")[0]
        if sock.startswith("/"):
            out["tmux"]["socket"] = sock
        out["surface"] = "tmux"
    task = env.get("DEV3_TASK_ID", "")
    if _TASK.match(task):
        out.update(surface="dev3", dev3_task=task)
        facts = _dev3_task(task, cwd)
        if facts.get("title"):
            out["title"] = facts["title"]
        if facts.get("overview"):
            out["recap"] = facts["overview"]
    return out


def write_card(ws: Path, at: datetime, doc) -> Optional[dict]:
    """Store the card atomically (temp file, then rename); None when it is refused. `cwd` is not kept: it says which
    checkout the repo and branch are read from. The same question, still asked on the card it replaces, keeps that
    card's `at` and so its `ask_id`: a refresh that writes it again doesn't make it a new question."""
    if isinstance(doc, dict):
        doc = {k: v for k, v in doc.items() if k not in ("waiting", "done_kept")}   # those are the file's, never the caller's
        if doc.get("identify") is True:
            env = doc.get("env") if isinstance(doc.get("env"), dict) else None   # a spooled Codex event's environment
            found = identify_env(env={k: str(v) for k, v in env.items()} if env else None, cwd=doc.get("cwd"))
            if doc.get("surface") == "desktop":
                found["surface"] = "desktop"
            doc = dict(found, **{k: v for k, v in doc.items()
                                 if k not in ("surface", "tmux", "dev3_task") and not (k in ("title", "recap") and not v)})
            if found.get("recap"):   # a dev3 task's overview is the better TLDR than a turn's last lines
                doc["recap"] = found["recap"]
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
    if doc.get("ask_from_waiting") is True and not card["asks"] and wait and wait.get("kind") == "manager" \
            and new is not None and wait.get("what"):
        card.update(asks=True, question=_line(wait["what"], QUESTION_MAX), options=[], waiting_on=None)
        if old.get("asks") is True and old.get("question") == card["question"]:
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
    """Every readable card, oldest first. A card older than 7 days is deleted; an unreadable file is skipped.
    Codex events waiting in the spool are folded in first."""
    fold_spool(ws, at)
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


# ---- Codex: its hooks feed the same cards ----
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "SessionEnd")
CODEX_MARK = "# mother floor: Codex sessions on the floor (mother floor codex-setup)"


def _codex_ask(doc) -> str:
    """A PermissionRequest as one line: the tool and what it would run."""
    tool = _safe_line(doc.get("tool_name"), 40) or "a tool"
    ti = doc.get("tool_input")
    what = ti.get("command") or ti.get("file_path") or ti.get("path") if isinstance(ti, dict) else ti
    if isinstance(what, list):
        what = " ".join(str(x) for x in what)
    what = _safe_line(what, 140)
    return ("Approve %s: %s" % (tool, what) if what else "Approve %s" % tool)[:QUESTION_MAX]


# Codex runs its hooks inside its sandbox, which can't write the workspace but can write /tmp. So the hook only spools
# the event (with the environment that says where the session runs) into a private folder there, and the next
# `floor list` (outside the sandbox: the pane's 5-second and minute reads) folds the events into cards, in order.
SPOOL_ENV = ("TMUX_PANE", "TMUX", "DEV3_TASK_ID")
SPOOL_MAX = 500       # events kept at most; older ones are dropped unread
_seq = [0]


def spool_dir() -> Path:
    if os.environ.get("MOTHER_FLOOR_SPOOL", "").startswith("/"):   # tests point it at their own folder
        return Path(os.environ["MOTHER_FLOOR_SPOOL"])
    return Path("/private/tmp" if os.path.isdir("/private/tmp") else "/tmp") / ("mother-floor-%d" % os.getuid())


def spool_event(doc, env=None, folder: Optional[Path] = None) -> Optional[Path]:
    """Write one Codex hook event to the spool; None when it is off-shape or the folder can't be made private."""
    if not isinstance(doc, dict) or doc.get("hook_event_name") not in CODEX_EVENTS:
        return None
    if not (isinstance(doc.get("session_id"), str) and SESSION_ID_RE.match(doc["session_id"])):
        return None
    env = os.environ if env is None else env
    folder = folder or spool_dir()
    folder.mkdir(mode=0o700, exist_ok=True)
    if not _private(folder):
        return None
    _seq[0] += 1
    name = "%020d-%06d-%04d.json" % (__import__("time").time_ns(), os.getpid() % 1000000, _seq[0] % 10000)
    body = {"event": doc, "env": {k: env[k] for k in SPOOL_ENV if isinstance(env.get(k), str)}}
    tmp = folder / (name + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False))
    os.replace(str(tmp), str(folder / name))
    return folder / name


def _private(folder: Path) -> bool:
    """Ours and nobody else's: anything another user could write there is never read."""
    try:
        st = folder.lstat()
    except OSError:
        return False
    import stat
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022


def fold_spool(ws: Path, at: datetime, folder: Optional[Path] = None) -> int:
    """Every spooled event into its card, oldest first, then deleted. Returns how many were folded."""
    folder = folder or spool_dir()
    if not folder.is_dir() or not _private(folder):
        return 0
    names = sorted(p for p in folder.glob("*.json"))
    for extra in names[:-SPOOL_MAX]:
        try:
            extra.unlink()
        except OSError:
            pass
    done = 0
    for path in names[-SPOOL_MAX:]:
        try:
            body = json.loads(path.read_text())
            if isinstance(body, dict) and isinstance(body.get("event"), dict):
                codex_event(ws, at, body["event"], body.get("env") if isinstance(body.get("env"), dict) else {})
                done += 1
        except (OSError, ValueError):
            pass
        try:
            path.unlink()
        except OSError:
            pass
    return done


def codex_event(ws: Path, at: datetime, doc, env=None) -> Optional[dict]:
    """One Codex hook event into its session's card; None when nothing was written. Codex's payload is Claude Code's
    shape: session_id, cwd, hook_event_name, plus last_assistant_message on Stop, tool_name and tool_input on
    PermissionRequest."""
    if not isinstance(doc, dict) or doc.get("hook_event_name") not in CODEX_EVENTS:
        return None
    sid, ev = doc.get("session_id"), doc["hook_event_name"]
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        return None
    path = floor_dir(ws) / (sid + ".json")
    if ev == "SessionEnd":
        try:
            path.unlink()
        except OSError:
            pass
        return None
    base = {"session_id": sid, "agent": "codex", "identify": True, "cwd": doc.get("cwd"), **({"env": env} if env is not None else {}),
            "asks": False, "question": "", "options": [], "waiting_on": None}
    if ev == "PostToolUse":   # an approval was given in Codex itself: the approval card is answered
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not (isinstance(old, dict) and old.get("asks") is True and str(old.get("question", "")).startswith("Approve ")):
            return None
        return write_card(ws, at, base)
    if ev == "PermissionRequest":
        return write_card(ws, at, dict(base, asks=True, question=_codex_ask(doc), waiting_on="approval in Codex",
                                       why_manager="Codex asks before it runs this"))
    if ev == "Stop":
        text = doc.get("last_assistant_message") if isinstance(doc.get("last_assistant_message"), str) else ""
        last = [l for l in text.splitlines() if l.strip() and not _WAITING_RE.match(l.strip())]
        return write_card(ws, at, dict(base, waiting_text=text[-WAITING_TEXT_MAX:], ask_from_waiting=True,
                                       recap=_line(" ".join(last[:3]), RECAP_MAX)))
    return write_card(ws, at, base)   # SessionStart, UserPromptSubmit: on the floor, not asking


def codex_hooks_toml(bin_path: str) -> str:
    cmd = '%s floor codex-hook' % bin_path
    rows = [CODEX_MARK]
    for ev in CODEX_EVENTS:
        rows += ["[[hooks.%s]]" % ev, "[[hooks.%s.hooks]]" % ev, 'type = "command"',
                 "command = %s" % json.dumps(cmd), "timeout = 5", ""]
    return "\n".join(rows)


def codex_setup(bin_path: str, config: Path, print_only: bool) -> str:
    """Append the hooks once. Codex runs a new or changed hook only after the user reviews it there (/hooks)."""
    block = codex_hooks_toml(bin_path)
    if print_only:
        return block
    text = config.read_text() if config.is_file() else ""
    if CODEX_MARK in text:
        return "already set up in %s" % config
    if not re.search(r"(?m)^\s*hooks\s*=\s*true", text):
        block = "# needs [features] hooks = true\n" + block
    config.parent.mkdir(parents=True, exist_ok=True)
    with open(config, "a") as f:
        f.write(("\n" if text and not text.endswith("\n") else "") + "\n" + block + "\n")
    return "added Codex hooks to %s; review them in Codex (/hooks) before they run" % config


def send(ws: Path, doc, run=None) -> dict:
    """Type `text` into the session's tmux pane, then Enter. ValueError when refused: no card, no pane, a pane that
    is gone, or empty text. The text is one line: a newline would submit early."""
    import subprocess
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=5))
    sid = doc.get("session_id") if isinstance(doc, dict) else None
    if not (isinstance(sid, str) and SESSION_ID_RE.match(sid)):
        raise ValueError("session_id must be a 36-character session id")
    text = _safe_line(doc.get("text"), TEXT_MAX)
    if not text:
        raise ValueError("nothing to send")
    try:
        card = json.loads((floor_dir(ws) / (sid + ".json")).read_text())
    except (OSError, ValueError):
        raise ValueError("no card for that session")
    t = identity(card if isinstance(card, dict) else {}).get("tmux")
    if not t:
        raise ValueError("that session has no tmux pane to type into; open it instead")
    base = ["tmux"] + (["-S", t["socket"]] if t.get("socket") else [])
    seen = run(base + ["display-message", "-p", "-t", t["pane"], "#{pane_id}"])
    if seen.returncode != 0 or seen.stdout.strip() != t["pane"]:
        raise ValueError("that session's tmux pane is gone")
    if run(base + ["send-keys", "-t", t["pane"], "-l", "--", text]).returncode != 0 or \
            run(base + ["send-keys", "-t", t["pane"], "Enter"]).returncode != 0:
        raise ValueError("tmux would not take the keys")
    return {"sent": True, "session_id": sid, "pane": t["pane"]}


def _bin_path() -> str:
    import shutil
    return shutil.which("mother") or str(Path(__file__).resolve().parent.parent / "bin" / "mother")


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
    if args == ["codex-hook"]:
        try:   # spool it: Codex's sandbox can't write the workspace; the next `floor list` folds it in
            spool_event(json.loads(sys.stdin.read() or "null"))
        except Exception:   # never block or break a Codex turn
            pass
        return 0
    if args[:1] == ["codex-setup"] and args[1:] in ([], ["--print"]):
        print(codex_setup(_bin_path(), Path.home() / ".codex" / "config.toml", args[1:] == ["--print"]))
        return 0
    if args == ["send", "--stdin"]:
        try:
            print(json.dumps(send(ws, json.loads(sys.stdin.read()))))
        except ValueError as e:
            print("mother floor send: %s" % e, file=sys.stderr)
            return 1
        return 0
    print(USAGE.strip(), file=sys.stderr)
    return 2
