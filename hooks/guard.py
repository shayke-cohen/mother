#!/usr/bin/env python3
"""MOTHER approval guard: a PreToolUse hook that enforces rules/outbound.md in code.

Prompt rules can be lost to context compaction or overridden by injected content; a hook cannot. For every
tool call that sends, posts or writes somewhere another person can see, the guard answers:

  deny   the kill switch <workspace>/.mother/PAUSED exists, or the call is on the never-list
         (credential entry, deletions, and any category the manager moved to `policy.never`)
  allow  a green action: a chat message to `manager.chat_dm`, a mail draft, a calendar event with no
         guests; a chat/tracker write covered by a `mother approve tracker|chat <target>` line at the top
         of a genuine user message in this session (12 h); or a valid run grant (.mother/grants.json)
  ask    every other outbound call: the human confirms it in the UI. In a session whose permission_mode is
         bypassPermissions, these get no output instead (logged as "pass") outside the workspace, or anywhere
         when policy.bypass_passes_asks is true; denies and the self-edit, grants.json and persona-file asks stay.
         In a workspace session, network programs, web fetches, browser navigation and MCP writes the guard
         can't place are outbound_unknown (ask; PAUSED and dry run deny), and a shell command runs unasked only
         when every stage is on the shell allow-list (else shell_unlisted: ask). Grants count only when signed
         with the grant key, which no file tool may touch.

WhatsApp works like chat: `mother whatsapp send --to WHO --text …` asks, showing the recipient and the text (a
send to the manager's own chat is `own_dm`, allowed). Any agent touch of the bridge socket or its state folder
is deny, as are `mother whatsapp setup|link|unlink` (the manager's own terminal only).

Calls that are not outbound get no output, so Claude Code's normal permission flow applies. The guard never
answers "allow" for a call it does not recognise: an allow would skip the user's own permission rules.

Outbound tools are recognised by capability, not by MCP server name: the tool's short name is normalised
(camelCase and kebab-case to snake_case, product words dropped) and matched against verbs per domain (chat,
mail, calendar, tracker, code host, docs). The domain comes from words in the tool name, then the server
segment, then the shape of the input. `gh` / `glab` write commands are matched inside Bash.

Fail closed: no workspace or an invalid config.json means no green list and no grants (every outbound call
asks); an unreadable payload asks; a crash while classifying asks. Every outbound decision is appended to
<workspace>/.mother/guard.log (target and reason only, never message text).

Contract: docs/CONTRACT.md section 5. Python 3.9 stdlib only.
"""
import datetime as dt
import json
import os
import re
import sys
import time

try:  # not on Windows; the ledger then works without a lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

TAG = "[mother guard] "
HERE = os.path.dirname(os.path.realpath(__file__))

# ------------------------------------------------------------------ policy vocabulary (rules/outbound.md)

DEFAULT_AUTO = ["own_dm", "drafts", "own_calendar_no_guests"]
DEFAULT_NEEDS_YES = ["any_message_to_others", "tracker_writes", "calendar_with_guests", "code_host_comments",
                     "standing_rules", "docs_writes", "mail_trash"]
ALWAYS_NEVER = ["credentials", "deletions", "payments"]

# Grant scopes an unattended run may hold, with the ceiling on uses per cited source message.
GRANT_SCOPES = {"chat-post": 3, "chat-reply": 3, "answer": 1, "tracker-create": 5, "tracker-comment": 5,
                "tracker-transition": 10}
GRANT_MAX_AGE_HOURS = 96

# ------------------------------------------------------------------ small helpers


def out(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": TAG + reason,
    }}))


def read_json(path):
    """(value, ok). ok is False when the file exists but cannot be parsed."""
    if not os.path.exists(path):
        return None, True
    try:
        with open(path) as f:
            return json.load(f), True
    except Exception:
        return None, False


def as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)] if v != "" else []


def first(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def now_iso():
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(v):
    """Epoch seconds from a number, a numeric string (chat ts) or an ISO-8601 UTC string; None if invalid."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    try:
        return float(s)
    except ValueError:
        pass
    s = s.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z"):
        try:
            return dt.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def project_of_key(key):
    m = re.match(r"^([A-Z][A-Z0-9_]+)-\d+$", str(key or ""))
    return m.group(1) if m else None


# ------------------------------------------------------------------ workspace and config


class Ctx(object):
    """Everything the decision needs from the workspace. `ok` False means: no green list, no grants."""

    def __init__(self):
        self.ws = None
        self.cfg = {}
        self.ok = False
        self.problem = "no workspace"
        self.stamps = set()
        self.sid = None  # the session id of the call, set by run()


def resolve_workspace():
    p = os.environ.get("MOTHER_WORKSPACE", "").strip()
    if not p:
        try:
            with open(os.path.join(os.path.expanduser("~"), ".mother", "workspace")) as f:
                p = f.readline().strip()
        except Exception:
            p = ""
    if not p:
        return None
    p = os.path.expanduser(p)
    return os.path.realpath(p) if os.path.isabs(p) and os.path.isdir(p) else None


def load_ctx():
    c = Ctx()
    c.ws = resolve_workspace()
    if not c.ws:
        return c
    cfg, ok = read_json(os.path.join(c.ws, "config.json"))
    if not ok or not isinstance(cfg, dict):
        c.problem = "config.json missing or unreadable"
        return c
    if cfg.get("version") != 1:
        c.problem = "config.json version is not 1"
        return c
    mgr = cfg.get("manager")
    if not isinstance(mgr, dict) or not as_list(mgr.get("chat_dm")):
        c.problem = "config.json has no manager.chat_dm"
        return c
    for key in ("policy", "agent", "connectors"):
        if key in cfg and not isinstance(cfg[key], dict):
            c.problem = "config.json %s is not an object" % key
            return c
    if "teams" in cfg and not isinstance(cfg["teams"], list):
        c.problem = "config.json teams is not a list"
        return c
    c.cfg, c.ok, c.problem = cfg, True, None
    c.stamps = load_stamps(c.ws)
    return c


def load_stamps(ws):
    """Server prefixes (capabilities.json -> connectors.<vendor>.stamps) of chat connectors that add their own
    attribution. Anything missing or malformed gives an empty set: today's decisions stay as they are."""
    stamps = set()
    try:
        caps, ok = read_json(os.path.join(ws, "capabilities.json"))
        conns = caps.get("connectors") if ok and isinstance(caps, dict) else None
        if isinstance(conns, dict):
            for v in conns.values():
                lst = v.get("stamps") if isinstance(v, dict) else None
                if isinstance(lst, list):
                    stamps |= {x.rstrip("_") for x in lst if isinstance(x, str) and x.rstrip("_")}
    except Exception:
        return set()
    return stamps


def dm_ids(cfg):
    """Targets that are the manager's own DM: manager.chat_dm (str or list) plus manager.chat_user."""
    m = cfg.get("manager") or {}
    return set(as_list(m.get("chat_dm"))) | set(as_list(m.get("chat_user")))


def owner_id(cfg):
    """The manager's own chat user id: manager.chat_user if set, else the first manager.chat_dm entry."""
    m = cfg.get("manager") or {}
    ids = as_list(m.get("chat_user")) or as_list(m.get("chat_dm"))
    return ids[0] if ids else None


def team_channels(cfg):
    """{channel id: channel dict} across all teams."""
    found = {}
    for t in cfg.get("teams") or []:
        if not isinstance(t, dict):
            continue
        for ch in t.get("channels") or []:
            if isinstance(ch, dict) and ch.get("id"):
                found[str(ch["id"])] = ch
    return found


def tracker_projects(cfg):
    found = set()
    for t in cfg.get("teams") or []:
        if isinstance(t, dict) and isinstance(t.get("tracker"), dict) and t["tracker"].get("project"):
            found.add(str(t["tracker"]["project"]))
    return found


def tracker_boards(cfg):
    found = set()
    for t in cfg.get("teams") or []:
        if isinstance(t, dict) and isinstance(t.get("tracker"), dict) and t["tracker"].get("board") is not None:
            found.add(str(t["tracker"]["board"]))
    return found


def policy_lists(cfg):
    """(auto, never). Overrides only tighten: auto can only shrink, never can only grow."""
    pol = cfg.get("policy") or {}
    auto = set(DEFAULT_AUTO)
    if isinstance(pol.get("auto_without_asking"), list):
        auto &= set(as_list(pol["auto_without_asking"]))
    never = set(ALWAYS_NEVER) | set(as_list(pol.get("never")))
    auto -= never
    return auto, never


def grant_limits(cfg):
    """(scopes {name: max uses}, max age hours) after policy.grants tightening."""
    g = (cfg.get("policy") or {}).get("grants")
    g = g if isinstance(g, dict) else {}
    if g.get("enabled") is False:
        return {}, 0
    scopes = dict(GRANT_SCOPES)
    if isinstance(g.get("scopes"), dict):
        allowed = {}
        for name, lim in g["scopes"].items():
            if name not in scopes:
                continue
            cap = scopes[name]
            if isinstance(lim, dict) and isinstance(lim.get("max_uses_per_source"), int):
                cap = max(0, min(cap, lim["max_uses_per_source"]))
            allowed[name] = cap
        scopes = allowed
    age = GRANT_MAX_AGE_HOURS
    if isinstance(g.get("max_age_hours"), (int, float)) and not isinstance(g.get("max_age_hours"), bool):
        age = max(0, min(age, g["max_age_hours"]))
    return scopes, age


# ------------------------------------------------------------------ classification

PRODUCT_WORDS = {"slack", "jira", "gmail", "google", "outlook", "linear", "atlassian", "msteams", "gcal",
                 "github", "gitlab", "monday", "teams"}
DOMAIN_WORDS = [
    ("chat", {"slack", "teams", "msteams", "chat", "discord", "mattermost", "messenger"}),
    ("mail", {"gmail", "mail", "email", "outlook", "inbox"}),
    ("calendar", {"calendar", "gcal", "cal", "event", "events"}),
    ("tracker", {"jira", "linear", "tracker", "monday", "atlassian", "issue", "issues", "ticket", "tickets"}),
    ("code", {"github", "gitlab", "pull", "pr", "mr", "repo", "repository"}),
    ("docs", {"confluence", "notion", "wiki"}),
]
READ_VERBS = {"get", "list", "search", "find", "read", "fetch", "download", "lookup", "query", "check",
              "describe", "show", "count", "suggest", "view", "export", "semantic", "explain", "available"}
DELETE_VERBS = {"delete", "destroy", "purge", "erase", "wipe"}
WRITE_VERBS = {"create", "post", "send", "reply", "schedule", "update", "edit", "patch", "put", "set", "add",
               "remove", "move", "transition", "comment", "assign", "link", "log", "attach", "bulk", "close",
               "reopen", "archive", "change", "rank", "invite", "kick", "join", "leave", "rename", "pin",
               "unpin", "share", "upload", "complete", "merge", "approve", "publish", "forward", "respond",
               "rsvp", "insert", "quick", "cancel", "execute", "submit", "push", "fork", "lock", "unlock",
               "transfer", "mark", "react", "convert", "start", "end", "invoke", "trigger", "run", "write",
               "trash", "spam", "junk"}

# Extra write words, used only for an MCP call no domain claims (outbound_unknown, CONTRACT §5).
UNKNOWN_WRITE_VERBS = {"copy", "batch", "save", "store", "import", "sync", "deploy", "notify", "broadcast",
                       "duplicate", "clone", "emit", "dispatch", "enqueue", "commit", "release"}
URLISH_RE = re.compile(r"://|\bwww\.|\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/\S")
# Programs that reach the network by themselves; named at a command position in a workspace session, the command
# is outbound_unknown whatever its arguments (CONTRACT §5).
NET_PROGS = ("curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet",
             "http", "https", "httpie", "xh")
# Words that run the rest of the command (skipped to find the program; their later words are checked too).
SH_WRAPPERS = {"env", "sudo", "doas", "command", "builtin", "exec", "nohup", "time", "nice", "xargs", "timeout",
               "watch", "caffeinate", "parallel", "then", "do", "else", "elif", "if", "while", "until", "!", "{",
               "}", "stdbuf", "unbuffer", "ionice", "chronic", "noglob", "nocorrect"}
SH_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
SH_INTERP_RE = re.compile(r"^(?:python[0-9.]*|node|nodejs|perl[0-9.]*|ruby|php[0-9.]*|osascript|deno|bun|swift)$")
# Inline interpreter code counts only when it can reach the network or start a process (CONTRACT §5).
NET_MARKERS_RE = re.compile(r"socket|urllib|http|requests|httpx|fetch|subprocess|system|popen|pty|/dev/(?:tcp|udp)"
                            r"|curl|wget|smtplib|ftplib|telnet|net::|lwp|open-uri|child_process|spawn|exec|`", re.I)
NET_MODULES_RE = re.compile(r"^(?:http|urllib|smtplib|ftplib|telnetlib|socketserver|webbrowser|SimpleHTTPServer"
                            r"|xmlrpc|pip)\b")
MOTHER_ARG_RE = re.compile(r"(?:^|/)(?:bin/)?mother$")
MOTHER_HEAD_RE = re.compile(r"^\s*(?:python3?\s+)?[\"']?(?:\$\{?CLAUDE_PLUGIN_ROOT\}?/bin/|[\w./-]*/bin/)?mother[\"']?\s[^;&|`$()]*$")
PLUGIN_CMD_RE = re.compile(r"^\"?\$\{?CLAUDE_PLUGIN_ROOT\}?\"?/")
# Shell writes beyond SHELL_WRITE_RE that can land a protected file (archives, patches, git checkouts).
EXTRA_WRITE_RE = re.compile(r"\b(?:tar|bsdtar|unzip|ditto|patch|cpio|pax|mkdir|git\s+(?:checkout|restore|reset"
                            r"|apply|am|stash|clone|pull|merge|cherry-pick|rebase|worktree))\b")
GRANTS_CLI_RE = re.compile(r"\bgrant\s+(?:add\b|\S*[$`])")
SECRET_RE = re.compile(
    r"xox[abposr]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}")
SENSITIVE_KEYS = re.compile(r"^(password|passwd|passphrase|pin_code|cvv|cvc|card_number|cardnumber|ssn|iban)$", re.I)

GH_DELETE_RE = re.compile(
    r"\b(?:gh|glab)\s+(?:issue|pr|mr|release|gist|repo|label|run|variable|cache|project|ruleset|snippet)\s+delete\b")
GH_CRED_RE = re.compile(r"\b(?:gh|glab)\s+(?:secret|variable|ssh-key|gpg-key|auth)\s+(?:set|add|login|create)\b")
GH_WRITE_RE = re.compile(
    r"\bgh\s+(?:(?:issue|pr)\s+(?:create|comment|review|edit|close|reopen|merge|ready|lock|unlock|transfer|develop)"
    r"|release\s+(?:create|edit|upload)|gist\s+(?:create|edit)|repo\s+(?:create|edit|rename|fork|archive|sync)"
    r"|label\s+(?:create|edit|clone)|workflow\s+(?:run|enable|disable)|run\s+(?:rerun|cancel)"
    r"|project\s+(?:create|edit|item-add|item-edit|close))\b"
    r"|\bglab\s+(?:(?:issue|mr)\s+(?:create|note|comment|approve|close|reopen|merge|update|revoke|subscribe)"
    r"|release\s+(?:create|update|upload)|repo\s+(?:create|fork|archive|transfer)|label\s+create"
    r"|ci\s+(?:run|retry|cancel|trigger))\b")
API_RE = re.compile(r"\b(?:gh|glab)\s+api\b")
API_WRITE_RE = re.compile(
    r"(?:-X|--method)\s*['\"]?(?:POST|PATCH|PUT|DELETE)\b|\s(?:-f|-F|--field|--raw-field|--input)[\s=]", re.I)
API_DELETE_RE = re.compile(r"(?:-X|--method)\s*['\"]?DELETE\b", re.I)
# A GraphQL call always sends -f, so API_WRITE_RE alone flags every read. Only an inline query literal that
# opens with `query` or `{` is a read; a mutation, a query from a file or a variable stays a write.
GRAPHQL_READ_RE = re.compile(
    r"\b(?:gh|glab)\s+api\s+graphql\b.*?(?:-f|-F|--field|--raw-field)[\s=]+['\"]?query=['\"]?\s*(?:query\b|\{)")
GRAPHQL_UNSAFE_RE = re.compile(r"\bmutation\b|--input\b|(?:-X|--method)\s*['\"]?(?:PATCH|PUT|DELETE)\b", re.I)
API_GET_RE = re.compile(r"(?:-X|--method)\s*['\"]?GET\b", re.I)
GRAPHQL_FILE_RE = re.compile(r"(?:-f|-F|--field|--raw-field)[\s=]+['\"]?query=@([^\s'\"]+)")
CMD_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")
# Text the shell will not run: quoted strings and quoted-delimiter heredoc bodies (commit messages, PR bodies,
# prompts). Blanked before matching, unless the command hands text to another interpreter.
EXEC_RE = re.compile(r"(?:^|[\s;&|(`$])(?:bash|sh|zsh|eval|xargs|ssh|parallel|watch|env|osascript|python3?|node|ruby"
                     r"|perl|source|chmod)(?=[\s;&|)]|$)|(?:^|[;&|(\n])\s*(?:\.{1,2}/|/)[^\s;&|]+")
# Writing a script and running it (`cat > x.sh <<'EOF' … EOF; chmod +x x.sh; ./x.sh`): heredoc bodies stay.
SCRIPT_RUN_RE = re.compile(r"(?:^|[\s;&|(])(?:chmod|source|bash|sh|zsh|\.)\s|(?:^|[;&|(\n])\s*(?:\.{1,2}/|/)[^\s;&|]+")
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
SHELL_WRITE_RE = re.compile(r">|\b(?:rm|mv|cp|ln|tee|truncate|chmod|chown|unlink|touch|sed\s+-i|dd|install|rsync"
                            r"|git\s+(?:checkout|restore|reset|rm|mv|apply|stash))\b")
SAFE_REDIRECT_RE = re.compile(r"\d*>>?\s*/dev/(?:null|stdout|stderr)\b|&>>?\s*/dev/null\b|\d*>&\d|<[^<>\s]+@[^<>\s]+>")
SCRIPT_RE = re.compile(r"(?:^|[\s;&|(`$])(?:python3?|node|ruby|perl)(?=[\s;&|)]|$)")
SCRIPT_WRITE_RE = re.compile(
    r"\.write\(|write_text|write_bytes|open\([^)]*['\"][wax]\+?b?['\"]|os\.(?:remove|unlink|rename|replace|symlink"
    r"|rmdir|truncate)|shutil\.|\.unlink\(|\.rename\(|\.touch\(|json\.dump\(|subprocess|os\.system|writeFile"
    r"|appendFile|fs\.\w*(?:write|unlink|rename|copy|rm)|rmSync|File\.(?:write|delete)|\bperl\s+-\w*i")
POINTER_READ_RE = re.compile(r"\bcat\s+[\"']?(?:~|\$\{?HOME\}?|/[^\s\"']*)/\.mother/workspace[\"']?")
POINTER_RE = re.compile(r"(?:~|\$\{?HOME\}?|/Users/[^/\s]+|/home/[^/\s]+)/\.mother/workspace(?![\w/.-])", re.I)


def tokens_of(name):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def domain_from(tokens):
    ts = set(tokens)
    for dom, words in DOMAIN_WORDS:
        if ts & words:
            return dom
    return None


def domain_from_shape(inp):
    if first(inp, "channel", "channel_id", "channelId", "chat_id", "chatId", "conversation_id"):
        return "chat"
    if first(inp, "to", "recipients", "cc", "bcc") and first(inp, "subject", "body", "html", "raw"):
        return "mail"
    if first(inp, "threadId", "thread_id", "messageId", "message_id", "messageIds", "draftId", "labelIds"):
        return "mail"  # mail tools behind an opaque server prefix (e.g. trash_thread {threadId})
    if first(inp, "projectKey", "project_key", "issueKey", "issueIdOrKey", "issue_key", "issueUpdates", "board_id",
             "boardId", "sprintId"):
        return "tracker"
    if first(inp, "attendees", "attendeeEmails", "calendarId", "calendar_id", "eventId", "event_id"):
        return "calendar"
    nested = inp.get("inputs") if isinstance(inp.get("inputs"), dict) else None
    if nested and first(nested, "issueIdOrKey", "issueKey", "projectKey"):
        return "tracker"
    return None


def text_of(inp):
    v = first(inp, "text", "message", "body", "comment", "markdown", "content", "blocks")
    if v is None and isinstance(inp.get("inputs"), dict):
        v = first(inp["inputs"], "text", "body", "comment", "commentBody", "content")
    if v is None:
        return ""
    return v if isinstance(v, str) else json.dumps(v)


def attendees_of(inp):
    found = []
    for key in ("attendees", "attendeeEmails", "guests", "invitees"):
        v = inp.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s]+", v) if x]
        if not isinstance(v, list):
            return None  # an unreadable guest list is treated as "has guests"
        for a in v:
            e = a.get("email") if isinstance(a, dict) else a
            if e:
                found.append(str(e).strip().lower())
    return found


def has_attendee_field(inp):
    return any(k in inp for k in ("attendees", "attendeeEmails", "guests", "invitees"))


def act(kind, category, what, **kw):
    a = {"kind": kind, "category": category, "what": what}
    a.update(kw)
    return a


def protected_paths(ctx):
    ps = {os.path.join(HERE, "guard.py"), os.path.join(HERE, "hooks.json"),
          os.path.join(os.path.expanduser("~"), ".mother", "workspace")}
    if ctx.ws:
        m = os.path.join(ctx.ws, ".mother")
        ps |= {os.path.join(ctx.ws, "config.json"), os.path.join(m, "PAUSED"), os.path.join(m, "ledger.json"),
               os.path.join(m, "guard.log")}
    return {_cf(p) for p in ps}  # casefolded: macOS file systems ignore case (Config.json is config.json)


# Files a later session obeys as standing instructions (CONTRACT §5): an edit to one always asks, bypass included.


def grants_path(ctx):
    return _cf(os.path.join(ctx.ws, ".mother", "grants.json")) if ctx.ws else None


def home_settings_path(fp):
    """~/.claude/settings.json, ~/.claude/settings.local.json, ~/.claude.json, or anything under ~/.claude/plugins/."""
    home = os.path.realpath(os.path.expanduser("~")).casefold()
    f = fp.casefold()
    c = os.path.join(home, ".claude")
    return f in (os.path.join(c, "settings.json"), os.path.join(c, "settings.local.json"),
                 os.path.join(home, ".claude.json")) or inside(f, os.path.join(c, "plugins"))


def persona_path(fp, ctx):
    """fp (real path) is the workspace's AGENTS.md, CLAUDE.md, .mcp.json, memory/MEMORY.md or under .claude/."""
    if not ctx.ws:
        return False
    ws = _cf(ctx.ws)
    f = fp.casefold()
    if not inside(f, ws) or f == ws:
        return False
    rel = os.path.relpath(f, ws).replace(os.sep, "/")
    parts = rel.split("/")
    return rel in ("agents.md", ".mcp.json", "memory/memory.md") or parts[-1] == "claude.md" or ".claude" in parts \
        or ".git" in parts  # git runs hooks and config (fsmonitor, aliases) from any .git folder


def graphql_file_is_query(seg, cwd):
    """`-F query=@file`: a read when the file holds a query and no mutation."""
    m = GRAPHQL_FILE_RE.search(seg)
    if not m:
        return False
    p = os.path.join(cwd or os.getcwd(), os.path.expanduser(os.path.expandvars(m.group(1))))
    try:
        with open(p) as f:
            q = f.read(65536)
    except Exception:
        return False
    q = re.sub(r"(?m)^\s*#.*$", "", q)
    return bool(re.match(r"\s*(?:query\b|\{)", q)) and not re.search(r"\bmutation\b", q)


def api_write(cmd, cwd=None):
    """A `gh api` / `glab api` write anywhere in the command line. GETs and read-only GraphQL queries pass."""
    if not (API_RE.search(cmd) and API_WRITE_RE.search(cmd)):
        return False
    unsafe = GRAPHQL_UNSAFE_RE.search(cmd)
    for seg in CMD_SPLIT_RE.split(cmd):
        if not (API_RE.search(seg) and API_WRITE_RE.search(seg)):
            continue
        if API_GET_RE.search(seg):
            continue
        if not unsafe and (GRAPHQL_READ_RE.search(seg) or graphql_file_is_query(seg, cwd)):
            continue
        return True
    return False


# ------------------------------------------------------------------ WhatsApp (CONTRACT §5, §5d)
# A send asks like a chat message (own chat: own_dm). Everything that could go around that ask — the bridge
# socket, its state, a send that isn't a plain command of its own — is denied. Paths are compared casefolded
# (macOS file systems ignore case), commands after stripping quotes and backslashes.

WA_PASS_SUBS = ("read", "chats", "status", "help", "install", "stop", "-h", "--help")
WA_TERMINAL_SUBS = ("setup", "link", "unlink")  # the manager's own terminal only: they link WhatsApp to MOTHER
_WA_BIN = (r"(?:python3?[ \t]+)?(?:\"\$\{CLAUDE_PLUGIN_ROOT\}/bin/mother\"|\$\{CLAUDE_PLUGIN_ROOT\}/bin/mother"
           r"|\"[^\"$`\\\n]*/mother\"|'[^'\n]*/mother'|(?:[\w.~+-]*/)*mother)")
# One argument of a passing subcommand: plain text, $VAR / ${VAR}, or a quoted string without $( ) or backticks.
_WA_ARG = r"""(?:[^\s;&|<>`$()\\'"]|\$\{?[A-Za-z_]\w*\}?|"(?:[^"`$\\\n]|\$\{?[A-Za-z_]\w*\}?)*"|'[^'\n]*')+"""
_WA_REDIR = r"[ \t]*(?:[0-9]?(?:<<<|<<-?|<|>>?|>\|)|&>>?)[ \t]*%s|[ \t]*[0-9]?[<>]&[ \t]*(?:[0-9]+|-)" % _WA_ARG
WA_PASS_RE = re.compile(r"^%s[ \t]+whatsapp(?:[ \t]+(?:%s)(?:[ \t]+%s)*)?(?:%s)*\Z" % (
    _WA_BIN, "|".join(re.escape(x) for x in WA_PASS_SUBS), _WA_ARG, _WA_REDIR))
WA_ENV_PREFIX_RE = re.compile(r"([A-Za-z_]\w*)=(?:%s)?[ \t]+" % _WA_ARG)  # a value as a passing argument
WA_ENV_CODE_RE = re.compile(r"(?:PATH|ENV|BASH_ENV|PYTHON\w*|DYLD_\w*|LD_\w*|PERL5\w*|NODE_OPTIONS)\Z", re.I)
# The reader sub-agent's one command: plain or quoted arguments (a chat name may hold spaces), nothing computed.
WA_READ_RE = re.compile(r"^%s[ \t]+whatsapp[ \t]+read(?:[ \t]+(?:[^\s;&|<>`$()\\'\"]|'[^'\n]*'|\"[^\"`$\\\n]*\")+)*\Z"
                        % _WA_BIN)
WA_STATE_RE = re.compile(r"bridge\.sock|\.mother/whatsapp\b|whatsapp/(?:session|app)(?=/|$|[\s;&|)])"
                         r"|whatsapp/sent\.jsonl", re.I)
WA_STATE_FILES = ("sent.jsonl", "bridge.sock", "session", "app")
WA_SEND_OPTS = ("--to", "--text")
# A recipient or text with these can't be shown as it is sent: NUL cuts an argv string, the rest hide or reorder.
# Characters the prompt can't show as sent: categories Cc (but \n, \t), Cf (but U+200D, the joiner emoji
# sequences use), Co, Cs, Cn, and the Hangul fillers, which render blank. U+FE0E/U+FE0F are Mn and pass.
WA_HIDDEN_CATS = ("Cc", "Cf", "Co", "Cs", "Cn")
WA_HIDDEN_OK = frozenset("\n\t\u200d")
WA_HIDDEN_EXTRA = frozenset("\u115f\u1160\u3164\uffa0\u00ad\u034f\u2028\u2029")  # + grapheme joiner, line/para separators
# Shell forms the guard would read differently from the shell: unquoted ~, globs and brace lists; $'…' escapes
# that make raw bytes (\x80-\xff, octal 200-377) or cut the string (\c).
WA_UNQUOTED_RISK_RE = re.compile(r"[*?\[]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
WA_ANSI_C_RE = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
WA_ANSI_C_RISK_RE = re.compile(r"\\(?:x[89a-fA-F][0-9a-fA-F]?|[23][0-7]{2}|c)")
WA_SEND_HEAD_RE = re.compile(r"%s[ \t]+whatsapp[ \t]+send(?:[ \t]|\Z)" % _WA_BIN)


def wa_norm(s):
    """The command as the shell will mostly see it: quotes and backslashes gone, casefolded."""
    return re.sub(r"[\"'\\]", "", s).casefold()


def wa_tokens(norm):
    return [t for t in re.split(r"[\s;&|<>()`]+", norm) if t]


def wa_names_cli(norm):
    """The normalised text names the `mother` CLI next to the word whatsapp (in any quoting or case)."""
    toks = wa_tokens(norm)
    return any(t.rstrip("/").split("/")[-1] == "mother" for t in toks) and any("whatsapp" in t for t in toks)


def wa_state_root():
    return os.path.expanduser(os.environ.get("MOTHER_STATE_DIR", "").strip() or "~/.mother")


def wa_dirs(ctx):
    """The folders only `mother whatsapp` and the bridge write: <state>/whatsapp and the default
    ~/.mother/whatsapp."""
    return [os.path.join(wa_state_root(), "whatsapp"), os.path.join(os.path.expanduser("~"), ".mother", "whatsapp")]


def _cf(path):
    return os.path.realpath(path).casefold()


def wa_inside(path, ctx):
    rp = _cf(path)
    return any(inside(rp, _cf(d)) for d in wa_dirs(ctx))


def wa_protected(ctx):
    """Every protected WhatsApp path, as written and resolved, casefolded, for glob matching."""
    found = set()
    for d in wa_dirs(ctx):
        for p in [d] + [os.path.join(d, k) for k in WA_STATE_FILES]:
            found |= {os.path.abspath(p).casefold(), os.path.realpath(p).casefold()}
    return found


# -- Reading a shell line (T5-h): the gate keys on shapes, not spellings. A command name the guard can't compute
# is denied; every argument has its unresolved expansions read as wildcards and is matched against the protected
# paths; destructive or copying commands are also matched against the folders that hold them. A deliberately
# evasive agent can still get past a reader like this one (CONTRACT §5d, "What the guard stops").

WA_RESERVED = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac"}
WA_WRAPPERS = {  # commands that run the rest of their arguments as a command; the options that take a value
    "env": ("-u", "--unset", "-C", "--chdir", "-P"), "command": (), "exec": ("-a",), "nohup": (), "time": (),
    "sudo": ("-u", "-g", "-h", "-p", "-C", "-D", "-U", "-r", "-t", "-T"), "doas": ("-u", "-C"), "nice": ("-n",),
    "timeout": ("-s", "-k", "--signal", "--kill-after"), "gtimeout": ("-s", "-k", "--signal", "--kill-after"),
    "xargs": ("-I", "-n", "-L", "-P", "-s", "-E", "-d", "-a", "-J", "-R", "-S"), "builtin": (),
    "stdbuf": ("-i", "-o", "-e"), "caffeinate": ("-t", "-w"), "unbuffer": (), "chronic": (),
    "watch": ("-n", "--interval"), "ionice": ("-c", "-n", "-p"), "taskpolicy": ("-c", "-d", "-g", "-b")}
WA_SCRIPT_OPTS = ("-t", "-T", "-F", "-O", "-I", "-B", "-E", "-o", "-m", "--log-out", "--log-in", "--log-io",
                  "--log-timing", "--echo", "--output-limit", "--logging-format")
WA_PTY_CMDS = {"expect", "tmux", "screen", "dtach", "abduco", "ptyexec", "pty", "rlwrap", "empty"}
WA_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
WA_INTERPS = {"python", "python2", "python3", "perl", "ruby", "node", "nodejs", "deno", "bun", "osascript", "php",
              "lua", "swift", "tclsh", "pwsh", "rscript", "awk", "gawk", "nawk", "mawk"}
WA_CODE_FLAGS = {"-c", "-e", "-E", "--eval", "-p", "--print", "-r"}
WA_SOCKET_CMDS = {"nc", "ncat", "netcat", "socat"}
WA_DESTRUCTIVE = {"rm", "rmdir", "unlink", "mv", "cp", "rsync", "tar", "ditto", "zip", "unzip", "ln", "chmod",
                  "chown", "chflags", "shred", "trash", "srm"}
WA_FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
_WA_REDIR_OP = re.compile(r"(\d*)(&>>|&>|<<<|<<-|<<|<>|<&|>&|>>|>\||<|>)")
_WA_SEP = re.compile(r"[()=:,'\"`;\s]+")
_WA_GLOB = re.compile(r"[*?]|\[.*\]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
_WA_SEND_WORDS = re.compile(r"(?:^|[\s/])mother\s+whatsapp\s+send\b")


def _wa_ansi_c(t):
    """The text of a $'…' string."""
    simple = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "e": "\x1b",
              "E": "\x1b", "\\": "\\", "'": "'", '"': '"', "?": "?"}

    def rep(m):
        e = m.group(1)
        if e in simple:
            return simple[e]
        try:
            if e[0] in "xuU" and len(e) > 1:
                return chr(int(e[1:], 16))
            if e[0] in "01234567":
                return chr(int(e, 8))
        except (ValueError, OverflowError):
            return ""
        return "" if e[0] == "c" else "\\" + e
    return re.sub(r"\\(x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|[0-7]{1,3}|c.|.)", rep, t, flags=re.S)


class _ShLex(object):
    """Reads one shell line into simple commands: {"words", "redirs", "heredocs", "start", "end"}. A word is a
    list of parts: ("t", text, q) literal text, q False unquoted, True quoted, "e" backslash-escaped; ("v", name,
    default, q) a variable; ("s", text, q) a command or process substitution; ("x", text, q) any other expansion.
    Never raises: anything unterminated runs to the end of the line."""

    STOP = " \t\n;&|()<>"

    def __init__(self, s, depth=0):
        self.s, self.i, self.n, self.depth = s, 0, len(s), depth

    @staticmethod
    def _new():
        return {"words": [], "redirs": [], "heredocs": [], "start": None, "end": None}

    def _close(self, segs, cur):
        if cur["words"] or cur["redirs"]:
            segs.append(cur)
        return self._new()

    def commands(self, stop_paren=False):
        s, segs, heredocs, depth = self.s, [], [], 0
        cur = self._new()
        while self.i < self.n:
            c = s[self.i]
            if c in " \t":
                self.i += 1
            elif c == "\\" and s.startswith("\n", self.i + 1):
                self.i += 2
            elif c == "\n":
                self.i += 1
                cur = self._close(segs, cur)
                for h in heredocs:
                    self._body(*h)
                heredocs = []
            elif c == "#":
                j = s.find("\n", self.i)
                self.i = self.n if j < 0 else j
            elif c in "<>" and s.startswith("(", self.i + 1):
                cur["start"] = self.i if cur["start"] is None else cur["start"]
                cur["words"].append(self._word())
                cur["end"] = self.i
            else:
                m = _WA_REDIR_OP.match(s, self.i) if (c in "<>&" or c.isdigit()) else None
                if m:
                    cur["start"] = self.i if cur["start"] is None else cur["start"]
                    self.i = m.end()
                    while self.i < self.n and s[self.i] in " \t":
                        self.i += 1
                    w = self._word() if self.i < self.n and s[self.i] not in self.STOP else []
                    cur["end"] = self.i
                    if m.group(2) in ("<<", "<<-"):
                        heredocs.append((cur, w, m.group(2) == "<<-"))
                    else:
                        cur["redirs"].append((m.group(2), w))
                elif c == "(":
                    depth += 1
                    self.i += 1
                    cur = self._close(segs, cur)
                elif c == ")":
                    if stop_paren and depth == 0:
                        break
                    depth = max(depth - 1, 0)
                    self.i += 1
                    cur = self._close(segs, cur)
                elif c in ";&|":
                    while self.i < self.n and s[self.i] in ";&|":
                        self.i += 1
                    cur = self._close(segs, cur)
                else:
                    cur["start"] = self.i if cur["start"] is None else cur["start"]
                    cur["words"].append(self._word())
                    cur["end"] = self.i
        self._close(segs, cur)
        return segs

    def _body(self, seg, delim, strip):
        """A heredoc body: the lines up to its delimiter."""
        word = "".join(p[1] for p in delim if p[0] == "t")
        quoted = any(p[0] == "t" and p[2] for p in delim)
        lines = []
        while self.i < self.n:
            j = self.s.find("\n", self.i)
            j = self.n if j < 0 else j
            ln = self.s[self.i:j]
            self.i = min(j + 1, self.n)
            if (ln.lstrip("\t") if strip else ln) == word:
                break
            lines.append(ln)
        seg["heredocs"].append(("\n".join(lines), quoted))

    def _word(self):
        s, parts, buf = self.s, [], []

        def flush():
            if buf:
                parts.append(("t", "".join(buf), False))
                del buf[:]
        while self.i < self.n:
            c = s[self.i]
            if c in "<>" and s.startswith("(", self.i + 1) and not parts and not buf:
                self.i += 2
                parts.append(("s", self._paren(), False))
            elif c in self.STOP:
                break
            elif c == "\\":
                flush()
                if self.i + 1 < self.n and s[self.i + 1] != "\n":
                    parts.append(("t", s[self.i + 1], "e"))
                self.i += 2
            elif c == "'":
                flush()
                j = s.find("'", self.i + 1)
                j = self.n if j < 0 else j
                parts.append(("t", s[self.i + 1:j], True))
                self.i = j + 1
            elif c == '"':
                flush()
                self.i += 1
                self.dquote(parts, '"')
            elif c == "$":
                flush()
                self._dollar(parts, False)
            elif c == "`":
                flush()
                parts.append(("s", self._backtick(), False))
            else:
                buf.append(c)
                self.i += 1
        flush()
        return parts

    def dquote(self, parts, end):
        """Double-quoted text up to `end` (None: to the end, as in a heredoc body)."""
        s, buf = self.s, []

        def flush():
            if buf:
                parts.append(("t", "".join(buf), True))
                del buf[:]
        while self.i < self.n:
            c = s[self.i]
            if c == end:
                self.i += 1
                break
            if c == "\\" and self.i + 1 < self.n and s[self.i + 1] in '$`"\\\n':
                if s[self.i + 1] != "\n":
                    buf.append(s[self.i + 1])
                self.i += 2
            elif c == "$":
                flush()
                self._dollar(parts, True)
            elif c == "`":
                flush()
                parts.append(("s", self._backtick(), True))
            else:
                buf.append(c)
                self.i += 1
        flush()

    def _dollar(self, parts, q):
        s, i = self.s, self.i
        nx = s[i + 1:i + 2]
        if nx == "'" and not q:
            j = i + 2
            while j < self.n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            parts.append(("t", _wa_ansi_c(s[i + 2:min(j, self.n)]), True))
            self.i = j + 1
        elif nx == '"' and not q:
            self.i = i + 2
            self.dquote(parts, '"')
        elif s.startswith("((", i + 1):
            self.i, d = i + 3, 2
            while self.i < self.n and d:
                d += {"(": 1, ")": -1}.get(s[self.i], 0)
                self.i += 1
            parts.append(("x", s[i + 3:self.i], q))
        elif nx == "(":
            self.i = i + 2
            parts.append(("s", self._paren(), q))
        elif nx == "{":
            j, d = i + 2, 1
            while j < self.n:
                ch = s[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch in "'\"":
                    k = s.find(ch, j + 1)
                    j = self.n if k < 0 else k + 1
                    continue
                d += {"{": 1, "}": -1}.get(ch, 0)
                if not d:
                    break
                j += 1
            body = s[i + 2:min(j, self.n)]
            self.i = min(j + 1, self.n)
            m = re.match(r"([A-Za-z_]\w*)(?:(:?[-=])(.*))?\Z", body, re.S)
            parts.append(("v", m.group(1), m.group(3), q) if m else ("x", body, q))
        else:
            m = re.compile(r"[A-Za-z_]\w*").match(s, i + 1)
            if m:
                parts.append(("v", m.group(0), None, q))
                self.i = m.end()
            elif nx and nx in "0123456789@*#?$!-":
                parts.append(("x", nx, q))
                self.i = i + 2
            else:
                parts.append(("t", "$", q))
                self.i = i + 1

    def _paren(self):
        """The text of a $( … ) (self.i just past the opening paren), read with heredocs and quotes."""
        start = self.i
        if self.depth > 40:
            self.i = self.n
            return self.s[start:]
        sub = _ShLex(self.s, self.depth + 1)
        sub.i = start
        sub.commands(stop_paren=True)
        self.i = min(sub.i + 1, self.n)
        return self.s[start:sub.i]

    def _backtick(self):
        j = self.i + 1
        while j < self.n and self.s[j] != "`":
            j += 2 if self.s[j] == "\\" else 1
        inner = self.s[self.i + 1:min(j, self.n)].replace("\\`", "`")
        self.i = j + 1
        return inner


def _wa_glob_atoms(text, active, braces):
    """Path-pattern atoms for literal text: ("c", ch); ("seg",) a * within one folder; ("one",) a ?;
    ("cls", body) a [..]; ("any",) a brace group (it may hold a /)."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if active and ch == "*":
            out.append(("seg",))
        elif active and ch == "?":
            out.append(("one",))
        elif active and ch == "[" and "]" in text[i + 2:]:
            j = text.index("]", i + 2)
            out.append(("cls", text[i + 1:j]))
            i = j + 1
            continue
        elif braces and ch == "{" and "}" in text[i + 1:] and re.match(r"[^{}]*(?:,|\.\.)", text[i + 1:]):
            j = text.index("}", i + 1)
            out.append(("any",))
            i = j + 1
            continue
        else:
            out.append(("c", ch))
        i += 1
    return out


class _WaGlob(object):
    """A path pattern from _wa_glob_atoms, matched casefolded in time linear in the pattern times the text (a
    regex of many wildcards backtracks past the hook's timeout). Runs of wildcards are collapsed first."""

    def __init__(self, atoms):
        self.atoms = []
        for a in atoms:
            if a[0] == "c":
                self.atoms += [("c", ch) for ch in a[1].casefold()]
            elif a[0] == "cls":
                body = a[1].casefold()
                neg = body[:1] in ("!", "^")
                body = (body[1:] if neg else body).replace("\\", "\\\\").replace("[", "\\[")
                for pair in ("--", "&&", "~~", "||"):  # set operations to a future re: read them literally
                    body = body.replace(pair, pair[0] + "\\" + pair[1])
                try:
                    rx = re.compile("[%s%s]" % ("^" if neg else "", body or "\\]"), re.S)
                except re.error:
                    rx = re.compile("[^/]")
                self.atoms.append(("cls", rx))
            elif a[0] in ("seg", "any") and self.atoms and self.atoms[-1][0] in ("seg", "any"):
                if a[0] == "any":
                    self.atoms[-1] = ("any",)  # ** or *{a,b}: one wildcard that may cross a /
            else:
                self.atoms.append((a[0],))
        self.n = len(self.atoms)

    def _close(self, states):
        out, todo = set(), list(states)
        while todo:
            k = todo.pop()
            if k in out:
                continue
            out.add(k)
            if k < self.n and self.atoms[k][0] in ("seg", "any"):
                todo.append(k + 1)
        return out

    def fullmatch(self, text):
        states = self._close({0})
        for ch in text:
            nxt = set()
            for k in states:
                if k == self.n:
                    continue
                a = self.atoms[k]
                if a[0] == "c":
                    if a[1] == ch:
                        nxt.add(k + 1)
                elif a[0] == "one":
                    if ch != "/":
                        nxt.add(k + 1)
                elif a[0] == "seg":
                    if ch != "/":
                        nxt.add(k)
                elif a[0] == "any":
                    nxt.add(k)
                elif a[1].match(ch):
                    nxt.add(k + 1)
            if not nxt:
                return False
            states = self._close(nxt)
        return self.n in states


class _WaHit(Exception):
    def __init__(self, action):
        Exception.__init__(self, action["what"])
        self.action = action


class _WaScan(object):
    """The WhatsApp gate's reading of one Bash command (CONTRACT §5d). run() raises _WaHit on the first problem.
    A resolved word is a list of tokens: ("L", text, q) literal text (q as in _ShLex, plus "v": from an unquoted
    variable, so split and globbed) or ("W",) something the guard can't compute."""

    def __init__(self, cmd, cwd, ctx):
        self.cmd, self.ctx = cmd, ctx
        self.home = os.path.expanduser("~")
        self.ws = ctx.ws or os.environ.get("MOTHER_WORKSPACE", "").strip() or None
        self.known = {"HOME": [("L", self.home, True)], "MOTHER_STATE_DIR": [("L", wa_state_root(), True)],
                      "CLAUDE_PLUGIN_ROOT": [("L", os.path.dirname(HERE), True)]}
        if self.ws:
            self.known["MOTHER_WORKSPACE"] = [("L", self.ws, True)]
        self.bases = [os.path.realpath(cwd)] if cwd else []  # None: a folder the guard can't compute
        self.protected = sorted(wa_protected(ctx))
        anc = [os.path.join(self.home, ".mother"), wa_state_root()]
        if self.ws:
            anc += [self.ws, os.path.join(self.ws, ".mother")]
        self.near = {f(p).casefold() for p in anc for f in (os.path.abspath, os.path.realpath)}
        far = set()
        for d in wa_dirs(ctx) + anc:
            for f in (os.path.abspath, os.path.realpath):
                p = os.path.dirname(f(d))
                while p and p != os.path.dirname(p):
                    far.add(p.casefold())
                    p = os.path.dirname(p)
        self.far = self.near | far  # every folder above the protected ones, but not /
        self.depth = 0
        self.runner = False   # a shell or interpreter reads code from a pipe or heredoc
        self.opaque = False   # something runs that the guard can't compute (a command name, an eval string)
        self.wa_word = False  # some word, expansions as *, could be `whatsapp`
        self.skeleton = []    # the line's unquoted words

    def hit(self, op, what):
        raise _WaHit(wa_act(op, what))

    def run(self):
        self.line(self.cmd, top=True)
        if self.opaque and self.wa_word:
            self.hit("computed", "a command the guard can't compute on a line that could be a whatsapp command")
        if _WA_SEND_WORDS.search(" ".join(self.skeleton).casefold()):
            self.hit("compound", "a whatsapp send that is not a command of its own")
        if self.runner:
            n = wa_norm(self.cmd)
            if _WA_SEND_WORDS.search(n):
                self.hit("compound", "a whatsapp send fed to a shell or interpreter")
            if WA_STATE_RE.search(n):
                self.hit("state", "WhatsApp's state paths fed to a shell or interpreter")

    def line(self, text, top=False):
        self.depth += 1
        if self.depth > 12:
            self.hit("unknown", "a shell line nested too deeply to read")
        for seg in _ShLex(text).commands():
            self.segment(seg, text, top)
        self.depth -= 1

    def subs_in(self, text):
        """Run the substitutions inside free text (a ${…} body, an unquoted heredoc) through line()."""
        self.depth += 1
        if self.depth > 12:
            self.hit("unknown", "a shell line nested too deeply to read")
        lx, parts = _ShLex(text), []
        lx.dquote(parts, None)
        for p in parts:
            if p[0] == "s":
                self.line(p[1])
            elif p[0] == "x" or (p[0] == "v" and p[2]):
                self.subs_in(p[1] if p[0] == "x" else p[2])
        self.depth -= 1

    # -- words

    @staticmethod
    def plain(word):
        return "".join(p[1] for p in word) if word and all(p[0] == "t" and p[2] is False for p in word) else None

    def resolve(self, word):
        out = []
        for k, p in enumerate(word):
            kind, q = p[0], p[-1]
            if kind == "t":
                text = p[1]
                if k == 0 and q is False and (text == "~" or text.startswith("~/")):
                    out.append(("L", self.home, True))
                    text = text[1:]
                if text:
                    out.append(("L", text, q))
            elif kind == "v":
                if p[2]:
                    self.subs_in(p[2])
                val = self.known.get(p[1])
                if val is None:
                    out.append(("W", p[1], q))  # the name, so a command name can still come from os.environ
                else:
                    out.extend(("L", t[1], True if q else "v") if t[0] == "L" else t for t in val)
            elif kind == "s":
                self.line(p[1])  # a substitution runs: it is a command line of its own
                v = self.subst_value(p[1])
                out.append(("L", v, True) if v else ("W",))
            else:
                self.subs_in(p[1])
                out.append(("W",))
        return out

    def subst_value(self, inner):
        """$(cat ~/.mother/workspace) and $(mother path) are the workspace; nothing else is computed."""
        t = " ".join(inner.split())
        if self.ws and (re.match(r"cat (?:~|\$HOME|\$\{HOME\})/\.mother/workspace\Z", t)
                        or re.match(r"(?:python3? )?\S*mother path\Z", t)):
            return self.ws
        return None

    @staticmethod
    def split(toks):
        """Word splitting: only the text an unquoted variable brought in splits."""
        if not any(t[0] == "L" and t[2] == "v" and re.search(r"\s", t[1]) for t in toks):
            return [toks]
        words, cur = [], []
        for t in toks:
            if t[0] == "L" and t[2] == "v":
                for b in re.split(r"(\s+)", t[1]):
                    if b and b.isspace():
                        if cur:
                            words.append(cur)
                        cur = []
                    elif b:
                        cur.append(("L", b, "v"))
            else:
                cur.append(t)
        if cur:
            words.append(cur)
        return words

    @staticmethod
    def value(toks):
        return None if any(t[0] == "W" for t in toks) else "".join(t[1] for t in toks)

    @staticmethod
    def computed(toks):
        return any(t[0] == "W" or (t[2] in (False, "v") and _WA_GLOB.search(t[1])) for t in toks)

    @staticmethod
    def atoms(toks):
        """Path-pattern atoms for a resolved word: unresolved parts are ("any",)."""
        out = []
        for t in toks:
            out += [("any",)] if t[0] == "W" else _wa_glob_atoms(t[1], True, t[2] in (False, "v"))
        return out

    def note_word(self, toks):
        """T5-j (b): a word that, expansions as wildcards, could be `whatsapp` (a lone wildcard can't)."""
        if self.wa_word or not toks or all(t[0] == "W" for t in toks):
            return
        atoms = self.atoms(toks)
        if all(a[0] in ("any", "seg") for a in atoms):
            return
        if _WaGlob(atoms).fullmatch("whatsapp"):
            self.wa_word = True

    def maybe_mother(self, toks):
        """T5-j (a): a computed command name whose last path part could be `mother`. A name that is only one
        expansion doesn't count (that is `$EDITOR`, decided by (b))."""
        if len(toks) == 1 and toks[0][0] == "W":
            return False
        atoms = self.atoms(toks)
        cut = max([k + 1 for k, a in enumerate(atoms) if a == ("c", "/")] or [0])
        return _WaGlob(atoms[cut:]).fullmatch("mother")

    def unreadable(self, toks_list, what):
        """Text that runs (an eval or -c string) but can't be computed: denied when its literal part names
        mother or whatsapp, else it counts as something opaque on the line."""
        lits = "".join(t[1] for toks in toks_list for t in toks if t[0] == "L")
        if re.search(r"mother|whatsapp", lits, re.I):
            self.hit("computed", what)
        self.opaque = True

    def assignment(self, toks):
        if not toks or toks[0][0] != "L" or toks[0][2] is not False:
            return False
        m = re.match(r"([A-Za-z_]\w*)\+?=", toks[0][1])
        if not m:
            return False
        rest = toks[0][1][m.end():]
        val = []
        if rest.startswith("~") and (len(rest) == 1 or rest[1] == "/"):
            val, rest = [("L", self.home, True)], rest[1:]
        val += ([("L", rest, False)] if rest else []) + [("L", t[1], True) if t[0] == "L" and t[2] == "v" else t
                                                        for t in toks[1:]]
        self.arg(val, False)
        self.known[m.group(1)] = val
        return True

    # -- commands

    def segment(self, seg, text, top):
        words = seg["words"]
        if top:
            self.skeleton += ["".join(p[1] for p in w if p[0] == "t" and p[2] in (False, "e")) for w in words]
        i = 0
        while i < len(words) and self.plain(words[i]) in WA_RESERVED:
            i += 1
        head = self.plain(words[i]) if i < len(words) else None
        if head in ("for", "select", "case", "function"):
            for w in words[i + 1:]:
                for t in self.split(self.resolve(w)):
                    self.note_word(t)
                    self.arg(t, False)
            if head in ("for", "select") and i + 1 < len(words):
                self.known.pop(self.plain(words[i + 1]) or "", None)
        else:
            argv, assigning = [], True
            for w in words[i:]:
                toks = self.resolve(w)
                if assigning and self.assignment(toks):
                    continue
                assigning = False
                argv.extend(self.split(toks))
            for t in argv:
                self.note_word(t)
            self.command(argv, seg, text, None)
        for op, w in seg["redirs"]:
            toks = self.resolve(w)
            if op in (">&", "<&") and re.match(r"(?:\d+|-)?\Z", self.value(toks) or "x"):
                continue
            self.arg(toks, False)
        for body, quoted in seg["heredocs"]:
            if not quoted:
                self.subs_in(body)

    def command(self, argv, seg, text, wrapped):
        if not argv:
            return
        if self.computed(argv[0]):  # T5-j: try the guard's own environment ($EDITOR), then decide narrowly
            cw = [("L", os.environ[t[1]], True if t[2] else "v") if t[0] == "W" and len(t) > 1 and t[1] in os.environ
                  else t for t in argv[0]]
            argv = self.split(cw) + argv[1:]
            if not argv:
                return
        if self.computed(argv[0]):
            if self.maybe_mother(argv[0]):
                self.hit("computed", "a computed command name that could be the mother CLI")
            self.opaque = True  # denied in run() only if the line could be a whatsapp command
            for a in argv[1:]:
                self.arg(a, None)
            return
        pieces = self.value(argv[0]).split()
        if not pieces:
            return
        name = pieces[0].rstrip("/").split("/")[-1].casefold()
        args = [[("L", p, True)] for p in pieces[1:]] + argv[1:]
        vals = [self.value(a) for a in args]
        clean = name == "git" and self.git_clean(vals)
        destructive = self.far if clean else self.reach(name, vals)
        for a in args:
            self.arg(a, destructive)
        self.track_dirs(name, vals)
        # git clean empties the folder it runs in (-C, --work-tree and pathspecs were checked as arguments above)
        if clean and self.bases and self.bases[-1] is not None and _cf(self.bases[-1]) in self.far:
            self.hit("state", "a git clean of a folder that holds WhatsApp's state")
        if name in WA_WRAPPERS:
            self.unwrap(name, args, vals, seg, text)
        elif name == "script":
            self.script(args, vals, seg, text)
        elif name in WA_PTY_CMDS:
            if wa_names_cli(wa_norm(self.cmd)):
                self.hit("unknown", "a terminal wrapper (%s) around a mother whatsapp command" % name)
        elif name == "eval":
            if any(v is None for v in vals):
                self.unreadable(args, "an eval of text the guard can't compute")
            else:
                self.line(" ".join(vals))
        elif name in WA_SHELLS:
            self.shell(args, vals, seg)
        elif name in WA_INTERPS:
            self.interp(name, args, vals, seg, text, wrapped)
        elif name in WA_SOCKET_CMDS or (name == "curl" and any(
                v and v.startswith(("--unix-socket", "--abstract-unix-socket")) for v in vals)):
            if re.search(r"whatsapp|bridge", wa_norm(self.cmd)) or any(v is None for v in vals):
                self.hit("state", "a socket client on a line about WhatsApp or its bridge")
        elif name == "find":
            for k, v in enumerate(vals):
                if v in ("-exec", "-execdir", "-ok", "-okdir"):
                    inner = []
                    for a, w in zip(args[k + 1:], vals[k + 1:]):
                        if w in (";", "+"):
                            break
                        inner.append(a)
                    self.command(inner, seg, text, "find")
        elif name == "mother":
            self.mother(args, vals, seg, text, wrapped)

    def reach(self, name, vals):
        """The folders a destructive or copying command must not name: None (not such a command), the folders
        that directly hold WhatsApp's state, or every folder above it (a recursive delete, move or copy)."""
        recursive = any(v and re.match(r"(?:-[A-Za-z]*[rRa][A-Za-z]*|--recursive|--archive)\Z", v) for v in vals)
        if (name == "find" and any(v in WA_FIND_ACTIONS for v in vals)) or name in (
                "rm", "rmdir", "mv", "tar", "zip", "unzip", "rsync", "ditto", "shred", "trash", "srm") or (
                name in ("cp", "chmod", "chown", "chflags") and recursive):
            return self.far
        return self.near if name in WA_DESTRUCTIVE else None

    @staticmethod
    def git_clean(vals):
        """git's subcommand is `clean`, and not a dry run (an uncomputed word counts as one that could be)."""
        k = 0
        while k < len(vals) and (vals[k] is None or vals[k].startswith("-")):
            k += 2 if vals[k] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
        if k >= len(vals) or vals[k].casefold() != "clean":
            return False
        return not any(v in ("-n", "--dry-run") for v in vals[k + 1:])

    def unwrap(self, name, args, vals, seg, text):
        opts, k, duration = WA_WRAPPERS[name], 0, name in ("timeout", "gtimeout")
        while k < len(args) and vals[k] is not None:
            v = vals[k]
            if name == "env" and v in ("-S", "--split-string") and k + 1 < len(args):
                if vals[k + 1] is None:
                    self.unreadable([args[k + 1]], "an env -S string the guard can't compute")
                else:
                    self.line(vals[k + 1])
                return
            if v == "--":
                k += 1
                break
            if (name == "env" and re.match(r"[A-Za-z_]\w*=", v)) or (v.startswith("-") and len(v) > 1):
                k += 2 if v in opts else 1
                continue
            if duration:
                duration = False
                k += 1
                continue
            break
        self.command(args[k:], seg, text, name)

    def script(self, args, vals, seg, text):
        """`script [opts] [file [command …]]` (BSD) or `script -c CMD [file]` (Linux): it gives the command a
        terminal, so the command is read like any other."""
        k = 0
        while k < len(args):
            v = vals[k]
            if v is None:
                return self.unreadable(args[k:], "a script command the guard can't compute")
            if v in ("-c", "--command") and k + 1 < len(args):
                if vals[k + 1] is None:
                    return self.unreadable([args[k + 1]], "a script -c string the guard can't compute")
                self.line(vals[k + 1])
                k += 2
                continue
            if v == "--":
                k += 1
                break
            if v.startswith("-") and len(v) > 1:
                k += 2 if v in WA_SCRIPT_OPTS else 1
                continue
            break
        if k + 1 < len(args):  # the first word is the typescript file; the rest is the command
            self.command(args[k + 1:], seg, text, "script")

    def shell(self, args, vals, seg):
        for k, v in enumerate(vals):
            if v is None:
                return
            if v.startswith(("-", "+")) and len(v) > 1 and not v.startswith("--"):
                if "c" in v[1:]:
                    if k + 1 < len(args):
                        if vals[k + 1] is None:
                            self.unreadable([args[k + 1]], "a shell -c string the guard can't compute")
                        else:
                            self.line(vals[k + 1])
                    return
                continue
            if v.startswith("--"):
                continue
            return  # a script file: its path was checked like any argument
        self.runner = True  # no -c and no script: it reads commands from stdin
        for body, _ in seg["heredocs"]:
            self.line(body)

    def interp(self, name, args, vals, seg, text, wrapped):
        awk = name.endswith("awk")
        k = 0
        while k < len(args):
            v = vals[k]
            if v is None:
                return
            if awk:
                if v in ("-F", "-v"):
                    k += 2
                    continue
                if v == "-f":
                    return
                if v.startswith("-") and len(v) > 1:
                    k += 1
                    continue
                return self.code(v)
            if v in WA_CODE_FLAGS:
                if k + 1 < len(args):
                    if vals[k + 1] is None:
                        self.unreadable([args[k + 1]], "inline code the guard can't compute")
                    else:
                        self.code(vals[k + 1])
                return
            if v == "-m" and name.startswith("python"):
                return
            if v == "-":
                break
            if v.startswith("-") and len(v) > 1:
                k += 1
                continue
            if name.startswith("python") and v.rstrip("/").split("/")[-1].casefold() == "mother":
                return self.mother(args[k + 1:], vals[k + 1:], seg, text, wrapped)
            if "bridge" in v.casefold().split("/") and "whatsapp" in wa_norm(self.cmd):
                self.hit("state", "a script from the WhatsApp bridge")
            return
        self.runner = True  # no inline code and no script: it reads code from stdin
        for body, _ in seg["heredocs"]:
            self.code(body)

    def code(self, text):
        n = wa_norm(text)
        if WA_STATE_RE.search(n):
            self.hit("state", "a script that names WhatsApp's bridge socket or its state folders")
        if "whatsapp" in n and re.search(r"mother\W+whatsapp|af_unix|unix[-_]connect|\.sock\b|createconnection", n):
            self.hit("unknown", "a script that runs the mother whatsapp CLI or reaches the bridge")
        if "whatsapp" in n and re.search(r"\bimport\s+whatsapp\b|\bfrom\s+whatsapp\s+import\b|__import__|"
                                         r"import_module|\brunpy\b|\bbridge_call\b|\bcmd_send\b", n):
            self.hit("unknown", "inline code that loads MOTHER's whatsapp module (send with `mother whatsapp send`)")
        if "baileys" in n:
            self.hit("state", "a script that loads the WhatsApp library (it could link or send without the bridge)")

    def mother(self, args, vals, seg, text, wrapped):
        if not args:
            return
        if vals[0] is None:
            self.hit("unknown", "a mother command whose subcommand is computed")
        if vals[0].casefold() != "whatsapp":
            if wrapped == "xargs":
                self.hit("unknown", "a mother command that takes its arguments from xargs")
            return
        if len(args) > 1:
            if vals[1] is None:
                self.hit("unknown", "a whatsapp subcommand that is computed")
            if vals[1].casefold() in WA_TERMINAL_SUBS:
                self.hit("terminal", "`mother whatsapp %s` links WhatsApp to MOTHER"
                         % vals[1].casefold())
            if vals[1].casefold() == "send":
                self.hit("compound", "a whatsapp send that is not a command of its own")
        own = text[seg["start"]:seg["end"]].strip()
        while True:  # literal NAME=value prefixes read like `export …;` (not ones that change what code runs)
            m = WA_ENV_PREFIX_RE.match(own)
            if not m or WA_ENV_CODE_RE.match(m.group(1)):
                break
            own = own[m.end():]
        if wrapped or not WA_PASS_RE.match(own):
            self.hit("unknown", "an unrecognised whatsapp command")

    def track_dirs(self, name, vals):
        """cd, pushd and -C/--directory move the folder relative paths resolve against."""
        def add(v):
            if v is None:
                self.bases.append(None)
                return
            p = os.path.expanduser(v)
            last = next((b for b in reversed(self.bases) if b), self.home)
            self.bases.append(os.path.normpath(p if os.path.isabs(p) else os.path.join(last, p)))
        if name in ("cd", "pushd"):
            rest = [v for v in vals if not (v and v.startswith("-") and len(v) > 1)]
            if not rest:
                self.bases.append(self.home)
            elif rest[0] != "-":
                add(rest[0])
        for k, v in enumerate(vals):
            if v in ("-C", "--directory") and k + 1 < len(vals):
                add(vals[k + 1])
            elif v and v.startswith("--directory="):
                add(v[len("--directory="):])

    # -- paths

    def arg(self, toks, destructive):
        """An argument that names a protected path (or, for a destructive command, a folder holding one) hits.
        Unresolved expansions are wildcards; a word that is only an expansion names nothing the guard can see."""
        if not toks or all(t[0] == "W" for t in toks):
            return
        lits = "".join(t[1] for t in toks if t[0] == "L")
        spaced = bool(re.search(r"\s", lits))
        if any(t[0] == "W" for t in toks) and not spaced and \
                re.search(r"\.mother|whatsapp|bridge\.sock", lits, re.I):
            self.hit("state", "a path next to WhatsApp's state built from something the guard can't resolve")
        out, cur = [], []
        for t in toks:  # pieces: --file=PATH, UNIX-CONNECT:PATH, code like open('PATH')
            if t[0] != "L":
                cur.append(t)
                continue
            for n, b in enumerate(_WA_SEP.split(t[1])):
                if n and cur:
                    out.append(cur)
                    cur = []
                if b:
                    cur.append(("L", b, t[2]))
        if cur:
            out.append(cur)
        for c in [toks] + (out if len(out) > 1 else []):
            self.path(c, destructive, not spaced)

    def path(self, toks, destructive, statere):
        if not toks or all(t[0] == "W" for t in toks) or (toks[0][0] == "L" and toks[0][1].startswith("-")):
            return
        atoms = []
        for t in toks:
            atoms += [("any",)] if t[0] == "W" else _wa_glob_atoms(t[1], True, t[2] in (False, "v"))
        if all(a[0] == "c" for a in atoms):
            p = "".join(a[1] for a in atoms)
            if statere and WA_STATE_RE.search(p):
                self.hit("state", "a shell command on WhatsApp's bridge socket or its state folders")
            p = os.path.expanduser(p)
            for base in ([""] if os.path.isabs(p) else self.bases):
                if base is None:
                    self.match([("any",), ("c", "/")] + [("c", ch) for ch in p], destructive)
                    continue
                c = os.path.normpath(os.path.join(base, p))
                if wa_inside(c, self.ctx) or (destructive and _cf(c) in destructive):
                    self.hit("state", "a shell command on WhatsApp's bridge socket or its state folders")
            return
        if atoms[0] == ("c", "~") and (len(atoms) == 1 or atoms[1] == ("c", "/")):
            atoms = [("c", ch) for ch in self.home] + atoms[1:]
        if atoms[0] in (("any",), ("c", "/")):
            cands = [atoms]
        else:
            cands = [([("any",)] if b is None else [("c", ch) for ch in b]) + [("c", "/")] + atoms
                     for b in self.bases]
        for c in cands:
            self.match(c, destructive)

    def match(self, atoms, destructive):
        first = next((k for k, a in enumerate(atoms) if a[0] != "c"), len(atoms))
        head = "".join(a[1] for a in atoms[:first])
        cut = head.rfind("/")
        if head.startswith("/") and cut >= 0:  # resolve the literal folders (/var → /private/var)
            real = os.path.realpath(head[:cut] or "/").rstrip("/")
            atoms = [("c", ch) for ch in real] + atoms[cut:]
        def vague(pre):  # only wildcards and /: it names anything, so it names nothing in particular
            return all(a[0] != "c" or a[1] == "/" for a in pre)
        ends = [k for k, a in enumerate(atoms) if a == ("c", "/") and k] + [len(atoms)]
        for end in ends:
            pre = atoms[:end]
            if vague(pre):
                continue
            rx = _WaGlob(pre)
            if any(rx.fullmatch(p) for p in self.protected):
                self.hit("state", "a shell command on WhatsApp's bridge socket or its state folders")
        if destructive and not vague(atoms):
            rx = _WaGlob(atoms)
            if any(rx.fullmatch(p) for p in destructive):
                self.hit("state", "a destructive or copying command on a folder that holds WhatsApp's state")


def _wa_literal(word, cli=False):
    """The text of a word that holds only literal parts (quotes, $'…' and backslashes resolved), else None. The
    CLI's own path may also use $CLAUDE_PLUGIN_ROOT and $HOME."""
    out = []
    for p in word:
        if p[0] == "t":
            out.append(p[1])
        elif cli and p[0] == "v" and p[1] in ("CLAUDE_PLUGIN_ROOT", "HOME") and p[2] is None:
            out.append(os.path.dirname(HERE) if p[1] == "CLAUDE_PLUGIN_ROOT" else os.path.expanduser("~"))
        else:
            return None
    return "".join(out)


def wa_send_parse(cmd, cwd=None):
    """A Bash line that is exactly one `mother whatsapp send …` command: {"to", "text"}, or {"error"}
    when it is that command but can't be read safely. None for any other line (the shell reader judges it)."""
    line = cmd.strip()
    if not WA_SEND_HEAD_RE.match(line):  # the CLI and `whatsapp send` written plainly, as WA_PASS_RE wants them
        return None
    segs = _ShLex(line).commands()
    if len(segs) != 1:
        return None
    seg = segs[0]
    if seg["heredocs"] or seg["start"] != 0 or seg["end"] != len(line):
        return None
    for op, w in seg["redirs"]:  # only 2>&1-style: nothing written, nothing read
        if op not in (">&", "<&") or not re.match(r"(?:\d+|-)\Z", _wa_literal(w) or "x"):
            return None
    words = seg["words"]
    k = 1 if words and _wa_literal(words[0]) in ("python", "python3") else 0
    if len(words) < k + 3:
        return None
    exe = _wa_literal(words[k], cli=True)
    sub = [_wa_literal(w) for w in words[k + 1:k + 3]]
    plain = all(p[0] == "t" and p[2] is False for w in words[k + 1:k + 3] for p in w)
    if exe is None or exe.rstrip("/").split("/")[-1] != "mother" or sub != ["whatsapp", "send"] or not plain:
        return None
    if not wa_is_plugin_cli(exe, cwd):
        return {"error": "%s is not this plugin's mother CLI" % exe}
    opts, rest = {}, words[k + 3:]
    for w in rest:
        for n, p in enumerate(w):
            if p[0] == "t" and p[2] is False and ((n == 0 and p[1].startswith("~")) or WA_UNQUOTED_RISK_RE.search(p[1])):
                return {"error": "an unquoted ~, glob or {a,b} the shell would expand; put the text in quotes"}
    if any(WA_ANSI_C_RISK_RE.search(m.group(1)) for m in WA_ANSI_C_RE.finditer(line)):
        return {"error": "a $'…' escape for a raw byte or \\c, which the shell turns into something else; write "
                         "the character itself, in quotes"}
    i = 0
    while i < len(rest):
        name = _wa_literal(rest[i])
        if name is None:
            return {"error": "an option the guard can't read"}
        val = None
        if "=" in name and name.split("=", 1)[0] in WA_SEND_OPTS:
            name, val = name.split("=", 1)
        if name not in WA_SEND_OPTS or name in opts:
            return {"error": "use --to WHO and --text TEXT, once each"}
        if val is None:
            if i + 1 >= len(rest):
                return {"error": "%s has no value" % name}
            val = _wa_literal(rest[i + 1])
            if val is None:
                return {"error": "the guard can't show %s: it holds a $ expansion, $( ) or a backtick. Write the "
                                 "text literally, in single quotes" % name}
            i += 2
        else:
            i += 1
        opts[name] = val
    if "--to" not in opts or "--text" not in opts:
        return {"error": "use --to WHO and --text TEXT"}
    for name in WA_SEND_OPTS:
        if wa_unshowable(opts[name]):
            return {"error": "%s holds a control, invisible or text-direction character, so the prompt couldn't show exactly "
                             "what is sent; write it without one" % name}
    return {"to": opts["--to"], "text": opts["--text"]}


def _plugin_name(root):
    """The `name` in <root>/.claude-plugin/plugin.json, or None."""
    data, ok = read_json(os.path.join(root, ".claude-plugin", "plugin.json"))
    return data.get("name") if ok and isinstance(data, dict) and isinstance(data.get("name"), str) else None


# A subdivision flag (England, Scotland, Wales): the black flag, tag letters, the cancel tag. It renders as one flag.
WA_TAG_FLAG_RE = re.compile("\U0001F3F4(?:%s)\U000E007F" % "|".join(
    "".join(chr(0xE0000 + ord(c)) for c in code) for code in ("gbeng", "gbsct", "gbwls")))


def wa_hidden(ch):
    import unicodedata
    if ch in WA_HIDDEN_EXTRA:
        return True
    o = ord(ch)
    if 0xFE00 <= o <= 0xFE0D or 0xE0100 <= o <= 0xE01EF or 0x180B <= o <= 0x180F:
        return True  # variation selectors (all but text/emoji presentation) render as nothing and carry bytes
    cat = unicodedata.category(ch)
    if cat == "Cn" and 0x1F000 <= ord(ch) <= 0x1FAFF:
        return False  # an emoji newer than this Python's Unicode tables (3.9 stops at Unicode 13)
    return ch not in WA_HIDDEN_OK and cat in WA_HIDDEN_CATS


def wa_unshowable(text):
    """The text holds a character the prompt can't show as it is sent (CONTRACT §5)."""
    return any(wa_hidden(ch) for ch in WA_TAG_FLAG_RE.sub("", text))


def wa_is_plugin_cli(exe, cwd):
    """The executable is a copy of this plugin's CLI: its realpath is <root>/bin/mother in a folder whose
    plugin.json names this plugin and which holds lib/whatsapp.py. That covers the versioned cache copy, the
    marketplace clone `mother link` points at, and a --plugin-dir checkout. A bare `mother` is looked up on PATH."""
    import shutil
    if "/" not in exe:
        exe = shutil.which(exe) or ""
        if not exe:
            return False
    real = os.path.realpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(exe)))
    if os.path.basename(real) != "mother" or os.path.basename(os.path.dirname(real)) != "bin" \
            or not os.path.isfile(real):
        return False
    root = os.path.dirname(os.path.dirname(real))
    mine = _plugin_name(os.path.dirname(HERE))
    return bool(mine) and _plugin_name(root) == mine and os.path.isfile(os.path.join(root, "lib", "whatsapp.py"))


def wa_is_self(to, cfg):
    """`--to` names the manager's own chat: `me`, whatsapp.self_jid, or the manager's own number."""
    w = cfg.get("whatsapp") if isinstance(cfg, dict) and isinstance(cfg.get("whatsapp"), dict) else {}
    me = w.get("self_jid")
    t = (to or "").strip()
    if not isinstance(me, str) or not me:
        return False  # no self_jid: nothing is the own chat (fail closed)
    if t.casefold() == "me":
        return True
    digits = re.sub(r"[\s+()\-]", "", t)
    return t == me or (re.fullmatch(r"\d{6,15}", digits) is not None and digits + "@s.whatsapp.net" == me)


def wa_is_number(to):
    """A phone number or a JID: the prompt marks it, the log hides it."""
    return "@" in to or len(re.sub(r"\D", "", to)) >= 6


def wa_ticket(to, text):
    """Write <state>/whatsapp/tickets/<sha256(to NUL text)> for the bridge (CONTRACT §5d). Returns None, or a
    plain reason it couldn't. Temp file O_EXCL|O_NOFOLLOW 0600, then renamed over any old one."""
    import hashlib
    try:
        hexname = hashlib.sha256((to + "\0" + text).encode("utf-8")).hexdigest()
        d = os.path.join(wa_state_root(), "whatsapp", "tickets")
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.path.islink(d) or not os.path.isdir(d):
            return "the ticket folder %s is not a plain folder" % d
        os.chmod(d, 0o700)
        tmp = os.path.join(d, ".%s.%d.tmp" % (hexname, os.getpid()))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, json.dumps({"at": int(time.time())}).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(d, hexname))
        return None
    except (OSError, UnicodeError, ValueError) as e:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return "the guard couldn't write the send ticket (%s)" % type(e).__name__


def wa_act(op, what, **kw):
    cat = {"state": "whatsapp_state", "computed": "computed_command",
           "terminal": "whatsapp_setup"}.get(op, "any_message_to_others")
    return act("whatsapp", cat, what, op=op, **kw)


def classify_whatsapp(cmd, cwd, ctx):
    """A WhatsApp action for a Bash command, or None when the command is not WhatsApp's business or is one of
    the passing subcommands (read, chats, status, …). Ops: send (asks; own chat allowed); state, compound,
    unknown, computed, terminal (always denied). A send must be exactly one plain `mother whatsapp send` command;
    any other line is read by _WaScan (CONTRACT §5, §5d). `what` never holds the text: guard.log holds none."""
    if cwd and wa_inside(cwd, ctx):
        return wa_act("state", "a shell command run inside WhatsApp's state folder")
    s = wa_send_parse(cmd, cwd)
    if s is not None:
        if "error" in s:
            return wa_act("unknown", "a whatsapp send the guard can't read: " + s["error"])
        to, text = s["to"], s["text"]
        if SECRET_RE.search(text):
            return act("credential", "credentials", "a WhatsApp message whose text looks like a credential")
        own = wa_is_self(to, ctx.cfg)
        logged = "a number" if wa_is_number(to) else to
        return act("whatsapp", "own_dm" if own else "any_message_to_others",
                   "a WhatsApp message to %s" % ("your own chat" if own else logged), op="send", to=to, text=text,
                   target=logged)
    try:
        _WaScan(cmd, cwd, ctx).run()
    except _WaHit as h:
        return h.action
    return None


_ABSENT = object()


def wa_config_path(fp, ctx):
    """fp (already realpath'd) is the workspace's config.json."""
    if not ctx.ws:
        return False
    p = os.path.join(ctx.ws, "config.json")
    return _cf(fp) in (_cf(p), _cf(os.path.realpath(p)))


def _wa_block(text):
    """The parsed whatsapp value of a config text (_ABSENT when there is none), or None when it doesn't parse."""
    try:
        cfg = json.loads(text)
    except (TypeError, ValueError):
        return None
    return cfg.get("whatsapp", _ABSENT) if isinstance(cfg, dict) else _ABSENT


def wa_block_now(ctx):
    """config.json holds a whatsapp block today (an unreadable file counts as one: fail closed)."""
    try:
        with open(os.path.join(ctx.ws, "config.json"), encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return False  # no config.json: no block
    except OSError:
        return True  # unreadable: fail closed
    old = _wa_block(text)
    return old is None or old is not _ABSENT


def wa_block_edit(tool, inp, fp):
    """A Write/Edit/MultiEdit/NotebookEdit of config.json that would change the parsed whatsapp value. An edit whose
    old text isn't in the file is left to the existing ask (the tool will fail); a result that doesn't parse
    counts as a change when the file has a whatsapp block today."""
    try:
        with open(fp, encoding="utf-8") as f:
            cur = f.read()
    except FileNotFoundError:
        cur = ""
    except OSError:
        return True
    old = _wa_block(cur)
    had = old is None or old is not _ABSENT
    if tool == "Write":
        new_text = inp.get("content")
        if not isinstance(new_text, str):
            return False  # malformed: the tool fails; the existing ask stands
    elif tool in ("Edit", "MultiEdit"):
        edits = inp.get("edits") if tool == "MultiEdit" else [inp]
        if not isinstance(edits, list):
            return False
        new_text = cur
        for e in edits:
            if not isinstance(e, dict) or not isinstance(e.get("old_string"), str) \
                    or not isinstance(e.get("new_string"), str):
                return False
            if e["old_string"] not in new_text:
                return False
            new_text = new_text.replace(e["old_string"], e["new_string"], -1 if e.get("replace_all") else 1)
    else:
        return had  # a notebook edit of config.json: nothing sane to compare
    new = _wa_block(new_text)
    if new is None:
        return had
    if old is None:
        return new is not _ABSENT
    return old != new


def run_text(cmd, quotes=True, bodies=False):
    """The command with text the shell won't run blanked out: quoted strings, and heredoc bodies that feed a
    plain command (`git commit -F -`, `cat >`). Nothing is blanked in quotes when the command hands text to
    another interpreter (`bash -c`, `python3 -c`, `ssh`, `xargs` …), nor in a heredoc that feeds one.
    `quotes=False` keeps quoted strings (they hold paths); `bodies=True` blanks every heredoc body."""
    blank = quotes and not EXEC_RE.search(cmd)
    scripted = not bodies and SCRIPT_RUN_RE.search(run_text(cmd, quotes=False, bodies=True))
    out, pending, i, n = [], [], 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\":
            out.append(cmd[i:i + 2])
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            j = n - 1 if j < 0 else j
            out.append("''" if blank else cmd[i:j + 1])
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                j += 2 if cmd[j] == "\\" else 1
            s = cmd[i:j + 1]
            live = re.search(r"\$\(|(?<!\\)`", s)
            out.append('""' if blank and not live else s)
            i = j + 1
        elif cmd.startswith("<<", i) and HEREDOC_RE.match(cmd, i):
            m = HEREDOC_RE.match(cmd, i)
            start = max(cmd.rfind(x, 0, i) for x in ("\n", ";", "&", "|", "(")) + 1
            pending.append((m.group(2), bool(m.group(1)), cmd[start:i]))
            out.append(m.group(0))
            i = m.end()
        elif c == "\n" and pending:
            out.append(c)
            i += 1
            for delim, quoted, head in pending:
                end = re.compile(r"^\t*" + re.escape(delim) + r"[ \t]*$", re.M).search(cmd, i)
                j = end.start() if end else n
                body = cmd[i:j]
                fed_mother = quoted and MOTHER_HEAD_RE.search(head)  # the CLI reads it as JSON, never runs it
                runs = not bodies and not fed_mother and (scripted or EXEC_RE.search(" " + head) or
                                                          (not quoted and re.search(r"\$\(|`", body)))
                out.append(body if runs else "\n" * body.count("\n"))
                i = j
            pending = []
        else:
            out.append(c)
            i += 1
    return "".join(out)


def shell_writes(cmd, run):
    if SHELL_WRITE_RE.search(SAFE_REDIRECT_RE.sub(" ", run)):
        return True
    return bool(SCRIPT_RE.search(run) and SCRIPT_WRITE_RE.search(cmd))


def shell_self_edit(cmd, run, cwd, ctx):
    """A shell command that could change the guard's own files: it writes, and it names one of them by a path
    that reaches the running copy (the installed plugin, the workspace) or runs from inside it. The same file
    names in a checkout of this repo or in another project pass. Reads (cat, grep, 2>/dev/null) pass.
    Returns the kind it touches: "config" (the guard, config.json, PAUSED, the ledger, the log), "grants"
    (.mother/grants.json) or "persona" (the workspace's AGENTS.md, CLAUDE.md, .mcp.json, .claude/, MEMORY.md);
    False for none."""
    if not shell_writes(cmd, run):
        return False
    text = run_text(cmd, quotes=False)  # a `cat > notes.md <<'EOF'` body that mentions config.json names nothing
    low = text.lower()
    shell = run_text(cmd, quotes=False, bodies=True).lower()  # markers count only where the shell expands them
    # the session's cwd says where relative names land, unless the command moves first (then its cd path counts)
    here = os.path.realpath(cwd).lower() if cwd and not re.search(r"(?:^|[\s;&|(])cd\s", shell) else ""
    home = os.path.expanduser("~").lower()
    homes = [(os.path.dirname(HERE), [r"\$\{?claude_plugin_root\b", r"\.claude/plugins/"], ["guard.py", "hooks.json"],
              "config")]
    if ctx.ws:
        # a command that sets its own MOTHER_WORKSPACE / MOTHER_STATE_DIR is working on a test workspace
        test_ws = re.search(r"\b(?:mother_workspace|mother_state_dir)=", shell)
        marks = [] if test_ws else [r"\$\{?mother_workspace\b", r"\.mother/workspace\b"]
        homes.append((ctx.ws, marks, ["config.json", "PAUSED", "ledger.json", "guard.log"], "config"))
    for base, marks, names, kind in homes:
        # exact case as a bare word (so prose "paused" is not PAUSED), any case after a slash (macOS ignores case)
        if not any(re.search(r"(?<![\w.-])" + re.escape(x) + r"(?![\w.-])", text) or
                   re.search(r"/" + re.escape(x.lower()) + r"(?![\w.-])", low) for x in names):
            continue
        base = base.lower()
        forms = [base] + (["~" + base[len(home):], "$home" + base[len(home):], "${home}" + base[len(home):]]
                          if base.startswith(home + os.sep) else [])
        if base.startswith("/private/"):  # macOS: /tmp and /var are links into /private
            forms.append(base[len("/private"):])
        if (here == base or here.startswith(base + os.sep) or any(f in low for f in forms)
                or any(re.search(mk, shell) for mk in marks)):
            return kind
    # the workspace pointer itself; reading it (`cat ~/.mother/workspace`) is how every routine finds the workspace
    return "config" if POINTER_RE.search(POINTER_READ_RE.sub(" ", text)) else False


def _home_prefixed(t, pos):
    """The match at t[pos] sits right after a home directory (`~/`, `$home/`, `/users/<x>/`): not the workspace."""
    pre = t[max(0, pos - 64):pos]
    return bool(re.search(r"(?:~|\$\{?home\}?|/users/[^/\s]+|/home/[^/\s]+)/$", pre))


def shell_protected(cmd, run, cwd, ctx):
    """"grants" or "persona" when a shell command writes and names a protected workspace file, on a normalised
    text (quotes and backslashes dropped, casefolded): grants.json or anything under .mother/; AGENTS.md,
    CLAUDE.md (anywhere in the workspace), .mcp.json, .claude/, MEMORY.md; or a glob or `$` right after
    `.mother/`, `memory/` or `.claude/`. A `cd` into those followed by a write, and `git checkout|restore|reset …
    -- <them>`, count. `~/.claude` (home, not the workspace) does not. The command must reach the workspace: run
    from inside it (unless it first cds to an absolute path), or name it, `$W/` or `$MOTHER_WORKSPACE`."""
    if not ctx.ws or not (shell_writes(cmd, run) or EXTRA_WRITE_RE.search(run)):
        return None
    t = re.sub(r"[\"'\\]", "", run_text(cmd, quotes=False)).casefold()
    ws = os.path.realpath(ctx.ws).casefold()
    home = os.path.expanduser("~").casefold()
    forms = [ws] + (["~" + ws[len(home):], "$home" + ws[len(home):], "${home}" + ws[len(home):]]
                    if ws.startswith(home + os.sep) else []) + ([ws[len("/private"):]] if ws.startswith("/private/") else [])
    here = os.path.realpath(cwd).casefold() if cwd else ""
    leaves = re.search(r"(?:^|[\s;&|(])cd\s+(?:/|~|\$\{?home)", t)
    reach = ((here == ws or here.startswith(ws + os.sep)) and not leaves) or any(f in t for f in forms) \
        or re.search(r"\$\{?(?:w|mother_workspace)\}?/", t)
    if not reach:
        return None

    def hit(pattern):
        return any(not _home_prefixed(t, m.start()) for m in re.finditer(pattern, t))

    if hit(r"grants\.json|(?<![\w-])\.mother(?![\w-])"):
        return "grants"
    if hit(r"(?<![\w.-])(?:agents|claude)\.md(?![\w.-])|\.mcp\.json|(?<![\w-])\.claude(?![\w-])|memory\.md"
           r"|(?:^|[\s/=])memory/\S*[*?\[$]"):
        return "persona"
    return None


def wa_shell_names_config(cmd):
    """A shell write that shell_self_edit already matched also names config.json (quoted paths included)."""
    text = run_text(cmd, quotes=False)
    return bool(re.search(r"(?<![\w.-])config\.json(?![\w.-])", text) or re.search(r"/config\.json(?![\w.-])", text.lower()))


def classify_bash(cmd, cwd, ctx):
    wa = classify_whatsapp(cmd, cwd, ctx)
    if wa:
        return wa
    run = run_text(cmd)
    norm = re.sub(r"[\"'\\]", "", run_text(cmd, quotes=False)).casefold()
    if GRANTS_CLI_RE.search(norm):
        return act("grants", "grants_file", "`mother grant add`: a new grant lets later writes through without a "
                                            "question", op="cli")
    touched = shell_self_edit(cmd, run, cwd, ctx)
    if not touched:
        prot = shell_protected(cmd, run, cwd, ctx)
        if prot == "grants":
            return act("grants", "grants_file", "a shell command that could change .mother/ (grants, ledger, "
                                                "guard state)")
        if prot == "persona":
            return act("persona", "persona_files", "a shell command that could change the workspace's standing "
                                                   "instructions (AGENTS.md, CLAUDE.md, .mcp.json, .claude/, "
                                                   "MEMORY.md)")
    if touched:
        # config.json holds the whatsapp block: only `mother whatsapp setup`, in the manager's terminal, changes it
        if wa_shell_names_config(cmd) and wa_block_now(ctx):
            return wa_act("terminal", "a shell write to config.json, which holds the whatsapp block (MOTHER's "
                                      "WhatsApp setup)")
        return act("config", "guard_config", "a shell command that could change the guard's own files")
    short = cmd.strip()[:160]
    if GH_CRED_RE.search(run):
        return act("credential", "credentials", "storing a credential on the code host: " + short)
    if GH_DELETE_RE.search(run) or (API_RE.search(run) and API_DELETE_RE.search(cmd)):
        return act("deletion", "deletions", "a code-host deletion: " + short)
    if GH_WRITE_RE.search(run) or (API_RE.search(run) and api_write(cmd, cwd)):
        return act("code", "code_host_comments", "a code-host write: " + short, text=cmd)
    return None


# The desktop app's own session tools reach only the manager's sessions, never another person, so they are not
# outbound. Exact names only: a same-named tool on another server is classified like any other.
IN_APP_SESSION_TOOLS = frozenset([
    "mcp__ccd_session__start_session", "mcp__ccd_session__dismiss_task",
    "mcp__ccd_session_mgmt__send_message", "mcp__ccd_session_mgmt__list_sessions",
    "SendMessage", "ListAgents"])


SESSION_ID_RE = re.compile(r"^(local_\S+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def live_hub_entries(ctx):
    """The `live` entries in <workspace>/.mother/hub/sessions.json; empty when it is missing or odd."""
    if not ctx.ws:
        return []
    doc, ok = read_json(os.path.join(ctx.ws, ".mother", "hub", "sessions.json"))
    items = doc.get("sessions") if ok and isinstance(doc, dict) else None
    return [s for s in (items.values() if isinstance(items, dict) else [])
            if isinstance(s, dict) and s.get("state") == "live"]


def live_hub_sessions(ctx):
    """The session ids of `live` entries in sessions.json."""
    return {str(s["session_id"]) for s in live_hub_entries(ctx) if s.get("session_id")}


def live_hub_names(ctx):
    """The names of `live` entries in sessions.json."""
    return {str(s["name"]).strip() for s in live_hub_entries(ctx) if str(s.get("name") or "").strip()}


NAME_REF_RE = re.compile(r"^(.*?)\s*\[[^\[\]]*\]$")


def in_workspace(cwd, ctx):
    """The session runs in the workspace or under it (the hub's home), judged on real paths."""
    if not ctx.ws:
        return False
    here = os.path.realpath(cwd or os.getcwd())
    return inside(here, os.path.realpath(ctx.ws))


def session_target(tool, inp, ctx, cwd=None):
    """A message into a session passes only when that session is one the hub routes to (a `live` entry in
    sessions.json); anything else could carry untrusted source text into an unrelated session, so it asks. In the
    workspace (where the hub runs) `SendMessage` by name passes only for `main` or a live hub session's name,
    with or without a ` [ref]` suffix. A card passes only as `own_initiative` (it waits for the manager) or as
    the start of an existing proposal."""
    what = None
    if tool == "mcp__ccd_session_mgmt__send_message":
        target = str(inp.get("session_id") or "")
        if target not in live_hub_sessions(ctx):
            what = "a message into session %s, which is not a live hub session" % (target or "(none named)")
    elif tool == "SendMessage":
        target = str(inp.get("to") or "").strip()
        if SESSION_ID_RE.match(target):
            if target not in live_hub_sessions(ctx):
                what = "a message into session %s, which is not a live hub session" % target
        elif in_workspace(cwd, ctx) and target != "main":
            m = NAME_REF_RE.match(target)
            name = m.group(1).strip() if m else target
            if not name or name not in live_hub_names(ctx):
                what = "a message to %s from the workspace, which is not main or a live hub session" % (
                    target or "(none named)")
    elif tool == "mcp__ccd_session__start_session":
        keys = set(inp) - {"wake_when_done"}
        if not (inp.get("initiation") == "own_initiative" or (keys == {"proposal_id"} and inp.get("proposal_id"))):
            what = "starting a session now (initiation %s), not a card the manager starts" % (
                inp.get("initiation") or "none")
    return act("session", "session_messages", what) if what else None


def classify_mcp(tool, inp):
    if tool in IN_APP_SESSION_TOOLS:
        return None
    parts = tool.split("__")
    server = parts[1] if len(parts) > 2 else ""
    short = parts[-1]
    toks = tokens_of(short)
    dom = domain_from(toks) or domain_from(tokens_of(server)) or domain_from_shape(inp)
    op_toks = [t for t in toks if t not in PRODUCT_WORDS]
    op = "_".join(op_toks)
    verbs = set(op_toks)
    lead = op_toks[0] if op_toks else ""

    if op in ("execute_destructive",):
        return act("deletion", "deletions", "a destructive operation (%s %s)" % (short, inp.get("name") or ""))
    if lead in READ_VERBS or not (verbs & (WRITE_VERBS | DELETE_VERBS)):
        return None
    if "draft" in verbs and dom in ("mail", "chat", None):
        return act("draft", "drafts", "a draft (%s)" % short)
    if not dom:
        return None
    if "reminder" in verbs or "reminders" in verbs:
        return None  # reminders reach only the manager
    if op == "execute_read" or verbs & {"monitor", "watch"}:
        return None  # a read-tier call; watching a PR or thread reaches no one
    if lead in DELETE_VERBS or "execute_destructive" in op:
        return act("deletion", "deletions", "a deletion (%s)" % short)

    if dom == "chat":
        keys = ("channel_id", "channelId", "channel", "chat_id", "chatId", "conversation_id", "user_id", "userId",
                "channels")
        # every target the call names (a list counts element by element): it reaches the DM only if all are
        targets = sorted({str(x) for k in keys for x in as_list(inp.get(k)) if x not in (None, "", [], {})})
        channel = ",".join(targets)
        thread = first(inp, "thread_ts", "threadTs", "thread_id", "reply_to_id", "parent_id")
        if "reaction" in verbs or "react" in verbs or "reactions" in verbs:
            return act("chat", "any_message_to_others", "a chat reaction in %s" % (channel or "?"),
                       op="reaction", channel=channel, targets=targets, thread=thread)
        posting = lead in ("post", "send", "reply", "schedule", "update", "edit", "share", "upload", "complete",
                           "forward") and not ({"canvas", "topic", "purpose"} & verbs)
        if posting:
            sub = "schedule" if lead == "schedule" else "edit" if lead in ("update", "edit") else \
                "reply" if (thread or lead == "reply") else "post"
            return act("chat", "any_message_to_others", "chat %s to %s" % (short, channel or "?"),
                       op=sub, channel=channel, targets=targets, thread=thread, broadcast=bool(inp.get("reply_broadcast")),
                       text=text_of(inp), blocks=json.dumps(inp["blocks"], ensure_ascii=False) if inp.get("blocks") else None)
        return act("chat", "any_message_to_others", "a chat change (%s)" % short, op="other", channel=channel, targets=targets,
                   thread=None)

    if dom == "mail":
        if {"filter", "filters", "forwarding", "vacation", "auto", "autoreply", "settings", "delegate",
                "rule", "rules"} & verbs:
            return act("standing-rule", "standing_rules", "a mail rule or setting change (%s)" % short)
        if lead in ("send", "reply", "forward", "submit") or "send" in verbs:
            return act("mail", "any_message_to_others", "sending mail to %s" % (first(inp, "to", "recipients")
                                                                                 or "?"), text=text_of(inp))
        if {"trash", "spam", "junk"} & verbs:
            # Recoverable, so not a deletion — but bulk-trashing an inbox is exactly how a real agent incident
            # destroyed a user's mail, so the manager confirms each one.
            return act("mail-trash", "mail_trash", "moving mail to trash/spam (%s)" % short)
        return None  # labels, archive, mark-as-read: reversible and no one else sees them

    if dom == "calendar":
        if "calendar" in verbs and "event" not in verbs and "events" not in verbs:
            return act("calendar", "calendar_with_guests", "a calendar change (%s)" % short, guests=None)
        if lead in ("respond", "rsvp") or "respond" in verbs:
            return act("calendar", "calendar_with_guests", "an RSVP (reaches the organiser)", guests=None)
        if lead in ("create", "insert", "quick", "add"):
            return act("calendar", "calendar_with_guests", "calendar event '%s'" % (inp.get("summary") or "?"),
                       guests=attendees_of(inp), update=False)
        # update / patch / move: an existing event may have guests who get notified, unless the call states
        # its full guest list explicitly.
        g = attendees_of(inp) if has_attendee_field(inp) else None
        return act("calendar", "calendar_with_guests", "a change to calendar event %s" %
                   (first(inp, "eventId", "event_id") or "?"), guests=g, update=True)

    if dom == "code":
        return act("code", "code_host_comments", "a code-host write (%s)" % short, text=text_of(inp))

    if dom == "docs":
        return act("docs", "docs_writes", "a shared-document write (%s)" % short)

    # tracker
    flat = inp.get("inputs") if isinstance(inp.get("inputs"), dict) else inp
    projects = set()
    p = first(flat, "projectKey", "project_key", "project")
    if p is not None:
        projects.add(str(p.get("key") if isinstance(p, dict) else p))
    k = first(flat, "issueKey", "issueIdOrKey", "issue_key", "issueId")
    if k is not None:
        projects.add(project_of_key(k))
    issues = flat.get("issues")
    if issues is not None:
        if not isinstance(issues, list):
            raise ValueError("issues is not a list")
        for i in issues:
            projects.add(project_of_key(i))
    updates = flat.get("issueUpdates")
    if updates is not None:
        if not isinstance(updates, list):
            raise ValueError("issueUpdates is not a list")
        for u in updates:
            projects.add(str(u.get("projectKey")) if isinstance(u, dict) and u.get("projectKey") else None)
    if "sprint" in verbs or "backlog" in verbs:
        sub = "sprint"
    elif lead in ("create", "bulk") and ({"issue", "issues", "item", "items", "ticket"} & verbs or op == "create"):
        sub = "create"
    elif "comment" in verbs or op in ("create_update", "create_updates"):
        sub = "comment"
    elif "transition" in verbs:
        sub = "transition"
    else:
        sub = "update"
    n = len(updates) if isinstance(updates, list) and updates else 1
    names = ",".join(sorted(str(x) for x in projects if x)) or "?"
    return act("tracker", "tracker_writes", "tracker %s on %s" % (short, names), op=sub, projects=projects,
               board=first(flat, "originBoardId", "boardId", "board_id"), creates=n, text=text_of(flat))


def _match_close(s, i, open_ch="(", close_ch=")"):
    """Index of the bracket closing the one at s[i] (quotes respected), or len(s) - 1."""
    depth, j, n = 0, i, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = s.find("'", j + 1)
            j = n if k < 0 else k + 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n - 1


def sh_commands(s):
    """The simple commands of a shell line, each a list of raw words (quotes kept), nested `$( )`, `<( )` and
    backtick commands included as commands of their own. A rough lexer: enough to find command names."""
    cmds, words, cur, i, n = [], [], [], 0, len(s)

    def end_word():
        if cur:
            words.append("".join(cur))
            del cur[:]

    def end_cmd():
        end_word()
        if words:
            cmds.append(list(words))
            del words[:]

    while i < n:
        c = s[i]
        if c in " \t":
            end_word()
            i += 1
        elif c in "\n;&|()":
            end_cmd()
            i += 1
        elif c == "\\":
            cur.append(s[i:i + 2])
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            j = n - 1 if j < 0 else j
            cur.append(s[i:j + 1])
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                if s[j] == "\\":
                    j += 2
                    continue
                if s.startswith("$(", j):
                    k = _match_close(s, j + 1)
                    cmds.extend(sh_commands(s[j + 2:k]))
                    j = k + 1
                    continue
                if s[j] == "`":
                    k = s.find("`", j + 1)
                    k = n - 1 if k < 0 else k
                    cmds.extend(sh_commands(s[j + 1:k]))
                    j = k + 1
                    continue
                j += 1
            cur.append(s[i:j + 1])
            i = j + 1
        elif s.startswith(("$(", "<(", ">("), i):
            k = _match_close(s, i + 1)
            cmds.extend(sh_commands(s[i + 2:k]))
            cur.append(s[i:k + 1])
            i = k + 1
        elif c == "`":
            k = s.find("`", i + 1)
            k = n - 1 if k < 0 else k
            cmds.extend(sh_commands(s[i + 1:k]))
            cur.append(s[i:k + 1])
            i = k + 1
        else:
            cur.append(c)
            i += 1
    end_cmd()
    return cmds


def _unq(w):
    return re.sub(r"[\"'\\]", "", w)


def _prog_index(words):
    """(index of the program word, wrappers skipped) for one simple command; index None when there is none."""
    i, wrappers, n = 0, [], len(words)
    while i < n:
        w = words[i]
        u = _unq(w)
        if re.match(r"^[A-Za-z_]\w*=", w):
            i += 1
        elif re.match(r"^\d*(?:[<>]|&>)", w):
            i += 1 if re.search(r"[<>]\S", w) else 2
        elif u in SH_WRAPPERS:
            wrappers.append(u)
            i += 1
            while i < n and _unq(words[i]).startswith("-") and len(_unq(words[i])) > 1:
                i += 1
            if u == "timeout" and i < n and re.match(r"^\d", _unq(words[i])):
                i += 1
        else:
            return i, wrappers
    return None, wrappers


def _interp_reason(base, args, text):
    """Why an interpreter call counts (inline code with network or process markers, a network module, a URL), or
    None. A `mother` CLI path as the script never counts."""
    ua = [_unq(a) for a in args]
    if ua and MOTHER_ARG_RE.search(ua[0]):
        return None
    if any("://" in a for a in ua):
        return "%s with a URL" % base
    for k, a in enumerate(ua):
        if a == "-m" and k + 1 < len(ua) and NET_MODULES_RE.match(ua[k + 1]):
            return "%s -m %s" % (base, ua[k + 1])
        if a.startswith("-m") and NET_MODULES_RE.match(a[2:]):
            return "%s %s" % (base, a)
    inline = any(a in ("-", "--eval", "--print", "-p") or re.match(r"^-[A-Za-z]*[ceEr]$", a) for a in ua) \
        or any(a.startswith("<<") for a in args)
    if not inline:
        return None
    if base == "osascript":
        return "osascript with inline script"  # AppleScript drives Mail and Messages with no network words
    body = " ".join(ua) + (text if any(a.startswith("<<") for a in args) else "")
    m = NET_MARKERS_RE.search(body)
    return "%s inline code using %s" % (base, m.group(0)) if m else None


def _cmd_reason(words, text, depth):
    i, wrappers = _prog_index(words)
    if i is None:
        return None
    raw = words[i]
    if (raw.lstrip("\"'").startswith("$") and not (PLUGIN_CMD_RE.match(raw) and "CLAUDE_PLUGIN_ROOT=" not in text)) \
            or "$(" in raw or "`" in raw:
        return "a command name built from a variable or substitution"
    prog = _unq(raw)
    base = os.path.basename(prog)
    args = words[i + 1:]
    ua = [_unq(a) for a in args]
    if base in NET_PROGS:
        return base
    if {"xargs", "parallel", "watch", "env", "sudo", "doas", "timeout", "nice"} & set(wrappers):
        for a in ua:  # these take option values the lexer can't tell from the program: any later word counts
            if os.path.basename(a) in NET_PROGS:
                return os.path.basename(a)
    for k, a in enumerate(ua):  # find -exec runs the next word
        if a in ("-exec", "-execdir", "-ok", "-okdir") and k + 1 < len(ua) and os.path.basename(ua[k + 1]) in NET_PROGS:
            return os.path.basename(ua[k + 1])
    if base in SH_SHELLS or base == "eval":
        inner = None
        if base == "eval":
            inner = " ".join(ua)
        else:
            for k, a in enumerate(ua):
                if re.match(r"^-[A-Za-z]*c[A-Za-z]*$", a) and k + 1 < len(ua):
                    inner = ua[k + 1]
                    break
        if inner and depth < 3:
            return net_command(inner, depth + 1)
        return None
    if SH_INTERP_RE.match(base):
        return _interp_reason(base, args, text)
    if base == "git":
        sub = [a for a in ua if not a.startswith("-")]
        if sub[:1] == ["push"] and any("://" in a or re.match(r"^[\w.-]+@[\w.-]+:", a) for a in sub[1:]):
            return "git push to a URL"
        if sub[:1] == ["remote"] and sub[1:2] and sub[1] in ("add", "set-url"):
            return "git remote %s" % sub[1]
    if base == "open" and any("://" in a or a.startswith("www.") for a in ua):
        return "open with a URL"
    if base in ("nslookup", "dig", "host", "drill") and any("$(" in a or "`" in a for a in args):
        return "%s with a computed name" % base
    if base in ("gh", "glab") and ua[:1] == ["api"] and any(a.startswith("--hostname") or "://" in a for a in ua):
        return "gh/glab api to another host"
    return None


def net_command(cmd, depth=0):
    """Why a shell command can send data off this machine, or None: /dev/tcp or /dev/udp anywhere; a command name
    built from `$VAR`, `$( )` or backticks (`${CLAUDE_PLUGIN_ROOT}/…` excepted); a program in NET_PROGS (quotes and
    backslashes ignored, so `c'u'rl` is curl), also behind env/sudo/xargs/find -exec; `sh -c` / `eval` text,
    classified the same way; python/node/perl/ruby/php/osascript inline code with a network or process marker, a
    network module or a URL argument (the `mother` CLI never counts); `git push <url>`, `git remote add|set-url`;
    `open <url>`; `nslookup|dig|host` with a computed name; gh/glab api to another host. Heredoc bodies fed to a
    plain command, or to `mother`, are data."""
    text = run_text(cmd, quotes=False)
    if re.search(r"/dev/(?:tcp|udp)/", _unq(text)):
        return "/dev/tcp or /dev/udp"
    for words in sh_commands(text):
        why = _cmd_reason(words, text, depth)
        if why:
            return why
    return None


def classify_shell(cmd, cwd, ctx):
    """A shell command (Bash, a Monitor command, a terminal-panel command). Outside the workspace: the Bash rules.
    In a workspace session: WhatsApp and `mother grant add` first; a command every stage of which is on the
    allow-list then gets no decision; anything else gets the Bash rules, then outbound_unknown for a program that
    can reach the network, else the protected-path kind or shell_unlisted (CONTRACT §5)."""
    norm = re.sub(r"[\"'\\]", "", cmd).casefold()
    if CHANNELS_MGR_RE.search(norm):
        return act("manager_only", "manager_only", "`mother channels resume|start` turns the channel reply loop "
                                                   "back on: %s" % cmd.strip()[:160])
    if hub_fed(ctx):
        why = hub_fed_shell(norm)
        if why:
            return act("grants", "grants_file", "%s in a session that reads hub lines: it reads replies as the "
                                                "manager's or makes a grant, and the text it was fed comes from "
                                                "other people (%s)" % (why, cmd.strip()[:160]), op="hub_fed")
    if not in_workspace(cwd, ctx):
        return classify_bash(cmd, cwd, ctx)
    wa = classify_whatsapp(cmd, cwd, ctx)
    if wa:
        return wa
    if GRANTS_CLI_RE.search(norm):
        return act("grants", "grants_file", "`mother grant add`: a new grant lets later writes through without a "
                                            "question", op="cli")
    found = shell_unlisted(cmd, cwd, ctx)
    if not found:
        return None
    why, kind = found
    a = classify_bash(cmd, cwd, ctx)
    if a:
        return a
    net = net_command(cmd)
    if net:
        return act("outbound_unknown", "outbound_unknown", "a shell command that can send data off this machine "
                                                           "(%s): %s" % (net, cmd.strip()[:160]))
    short = cmd.strip()[:160]
    if why == "grant" or (kind is None and "protected workspace path" not in why and re.search(r"\bgrants?\b", norm)):
        return act("grants", "grants_file", "a shell command that could add a grant (%s): %s" % (
            "mother grant add" if why == "grant" else why, short), op="cli")
    if kind == "grants":
        return act("grants", "grants_file", "a shell command on .mother/ (%s): %s" % (why, short))
    if kind == "persona":
        return act("persona", "persona_files", "a shell command on the workspace's standing instructions (%s): %s"
                   % (why, short))
    if kind == "config":
        return act("config", "guard_config", "a shell command on config.json or capabilities.json (%s): %s"
                   % (why, short))
    if kind == "home_settings":
        return act("home_settings", "home_settings", "a shell command on ~/.claude (%s): %s" % (why, short))
    return act("shell_unlisted", "shell_unlisted", "a shell command outside the workspace allow-list (%s): %s"
               % (why, short))


# ------------------------------------------------------------------ the workspace shell allow-list (CONTRACT §5)

SL_READ = {"ls", "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "grep", "egrep", "rg", "jq", "diff",
           "cmp", "stat", "file", "basename", "dirname", "realpath", "date", "pwd", "echo", "printf", "true",
           "false", "test", "[", "sleep", "which", "sed", "find"}
SL_FILE = {"mkdir", "touch", "cp", "mv", "rm"}
SL_GIT = {"status", "log", "diff", "show", "add", "commit", "pull", "push", "fetch", "rev-parse", "branch",
          "ls-files", "blame", "stash"}
SL_GH = {"gh", "glab"}
SL_SED_PART = re.compile(r"^\s*(?:(?:\d+|\$|/(?:[^/\\]|\\.)*/)(?:\s*,\s*(?:\d+|\$|/(?:[^/\\]|\\.)*/))?\s*!?\s*)?"
                         r"(?:[pdq=]|s(.)(?:(?!\1)[^\\]|\\.)*\1(?:(?!\1)[^\\]|\\.)*\1[gpIi0-9]*)\s*$", re.S)


class _Unlisted(Exception):
    def __init__(self, why, kind=None):
        Exception.__init__(self, why)
        self.kind = kind  # "grants", "persona" or "home_settings" for a protected path; None otherwise


def _sl_fail(why, kind=None):
    raise _Unlisted(why, kind)


def sl_lex(cmd, plugin):
    """Stages of a shell line under the strict grammar: a list of {words, redirs, heredocs}; raises _Unlisted with
    the first part it won't read. Words are plain strings with quotes resolved; `${CLAUDE_PLUGIN_ROOT}`,
    `$CLAUDE_PLUGIN_ROOT`, `$HOME` and a leading `~` are expanded; any other `$`, a backtick, a glob or brace
    character outside quotes, a comment, a subshell, `&`, `<<<` or a line continuation stops it."""
    home = os.path.expanduser("~")
    if re.search(r"\bCLAUDE_PLUGIN_ROOT=|\bHOME=", cmd):
        _sl_fail("sets CLAUDE_PLUGIN_ROOT or HOME")
    stages, cur, i, n = [], {"words": [], "redirs": [], "heredocs": []}, 0, len(cmd)
    word, have, pend_redir, pending_docs = [], False, None, []
    known = {"CLAUDE_PLUGIN_ROOT": plugin, "HOME": home}

    def var_at(j):
        m = re.match(r"\$(?:\{(CLAUDE_PLUGIN_ROOT|HOME)\}|(CLAUDE_PLUGIN_ROOT|HOME)(?![\w]))", cmd[j:])
        return (known[m.group(1) or m.group(2)], m.end()) if m else (None, 0)

    def end_word():
        nonlocal word, have, pend_redir
        if have:
            w = "".join(word)
            if pend_redir is not None:
                cur["redirs"].append((pend_redir, w))
                pend_redir = None
            else:
                cur["words"].append(w)
        word, have = [], False

    def end_stage(sep):
        nonlocal cur
        end_word()
        if pend_redir is not None:
            _sl_fail("a redirection with no target")
        if cur["words"] or cur["redirs"]:
            cur["sep"] = sep
            stages.append(cur)
        elif sep in ("|", "&&", "||"):
            _sl_fail("an empty stage")
        cur = {"words": [], "redirs": [], "heredocs": []}

    while i < n:
        c = cmd[i]
        if c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_word()
            docs = pending_docs
            pending_docs = []
            end_stage("\n")
            i += 1
            for delim, strip_tabs in docs:  # heredoc bodies follow the line that opened them
                lines_re = re.compile(r"^%s%s$" % (r"\t*" if strip_tabs else "", re.escape(delim)), re.M)
                m = lines_re.search(cmd, i)
                if not m:
                    _sl_fail("a heredoc with no end line")
                i = m.end() + 1
        elif c == "\\":
            _sl_fail("a backslash outside single quotes")
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                _sl_fail("an unclosed quote")
            word.append(cmd[i + 1:j])
            have, i = True, j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\":
                    _sl_fail("a backslash inside double quotes")
                elif cmd[j] == "$":
                    val, k = var_at(j)
                    if val is None:
                        _sl_fail("a `$` expansion")
                    buf.append(val)
                    j += k
                elif cmd[j] in "`":
                    _sl_fail("a backtick")
                else:
                    buf.append(cmd[j])
                    j += 1
            if j >= n:
                _sl_fail("an unclosed quote")
            word.append("".join(buf))
            have, i = True, j + 1
        elif c == "$":
            val, k = var_at(i)
            if val is None:
                _sl_fail("a `$` expansion")
            word.append(val)
            have, i = True, i + k
        elif c == "`":
            _sl_fail("a backtick")
        elif c == "~" and not have:
            if cmd[i + 1:i + 2] != "/":
                _sl_fail("a `~` not followed by `/`")
            word.append(home)
            have, i = True, i + 1
        elif c == "=" and not have:
            _sl_fail("a word starting with `=` (zsh expands it to a program path)")
        elif c in "*?[]{}!" and not (c in "[]" and not have and cmd[i + 1:i + 2] in (" ", "\t", "\n", "")):
            _sl_fail("a glob, brace or `!` outside quotes (%s)" % c)
        elif c == "#" and not have:
            _sl_fail("a comment")
        elif c in "()":
            _sl_fail("a subshell or process substitution")
        elif c == ";":
            end_stage(";")
            i += 1
        elif c == "&":
            if cmd.startswith("&&", i):
                end_stage("&&")
                i += 2
            elif cmd.startswith("&>", i):
                end_word()
                pend_redir = "&>>" if cmd.startswith("&>>", i) else "&>"
                i += len(pend_redir)
            else:
                _sl_fail("a background `&`")
        elif c == "|":
            if cmd.startswith("||", i):
                end_stage("||")
                i += 2
            elif cmd.startswith("|&", i):
                _sl_fail("`|&`")
            else:
                end_stage("|")
                i += 1
        elif c in "<>":
            if re.match(r"<\d*-\d*>", cmd[i:]):
                _sl_fail("a zsh numeric glob")
            m = re.match(r"(<<<|<<-|<<|<>|>>|>\||>&|<&|<|>)", cmd[i:])
            op = m.group(1)
            fd = "".join(word) if have and re.fullmatch(r"\d", "".join(word)) else ""
            if fd:
                word, have = [], False
            else:
                end_word()
            if op == "<<<":
                _sl_fail("a here-string `<<<`")
            if op in ("<>", ">|"):
                _sl_fail("the redirection %s" % op)
            i += len(op)
            if op in ("<<", "<<-"):
                m2 = re.match(r"(['\"])([A-Za-z_][A-Za-z0-9_]*)\1(?=[ \t\n]|$)", cmd[i:])
                if not m2:
                    _sl_fail("a heredoc that isn't exactly <<'WORD' (a quoted delimiter of letters, digits and _)")
                cur["heredocs"].append(m2.group(2))
                pending_docs.append((m2.group(2), op == "<<-"))
                i += m2.end()
                continue
            if op in (">&", "<&"):
                m2 = re.match(r"[ \t]*(\d|-)(?![\w./])", cmd[i:])
                if not m2:
                    _sl_fail("a redirection to a file descriptor the guard can't read")
                cur["redirs"].append((fd + op, "&" + m2.group(1)))
                i += m2.end()
                continue
            pend_redir = fd + op
        else:
            word.append(c)
            have, i = True, i + 1
    end_word()
    if pending_docs:
        _sl_fail("a heredoc with no body")
    end_stage("")
    return stages


def _home_dirs():
    home = os.path.realpath(os.path.expanduser("~"))
    state = os.path.realpath(os.environ.get("MOTHER_STATE_DIR") or os.path.join(home, ".mother"))
    return home, state


def ws_protected(p, ws):
    """A real path inside the workspace that only a question may change: .mother/, any .git or .claude folder,
    CLAUDE.md, .mcp.json or grants.json anywhere, AGENTS.md and memory/MEMORY.md at the top, or the root."""
    if not inside(p.casefold(), ws.casefold()):
        return False
    rel = os.path.relpath(p, ws).replace(os.sep, "/")
    parts = rel.casefold().split("/")
    return rel == "." or parts[0] == ".mother" or ".claude" in parts or ".git" in parts \
        or parts[-1] in ("claude.md", ".mcp.json", "grants.json") \
        or "/".join(parts) in ("agents.md", "memory/memory.md", "config.json", "capabilities.json")


def ws_protected_tree(p, ws):
    """A path a cp/mv/rm/mkdir may not take as source or destination: a protected path, a folder above one
    (`memory`, the root) or a folder that holds one (checked on disk), so renaming folders can't swap one in."""
    if not inside(p.casefold(), ws.casefold()):
        return False
    rel = os.path.relpath(p, ws).replace(os.sep, "/").casefold()
    if ws_protected(p, ws) or rel == "memory":
        return True
    if os.path.isdir(p) and not os.path.islink(p):
        count = 0
        for root, dirs, files in os.walk(p):
            for x in dirs + files:
                count += 1
                if count > 2000 or ws_protected(os.path.join(root, x), ws):
                    return True
    return False


def sl_path(word, cwd, ws, write=False):
    """Fail on a path argument the allow-list won't take: ~/.mother (but reading ~/.mother/workspace), ~/.claude and
    ~/.claude.json, a protected workspace path, and, for a write, anything outside the workspace."""
    if word in ("/dev/null", "-"):
        return
    p = os.path.realpath(os.path.join(cwd, word))
    home, state = _home_dirs()
    if (inside(p, state) or inside(p, os.path.join(home, ".mother"))) and not inside(p, ws):
        if write or p not in (os.path.join(state, "workspace"), os.path.join(home, ".mother", "workspace")):
            _sl_fail("a path in ~/.mother (%s)" % word, "grants")  # the grant key lives there
    if inside(p, os.path.join(home, ".claude")) or p == os.path.join(home, ".claude.json"):
        _sl_fail("a path in ~/.claude (%s)" % word, "home_settings" if write else None)
    if ws_protected(p, ws) and (write or p != ws):
        top = os.path.relpath(p, ws).split(os.sep)[0].casefold()
        kind = ("grants" if top == ".mother" or os.path.basename(p).casefold() == "grants.json" else
                "config" if top in ("config.json", "capabilities.json") else "persona") if write else None  # reading a protected file is a question, not a protected-file write
        _sl_fail("a protected workspace path (%s)" % word, kind)
    if write and not inside(p, ws):
        _sl_fail("a write outside the workspace (%s)" % word)


def _git_remotes(ws):
    names = set()
    try:
        with open(os.path.join(ws, ".git", "config")) as f:
            names = set(re.findall(r'^\s*\[remote\s+"([^"]+)"\]', f.read(), re.M))
    except Exception:
        pass
    return names


def _sl_args(words):
    """(options, operands) with `--` ending options."""
    opts, ops, ended = [], [], False
    for w in words:
        if not ended and w == "--":
            ended = True
        elif not ended and w.startswith("-") and w != "-":
            opts.append(w)
        else:
            ops.append(w)
    return opts, ops


SL_TEMP_ROOTS = None


def _sl_roots(ws, plugin):
    """Where an absolute path argument may point: the workspace, the plugin, the system temp folders."""
    global SL_TEMP_ROOTS
    if SL_TEMP_ROOTS is None:
        import tempfile
        SL_TEMP_ROOTS = sorted({os.path.realpath(x) for x in (tempfile.gettempdir(), "/tmp", "/var/tmp")})
    return [ws, os.path.realpath(plugin)] + SL_TEMP_ROOTS


def sl_hidden_paths(words, cwd, ws, plugin):
    """Every path a word may carry (the word, the text after `=`, the text after a short-option cluster such as
    `-f` or `-m`): one starting with `/` or `~` or holding `..` must resolve inside the workspace, the plugin or the
    temp folders (`/dev/null` too), and never to a protected place."""
    for w in words:
        cands = [w]
        if "=" in w:
            cands.append(w.split("=", 1)[1])
        m = re.match(r"^-[A-Za-z]+(.+)$", w)
        if m:
            cands.append(m.group(1))
        for c in cands:
            if not (c.startswith(("/", "~")) or ".." in c):
                continue
            c2 = os.path.expanduser(c) if c.startswith("~/") else c
            if c2.startswith("~"):
                _sl_fail("a `~` path the guard can't read (%s)" % c)
            sl_path(c2, cwd, ws)
            if c2 == "/dev/null":
                continue
            p = os.path.realpath(os.path.join(cwd, c2))
            if not any(inside(p, r) for r in _sl_roots(ws, plugin)):
                _sl_fail("a path outside the workspace, the plugin and the temp folder (%s)" % c)


MOTHER_VERBS = {"path", "version", "help", "-h", "--help", "status", "runs", "scan", "sync", "now", "hub", "health",
                "items", "memory", "speak", "channels", "whatsapp", "grant", "home", "eval", "register"}


def _sl_mother(args):
    """The `mother` verbs a workspace session may run unasked (CONTRACT §5)."""
    verb, sub = (args + ["", ""])[:2]
    if verb not in MOTHER_VERBS:
        _sl_fail("mother %s is not on the allow-list" % (verb or "with no verb"))
    if verb == "grant" and sub not in ("remove", "list", "help", "-h", "--help"):
        raise _Unlisted("grant")
    if verb == "hub" and sub in ("install", "uninstall"):
        _sl_fail("mother hub %s" % sub)
    if verb == "home" and sub not in ("check", "show"):
        _sl_fail("mother home %s" % (sub or "with no subcommand"))
    if verb == "eval" and any(a.split("=", 1)[0] in ("--plugin-dir", "--skip-permissions") for a in args):
        _sl_fail("mother eval with --plugin-dir or --skip-permissions")
    if verb == "register" and len(args) > 1:
        _sl_fail("mother register with arguments")


PLUGIN_SCRIPTS = ("assets/plan/build.py", "assets/deck/build.py")


def _sl_stage(st, cwd, ws, ctx, plugin):
    """Check one stage; returns the new cwd (for `cd`)."""
    words = st["words"]
    if not words:
        _sl_fail("a redirection with no command")
    if re.match(r"^[A-Za-z_]\w*=", words[0]):
        _sl_fail("a variable assignment")
    prog, args = words[0], words[1:]
    mother_bin = os.path.realpath(os.path.join(plugin, "bin", "mother"))
    is_mother = prog == "mother" or ("/" in prog and os.path.realpath(prog) == mother_bin) or (
        prog in ("python3", "python") and args and "/" in args[0] and os.path.realpath(args[0]) == mother_bin)
    plugin_script = prog in ("python3", "python") and args and "/" in args[0] and os.path.realpath(args[0]) in \
        {os.path.realpath(os.path.join(plugin, x)) for x in PLUGIN_SCRIPTS}
    if is_mother:
        if prog in ("python3", "python"):
            args = args[1:]
        _sl_mother(args)
    elif plugin_script or (prog == "claude" and args == ["mcp", "list"]):
        if st["heredocs"]:
            _sl_fail("a heredoc into %s" % prog)
        sl_hidden_paths(args[1:] if plugin_script else [], cwd, ws, plugin)
        for w in (args[1:] if plugin_script else []):
            if not w.startswith("-"):
                sl_path(w, cwd, ws, write=w.endswith(".html"))
        for op, target in st["redirs"]:
            if not target.startswith("&"):
                sl_hidden_paths([target], cwd, ws, plugin)
                sl_path(target, cwd, ws, write=">" in op)
        return cwd
    else:
        if st["heredocs"]:
            _sl_fail("a heredoc into %s (only `mother` reads one)" % prog)
        if "/" in prog:
            _sl_fail("a program by path (%s)" % prog)
        if prog not in SL_READ | SL_FILE | SL_GH | {"git", "cd"}:
            _sl_fail("%s is not on the allow-list" % prog)
    opts, ops = _sl_args(args)
    sl_hidden_paths(args, cwd, ws, plugin)
    for op, target in st["redirs"]:
        if target.startswith("&"):
            continue
        sl_hidden_paths([target], cwd, ws, plugin)  # a `<` source too: outside the workspace, plugin and temp asks
        sl_path(target, cwd, ws, write=">" in op)
    if prog == "cd":
        if not st.get("first") or st.get("sep") not in ("&&", ""):
            _sl_fail("cd other than as the first stage, joined by &&")
        if len(args) != 1:
            _sl_fail("cd needs one literal path")
        dest = os.path.realpath(os.path.join(cwd, args[0]))
        if not inside(dest, ws) or not os.path.isdir(dest):
            _sl_fail("cd out of the workspace or into a folder that doesn't exist")
        sl_path(args[0], cwd, ws)
        return dest
    if is_mother:
        for w in args:
            if not w.startswith("-") or "=" in w:
                sl_path(w.split("=", 1)[-1] if w.startswith("-") else w, cwd, ws)
        return cwd
    if prog in SL_GH:
        if not reader_bash_ok(prog, args) and args[:2] != ["auth", "status"]:
            _sl_fail("%s %s is not a read the allow-list takes" % (prog, " ".join(args[:2])))
        return cwd
    if prog == "git":
        _sl_git(args, cwd, ws)
        return cwd
    if prog == "find":
        _sl_find(args, cwd, ws)
        return cwd
    if prog in SL_OPTS:
        vals, ops = _sl_opts(prog, args)
        opts = [f for f, _ in vals]
        if prog == "sed":
            _sl_sed([v for f, v in vals if f == "-e"] or ops[:1])
            for w in (ops if any(f == "-e" for f in opts) else ops[1:]):
                sl_path(w, cwd, ws)
            return cwd
        if prog in ("grep", "egrep", "rg"):
            for f, v in vals:
                if f in ("-f", "--file"):
                    sl_path(v, cwd, ws)
            patterned = any(f in ("-e", "--regexp", "-f", "--file") for f in opts) or "--files" in opts
            for w in (ops if patterned else ops[1:]):
                sl_path(w, cwd, ws)
            return cwd
        if prog == "jq":
            if not ops and not any(f in ("-n", "--null-input") for f in opts):
                return cwd
            filt = ops[0] if ops else ""
            if re.search(r"\$ENV\b|(?<![.\w$])(?:env|import|include|input_filename|\$__loc__)\b", filt):
                _sl_fail("a jq filter that reads the environment or modules")
            for w in ops[1:]:
                sl_path(w, cwd, ws)
            return cwd
        if prog == "uniq" and len(ops) > 1:
            _sl_fail("uniq with an output file")
        if prog == "tr":
            return cwd
        for w in ops:
            sl_path(w, cwd, ws)
        return cwd
    if prog == "date" and any(o in ("-s", "--set") or o.startswith("--set=") for o in opts):
        _sl_fail("date -s")
    if prog in SL_FILE:
        if not ops:
            _sl_fail("%s with no path" % prog)
        if prog in ("cp", "mv") and any(re.match(r"^-[A-Za-z]*[rRa]", o) or o in ("--recursive", "--archive")
                                        for o in opts):
            _sl_fail("a recursive %s" % prog)
        if prog in ("cp", "mv") and any(not re.fullmatch(r"-[finvp]+", o) for o in opts):
            _sl_fail("%s with an option other than -f -i -n -v -p" % prog)
        if prog in ("mkdir", "rm", "touch") and any(not re.fullmatch({"mkdir": r"-[pv]+", "rm": r"-[rRfiv]+",
                                                                       "touch": r"-[c]+"}[prog], o) for o in opts):
            _sl_fail("%s with an option the allow-list doesn't take" % prog)
        targets = list(ops)
        if prog in ("cp", "mv") and len(ops) >= 2:
            dst = os.path.realpath(os.path.join(cwd, ops[-1]))
            if os.path.isdir(dst):  # a file copied or moved into a folder lands at folder/name
                targets = ops[:-1] + [os.path.join(ops[-1], os.path.basename(x.rstrip("/"))) for x in ops[:-1]]
        for w in targets:
            sl_path(w, cwd, ws, write=True)
        # sources, destinations and (mkdir -p) the folders that would be made: none may be, hold or sit above a
        # protected path, or a rename could swap a new memory/ or .git in
        checks = list(ops) + list(targets)
        if prog == "mkdir":
            for w in ops:
                d = os.path.realpath(os.path.join(cwd, w))
                while inside(d, ws) and d != ws and not os.path.exists(d):
                    checks.append(d)
                    d = os.path.dirname(d)
        for w in checks:
            p = os.path.realpath(os.path.join(cwd, w))
            if ws_protected_tree(p, ws):
                top = os.path.relpath(p, ws).split(os.sep)[0].casefold()
                _sl_fail("%s of a protected path, a folder above one or a folder holding one (%s)" % (prog, w),
                         "grants" if top == ".mother" else "persona")
        if prog == "rm" and any(re.match(r"^-[A-Za-z]*[rR]", o) or o == "--recursive" for o in opts):
            for w in ops:
                _sl_rm_tree(os.path.realpath(os.path.join(cwd, w)), ws)
        return cwd
    for w in ops:
        if prog in ("echo", "printf", "test", "[", "sleep", "date", "true", "false", "which"):
            continue  # text, not paths
        sl_path(w, cwd, ws)
    return cwd


# Options each read program may take (CONTRACT §5): (flags with no value, flags that take one). A short-option
# cluster is read letter by letter; a value-taking letter takes the rest of the cluster or the next word. Anything
# else asks: rg --pre/--hostname-bin, grep/sed/sort file and program options, tail -f, wc --files0-from, and so on.
SL_OPTS = {
    "rg": ({"-n", "-i", "-l", "-c", "-w", "-v", "-F", "-S", "--files", "--hidden", "--json", "--no-heading",
            "--line-number", "--ignore-case", "--files-with-matches", "--count", "--word-regexp", "--invert-match",
            "--fixed-strings", "--smart-case"},
           {"-e", "-g", "--glob", "-t", "--type", "-A", "-B", "-C", "--max-count", "-m", "--regexp"}),
    "grep": ({"-n", "-i", "-l", "-c", "-w", "-v", "-E", "-F", "-r", "-R", "-h", "-H", "-o", "-q", "-s"},
             {"-e", "-A", "-B", "-C", "-m", "--include", "--exclude", "-f"}),
    "sed": ({"-n", "-E"}, {"-e"}),
    "sort": ({"-n", "-r", "-u", "-f", "-V"}, {"-k", "-t"}),
    "jq": ({"-r", "-c", "-s", "-n", "-e", "-S", "--raw-output", "--compact-output", "--slurp", "--null-input",
            "--exit-status", "--sort-keys"}, {"--arg", "--argjson"}),
    "cut": ({"-s"}, {"-f", "-d", "-c", "-b"}),
    "tr": ({"-d", "-s", "-c", "-C"}, set()),
    "head": (set(), {"-n", "-c"}),
    "tail": ({"-r", "-f", "-F"}, {"-n", "-c"}),  # -f: a Monitor follows a log
    "wc": ({"-l", "-w", "-c", "-m"}, set()),
    "uniq": ({"-c", "-d", "-u", "-i"}, {"-f", "-s"}),
    "diff": ({"-u", "-q", "-r", "-N", "-i", "-w", "-b", "-B", "-a", "--brief"}, {"-U"}),
    "file": ({"-b", "-i", "-I", "-L", "-h", "--brief", "--mime", "--mime-type"}, set()),
}
SL_OPTS["egrep"] = SL_OPTS["grep"]
SL_FIND_FLAGS = {"-empty", "-print", "-print0", "-prune", "-not", "!", "-o", "-or", "-a", "-and", "(", ")", "-depth"}
SL_FIND_VALUES = {"-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth", "-mtime", "-mmin", "-newer",
                  "-size"}


def _sl_opts(prog, args):
    """([(flag, value or None)], operands) for an SL_OPTS program; fails on any flag not in its table."""
    flags, valued = SL_OPTS[prog]
    vals, ops, k, ended = [], [], 0, False
    while k < len(args):
        a = args[k]
        k += 1
        if ended or a == "-" or not a.startswith("-"):
            ops.append(a)
        elif a == "--":
            ended = True
        elif prog in ("head", "tail") and re.fullmatch(r"-\d+", a):
            vals.append(("-n", a[1:]))
        elif a.startswith("--"):
            name, eq, v = a.partition("=")
            if name in flags and not eq:
                vals.append((name, None))
            elif name in valued:
                if not eq:
                    if k >= len(args):
                        _sl_fail("%s %s with no value" % (prog, name))
                    v, k = args[k], k + 1
                if name in ("--arg", "--argjson"):  # jq: a name and a value
                    if k >= len(args):
                        _sl_fail("jq %s with no value" % name)
                    v, k = v + "=" + args[k], k + 1
                vals.append((name, v))
            else:
                _sl_fail("%s %s is not an option the allow-list takes" % (prog, name))
        else:
            j = 1
            while j < len(a):
                f = "-" + a[j]
                if f in flags:
                    vals.append((f, None))
                    j += 1
                elif f in valued:
                    v = a[j + 1:]
                    if not v:
                        if k >= len(args):
                            _sl_fail("%s %s with no value" % (prog, f))
                        v, k = args[k], k + 1
                    vals.append((f, v))
                    break
                else:
                    _sl_fail("%s %s is not an option the allow-list takes" % (prog, f))
    return vals, ops


def _sl_find(args, cwd, ws):
    """find: start paths, then only the predicates in SL_FIND_FLAGS / SL_FIND_VALUES (no -exec, -delete, -rm,
    -fprint, -files0-from, ...)."""
    k = 0
    while k < len(args) and not (args[k].startswith("-") or args[k] in ("!", "(", ")")):
        sl_path(args[k], cwd, ws)
        k += 1
    while k < len(args):
        a = args[k]
        if a in SL_FIND_FLAGS:
            k += 1
        elif a in SL_FIND_VALUES and k + 1 < len(args):
            if a == "-newer":
                sl_path(args[k + 1], cwd, ws)
            k += 2
        else:
            _sl_fail("find %s is not a predicate the allow-list takes" % a)


def _sl_rm_tree(p, ws):
    if p == ws or not inside(p, ws):
        _sl_fail("rm -r of the workspace root or outside it")
    count = 0
    for root, dirs, files in os.walk(p):
        for x in dirs + files:
            count += 1
            if count > 2000 or ws_protected(os.path.join(root, x), ws):
                _sl_fail("rm -r of a folder that holds a protected path")


def _sl_sed(scripts):
    """Each script may only print, delete, quit or substitute (no w/W/r/R/e commands, no s///w or s///e)."""
    for sc in scripts:
        for part in re.split(r";|\n", sc):
            if part.strip() and not SL_SED_PART.match(part):
                _sl_fail("a sed script the guard can't read as print/delete/substitute (%s)" % part.strip()[:30])


def _sl_git(args, cwd, ws):
    k, gitdir = 0, os.path.realpath(cwd)
    while k < len(args) and args[k].startswith("-"):
        a = args[k]
        if a == "-C" and k + 1 < len(args):
            gitdir = os.path.realpath(os.path.join(gitdir, args[k + 1]))
            k += 2
        elif a in ("--no-pager", "--no-optional-locks"):
            k += 1
        else:
            _sl_fail("the git option %s" % a)
    if gitdir != ws:  # a .git below the root brings its own config (fsmonitor, hooks, aliases)
        _sl_fail("git run anywhere but the workspace root (%s)" % gitdir)
    sub, rest = (args[k], args[k + 1:]) if k < len(args) else ("", [])
    if sub not in SL_GIT:
        _sl_fail("git %s is not on the allow-list" % (sub or "with no subcommand"))
    if sub == "stash" and rest != ["list"]:
        _sl_fail("git stash other than list")
    for a in rest:
        if (sub in ("diff", "log", "show") and re.match(r"^-o", a)) or \
                (sub == "commit" and (a in ("-F", "-t") or re.match(r"^-[A-Za-z]*[Ft]", a) or
                                 a.startswith(("--file", "--template")))) or \
                (sub == "blame" and a.startswith("--contents")):
            _sl_fail("git %s %s (reads a file into the commit)" % (sub, a))
    for a in rest:
        low = a.lower()
        if low.startswith(("-c", "--exec", "--upload-pack", "--receive-pack", "--repo", "--output", "--ext-diff",
                           "--textconv", "--config")) or "://" in a or re.match(r"^[\w.-]+@[\w.-]+:", a):
            _sl_fail("the git argument %s" % a)
    opts, ops = _sl_args(rest)
    if sub == "commit" and any(o.startswith("--amend") for o in opts):
        _sl_fail("git commit --amend (rewrites a commit that may be pushed)")
    if sub in ("push", "pull", "fetch"):
        if ops and ops[0] not in _git_remotes(ws):
            _sl_fail("git %s to %s, which is not a remote of the workspace" % (sub, ops[0]))
        for o in opts:  # rewriting or deleting what is on the remote, or every ref at once
            if (sub == "push" and (re.match(r"^-[A-Za-z]*[fd]", o) or o.split("=", 1)[0] in (
                    "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--all", "--branches",
                    "--tags", "--delete", "--prune"))) or (sub != "push" and o.split("=", 1)[0] in (
                    "--force", "--prune", "--prune-tags", "--refmap", "--update-head-ok")) or \
                    (sub != "push" and re.match(r"^-[A-Za-z]*[fpPu]", o) and o not in ("-q", "-v")):
                _sl_fail("git %s %s" % (sub, o))
        for r in ops[1:]:
            if r.startswith(("+", ":")) or (sub != "push" and ":" in r):
                _sl_fail("git %s with the refspec %s" % (sub, r))
        return
    if sub in ("add", "blame", "ls-files", "diff", "log", "show"):
        for w in ops:
            if os.path.exists(os.path.join(cwd, w)) or "/" in w or sub == "add":
                sl_path(w, cwd, ws)


def shell_unlisted(cmd, cwd, ctx):
    """None when every stage of a workspace session's shell command is on the allow-list; else (the first part it
    won't take, the kind of protected path it names or None); "grant" for `mother grant add`."""
    plugin = os.path.dirname(HERE)
    ws = os.path.realpath(ctx.ws)
    try:
        stages = sl_lex(cmd, plugin)
        here = os.path.realpath(cwd or ws)
        for k, st in enumerate(stages):
            st["first"] = k == 0
            here = _sl_stage(st, here, ws, ctx, plugin)
    except _Unlisted as e:
        return str(e), e.kind
    except Exception as e:  # an odd line the checker can't read is a question, never a pass
        return "a line the guard can't read (%s)" % type(e).__name__, None
    return None


def approve_line(inp):
    """True when any text in a session tool's input has a line starting `mother approve`."""
    def strings(v):
        if isinstance(v, str):
            yield v
        elif isinstance(v, dict):
            for x in v.values():
                for s in strings(x):
                    yield s
        elif isinstance(v, list):
            for x in v:
                for s in strings(x):
                    yield s
    import unicodedata
    for s in strings(inp):
        s = LINE_BREAKS_RE.sub("\n", "".join(ch for ch in s if unicodedata.category(ch) != "Cf"))
        if APPROVE_ANY_RE.search(s):
            return True
    return False


def mcp_navigates(tool, inp):
    """A browser navigation: a tool named …navigate…, or a batch tool whose actions include one."""
    if "navigate" in tokens_of(tool.split("__")[-1]):
        return True
    if "batch" in tokens_of(tool.split("__")[-1]):
        return '"navigate"' in json.dumps(inp) or re.search(r'"name":\s*"[^"]*navigate', json.dumps(inp)) is not None
    return False


MCP_NET_TOKENS = {"url", "http", "https", "fetch", "download", "web", "scrape", "page", "request", "browse",
                  "navigate", "crawl"}
HOST_LIKE_RE = re.compile(r"^(?:[a-z][\w+.-]*://|//)?(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]|$)", re.I)


URL_FIELDS = {"url", "uri", "target", "href", "endpoint", "link", "urls", "uris", "targets", "hrefs", "endpoints",
              "links", "host", "hosts", "server", "servers", "hook", "hooks", "webhook", "webhooks", "webhook_url"}


def mcp_host_field(v):
    """A url/target/href (or …_url) field whose value looks like a host, with or without a scheme."""
    if isinstance(v, dict):
        for k, x in v.items():
            name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(k)).lower()
            urlish = name in URL_FIELDS or name.endswith(("_url", "_uri", "_urls", "_uris", "_link", "_links"))
            vals = x if isinstance(x, list) else [x]
            if urlish and any(isinstance(y, str) and (HOST_LIKE_RE.match(y.strip()) or "://" in y) for y in vals):
                return True
            if mcp_host_field(x):
                return True
    elif isinstance(v, list):
        return any(mcp_host_field(x) for x in v)
    return False


def mcp_unknown_write(tool, inp):
    """An MCP call whose operation writes but that no domain claims (classify_mcp gave it no decision). The desktop
    app's own tools (`mcp__ccd_…`) reach only the manager's app and are left alone."""
    parts = tool.split("__")
    server = parts[1] if len(parts) > 2 else ""
    if server.startswith("ccd_"):
        return False
    toks = tokens_of(parts[-1])
    if domain_from(toks) or domain_from(tokens_of(server)) or domain_from_shape(inp):
        return False
    ops = [t for t in toks if t not in PRODUCT_WORDS]
    if not ops or ops[0] in READ_VERBS:
        return False
    return bool(set(ops) & (WRITE_VERBS | DELETE_VERBS | UNKNOWN_WRITE_VERBS))


def classify(tool, inp, cwd, ctx):
    if tool == "Bash":
        return classify_shell(str(inp.get("command", "")), cwd, ctx)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if fp:
            fp = os.path.realpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(fp)))
            if wa_inside(fp, ctx):
                return wa_act("state", "an edit to the WhatsApp bridge's state: " + fp)
            if wa_config_path(fp, ctx) and wa_block_edit(tool, inp, fp):
                return wa_act("terminal", "an edit that changes the whatsapp block of config.json (MOTHER's "
                                          "WhatsApp setup)")
            if fp.casefold() in protected_paths(ctx):
                return act("config", "guard_config", "an edit to the guard's own files: " + fp)
            if ctx.ws and fp.casefold() == _cf(os.path.join(ctx.ws, "capabilities.json")):
                return act("config", "guard_config", "an edit to capabilities.json (written only by `mother health`): "
                           + fp)
            if home_settings_path(fp):
                return act("home_settings", "home_settings", "an edit to Claude Code's settings: " + fp)
            if fp.casefold() == grants_path(ctx):
                return act("grants", "grants_file", "an edit to .mother/grants.json")
            if persona_path(fp, ctx):
                return act("persona", "persona_files", "an edit to the workspace's standing instructions: " + fp)
        return None
    if tool == "Monitor":
        a = classify_shell(str(inp.get("command") or ""), cwd, ctx)
        if a is None and inp.get("ws") not in (None, "", {}, []) and in_workspace(cwd, ctx):
            url = inp["ws"].get("url") if isinstance(inp["ws"], dict) else inp["ws"]
            a = act("outbound_unknown", "outbound_unknown", "a Monitor websocket to %s" % str(url)[:120])
        return a
    if tool == "WebFetch":
        if in_workspace(cwd, ctx):
            return act("outbound_unknown", "outbound_unknown", "a web fetch of %s" % str(inp.get("url") or "?")[:160])
        return None
    if tool == "WebSearch":
        q = str(inp.get("query") or "")
        if in_workspace(cwd, ctx) and URLISH_RE.search(q):
            return act("outbound_unknown", "outbound_unknown", "a web search naming a URL: %s" % q[:160])
        return None
    if tool in IN_APP_SESSION_TOOLS:
        # a `mother approve` line in a card brief or a session message could read as the manager's own approval
        if approve_line(inp):
            return act("approve-line", "session_approvals", "a session message or card brief with a line that "
                                                            "starts `mother approve` (%s)" % tool)
        # not outbound, but a token pasted into a message or card brief is still caught
        if SECRET_RE.search(json.dumps(inp)):
            return act("credential", "credentials", "a session message or brief whose content looks like a "
                                                    "credential (%s)" % tool)
        return session_target(tool, inp, ctx, cwd)
    if not tool.startswith("mcp__"):
        return None
    if any(SENSITIVE_KEYS.match(str(k)) for k in inp.keys()):
        return act("credential", "credentials", "a tool call carrying a password or card field (%s)" % tool)
    if tool.split("__")[-1] == "run_in_terminal":  # a shell in the manager's terminal panel: read as Bash
        return classify_shell(str(inp.get("command") or ""), cwd, ctx)
    a = classify_mcp(tool, inp)
    if a and a["kind"] not in ("draft", "deletion") and SECRET_RE.search(json.dumps(inp)):
        return act("credential", "credentials", "an outbound call whose content looks like a credential (%s)" % tool)
    if a is None and in_workspace(cwd, ctx):
        if mcp_navigates(tool, inp):
            return act("outbound_unknown", "outbound_unknown", "a browser navigation (%s)" % tool)
        toks = set(tokens_of(tool.split("__")[-1]))
        server = tool.split("__")[1] if tool.count("__") >= 2 else ""
        if not server.startswith("ccd_") and (toks & MCP_NET_TOKENS or mcp_host_field(inp)):
            return act("outbound_unknown", "outbound_unknown", "a tool that reaches a web address (%s)" % tool)
        if toks == {"form", "input"} or (toks == {"computer"} and str(inp.get("action")) in ("type", "key")):
            return act("outbound_unknown", "outbound_unknown", "typing into a page (%s)" % tool)
        if toks & {"javascript", "evaluate", "eval", "js"}:
            return act("outbound_unknown", "outbound_unknown", "script run in a page (%s)" % tool)
        if "fetch" in toks and (hub_bad_field(inp) or "://" in json.dumps(inp)):
            return act("outbound_unknown", "outbound_unknown", "a fetch of a URL (%s)" % tool)
        if mcp_unknown_write(tool, inp):
            return act("outbound_unknown", "outbound_unknown", "a write through %s, which the guard can't place "
                                                              "(chat, mail, calendar, tracker, code, docs)" % tool)
    return a


# ------------------------------------------------------------------ green list


def is_dm(a, cfg):
    """Every target the call names is the manager's own (DM channel or user id); none named is not the DM."""
    targets = a.get("targets") if a.get("targets") is not None else ([a["channel"]] if a.get("channel") else [])
    ids = dm_ids(cfg)
    return bool(targets) and all(str(t) in ids for t in targets)


def green(a, ctx, auto):
    if a["kind"] == "chat" and is_dm(a, ctx.cfg) and "own_dm" in auto:
        return "own_dm", "the manager's own DM"
    if a["kind"] == "draft" and "drafts" in auto:
        return "drafts", "a draft delivers nothing"
    if a["kind"] == "calendar" and a.get("guests") is not None and "own_calendar_no_guests" in auto:
        me = {e.lower() for e in as_list((ctx.cfg.get("manager") or {}).get("email"))}
        if all(e in me for e in a["guests"]):
            return "own_calendar_no_guests", "no guests besides the manager"
    return None


# ------------------------------------------------------------------ session approvals (attended runs)

APPROVE_RE = re.compile(r"^\s*mother approve (tracker|chat) ([A-Za-z0-9_-]+)\s*$", re.I)
APPROVE_ANY_RE = re.compile(r"^\s*mother\s*approve\b", re.I | re.M)
LINE_BREAKS_RE = re.compile("[\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
SESSION_APPROVAL_MAX_HOURS = 12


def user_text(entry):
    """The text of a message a human typed, or None for anything else.

    Transcripts also hold `type: user` entries no human wrote: tool results, meta and compaction summaries,
    sidechain (subagent) entries, peer-session hand-backs and scheduled triggers. Only an entry whose
    origin.kind is "human" counts; an entry without an origin fails closed."""
    if not isinstance(entry, dict) or entry.get("type") != "user" or entry.get("isMeta") \
            or entry.get("isCompactSummary") or entry.get("isSidechain") or "toolUseResult" in entry:
        return None
    if not isinstance(entry.get("origin"), dict) or entry["origin"].get("kind") != "human":
        return None
    msg = entry.get("message") or {}
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return None


def session_approvals(transcript, max_hours=SESSION_APPROVAL_MAX_HOURS, now=None):
    """{(scope, target)} from the leading `mother approve …` lines of genuine user messages."""
    found = set()
    if not isinstance(transcript, str) or not os.path.isfile(transcript):
        return found
    now = time.time() if now is None else now
    with open(transcript, errors="replace") as f:
        for line in f:
            if "mother approve" not in line.lower():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            text = user_text(e)
            if not text:
                continue
            ts = parse_time(e.get("timestamp"))
            if ts is None or now - ts > max_hours * 3600 or ts > now + 60:
                continue
            for ln in text.strip().splitlines():  # only the leading block of approve lines counts
                m = APPROVE_RE.match(ln)
                if not m:
                    break
                scope = m.group(1).lower()
                found.add((scope, m.group(2).upper() if scope == "tracker" else m.group(2)))
    return found


def covered_by_session(a, approvals, cfg):
    if a["kind"] == "chat":
        return a.get("op") in ("post", "reply", "edit") and bool(a.get("channel")) \
            and ("chat", a["channel"]) in approvals
    if a["kind"] == "tracker":
        projs = a.get("projects") or set()
        if projs:
            return None not in projs and all(("tracker", str(p).upper()) in approvals for p in projs)
        if a.get("op") == "sprint" and a.get("board") is not None:
            approved = {p for s, p in approvals if s == "tracker"}
            for t in cfg.get("teams") or []:
                tr = t.get("tracker") if isinstance(t, dict) else None
                if isinstance(tr, dict) and str(tr.get("project", "")).upper() in approved \
                        and str(tr.get("board")) == str(a["board"]):
                    return True
    return False


# ------------------------------------------------------------------ grants (unattended runs)


def grant_key_path():
    """The HMAC key `mother grant` signs grants.json with: <state>/grant.key, outside the workspace."""
    return os.path.join(os.environ.get("MOTHER_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".mother"),
                        "grant.key")


def grant_key(create=False):
    """The key bytes, or None. With create, makes it (0600, 32 random bytes) when missing."""
    p = grant_key_path()
    try:
        with open(p, "rb") as f:
            k = f.read().strip()
        if k:
            return k
    except FileNotFoundError:
        pass
    except Exception:
        return None
    if not create:
        return None
    import secrets
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_hex(32).encode())
    except FileExistsError:
        pass
    with open(p, "rb") as f:
        return f.read().strip() or None


def grants_mac(grants, key):
    import hashlib
    import hmac
    body = json.dumps(grants, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def sign_grants(doc, key):
    """doc with `sig` set over its `grants` list."""
    doc = dict(doc)
    doc["sig"] = grants_mac(doc.get("grants") or [], key)
    return doc


def grants_signed(doc):
    """True when grants.json's `sig` is the HMAC of its grants under the grant key."""
    import hmac
    key = grant_key()
    sig = doc.get("sig") if isinstance(doc, dict) else None
    return bool(key) and isinstance(sig, str) and hmac.compare_digest(sig, grants_mac(doc.get("grants"), key))


def read_grants_for_write(path, ws=None, who="mother grant"):
    """(doc, problem) for a writer that is about to change grants.json: a missing file is empty; a file that parses
    but whose signature doesn't verify is treated as empty too (its grants are dropped, never re-signed) and one
    line goes to guard.log; an unparseable file is a problem the caller reports, never overwrites."""
    if not os.path.exists(path):
        return {"grants": []}, None
    data, ok = read_json(path)
    if not ok:
        return None, "grants.json is unreadable; fix or remove it by hand"
    if isinstance(data, dict) and isinstance(data.get("grants"), list) and grants_signed(data):
        return {"grants": list(data["grants"])}, None
    if ws:
        try:
            with open(os.path.join(ws, ".mother", "guard.log"), "a") as f:
                f.write(json.dumps({"at": now_iso(), "tool": who, "kind": "grants", "category": "grants_file",
                                    "target": None, "decision": "reset",
                                    "reason": "grants.json had no valid signature: its grants were dropped"}) + "\n")
        except Exception:
            pass
    return {"grants": []}, None


def grant_scope_of(a):
    """(scope, target, n) for an action a grant could cover, or None."""
    if a["kind"] == "chat":
        if a.get("op") == "post" and not a.get("thread"):
            return "chat-post", a.get("channel"), 1
        if a.get("op") == "reply" and a.get("thread") and not a.get("broadcast"):
            return "chat-reply", a.get("channel"), 1
        return None
    if a["kind"] == "tracker" and a.get("op") in ("create", "comment", "transition"):
        projs = a.get("projects") or set()
        if len(projs) != 1 or None in projs:
            return None
        return "tracker-" + a["op"], next(iter(projs)), (a.get("creates") or 1) if a["op"] == "create" else 1
    return None


def allowed_targets(scope, cfg):
    if scope == "answer":
        dms = dm_ids(cfg)
        return {cid for cid, ch in team_channels(cfg).items()
                if ch.get("external") is not True and ch.get("mode") in ("mention", "auto") and cid not in dms}
    if scope.startswith("chat-"):
        dms = dm_ids(cfg)
        return {cid for cid, ch in team_channels(cfg).items()
                if ch.get("external") is not True and ch.get("mode") != "off" and cid not in dms}
    return tracker_projects(cfg)


class Ledger(object):
    """.mother/ledger.json: {"version": 1, "uses": [...]}. A corrupt ledger disables grants (fail closed)."""

    def __init__(self, ws):
        self.dir = os.path.join(ws, ".mother")
        self.path = os.path.join(self.dir, "ledger.json")
        self.lock = None

    def __enter__(self):
        os.makedirs(self.dir, exist_ok=True)
        self.lock = open(os.path.join(self.dir, "ledger.lock"), "a")
        if fcntl:
            fcntl.flock(self.lock, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if fcntl:
                fcntl.flock(self.lock, fcntl.LOCK_UN)
        finally:
            self.lock.close()

    def read(self):
        data, ok = read_json(self.path)
        if not ok:
            return None
        if data is None:
            return {"version": 1, "uses": []}
        if not isinstance(data, dict) or not isinstance(data.get("uses"), list):
            return None
        return data

    def write(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)


def team_people(cfg, channel):
    """Chat ids of the people on the team(s) that own `channel`."""
    ids = set()
    for t in cfg.get("teams") or []:
        if not isinstance(t, dict):
            continue
        if any(isinstance(c, dict) and str(c.get("id")) == str(channel) for c in t.get("channels") or []):
            ids |= {str(p["chat"]) for p in t.get("people") or [] if isinstance(p, dict) and p.get("chat")}
    return ids


def may_trigger(cfg, channel, user):
    """Whether `user` may make the agent answer in `channel` (CONTRACT §3 channels: mode + trigger)."""
    ch = team_channels(cfg).get(str(channel)) or {}
    if ch.get("external") is True:
        return False
    if str(user) == str(owner_id(cfg)):
        return ch.get("mode") in ("mention", "auto")
    if ch.get("mode") == "auto":
        return True
    if ch.get("mode") != "mention":
        return False
    trig = ch.get("trigger", "manager")
    return trig == "anyone" or (trig == "team" and str(user) in team_people(cfg, channel))


def valid_grant(g, scope, target, cfg, scopes, max_age, now, thread=None):
    """The grant's (source key, cap) if it is well-formed and covers (scope, target); else None."""
    if not isinstance(g, dict) or g.get("scope") != scope or str(g.get("target")) != str(target):
        return None
    if (scope == "answer" or g.get("thread")) and (not thread or str(g.get("thread")) != str(thread)):
        return None  # a thread-bound grant (every answer grant) covers that one thread
    src = g.get("source")
    if not isinstance(src, dict):
        return None
    channel, user = src.get("channel"), src.get("user")
    ts = parse_time(src.get("ts"))
    uses = g.get("uses")
    expires = parse_time(g.get("expires"))
    if not channel or not user or ts is None or expires is None:
        return None
    if not isinstance(uses, int) or isinstance(uses, bool) or uses < 1:
        return None
    if scope == "answer":
        if str(channel) != str(target) or not may_trigger(cfg, target, user):
            return None  # a teammate's message authorises only an answer in that channel, per its mode
    elif str(user) != str(owner_id(cfg)):
        return None  # only the manager's own message can authorise a write
    if str(channel) not in dm_ids(cfg) | set(team_channels(cfg)):
        return None
    if not (now - max_age * 3600 <= ts <= now + 60):
        return None  # the cited message is too old, or in the future
    if not (now < expires <= ts + max_age * 3600):
        return None  # expired, or expiring later than the policy allows
    return "%s|%s" % (channel, src.get("ts")), min(uses, scopes[scope])


def run_grant(a, ctx, now=None):
    """Consume one use of a matching grant; return a description, or None."""
    sc = grant_scope_of(a)
    if not sc or not ctx.ws:
        return None
    found = None
    for cand in [sc] + ([("answer", sc[1], 1)] if sc[0] == "chat-reply" else []):
        found = run_grant_scope(a, ctx, cand, now)
        if found:
            break
    return found


def answers_last_hour(book, target, now):
    n = 0
    for r in book["uses"]:
        if isinstance(r, dict) and r.get("scope") == "answer" and r.get("target") == target:
            t = parse_time(r.get("at"))
            if t is not None and now - t < 3600:
                n += 1
    return n


def run_grant_scope(a, ctx, sc, now=None):
    scope, target, n = sc
    scopes, max_age = grant_limits(ctx.cfg)
    if scope not in scopes or not target or target not in allowed_targets(scope, ctx.cfg):
        return None
    if scope in ("chat-post", "chat-reply", "tracker-comment", "answer"):
        agent = ctx.cfg.get("agent") or {}
        marks = ["%s (AI)" % agent["name"]] if agent.get("name") else []
        if isinstance(agent.get("signature"), str) and agent["signature"].strip():
            marks.append(agent["signature"].strip())
        if not any(m in (a.get("text") or "") for m in marks):
            return None  # unattended writes must carry the disclosure signature
        if a.get("blocks") and not any(m in a["blocks"] for m in marks):
            return None  # and so must the blocks, which are what the reader actually sees
    data, ok = read_json(os.path.join(ctx.ws, ".mother", "grants.json"))
    if not ok or not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        return None
    if not grants_signed(data):
        return None  # unsigned or forged: as if there were no grants
    now = time.time() if now is None else now
    with Ledger(ctx.ws) as led:
        book = led.read()
        if book is None:
            return None
        if scope == "answer":
            ch = team_channels(ctx.cfg).get(str(target)) or {}
            cap = ch.get("max_replies_per_hour", 6)
            if not isinstance(cap, int) or isinstance(cap, bool) or answers_last_hour(book, target, now) >= cap:
                return None  # rate limit, enforced here too in case the loop's own check is bypassed
        for g in data["grants"]:
            v = valid_grant(g, scope, target, ctx.cfg, scopes, max_age, now, thread=a.get("thread"))
            if not v:
                continue
            key, cap = v
            used = 0
            for r in book["uses"]:
                if isinstance(r, dict) and r.get("scope") == scope and r.get("source") == key:
                    used += r.get("n", 1) if isinstance(r.get("n", 1), int) else 1
            if used + n > cap:
                continue
            book["uses"].append({"at": now_iso(), "scope": scope, "target": target, "source": key, "n": n,
                                 "run": g.get("run")})
            led.write(book)
            whose = "the message" if scope == "answer" else "the manager's message"
            return "run grant %s on %s citing %s %s (%d/%d used)" % (scope, target, whose, key, used + n, cap)
    return None


# ------------------------------------------------------------------ decision


def bypass_passes(ctx):
    """policy.bypass_passes_asks is true (an invalid config counts as false)."""
    return bool(ctx.ok) and ((ctx.cfg.get("policy") or {}).get("bypass_passes_asks") is True)


def paused(ctx):
    if not ctx.ws:
        return None
    p = os.path.join(ctx.ws, ".mother", "PAUSED")
    return p if os.path.exists(p) else None


def decide_whatsapp(a, ctx):
    """A send asks like a chat message (own chat: allow while own_dm is auto); everything else denies."""
    op, what = a.get("op"), a["what"]
    if op == "state":
        return "deny", "%s. Only `mother whatsapp` and the bridge touch it; send with `mother whatsapp send`." % what
    if op == "terminal":
        return "deny", "%s: the manager runs it in their own terminal, never the agent. Tell them to run it in " \
                       "their own terminal." % what
    if op == "unknown":
        return "deny", "%s. See `mother whatsapp help`." % what
    if op == "compound":
        return "deny", "%s. Run `mother whatsapp send --to WHO --text '...'` alone, as the whole command." % what
    if op == "computed":
        return "deny", "%s: the guard can't tell what it runs. Write the command name out plainly (a variable " \
                       "is fine as an argument, or set it earlier in the same line)." % what
    if op != "send":
        return "deny", "%s: not a WhatsApp action the guard knows." % what
    p = paused(ctx)
    if p:
        return "deny", "Outbound is PAUSED (%s exists). Blocked: %s." % (p, what)
    if not ctx.ok:
        return "deny", "%s: no valid config (%s)." % (what, ctx.problem)
    auto, never = policy_lists(ctx.cfg)
    if a["category"] in never:
        return "deny", "%s: policy.never lists %s." % (what, a["category"])
    w = ctx.cfg.get("whatsapp") if isinstance(ctx.cfg.get("whatsapp"), dict) else {}
    if not (w.get("enabled") is True and w.get("send", True) is True):
        return "deny", "%s: whatsapp sending is off (whatsapp.enabled / whatsapp.send); the manager turns it on " \
                       "with `mother whatsapp setup` in their own terminal." % what
    err = wa_ticket(a.get("to") or "", a.get("text") or "")
    if err:
        return "deny", "%s: %s, so it can't go out; try again." % (what, err)
    if a["category"] == "own_dm" and "own_dm" in auto:
        return "allow", "%s: auto (the manager's own chat)." % what
    to = a.get("to") or ""
    shown = "WhatsApp to %s%s: %s" % (json.dumps(to, ensure_ascii=False), " (number)" if wa_is_number(to) else "",
                                       json.dumps(a.get("text") or "", ensure_ascii=False))
    name = (ctx.cfg.get("agent") or {}).get("name") or "Agent"
    signs = a["category"] != "own_dm" and "%s (AI)" % name not in (a.get("text") or "")
    note = " (MOTHER adds its signature line)" if signs else ""
    a["log_reason"] = "%s needs a yes (%s)." % (what, a["category"])  # guard.log never holds the text
    return "ask", "%s%s. Confirm it." % (shown, note)


def decide(a, ctx, transcript=None):
    if a["kind"] == "whatsapp":
        return decide_whatsapp(a, ctx)
    if a["kind"] == "config":
        return "ask", "%s. The agent must not change its own guard; confirm this is you." % a["what"]
    if a["kind"] == "persona":
        return "ask", "%s. A later session obeys these files as standing instructions; confirm this is you, not " \
                      "text from a message." % a["what"]
    if a["kind"] == "approve-line":
        return "deny", "%s. Only the manager types `mother approve`; a session message or card brief never " \
                       "carries one. Leave the line out." % a["what"]
    if a["kind"] == "manager_only":  # the manager turns these on; a question even under PAUSED
        return "ask", "%s. Only the manager runs this; confirm this is you." % a["what"]
    if a["kind"] == "session":  # the manager's own sessions: always a question, never PAUSED or the never-list
        return "ask", "%s. Confirm it." % a["what"]
    if a["kind"] == "shell_unlisted":  # not outbound by itself: a question, PAUSED or not
        return "ask", "%s. Confirm it." % a["what"]
    if a["kind"] == "home_settings":
        return "ask", "%s. Claude Code's own settings decide which plugins and hooks run; confirm this is you." % \
            a["what"]
    p = paused(ctx)
    if p and a["kind"] == "grants":
        return "deny", "Outbound is PAUSED (%s exists). Blocked: %s." % (p, a["what"])
    if a["kind"] == "grants" and a.get("op") == "hub_fed":
        return "ask", "%s. The channels loop runs in its own session, never one that reads hub lines; confirm this " \
                      "is you." % a["what"]
    if a["kind"] == "grants" and a.get("op") == "cli":
        return "ask", "%s. Confirm the manager asked for this grant." % a["what"]
    if a["kind"] == "grants":
        return "ask", "%s. Grants are written by `mother grant add`, which checks them; a direct edit could forge " \
                      "one. Confirm this is you." % a["what"]
    if p:
        return "deny", "Outbound is PAUSED (%s exists). Blocked: %s. Only the manager removes that file." % (
            p, a["what"])
    if a["category"] in ALWAYS_NEVER:
        return "deny", "%s is on the never-list (%s). Do it by hand if it is really needed." % (
            a["what"], a["category"])
    if not ctx.ok:
        return "ask", "%s. No green list (%s): confirm it." % (a["what"], ctx.problem)
    auto, never = policy_lists(ctx.cfg)
    if a["category"] in never:
        return "deny", "%s: policy.never lists %s." % (a["what"], a["category"])
    g = green(a, ctx, auto)
    if g:
        return "allow", "%s: auto (%s)." % (a["what"], g[1])
    if a["kind"] in ("chat", "tracker") and covered_by_session(a, session_approvals(transcript), ctx.cfg):
        return "allow", "%s: covered by the manager's `mother approve` line in this session." % a["what"]
    if a["kind"] == "outbound_unknown":
        return "ask", "%s: the guard can't tell where this sends data or who sees it. Confirm it." % a["what"]
    why = run_grant(a, ctx)
    if why:
        return "allow", "%s: %s." % (a["what"], why)
    return "ask", "%s reaches someone other than the manager (needs a yes: %s). Confirm it." % (
        a["what"], a["category"])


def _server_of(tool):
    """The `__`-separated segments of an MCP tool name, without the tool's own last segment."""
    return tool.split("__")[:-1]


def stamp_block(tool, a, ctx):
    """A chat write (a message, or a canvas/topic/purpose change) through a connector that stamps its own
    attribution may only go to the manager's DM. Reactions carry no text, so no stamp, and are left to decide(); so
    is a channel-less canvas edit.
    Matches whole `__` segments (mcp__gw__slack matches mcp__gw__slack__post, not mcp__gw__slackbot__post).
    An empty or unknown channel is denied: we can't show it is the DM. Returns (decision, reason) or None."""
    if a.get("kind") != "chat" or a.get("op") not in ("post", "reply", "schedule", "edit", "other"):
        return None
    if a["op"] == "other" and not a.get("channel"):
        return None  # a canvas made or edited without a channel reaches no one until it is shared
    server = _server_of(tool)
    for prefix in sorted(getattr(ctx, "stamps", None) or ()):
        segs = prefix.split("__")
        if server[:len(segs)] == segs:
            if is_dm(a, ctx.cfg):
                return None
            return "deny", ("%s goes through a connector that adds its own stamp (%s). Send it through the "
                            "preferred chat server, or ask the manager." % (a["what"], prefix))
    return None


def log(ctx, tool, a, decision, reason):
    if not ctx.ws:
        return
    try:
        d = os.path.join(ctx.ws, ".mother")
        os.makedirs(d, exist_ok=True)
        rec = {"at": now_iso(), "tool": tool, "kind": a.get("kind") if a else None,
               "category": a.get("category") if a else None,
               "target": (a.get("channel") or a.get("target") or ",".join(sorted(str(x) for x in (a.get("projects") or []) if x))
                          or None) if a else None,
               "decision": decision, "reason": reason}
        if a and a.get("log_reason"):  # a WhatsApp ask shows the text to the manager; the log never keeps it
            rec["reason"] = ("bypassPermissions: " if decision == "pass" else "") + a["log_reason"]
        with open(os.path.join(d, "guard.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ channel reply sessions

CHANNEL_PRIVATE = ["people", "journal", "memory", "todos", ".mother", "bridge"]
CHANNEL_NOTE_DIRS = ("todos", "memory", "projects", "teams", "people")
CHANNEL_NO_TOOLS = {"Bash", "Monitor", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"}


def inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def channel_session(tool, inp, cwd, ctx, role):
    """`mother channels run` sets MOTHER_CHANNEL_ROLE on the headless session that writes one chat answer. Such
    a session writes nothing outbound — the loop posts its reply — and a teammate's session reads only work data:
    the workspace minus private folders, plus the servers in MOTHER_CHANNEL_SERVERS. Enforced here, so neither
    the user's own allow rules nor a green-list entry can widen it. Returns (decision, reason) or None."""
    who = "a channel session (asked by %s)" % ("the manager" if role == "manager" else "a teammate")
    if not ctx.ws:
        return "deny", "%s has no workspace: refusing %s." % (who, tool)
    ws = os.path.realpath(ctx.ws)
    plugin = os.path.realpath(os.path.dirname(HERE))
    if tool in CHANNEL_NO_TOOLS or tool in IN_APP_SESSION_TOOLS:
        return "deny", "%s can't use %s." % (who, tool)
    if tool in ("Write", "Edit", "MultiEdit"):
        fp = os.path.realpath(os.path.join(cwd or ws, os.path.expanduser(str(inp.get("file_path") or ""))))
        if role != "manager":
            return "deny", "%s can't change files." % who
        rel = os.path.relpath(fp, ws) if inside(fp, ws) else None
        # An allow-list, not a deny-list: anything else in a workspace (.mcp.json, skills/, scripts/, pointer
        # files) is code or config that a later session would run.
        if rel is None or rel.split(os.sep)[0] not in CHANNEL_NOTE_DIRS or not rel.endswith(".md"):
            return "deny", "%s may write only notes (*.md under %s), not %s." % (
                who, ", ".join(d + "/" for d in CHANNEL_NOTE_DIRS), fp)
        if persona_path(fp, ctx):
            return "ask", "%s: an edit to memory/MEMORY.md, which later sessions obey; confirm it." % who
        return None
    if tool in ("Read", "Grep", "Glob", "LS"):
        raw = inp.get("file_path") or inp.get("path") or ""
        fp = os.path.realpath(os.path.join(cwd or ws, os.path.expanduser(str(raw)))) if raw else os.path.realpath(
            cwd or ws)
        if inside(fp, plugin):
            return None
        if not inside(fp, ws):
            return "deny", "%s reads only the workspace: %s is outside it." % (who, fp)
        if role != "manager":
            top = os.path.relpath(fp, ws).split(os.sep)[0]
            if top in CHANNEL_PRIVATE:
                return "deny", "%s can't read %s/: it holds the manager's private notes." % (who, top)
            if tool in ("Grep", "Glob") and fp == ws:
                return "deny", "%s searches a folder, not the whole workspace (private folders live there)." % who
            pats = [str(inp.get(k) or "") for k in ("pattern", "glob")] if tool == "Glob" else [str(inp.get("glob") or "")]
            if any(".." in x or x.startswith(("/", "~")) for x in pats):
                return "deny", "%s: a search pattern can't leave its folder." % who
        return None
    if tool.startswith("mcp__"):
        a = classify(tool, inp, cwd, ctx)
        if a is not None:
            return "deny", "%s writes nothing (%s): the loop posts the reply." % (who, a["what"])
        if role != "manager":
            # Prefixes, not server names: one gateway server can carry chat, mail and the tracker side by side
            # (mcp__gw__jira__… next to mcp__gw__gmail__…), so the prefix includes the product segment.
            ok = [p.strip() for p in os.environ.get("MOTHER_CHANNEL_SERVERS", "").split(",") if p.strip()]
            if not any(tool.startswith(p + "__") for p in ok):
                return "deny", "%s can't use %s: only the tracker and code-host tools." % (who, tool)
        return None
    return None


# ------------------------------------------------------------------ MOTHER's own sub-agents

# agents/*.md. Plugin agents can't carry their own hooks, so this is where their limits live. Hooks run inside
# sub-agents, and the payload names the agent in `agent_type`, scoped ("mother:reader") or bare.
SUBAGENT_TOOLS = {"reader": {"Read", "Grep", "Glob", "LS", "Bash"}, "critic": {"Read", "Grep", "Glob", "LS"}}
DRY_RUN_ENV = "MOTHER_DRY_RUN"


def mother_subagent(payload):
    t = str(payload.get("agent_type") or "").strip()
    if not t:
        return None
    if t.startswith("mother:"):
        t = t[len("mother:"):]
    return t if t in SUBAGENT_TOOLS else None


# The reader's code-source reads (CONTRACT §5c). The hub's exact shapes (HUB_GH_SHAPES) pass too. Anything that
# could name another host or send data in a header or body is refused.
READER_GH_SHAPES = {
    "gh": {("pr", "list"), ("pr", "view"), ("pr", "checks"), ("pr", "status"), ("pr", "diff"), ("issue", "list"),
           ("issue", "view"), ("issue", "status"), ("search", "prs"), ("search", "issues"), ("run", "list"),
           ("run", "view"), ("repo", "view"), ("release", "list"), ("release", "view"), ("api", None)},
    "glab": {("mr", "list"), ("mr", "view"), ("issue", "list"), ("issue", "view"), ("ci", "list"), ("ci", "view"),
             ("ci", "status"), ("repo", "view"), ("release", "list"), ("release", "view"), ("api", None)}}
READER_BAD_FLAGS = {"--hostname", "-H", "--header", "--web", "-w", "--input", "-f", "-F", "--field",
                    "--raw-field", "-X", "--method", "--browser"}


def reader_argv(cmd):
    """argv of one plain command: words of plain characters, single-quoted strings, or double-quoted strings
    with no `$`, backtick, backslash or `!`; nothing else (no expansion, chaining, redirect, glob or comment)."""
    if hub_odd_chars(cmd) or "\n" in cmd.strip():
        return None
    argv, i, n, word, have = [], 0, len(cmd.strip()), [], False
    cmd = cmd.strip()
    while i < n:
        c = cmd[i]
        if c == " " or c == "\t":
            if have:
                argv.append("".join(word))
            word, have = [], False
            i += 1
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                return None
            word.append(cmd[i + 1:j])
            have, i = True, j + 1
        elif c == '"':
            j = cmd.find('"', i + 1)
            if j < 0 or re.search(r"[$`\\!]", cmd[i + 1:j]):
                return None
            word.append(cmd[i + 1:j])
            have, i = True, j + 1
        elif re.match(r"[A-Za-z0-9_.:/@=,+%^#?-]", c) and not (c == "#" and not have):
            word.append(c)
            have, i = True, i + 1
        else:
            return None
    if have:
        argv.append("".join(word))
    return argv or None


def reader_bash_ok(prog, args):
    """`date`, or a gh/glab read: a shape in READER_GH_SHAPES (an `api` call only GET, to a relative endpoint),
    no flag that names a host, adds a header or sends a body, `--repo`/`-R` only as owner/repo, no URL anywhere."""
    if prog == "date":
        return not any(a in ("-s", "--set") or a.startswith("--set=") for a in args)
    if prog not in READER_GH_SHAPES:
        return False
    if prog in HUB_GH_SHAPES and hub_gh_ok(prog, list(args)):
        return True
    if not args or (args[0], args[1] if len(args) > 1 else None) not in READER_GH_SHAPES[prog] \
            and (args[0], None) not in READER_GH_SHAPES[prog]:
        return False
    rest = list(args[1:]) if args[0] == "api" else list(args[2:])
    if args[0] == "api":
        if not rest or rest[0].startswith("-") or not re.fullmatch(r"/?[A-Za-z0-9_./{}?=&,:%+-]+", rest[0]) \
                or rest[0].lstrip("/").startswith("graphql"):
            return False
    for k, a in enumerate(rest):
        flag = a.split("=", 1)[0]
        if "://" in a or flag in READER_BAD_FLAGS or (flag.startswith("-") and not flag.startswith("--")
                                                    and any(ch in flag for ch in "HfFXw")):
            return False
        if flag in ("--repo", "-R"):
            val = a.split("=", 1)[1] if "=" in a else (rest[k + 1] if k + 1 < len(rest) else "")
            if not HUB_REPO_RE.match(val):
                return False
    return True


def subagent_session(kind, tool, inp, cwd, ctx):
    """A `mother:reader` gathers one source and hands back JSON; a `mother:critic` reads a draft and answers
    PASS or fixes. Both are read-only: MOTHER is the single writer. Every classified write is denied — the
    manager's own DM and drafts included — and so is any tool outside the agent's list. Returns
    (decision, reason) or None (not outbound: normal permissions)."""
    who = "the %s sub-agent" % kind
    if tool in IN_APP_SESSION_TOOLS:
        return "deny", "%s can't use %s: it reaches the manager's sessions." % (who, tool)
    if tool.startswith("mcp__"):
        if kind == "critic":
            return "deny", "%s reads only the files it is given, not %s." % (who, tool)
        a = classify(tool, inp, cwd, ctx)
        if a is not None:
            return "deny", "%s writes nothing (%s): only MOTHER posts, drafts or files." % (who, a["what"])
        return None
    if tool not in SUBAGENT_TOOLS[kind]:
        return "deny", "%s can't use %s: it is read-only." % (who, tool)
    if tool == "Bash":
        cmd = str(inp.get("command") or "").strip()
        if wa_names_cli(wa_norm(cmd)) or classify_whatsapp(cmd, cwd, ctx):
            if kind == "reader" and WA_READ_RE.match(cmd) and classify_whatsapp(cmd, cwd, ctx) is None:
                return None
            return "deny", "%s may run only a single `mother whatsapp read` command, not: %s" % (who, cmd[:120])
        argv = reader_argv(cmd)
        if not argv or not reader_bash_ok(argv[0], argv[1:]):
            return "deny", "%s runs only single gh/glab read commands, not: %s" % (who, cmd[:120])
        a = classify_bash(cmd, cwd, ctx)
        if a is not None:
            return "deny", "%s writes nothing (%s)." % (who, a["what"])
    return None


# ------------------------------------------------------------------ the hub session

HUB_DENY = "the hub session can only do hub work; ask the manager to open a session for this"
HUB_NO_VERBS = {"install", "uninstall", "watch"}   # persistent jobs: the manager starts these
HUB_TOOLS = {"CronCreate", "CronDelete", "CronList", "ToolSearch", "TodoWrite", "Skill", "TaskStop"}
HUB_SELF_TOOLS = {"mcp__ccd_session_mgmt__set_session_model", "mcp__ccd_session_mgmt__set_session_effort"}
HUB_NET_TOKENS = {"fetch", "download", "upload", "export", "url", "http", "browse", "navigate"}
HUB_NET_FIELDS = {"url", "uri", "href", "path", "file", "dest", "destination"}
HUB_ID_CAP = 5
# The hub skill's only cron prompts (skills/hub/SKILL.md quotes them; tests check the two agree).
HUB_CRON_PROMPTS = ("/mother:hub",
                    "mother hub tail --once — run it and handle the lines; if it prints nothing, end the turn at once")


def hub_session_path(ws):
    return os.path.join(ws, ".mother", "hub", "hub-session.json")


def hub_session_ids(ws):
    """The hub session ids in hub-session.json, newest first (empty when it is missing or odd)."""
    if not ws:
        return []
    doc, ok = read_json(hub_session_path(ws))
    if not ok or not isinstance(doc, dict):
        return []
    ids = doc.get("session_ids")
    ids = ids if isinstance(ids, list) else [doc.get("session_id")]
    return [i for i in ids if isinstance(i, str) and i]


def hub_session_id(ws):
    """The newest hub session id, or None."""
    ids = hub_session_ids(ws)
    return ids[0] if ids else None


def record_hub_session(ws, session_id, at):
    """Add session_id to the set in hub-session.json (newest first, at most HUB_ID_CAP), atomically under
    hub.lock. Never raises: recording must not fail the hook."""
    try:
        d = os.path.join(ws, ".mother", "hub")
        os.makedirs(d, exist_ok=True)
        lock = open(os.path.join(d, "hub.lock"), "a")
        try:
            try:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            except ImportError:
                pass
            ids = [session_id] + [i for i in hub_session_ids(ws) if i != session_id]
            tmp = os.path.join(d, ".hub-session.%d.tmp" % os.getpid())
            with open(tmp, "w") as f:
                json.dump({"session_id": session_id, "session_ids": ids[:HUB_ID_CAP], "at": at}, f)
            os.replace(tmp, hub_session_path(ws))
        finally:
            lock.close()
    except Exception:
        pass


HUB_FED_CAP = 50
HUB_CMD_RE = re.compile(r"(?:^|[^\w.-])mother\s+hub\b")


def hub_fed_path(ws):
    return os.path.join(ws, ".mother", "hub", "fed-sessions.json")


def hub_fed_ids(ws):
    """Session ids that ran a `mother hub …` command (fed-sessions.json `session_ids`, newest first)."""
    doc, ok = read_json(hub_fed_path(ws)) if ws else ({}, False)
    ids = doc.get("session_ids") if ok and isinstance(doc, dict) else None
    return [i for i in ids if isinstance(i, str) and i] if isinstance(ids, list) else []


def record_hub_fed(ws, session_id):
    """Add session_id to fed-sessions.json (newest first, at most HUB_FED_CAP), atomically under hub.lock.
    Never raises."""
    if session_id in hub_fed_ids(ws):
        return
    try:
        d = os.path.join(ws, ".mother", "hub")
        os.makedirs(d, exist_ok=True)
        lock = open(os.path.join(d, "hub.lock"), "a")
        try:
            try:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            except ImportError:
                pass
            ids = [session_id] + [i for i in hub_fed_ids(ws) if i != session_id]
            tmp = os.path.join(d, ".fed-sessions.%d.tmp" % os.getpid())
            with open(tmp, "w") as f:
                json.dump({"session_ids": ids[:HUB_FED_CAP]}, f)
            os.replace(tmp, hub_fed_path(ws))
        finally:
            lock.close()
    except Exception:
        pass


def hub_fed(ctx):
    """The call's session reads hub lines (text from people who can message the manager): an id in
    sessions.json in any state, a hub session, or a session that ran `mother hub …` (CONTRACT §5e)."""
    sid = ctx.sid
    if not (ctx.ws and sid):
        return False
    if sid in hub_session_ids(ctx.ws) or sid in hub_fed_ids(ctx.ws):
        return True
    doc, ok = read_json(os.path.join(ctx.ws, ".mother", "hub", "sessions.json"))
    items = doc.get("sessions") if ok and isinstance(doc, dict) else None
    return any(isinstance(e, dict) and str(e.get("session_id") or "") == sid
               for e in (items.values() if isinstance(items, dict) else []))


# `mother channels` verbs that read the manager's replies or make or use a grant, and the ones only the manager runs
CHANNELS_SAFE_VERBS = {"plan", "status", "mute", "unmute", "stop", "pause", "prune", "help", "-h", "--help"}
CHANNELS_REPLY_RE = re.compile(r"\bchannels\s+(?:approvals|triage|run|answer|done|send)\b")
CHANNELS_MGR_RE = re.compile(r"(?:^|[^\w.-])mother\s+channels\s+(?:resume|start)\b")


def hub_fed_shell(norm):
    """Why a shell command (quotes and backslashes dropped, casefolded) may not run unasked in a hub-fed session."""
    if re.search(r"(?:^|[^\w.-])mother\s+grant\b", norm):
        return "`mother grant`"
    m = re.search(r"(?:^|[^\w.-])mother\s+channels\b\s*(\S*)", norm)
    if (m and m.group(1) not in CHANNELS_SAFE_VERBS) or CHANNELS_REPLY_RE.search(norm):
        return "`mother channels %s`" % (m.group(1) if m else "…")
    return None


def hub_odd_chars(text):
    """True for a control character other than newline and tab, or a Unicode space, line/paragraph separator or
    format character: the shell and a regex can disagree about where a line ends."""
    import unicodedata
    for ch in text:
        if ch in "\n\t":
            continue
        o = ord(ch)
        if o < 32 or 0x7f <= o <= 0x9f or (o > 127 and unicodedata.category(ch) in ("Zs", "Zl", "Zp", "Cf")):
            return True
    return False


HUB_BARE = r"[A-Za-z0-9_.:/@=,+-][A-Za-z0-9_.:/@=,+#-]*"
HUB_TOKEN = r"(?:'[^'\n\t]*'|%s)" % HUB_BARE
HUB_LINE_RE = re.compile(r"%s(?: %s)*" % (HUB_TOKEN, HUB_TOKEN))
HUB_TOKEN_RE = re.compile(HUB_TOKEN)
HUB_HEREDOC = "<<'HUBEOF'"
HUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HUB_FIELDS_RE = re.compile(r"^[A-Za-z,]+$")
HUB_GH_SHAPES = {"gh": {("pr", "view"), ("pr", "checks"), ("issue", "view"), ("run", "view")},
                 "glab": {("mr", "view"), ("mr", "checks"), ("issue", "view"), ("run", "view")}}


def hub_simple_command(cmd):
    """(program, args) of a command that matches an exact template, else None. A command is one line of tokens
    separated by single spaces, each token plain characters or a single-quoted string, nothing else (no
    expansion, chaining, redirect or comment). The one stdin form is `mother hub decide <<'HUBEOF'`, a body
    and a last line `HUBEOF`."""
    if hub_odd_chars(cmd):
        return None
    cmd = cmd.replace("${CLAUDE_PLUGIN_ROOT}", os.path.dirname(HERE)).rstrip("\n")
    lines = cmd.split("\n")
    head = lines[0]
    heredoc = len(lines) > 1
    if heredoc:
        if len(lines) < 3 or lines[-1] != "HUBEOF" or any(x.strip(" \t") == "HUBEOF" for x in lines[1:-1]) \
                or not head.endswith(" " + HUB_HEREDOC):
            return None
        head = head[:-len(HUB_HEREDOC) - 1]
    if not HUB_LINE_RE.fullmatch(head):
        return None
    argv = [t[1:-1] if t.startswith("'") else t for t in HUB_TOKEN_RE.findall(head)]
    prog = argv[0]
    if prog.startswith("/"):
        if os.path.realpath(prog) != os.path.realpath(os.path.join(os.path.dirname(HERE), "bin", "mother")):
            return None
        prog = "mother"
    if heredoc and (prog, argv[1:]) != ("mother", ["hub", "decide"]):
        return None
    return prog, argv[1:]


def hub_is_tail(tool, inp):
    if tool != "Bash":
        return False
    parsed = hub_simple_command(str(inp.get("command") or ""))
    return bool(parsed) and parsed[0] == "mother" and parsed[1][:2] == ["hub", "tail"]


def hub_bash_ok(parsed):
    """One command the hub skill runs: a `mother` verb, `pwd`, `date`, or one of the exact `gh`/`glab` reads."""
    if not parsed:
        return False
    prog, args = parsed
    if prog == "mother":
        return bool(args) and (
            (args[0] == "hub" and len(args) > 1 and args[1] not in HUB_NO_VERBS)
            or args[:2] in (["home", "check"], ["now", "show"]) or args == ["path"])
    if prog == "pwd":
        return not args
    if prog == "date":
        return all(a.startswith("+") for a in args)
    if prog in HUB_GH_SHAPES:
        return hub_gh_ok(prog, args)
    return False


def hub_gh_ok(prog, args):
    """`gh pr|issue|run view <N>` or `pr checks <N>` (`mr` for glab), then optionally `--repo owner/repo`,
    `--json fields` and (view of a pr, issue or mr) `--comments`, each once, as separate long-form tokens."""
    if len(args) < 3 or tuple(args[:2]) not in HUB_GH_SHAPES[prog] or not re.fullmatch(r"[0-9]+", args[2]):
        return False
    rest, seen = args[3:], set()
    while rest:
        flag = rest.pop(0)
        if flag in seen:
            return False
        seen.add(flag)
        if flag == "--comments" and args[0] != "run" and args[1] == "view":
            continue
        pat = HUB_REPO_RE if flag == "--repo" else HUB_FIELDS_RE if flag == "--json" else None
        if pat is None or not rest or not pat.match(rest.pop(0)):
            return False
    return True


def hub_bad_field(v):
    """True when a nested input names a field that is, or ends in, url/uri/href/path/file/dest/destination."""
    if isinstance(v, dict):
        for k, x in v.items():
            name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(k)).lower().replace("-", "_")
            if name in HUB_NET_FIELDS or any(name.endswith("_" + f) for f in HUB_NET_FIELDS) or hub_bad_field(x):
                return True
    elif isinstance(v, list):
        return any(hub_bad_field(x) for x in v)
    return False


def hub_mcp_ok(tool, inp):
    """A read that cannot carry data out: a read lead verb, no fetch/download/url-style name, no url/path field."""
    short = tool.split("__")[-1]
    toks = tokens_of(short)
    if set(toks) & HUB_NET_TOKENS:
        return False
    ops = [t for t in toks if t not in PRODUCT_WORDS]
    if not ops or ops[0] not in READ_VERBS:
        return False

    return not hub_bad_field(inp)


def hub_session(tool, inp, cwd, ctx, sid=None):
    """The pinned hub session reads untrusted mail, chat and tracker summaries, so it may only do hub work:
    the hub skill's own commands, reads inside the workspace and plugin, MCP reads that carry nothing out, the
    in-app session tools, and its wake mechanism. Returns (decision, reason) to deny, None when allowed with
    no further check, or "normal" to run the normal rules (the in-app session tools keep their ask)."""
    deny = "deny", "%s (%s)." % (HUB_DENY, tool)
    if tool == "CronCreate":  # the cron fallback wake only: a standing prompt could carry anything
        return None if str(inp.get("prompt") or "") in HUB_CRON_PROMPTS else deny
    if tool in HUB_TOOLS:
        return None
    if tool == "Monitor":
        cmd = str(inp.get("command") or "")
        if hub_bad_field(inp) or any(re.search(r"(^|_)(ws|wss|websocket|socket)(_|$)", str(k).lower()) for k in inp):
            return deny
        return None if hub_bash_ok(hub_simple_command(cmd)) else deny
    if tool in IN_APP_SESSION_TOOLS:
        return "normal"
    if tool in HUB_SELF_TOOLS:
        target = inp.get("session_id")
        target = inp.get("sessionId") if target is None else target
        return None if target in (None, "", "self", sid) else deny
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        return None if hub_bash_ok(hub_simple_command(cmd)) else deny
    if tool in ("Read", "Grep", "Glob", "LS"):
        ws = os.path.realpath(ctx.ws)
        plugin = os.path.realpath(os.path.dirname(HERE))
        raw = inp.get("file_path") or inp.get("path") or ""
        base = cwd or ws
        fp = os.path.realpath(os.path.join(base, os.path.expanduser(str(raw)))) if raw else os.path.realpath(base)
        if not (inside(fp, ws) or inside(fp, plugin)):
            return deny
        pats = [str(inp.get(k) or "") for k in ("pattern", "glob")] if tool == "Glob" else [str(inp.get("glob") or "")]
        if any(".." in x or x.startswith(("/", "~")) for x in pats):
            return deny
        return None
    if tool.startswith("mcp__") and classify(tool, inp, cwd, ctx) is None and hub_mcp_ok(tool, inp):
        return None
    return deny


def key_touch(tool, inp, cwd):
    """A file tool aimed at the grant key: Read/Edit/Write of it, or a Grep/Glob/LS whose root holds it."""
    if tool not in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "Glob", "LS"):
        return False
    raw = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    base = cwd or os.getcwd()
    fp = os.path.realpath(os.path.join(base, os.path.expanduser(str(raw)))) if raw else os.path.realpath(base)
    key = os.path.realpath(grant_key_path())
    if tool in ("Grep", "Glob", "LS"):
        return inside(key, fp)
    return fp.casefold() == key.casefold()


def run(raw):
    """Returns (decision, reason) or None. Never raises."""
    try:
        payload = json.loads(raw or "")
        if not isinstance(payload, dict) or not isinstance(payload.get("tool_name"), str):
            raise ValueError("no tool_name")
    except Exception:
        return "ask", "unreadable hook payload: confirm this call manually."
    tool = payload["tool_name"]
    inp = payload.get("tool_input")
    try:
        ctx = load_ctx()
    except Exception as e:
        ctx = Ctx()
        ctx.problem = "workspace error %s" % type(e).__name__
    a = None
    try:
        if not isinstance(inp, dict):
            if tool == "Bash" or tool.startswith("mcp__"):
                raise ValueError("tool_input is not an object")
            return None
        if key_touch(tool, inp, payload.get("cwd")):
            r = "deny", "%s on the grant key (%s): only `mother grant` reads it." % (tool, grant_key_path())
            log(ctx, tool, {"kind": "grant-key", "category": "grant_key"}, r[0], r[1])
            return r
        role = os.environ.get("MOTHER_CHANNEL_ROLE", "").strip()
        if role:
            r = channel_session(tool, inp, payload.get("cwd"), ctx, role)
            if r is None:
                return None
            log(ctx, tool, {"kind": "channel-session", "category": role}, r[0], r[1])
            return r
        sub = mother_subagent(payload)
        if sub:
            r = subagent_session(sub, tool, inp, payload.get("cwd"), ctx)
            if r is None:
                return None
            log(ctx, tool, {"kind": "subagent", "category": sub}, r[0], r[1])
            return r
        sid = payload.get("session_id")
        ctx.sid = sid if isinstance(sid, str) and sid else None
        if ctx.ws and isinstance(sid, str) and sid:
            text = str(inp.get("command") or "") if tool == "Bash" or tool == "Monitor" or \
                tool.split("__")[-1] == "run_in_terminal" else ""
            if text and HUB_CMD_RE.search(re.sub(r"[\"'\\]", "", text).casefold()):
                record_hub_fed(ctx.ws, sid)
            if hub_is_tail(tool, inp) and not payload.get("agent_type") and payload.get("cwd") \
                    and inside(os.path.realpath(str(payload["cwd"])), os.path.realpath(ctx.ws)):
                record_hub_session(ctx.ws, sid, now_iso())
            if sid in hub_session_ids(ctx.ws):
                r = hub_session(tool, inp, payload.get("cwd"), ctx, sid)
                if r is None:
                    return None
                if r != "normal":
                    log(ctx, tool, {"kind": "hub-session", "category": "hub"}, r[0], r[1])
                    return r
        a = classify(tool, inp, payload.get("cwd"), ctx)
        if a is None:
            return None
        if os.environ.get(DRY_RUN_ENV, "").strip():
            r = "deny", "%s: dry run (%s is set), nothing leaves the machine." % (a["what"], DRY_RUN_ENV)
        else:
            r = stamp_block(tool, a, ctx) or decide(a, ctx, payload.get("transcript_path"))
        # The manager chose bypass mode for this session: an ask becomes no decision, so Claude Code's own
        # mode applies, WhatsApp sends included (as for chat). Denies (PAUSED, the never-list, dry run, the
        # WhatsApp state and setup rules) and the guard's self-edit check still hold.
        # In the workspace an ask stays an ask unless the manager set policy.bypass_passes_asks; the guard's own
        # files, grants.json and the persona files always ask.
        if r[0] == "ask" and a["kind"] not in ("config", "grants", "persona", "home_settings", "manager_only") \
                and payload.get("permission_mode") == "bypassPermissions" \
                and (bypass_passes(ctx) or not in_workspace(payload.get("cwd"), ctx)):
            log(ctx, tool, a, "pass", "bypassPermissions: " + r[1])
            return None
    except Exception as e:  # fail closed on anything that might be outbound; WhatsApp fails to deny
        blob = str(inp.get("command") or inp.get("file_path") or inp.get("notebook_path") or "") \
            if isinstance(inp, dict) else ""
        if (a and a.get("kind") == "whatsapp") or "whatsapp" in blob.lower():
            r = "deny", "guard error (%s) on a WhatsApp call: denied." % type(e).__name__
        else:
            r = "ask", "guard error (%s: %s): confirm %s manually." % (type(e).__name__, e, tool)
    log(ctx, tool, a, r[0], r[1])
    return r


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    r = run(raw)
    if r:
        out(*r)
    sys.exit(0)


if __name__ == "__main__":
    main()
