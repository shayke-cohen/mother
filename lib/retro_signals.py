"""retro_signals: the model-free first stage of the weekly session retro (`mother retro`).

`mother retro signals` reads every Claude Code session transcript under ~/.claude/projects that moved in the last N
days and records, per session, where time, tokens and friction went: human turns, status pings ("?", "status?",
"are we progressing?"), corrections, interrupts, tool errors, permission denials, API errors, compactions, Stop-hook
blocks, wait and poll commands, repeated commands and in-window token volume. A sub-agent's transcript
(`<session>/subagents/*.jsonl`) is counted under the session that started it.

A fork's transcript starts with a replay of its parent's history, and its `subagents/` folder holds verbatim copies of
the parent's sub-agent files. Neither may count twice, so a line is skipped when it carries another session's
sessionId, or when its `uuid` also appears in another scanned transcript that owns it (see `fork_index`). The session
row records the parent in `forked_from`.

Raw rows stay outside git: `<state>/retro/<date>/sessions.jsonl` and `summary.json` (state = $MOTHER_STATE_DIR or
~/.mother). Transcripts can hold anything the manager pasted, so only the routine's distilled findings reach the
workspace. The summary is also printed to stdout.

`mother retro ledger <file>` folds the decision ledger (one JSON object per line; a later line for the same run and
item supersedes an earlier one) into what the next run must not propose again and what it must measure.

Python 3.9, stdlib only. Never imports bin/mother.
"""
import collections
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

USAGE = ("usage: mother retro signals [--days N] [--min-kb K] [--max-areas N] [--out DIR] [--projects DIR]\n"
         "       mother retro ledger <ledger.jsonl> [--weeks N]")

DEFAULT_DAYS = 7
DEFAULT_MIN_KB = 50
DEFAULT_MAX_AREAS = 4
TOP_PER_AREA = 10

CORRECTION = re.compile(
    r"^(no|nope|wrong|stop|not that|undo|revert)\b|"
    r"\b(that'?s (wrong|not (it|what))|not what i|you didn'?t|why did you|why are you|i (said|told you|asked( for| you)?)|"
    r"still (failing|broken|not)|again\?|doesn'?t work|didn'?t work|you missed|you forgot|please don'?t|"
    r"i meant|that'?s not)\b",
    re.I,
)
# The manager asking whether anything is happening: the main human-friction signal (corrections rarely look like
# "no" or "wrong"; a bare "?" after a long silence does).
PING_PART = re.compile(
    r"(status|progre+s+(ing)?|update|updates|eta|ping|nu+|and|so|well|done|merged|finished|ready|"
    r"(are|r) (we|you) (still )?(progre+s+ing|done|there|working|on it)( here| yet)?|"
    r"what('?s| is) (the )?(status|progress|update)|(any|what'?s the) (news|update|updates|progress)|"
    r"(is it|is that|did it) (done|ready|merged|work|pass)|still (working|going|there|on it)|"
    r"where are we( at| on this| with this)?|how'?s it going|how is it going|how far|what'?s happening|"
    r"what next|what now|now what|check (the )?status( and progress)?)",
    re.I,
)
# The session stopped and needed a push to go on.
NUDGE = re.compile(r"(\.+|,|continue|go( on| ahead)?|keep going|try again|retry|continue / retry|proceed|carry on|"
                   r"resume)[.!]*", re.I)


def is_status_ping(txt):
    """A message that only asks whether anything is happening: "?", "status?", "are we progressing? what next?".
    Every fragment must be a ping, and it must ask (a "?"), except "check status and progress"."""
    t = txt.strip().lstrip("❯").strip()
    if not t:
        return False
    if re.fullmatch(r"\?+", t):
        return True
    parts = [p.strip() for p in re.split(r"[?.!,;/]+", t) if p.strip()]
    if not parts or not all(PING_PART.fullmatch(p) for p in parts):
        return False
    return "?" in t or any(p.lower().startswith("check") for p in parts)


def is_nudge(txt):
    return bool(NUDGE.fullmatch(txt.strip().lstrip("❯").strip()))
DENIAL = re.compile(r"doesn't want to proceed|was rejected|permission (to use|denied)|denied by|blocked by (a )?hook|"
                    r"needs a person's approval", re.I)
WAIT_CMD = re.compile(r"\bsleep \d|gh (pr|run) (checks|watch|view)|--watch\b|until .*; do|while .*; do", re.I)
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
HASH = re.compile(r"^[0-9a-f]{6,}$")

COUNTS = ("human_turns", "corrections", "status_pings", "nudges", "interrupts", "tool_calls", "tool_errors", "denials",
          "api_errors", "compactions", "stop_blocks", "subagent_calls", "wait_cmds", "repeats", "friction",
          "sub_files", "sub_tool_calls", "sub_tool_errors", "lines_added")
FLOATS = ("tok_weighted_m", "main_tok_weighted_m", "sub_tok_weighted_m")
DELTA_KEYS = ("sessions", "human_turns", "status_pings", "nudges", "corrections", "interrupts", "tool_errors", "denials",
              "api_errors", "compactions", "stop_blocks", "wait_cmds", "repeats", "friction", "tok_weighted_m",
              "sub_tok_weighted_m", "unattended")
ROW_FIELDS = ("area", "session", "title", "first_prompt", "friction", "status_pings", "nudges", "corrections", "tool_errors",
              "stop_blocks", "wait_cmds", "tok_weighted_m", "sub_tok_weighted_m", "sub_files", "ctx_max", "wall_min",
              "forked_from", "file", "subagents_dir")


# ---------------------------------------------------------------- areas

_GITDIR_CACHE = {}


def _repo_from_git_file(path):
    """A linked worktree's `.git` file names the main repository: `gitdir: <repo>/.git/worktrees/<name>`."""
    if path in _GITDIR_CACHE:
        return _GITDIR_CACHE[path]
    repo = ""
    try:
        with open(os.path.join(path, ".git")) as f:
            line = f.readline().strip()
        m = re.match(r"gitdir:\s*(.+?)/\.git/worktrees/[^/]+/?$", line)
        if m:
            repo = os.path.basename(m.group(1).rstrip("/"))
    except OSError:
        pass
    _GITDIR_CACHE[path] = repo
    return repo


def _decode_dev3(enc, home):
    """dev3 keeps worktrees under ~/.dev3.0/worktrees/<repo path with / as ->/<hash>/worktree. The repo's own name
    may hold dashes, so take what follows the encoded home folder, and prefer a split that exists on disk."""
    parts = [p for p in enc.split("-") if p]
    home_parts = [p for p in home.strip("/").split("/") if p] if home else []
    if home_parts and parts[:len(home_parts)] == home_parts:
        rest = parts[len(home_parts):]
        for i in range(len(rest)):
            name = "-".join(rest[i:])
            if os.path.isdir(os.path.join(home, *rest[:i], name)):
                return name
        return "-".join(rest) if rest else "home"
    return parts[-1] if parts else ""


def repo_of(cwd, home=None):
    """The repository a session worked in, from its cwd: the folder name, with worktree paths collapsed onto the
    repository they belong to. Generic on purpose: no repository is named here."""
    if not cwd:
        return "other"
    home = (home if home is not None else str(Path.home())).rstrip("/")
    p = cwd.rstrip("/") or "/"
    m = re.search(r"/\.dev3\.0/worktrees/([^/]+)/([^/]+)/worktree(?:/|$)", p)
    if m and HASH.match(m.group(2)):
        return _decode_dev3(m.group(1), home) or "other"
    m = re.search(r"-dev3-0-worktrees-(.+)-([0-9a-f]{6,})-worktree$", p)   # the projects folder's encoded form
    if m:
        return _decode_dev3(m.group(1), home) or "other"
    m = re.match(r"^(.*?)/\.(?:claude|dev3)/worktrees/[^/]+", p)
    if m:
        p = m.group(1) or "/"
    if p == home:
        return "home"
    repo = _repo_from_git_file(p)
    return repo or os.path.basename(p) or "other"


def area_of(cwd, areas=None, home=None):
    """repo_of, then the optional `routines.retro.areas` map (derived name -> area) to merge or rename."""
    name = repo_of(cwd, home)
    if areas and isinstance(areas.get(name), str) and areas[name].strip():
        return areas[name].strip()
    return name


# ---------------------------------------------------------------- one transcript

def norm_cmd(cmd):
    cmd = cmd.strip().splitlines()[0] if cmd.strip() else ""
    cmd = re.sub(r"^(cd [^;&]+(&&|;)\s*)+", "", cmd)
    return " ".join(cmd.split()[:3])[:80]


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_human(o):
    """A turn the manager typed: not a sub-agent's, not a tool result, not a hook, skill or command expansion."""
    if o.get("isSidechain"):
        return False
    origin = o.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin.get("kind") == "human"
    content = (o.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    t = REMINDER.sub("", text_of(content)).lstrip()
    if not t or t.startswith(("<", "Base directory for this skill", "Caveat:", "Stop hook feedback")):
        return False
    return True


def new_record():
    return {"session": "", "file": "", "subagents_dir": "", "cwd": "", "title": "", "start": None, "end": None,
            "human_turns": 0, "corrections": 0, "status_pings": 0, "nudges": 0, "interrupts": 0, "tool_calls": 0,
            "tool_errors": 0, "denials": 0, "api_errors": 0, "compactions": 0, "stop_blocks": 0,
            "subagent_calls": 0, "wait_cmds": 0, "lines_added": 0, "first_prompt": "", "forked_from": "",
            "replayed": 0, "fork_marker": "", "lines_base": 0,
            "tok": [0, 0, 0, 0], "sub_tok": [0, 0, 0, 0], "ctx_max": 0, "sub_files": 0, "sub_tool_calls": 0,
            "sub_tool_errors": 0, "tools": collections.Counter(), "error_tools": collections.Counter(),
            "cmds": collections.Counter(), "foreign": collections.Counter()}


def weighted_m(tok):
    """In-window token volume in input-equivalent millions, weighted like list-price ratios: cache read 0.1x,
    cache write 1.25x, output 5x."""
    return round((tok[0] + 0.1 * tok[1] + 1.25 * tok[2] + 5 * tok[3]) / 1e6, 3)


def _stamp(s, ts):
    if not s["start"] or ts < s["start"]:
        s["start"] = ts
    if not s["end"] or ts > s["end"]:
        s["end"] = ts


def _marker(o):
    """The parent session a line's explicit `forkedFrom` marker names, or ""."""
    f = o.get("forkedFrom")
    if isinstance(f, dict):
        f = f.get("sessionId")
    return f if isinstance(f, str) else ""


def parse_file(path, s, own, cutoff_iso, sub=False, skip=None):
    """Fold one transcript into the session record `s`. `own` is the session id. A line under another sessionId is
    a fork's replay of its parent (or, in a sub-agent file, a copy of the parent's sub-agent) and is skipped, and so
    is a line whose uuid is in `skip` ({uuid: owning session}, from `fork_index`). A sub-agent file (`sub`) adds its
    tokens and tool calls to the sub_* counters and never counts as the manager's turns."""
    pending = {}
    seen_msg = set()
    skip = skip or {}
    in_replay = own_started = False
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            t = o.get("type")
            sid = o.get("sessionId")
            owner = skip.get(o.get("uuid")) if skip and isinstance(o.get("uuid"), str) else None
            if not owner and sid and sid != own:
                owner = sid
            # Running totals inside the replay are the parent's as of the fork point: the fork's own count starts there.
            if t == "cost-state" and not sub and (owner or (in_replay and not own_started)):
                s["lines_base"] = o.get("totalLinesAdded") or 0
            if owner:
                s["foreign"][owner] += 1
                s["replayed"] += 1
                in_replay = True
                continue
            if t == "cost-state" and not sub and in_replay and not own_started:
                continue
            if isinstance(o.get("uuid"), str):
                own_started = True
            if not sub and not s["fork_marker"] and _marker(o):
                s["fork_marker"] = _marker(o)
            ts = o.get("timestamp") if isinstance(o.get("timestamp"), str) else None
            old = bool(ts) and ts < cutoff_iso     # long-lived sessions: only events inside the window count
            if old and t != "assistant":
                continue
            if ts and not old and t in ("user", "assistant"):
                _stamp(s, ts)
            if not s["cwd"] and isinstance(o.get("cwd"), str):
                s["cwd"] = o["cwd"]
            if t == "custom-title" and not sub:
                s["title"] = str(o.get("customTitle") or "")[:100]
            elif t == "cost-state" and not sub:
                n = o.get("totalLinesAdded") or 0
                base = s["lines_base"] if isinstance(s["lines_base"], int) else 0
                s["lines_added"] = max(0, n - base) if isinstance(n, int) else 0
            elif t == "system" and not sub:
                st = o.get("subtype")
                if st == "api_error":
                    s["api_errors"] += 1
                elif st == "compact_boundary":
                    s["compactions"] += 1
            elif t == "assistant":
                msg = o.get("message") or {}
                u = msg.get("usage") or {}
                if u and not old and msg.get("id") not in seen_msg:
                    seen_msg.add(msg.get("id"))
                    tok = s["sub_tok"] if sub else s["tok"]
                    vals = (u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0,
                            u.get("cache_creation_input_tokens") or 0, u.get("output_tokens") or 0)
                    for i, v in enumerate(vals):
                        tok[i] += v if isinstance(v, int) else 0
                    if not sub:
                        s["ctx_max"] = max(s["ctx_max"], sum(v for v in vals[:3] if isinstance(v, int)))
                for b in msg.get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = str(b.get("name") or "?")
                    pending[b.get("id")] = name
                    if old:
                        continue
                    if sub:
                        s["sub_tool_calls"] += 1
                        continue
                    s["tool_calls"] += 1
                    s["tools"][name] += 1
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if name in ("Agent", "Task"):
                        s["subagent_calls"] += 1
                    if name == "Bash":
                        cmd = str(inp.get("command") or "")
                        s["cmds"][norm_cmd(cmd)] += 1
                        if WAIT_CMD.search(cmd):
                            s["wait_cmds"] += 1
                    if name in ("Monitor", "ScheduleWakeup"):
                        s["wait_cmds"] += 1
            elif t == "user":
                content = (o.get("message") or {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_result" and b.get("is_error"):
                            name = pending.get(b.get("tool_use_id"), "?")
                            rt = b["content"] if isinstance(b.get("content"), str) else text_of(b.get("content"))
                            if sub:
                                s["sub_tool_errors"] += 1
                            elif DENIAL.search(rt or ""):
                                s["denials"] += 1
                            else:
                                s["tool_errors"] += 1
                                s["error_tools"][name] += 1
                        elif b.get("type") == "text" and "[Request interrupted by user" in str(b.get("text")) \
                                and not sub:
                            s["interrupts"] += 1
                if sub:
                    continue
                raw = text_of(content).lstrip()
                if raw.startswith("Stop hook feedback"):
                    s["stop_blocks"] += 1
                elif is_human(o):
                    txt = REMINDER.sub("", text_of(content)).strip()
                    if "[Request interrupted by user" in txt:
                        if isinstance(content, str):
                            s["interrupts"] += 1
                        continue
                    s["human_turns"] += 1
                    if not s["first_prompt"]:
                        s["first_prompt"] = " ".join(txt.split())[:120]
                    if is_status_ping(txt):
                        s["status_pings"] += 1
                    elif is_nudge(txt):
                        s["nudges"] += 1
                    elif CORRECTION.search(txt[:300]):
                        s["corrections"] += 1


def finish(s, areas=None, home=None):
    """Derived fields; returns None for a session with no event inside the window."""
    if not s["start"]:
        return None
    s["area"] = area_of(s["cwd"], areas, home)
    if s["foreign"]:
        s["forked_from"] = s["foreign"].most_common(1)[0][0]
    elif s["fork_marker"]:
        s["forked_from"] = s["fork_marker"]
    s["main_tok_weighted_m"] = weighted_m(s["tok"])
    s["sub_tok_weighted_m"] = weighted_m(s["sub_tok"])
    s["tok_weighted_m"] = round(s["main_tok_weighted_m"] + s["sub_tok_weighted_m"], 3)
    s["tok_cache_read"] = s["tok"][1] + s["sub_tok"][1]
    try:
        a = dt.datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(s["end"].replace("Z", "+00:00"))
        s["wall_min"] = round((b - a).total_seconds() / 60, 1)
    except ValueError:
        s["wall_min"] = 0
    s["repeat_cmds"] = {c: n for c, n in s["cmds"].items() if n >= 4 and c}
    s["repeats"] = sum(n - 3 for n in s["repeat_cmds"].values())
    # Status pings weigh most: they are the manager waiting with no word from the session.
    s["friction"] = (3 * s["status_pings"] + s["nudges"] + 2 * s["corrections"] + 2 * s["interrupts"] + 2 * s["denials"]
                     + s["tool_errors"] + 2 * s["stop_blocks"] + s["api_errors"] + min(s["repeats"], 30)
                     + min(s["wait_cmds"], 30) // 3)
    s["tools"] = dict(s["tools"].most_common(8))
    s["error_tools"] = dict(s["error_tools"].most_common(8))
    for k in ("cmds", "foreign", "tok", "sub_tok", "fork_marker", "lines_base"):
        del s[k]
    return s


# ---------------------------------------------------------------- the scan

def find_sessions(projects, cutoff_ts):
    """{(project dir, session id): {"main": path or None, "subs": [paths], "bytes": n}} for files moved since the
    cutoff. Sub-agent transcripts sit under `<project>/<session id>/subagents/` (any depth)."""
    out = {}
    try:
        dirs = [d for d in Path(projects).iterdir() if d.is_dir()]
    except OSError:
        return out
    for d in dirs:
        for f in d.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff_ts:
                e = out.setdefault((str(d), f.stem), {"main": None, "subs": [], "bytes": 0})
                e["main"] = str(f)
                e["bytes"] += st.st_size
        for f in d.glob("*/subagents/**/*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff_ts:
                sid = f.relative_to(d).parts[0]
                e = out.setdefault((str(d), sid), {"main": None, "subs": [], "bytes": 0})
                e["subs"].append(str(f))
                e["bytes"] += st.st_size
    return out


FORK_PROBE = 8      # leading uuids per transcript compared to find a fork and its parent


def _uuid_lines(path, limit=None):
    """Yield (uuid, sessionId, timestamp, forkedFrom marker) for each line with a uuid; stop after `limit`."""
    n = 0
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if '"uuid"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or not isinstance(o.get("uuid"), str):
                continue
            ts = o.get("timestamp") if isinstance(o.get("timestamp"), str) else ""
            yield o["uuid"], o.get("sessionId") or "", ts, _marker(o)
            n += 1
            if limit and n >= limit:
                return


def _birth(path):
    try:
        st = os.stat(path)
    except OSError:
        return float("inf")
    return getattr(st, "st_birthtime", None) or float("inf")


def fork_index(files):
    """Which lines of which transcript belong to another one. `files` maps a transcript path to the session it
    counts under (a sub-agent file counts under its parent session). Returns {path: {uuid: owning session}}.

    A fork's replay starts at its first line and copies the parent's lines with their uuids (and, in every fork
    seen so far, the parent's sessionId too). Transcripts whose first FORK_PROBE uuids overlap are read in full, and
    each uuid found in more than one is owned by one of them: the only file whose session matches the line's
    sessionId; else the file that does not carry a `forkedFrom` marker, then the one whose first event is earliest,
    then the one created first, then path order."""
    first = collections.defaultdict(set)
    for p in files:
        for u, _, _, _ in _uuid_lines(p, FORK_PROBE):
            first[u].add(p)
    cand = sorted({p for ps in first.values() if len(ps) > 1 for p in ps})
    if not cand:
        return {}
    info, holders = {}, collections.defaultdict(list)
    for p in cand:
        uu, start, marked = {}, "", False
        for u, sid, ts, mk in _uuid_lines(p):
            uu.setdefault(u, sid)
            if ts and (not start or ts < start):
                start = ts
            marked = marked or bool(mk)
        info[p] = {"own": files[p], "uuids": uu, "rank": (marked, start or "~", _birth(p), p)}
        for u in uu:
            holders[u].append(p)
    out = {}
    for u, ps in holders.items():
        if len(ps) < 2:
            continue
        match = [p for p in ps if info[p]["uuids"][u] == info[p]["own"]]
        owner = match[0] if len(match) == 1 else min(match or ps, key=lambda p: info[p]["rank"])
        for p in ps:
            if p != owner and info[p]["own"] != info[owner]["own"]:
                out.setdefault(p, {})[u] = info[owner]["own"]
    return out


def scan(projects, days=DEFAULT_DAYS, min_kb=DEFAULT_MIN_KB, now=None, areas=None, home=None, parallel=True):
    """One row per session with an event in the last `days` days whose transcripts (its own file plus its
    sub-agents') add up to at least `min_kb`. Returns (rows, files_scanned)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    found = find_sessions(projects, cutoff.timestamp())
    owners = {}
    for (d, sid), e in found.items():     # every moved transcript, so a parent under --min-kb still owns its lines
        if e["main"]:
            owners[e["main"]] = sid
        for f in e["subs"]:
            owners[f] = sid
    replays = fork_index(owners)
    jobs = []
    for (d, sid), e in sorted(found.items()):
        if e["bytes"] >= min_kb * 1024:
            mine = [p for p in [e["main"]] + e["subs"] if p in replays]
            jobs.append((d, sid, e, cutoff_iso, areas, home, {p: replays[p] for p in mine}))
    files = sum((1 if j[2]["main"] else 0) + len(j[2]["subs"]) for j in jobs)
    results = None
    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    try:   # tests lower it to exercise the pool on a small fixture
        floor = int(os.environ.get("MOTHER_RETRO_PARALLEL_MIN") or 40)
    except ValueError:
        floor = 40
    if workers > 1 and len(jobs) > floor and parallel:
        try:
            import multiprocessing
            with multiprocessing.Pool(workers) as pool:
                results = list(pool.imap(_scan_one, jobs, chunksize=8))
        except Exception:      # no fork/spawn here (sandbox, frozen interpreter): the serial pass gives the same rows
            results = None
    if results is None:
        results = [_scan_one(j) for j in jobs]
    return [r for r in results if r], files


def _scan_one(job):
    d, sid, e, cutoff_iso, areas, home, replays = job
    s = new_record()
    s["session"] = sid
    if e["main"]:
        s["file"] = e["main"]
        parse_file(e["main"], s, sid, cutoff_iso, skip=replays.get(e["main"]))
    if e["subs"]:
        s["subagents_dir"] = os.path.join(d, sid, "subagents")
        for f in sorted(e["subs"]):
            before = (s["sub_tok"][:], s["sub_tool_calls"])
            parse_file(f, s, sid, cutoff_iso, sub=True, skip=replays.get(f))
            if (s["sub_tok"], s["sub_tool_calls"]) != before:
                s["sub_files"] += 1
        if not s["file"]:
            s["file"] = sorted(e["subs"])[0]
    return finish(s, areas, home)


def _round(v):
    return round(v, 3) if isinstance(v, float) else v


def summarize(rows, days, files, max_areas=DEFAULT_MAX_AREAS, generated=None):
    areas = collections.defaultdict(collections.Counter)
    err_tools = collections.defaultdict(collections.Counter)
    repeats = collections.defaultdict(collections.Counter)
    for r in rows:
        a = areas[r["area"]]
        a["sessions"] += 1
        for k in COUNTS + FLOATS:
            a[k] += r[k]
        a["wall_h"] += r["wall_min"] / 60
        a["unattended"] += 1 if r["human_turns"] == 0 else 0
        a["forks"] += 1 if r["forked_from"] else 0
        for t, n in r["error_tools"].items():
            err_tools[r["area"]][t] += n
        for c, n in r["repeat_cmds"].items():
            repeats[r["area"]][c] += n

    def row(r):
        return {k: r.get(k) for k in ROW_FIELDS}

    def top(key, n, pool=None):
        pool = rows if pool is None else pool
        return [row(r) for r in sorted(pool, key=lambda r: (r[key], r["tok_weighted_m"]), reverse=True)[:n] if r[key]]

    ordered = sorted(areas.items(), key=lambda kv: (-kv[1]["tok_weighted_m"], -kv[1]["sessions"], kv[0]))
    biggest = [k for k, _ in ordered[:max_areas]]
    return {
        "generated": generated or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": days, "files_scanned": files, "sessions": len(rows),
        "areas": {k: {kk: _round(vv) for kk, vv in v.items()} for k, v in ordered},
        "readers": biggest,
        "areas_top": {k: {"friction": top("friction", TOP_PER_AREA, [r for r in rows if r["area"] == k]),
                          "tokens": top("tok_weighted_m", TOP_PER_AREA, [r for r in rows if r["area"] == k])}
                      for k in biggest},
        "error_tools": {k: dict(v.most_common(8)) for k, v in err_tools.items()},
        "repeated_commands": {k: dict(v.most_common(10)) for k, v in repeats.items()},
        "top_friction": top("friction", 25),
        "top_tokens": top("tok_weighted_m", 10),
        "top_status_pings": top("status_pings", 10),
        "top_nudges": top("nudges", 10),
        "top_corrections": top("corrections", 10),
        "top_stop_blocks": top("stop_blocks", 10),
        "top_wait_loops": top("wait_cmds", 10),
        "top_repeats": top("repeats", 10),
    }


def previous_summary(root, today):
    """The newest `<root>/<date>/summary.json` dated before today, as (date, path, doc) or None."""
    try:
        dates = sorted(p.name for p in Path(root).iterdir()
                       if p.is_dir() and re.fullmatch(r"\d{4}-\d\d-\d\d", p.name) and p.name < today)
    except OSError:
        return None
    for d in reversed(dates):
        p = Path(root) / d / "summary.json"
        try:
            return d, str(p), json.loads(p.read_text())
        except (OSError, ValueError):
            continue
    return None


def delta(cur, prev):
    out = {}
    for area in sorted(set(cur.get("areas", {})) | set(prev.get("areas", {}))):
        a, b = cur.get("areas", {}).get(area, {}), prev.get("areas", {}).get(area, {})
        out[area] = {k: {"was": _round(b.get(k, 0)), "now": _round(a.get(k, 0))} for k in DELTA_KEYS
                     if a.get(k, 0) or b.get(k, 0)}
    return out


def output_paths(ws, today):
    """Where the routine writes in the workspace: the session-retro project when it exists, else retro/."""
    if not ws:
        return {"report": None, "ledger": None}
    proj = Path(ws) / "projects" / "session-retro"
    if proj.is_dir():
        return {"report": str(proj / "files" / ("%s-retro.md" % today)), "ledger": str(proj / "files" / "suggestions.jsonl")}
    return {"report": str(Path(ws) / "retro" / ("%s.md" % today)), "ledger": str(Path(ws) / "retro" / "suggestions.jsonl")}


# ---------------------------------------------------------------- the ledger

LEDGER_STATUSES = ("proposed", "queued", "in_progress", "pr", "applied", "merged", "rejected")


def _day(v):
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def ledger(path, today, weeks=8, root=None, current=None):
    """Fold the ledger and classify each run+item: `suppress` (never propose again: rejected, or applied/merged
    within `weeks`), `open` (decided, not done: queued, in_progress, pr), `proposed` (not decided yet) and `measure`
    (applied/merged within `weeks`: the metric the next run reports on). An entry's optional `metric`
    {area, key} is looked up in the summary of the run that proposed it (`<root>/<run>/summary.json`) and in
    `current`, the newest summary, so `was` and `now` come from code, not from a model."""
    entries, order, bad = {}, [], 0
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(o, dict) or "run" not in o or "item" not in o:
            bad += 1
            continue
        key = (str(o["run"]), str(o["item"]))
        if key not in entries:
            order.append(key)
            entries[key] = {}
        entries[key].update(o)
    out = {"entries": len(order), "bad_lines": bad, "suppress": [], "open": [], "proposed": [], "measure": []}
    horizon = today - dt.timedelta(weeks=weeks)
    for key in order:
        e = entries[key]
        st = str(e.get("status") or "proposed")
        decided = _day(e.get("decided")) or _day(e.get("run"))
        brief = {k: e.get(k) for k in ("run", "item", "title", "status", "decided", "note", "target")
                 if e.get(k) is not None}
        recent = decided is not None and decided >= horizon
        if st == "rejected":
            out["suppress"].append(dict(brief, reason="rejected"))
        elif st in ("applied", "merged") and recent:
            out["suppress"].append(dict(brief, reason="%s within %d weeks" % (st, weeks)))
            m = dict(brief, measure_next_run=e.get("measure_next_run"))
            metric = e.get("metric")
            if isinstance(metric, dict) and metric.get("area") and metric.get("key"):
                m["metric"] = {"area": metric["area"], "key": metric["key"]}
                if root:
                    base = Path(root) / str(e["run"]) / "summary.json"
                    try:
                        was = json.loads(base.read_text()).get("areas", {}).get(metric["area"], {}).get(metric["key"])
                    except (OSError, ValueError):
                        was = None
                    m["was"] = was
                if current is not None:
                    m["now"] = current.get("areas", {}).get(metric["area"], {}).get(metric["key"], 0)
            out["measure"].append(m)
        elif st in ("queued", "in_progress", "pr"):
            out["open"].append(brief)
        elif st == "proposed":
            out["proposed"].append(brief)
    return out


# ---------------------------------------------------------------- CLI

def _opts(args, spec):
    """Parse `--name value` pairs from `spec` {name: type}; returns (opts, positional) or raises ValueError."""
    opts, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name = a[2:]
            if name not in spec or i + 1 >= len(args):
                raise ValueError("unknown option or missing value: %s" % a)
            try:
                opts[name] = spec[name](args[i + 1])
            except ValueError:
                raise ValueError("bad value for %s: %s" % (a, args[i + 1]))
            i += 2
        else:
            pos.append(a)
            i += 1
    return opts, pos


def main(args, cfg=None, root=None, now=None, ws=None, out=sys.stdout):
    """`mother retro signals|ledger`. `cfg` is the workspace config ({} without one), `root` the data folder
    (<state>/retro), `now` an aware UTC datetime, `ws` the workspace path or None. Returns the exit code."""
    cfg = cfg or {}
    now = now or dt.datetime.now(dt.timezone.utc)
    root = Path(root or Path.home() / ".mother" / "retro")
    opts_cfg = ((cfg.get("routines") or {}).get("retro") or {}) if isinstance(cfg, dict) else {}
    if not args or args[0] in ("help", "-h", "--help"):
        print(USAGE, file=out)
        return 0
    verb, rest = args[0], args[1:]
    today = now.astimezone().date().isoformat()
    try:
        if verb == "signals":
            o, pos = _opts(rest, {"days": float, "min-kb": int, "max-areas": int, "out": str, "projects": str})
            if pos:
                raise ValueError("unexpected argument: %s" % pos[0])
            days = o.get("days", opts_cfg.get("window_days", DEFAULT_DAYS))
            min_kb = o.get("min-kb", opts_cfg.get("min_kb", DEFAULT_MIN_KB))
            max_areas = o.get("max-areas", opts_cfg.get("max_areas", DEFAULT_MAX_AREAS))
            if days <= 0 or min_kb < 0 or max_areas < 1:
                raise ValueError("--days and --max-areas must be positive, --min-kb not negative")
            projects = Path(o.get("projects") or Path.home() / ".claude" / "projects").expanduser()
            dest = Path(o["out"]).expanduser() if o.get("out") else root / today
            rows, files = scan(projects, days, min_kb, now, opts_cfg.get("areas"))
            summary = summarize(rows, days, files, max_areas, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            prev = previous_summary(dest.parent, dest.name)
            summary["previous"] = {"date": prev[0], "path": prev[1]} if prev else None
            summary["delta"] = delta(summary, prev[2]) if prev else {}
            summary["out"] = str(dest)
            summary.update(output_paths(ws, today))
            dest.mkdir(parents=True, exist_ok=True)
            tmp = dest / "sessions.jsonl.tmp"
            with open(tmp, "w") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, dest / "sessions.jsonl")
            tmp = dest / "summary.json.tmp"
            tmp.write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
            os.replace(tmp, dest / "summary.json")
            out.write(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
            return 0
        if verb == "ledger":
            o, pos = _opts(rest, {"weeks": int})
            if len(pos) != 1:
                raise ValueError("ledger takes one file")
            cur = previous_summary(root, "9999-99-99")
            doc = ledger(pos[0], now.date(), o.get("weeks", 8), root, cur[2] if cur else None)
            doc["current"] = {"date": cur[0], "path": cur[1]} if cur else None
            out.write(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
            return 0
    except ValueError as e:
        print("mother retro: %s\n%s" % (e, USAGE), file=sys.stderr)
        return 2
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
