"""`mother todo close|list`: the open-loop store's writer, so no agent hand-writes an archive line.

Grammar: rules/todos.md. An archived line is the open line with `- [x]` and one outcome token
(`done:YYYY-MM-DD` or `expired:YYYY-MM-DD`) inserted just before the origin token, which stays last.
Python 3.9 stdlib only.
"""
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

USAGE = """usage:
  mother todo close <match> [--expired] [--date YYYY-MM-DD]
      move the one item in todos/open.md whose text contains <match> (case-insensitive) to
      todos/archive.md as `- [x] ... `done:<date>` `~origin@date``; --expired writes `expired:` instead.
      0 or 2+ matches: nothing is written, the candidates are listed, exit 1.
      Runs the workspace's scripts/lint-todos.sh afterwards when it has one; exit 3 if that fails.
  mother todo list [--json]
      the items in todos/open.md, by section"""

ITEM_RE = re.compile(r"^- \[( |x)\] ")
ORIGIN_RE = re.compile(r"\s*(`~[^`\s]+@\d{4}-\d\d-\d\d`)\s*$")
OUTCOME_RE = re.compile(r"`(?:done|expired):[^`]*`")
DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
TEMPLATES = Path(__file__).resolve().parent.parent / "skills" / "todo" / "templates"


def _usage(code=2):
    print(USAGE, file=sys.stderr)
    sys.exit(code)


def _die(msg, code=1):
    print("mother todo: %s" % msg, file=sys.stderr)
    sys.exit(code)


def items(text: str) -> list:
    """[{n, section, line, checked, body}] for each item line; body is the line without checkbox and origin."""
    out, section = [], None
    for n, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = ITEM_RE.match(line)
        if not m:
            continue
        rest = line[m.end():]
        body = ORIGIN_RE.sub("", rest).strip()
        out.append({"n": n, "section": section, "line": line, "checked": m.group(1) == "x", "body": body})
    return out


def find(text: str, match: str) -> list:
    needle = match.strip().lower()
    return [it for it in items(text) if needle and needle in it["body"].lower()]


def archived(line: str, outcome: str, date: str) -> str:
    """The archive form of an open line; raises ValueError when the line is not well formed."""
    m = ITEM_RE.match(line)
    if not m:
        raise ValueError("not an item line: %s" % line)
    rest = line[m.end():].rstrip()
    o = ORIGIN_RE.search(rest)
    if not o:
        raise ValueError("the line does not end with its `~<origin>@YYYY-MM-DD` token, so it can't be archived "
                         "safely; fix it first: %s" % line)
    if OUTCOME_RE.search(rest):
        raise ValueError("the line already carries an outcome token: %s" % line)
    head = rest[:o.start()].rstrip()
    return "- [x] %s `%s:%s` %s" % (head, outcome, date, o.group(1))


def remove_line(text: str, n: int) -> str:
    """text without line n; a section left with no lines gets `_none_` (rules/todos.md)."""
    lines = text.splitlines()
    del lines[n]
    # the section the line was in: from its heading to the next heading
    start = n - 1
    while start >= 0 and not lines[start].startswith("## "):
        start -= 1
    end = n
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    if start >= 0 and not any(ln.strip() for ln in lines[start + 1:end]):
        lines.insert(start + 1, "_none_")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _write(path: Path, text: str):
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(text)
    os.replace(tmp, path)


def _lint(ws: Path):
    """(ran, ok, output) from the workspace's own scripts/lint-todos.sh, if it has one."""
    script = ws / "scripts" / "lint-todos.sh"
    if not script.is_file():
        return False, True, ""
    p = subprocess.run(["sh", str(script)], cwd=str(ws), capture_output=True, text=True)
    return True, p.returncode == 0, (p.stdout + p.stderr).strip()


def close(ws: Path, match: str, outcome: str, date: str) -> str:
    """Archive the one matching item and return its archived line; raises LookupError / ValueError."""
    todos = ws / "todos"
    open_md = todos / "open.md"
    if not open_md.is_file():
        raise LookupError("no todos/open.md in %s" % ws)
    lock = ws / ".mother" / "todos.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(lock, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        text = open_md.read_text()
        hits = find(text, match)
        if len(hits) != 1:
            cands = hits or items(text)
            head = ("no item in todos/open.md matches '%s'" % match) if not hits else \
                ("%d items in todos/open.md match '%s'; give a longer match" % (len(hits), match))
            raise LookupError("\n".join([head + (":" if cands else " (the list is empty)")]
                                        + ["  %s" % c["line"] for c in cands]))
        it = hits[0]
        line = archived(it["line"], outcome, date)
        arch = todos / "archive.md"
        if arch.is_file():
            cur = arch.read_text()
        else:
            tpl = TEMPLATES / "archive.md"
            cur = tpl.read_text() if tpl.is_file() else "# Archive\n"
        new_arch = cur + ("" if cur.endswith("\n") or not cur else "\n") + line + "\n"
        # archive first: a crash in between leaves the item in both files (lint reports it), never in neither
        _write(arch, new_arch)
        _write(open_md, remove_line(text, it["n"]))
    return line


def _today(ws: Path, now: _dt.datetime) -> str:
    tz = _dt.timezone.utc
    try:
        cfg = json.loads((ws / "config.json").read_text())
        from zoneinfo import ZoneInfo
        tz = ZoneInfo((cfg.get("manager") or {}).get("timezone") or "UTC")
    except Exception:
        pass
    return now.astimezone(tz).strftime("%Y-%m-%d")


def main(args, ws, now):
    if not args or args[0] in ("help", "-h", "--help"):
        _usage(0 if args else 2)
    verb, rest = args[0], args[1:]
    if verb == "list":
        if rest not in ([], ["--json"]):
            _usage()
        open_md = ws / "todos" / "open.md"
        if not open_md.is_file():
            _die("no todos/open.md in %s" % ws)
        its = items(open_md.read_text())
        if rest:
            print(json.dumps([{k: it[k] for k in ("section", "line", "checked")} for it in its], ensure_ascii=False))
            return 0
        section = object()
        for it in its:
            if it["section"] != section:
                section = it["section"]
                print("## %s" % section)
            print(it["line"])
        print("%d item%s" % (len(its), "" if len(its) == 1 else "s"))
        return 0
    if verb != "close":
        _usage()
    outcome, date, pos, i = "done", None, [], 0
    while i < len(rest):
        a = rest[i]
        if a == "--expired":
            outcome = "expired"
        elif a == "--date":
            if i + 1 >= len(rest):
                _usage()
            date = rest[i + 1]
            i += 1
        elif a.startswith("--date="):
            date = a.split("=", 1)[1]
        elif a.startswith("--"):
            _usage()
        else:
            pos.append(a)
        i += 1
    if len(pos) != 1 or not pos[0].strip():
        _usage()
    if date is None:
        date = _today(ws, now)
    elif not DATE_RE.match(date):
        _die("--date must be YYYY-MM-DD, got '%s'" % date, 2)
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        _die("--date is not a real date: '%s'" % date, 2)
    try:
        line = close(ws, pos[0], outcome, date)
    except (LookupError, ValueError) as e:
        _die(str(e))
    print(line)
    ran, ok, out = _lint(ws)
    if ran:
        print("lint-todos.sh: %s" % ("ok" if ok else "FAILED"))
        if not ok:
            print(out)
            return 3
    return 0
