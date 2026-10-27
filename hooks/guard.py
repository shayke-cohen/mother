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
         bypassPermissions, these get no output instead (logged as "pass"); denies and the self-edit ask stay

WhatsApp is the exception to "ask": `mother whatsapp send <id>` is allow with a proven yes (lib/whatsapp.py
prove()) or deny, in every mode, and any agent touch of the bridge socket or WhatsApp's state folders is deny.

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
                "tracker-transition": 10, "whatsapp": 1}  # whatsapp: written by `mother whatsapp pending` (§5d)
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
CMD_SPLIT_RE = re.compile(r"&&|\|\||[;\n|]")
SHELL_WRITE_RE = re.compile(r">|\b(?:rm|mv|cp|ln|tee|truncate|chmod|chown|unlink|touch|sed\s+-i|perl|python3?|"
                            r"node|ruby|jq|dd|install|rsync|git\s+(?:checkout|restore|reset|rm|mv|apply|stash))\b")


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


def api_write(cmd):
    """A `gh api` / `glab api` write anywhere in the command line. Read-only GraphQL queries pass."""
    if not (API_RE.search(cmd) and API_WRITE_RE.search(cmd)):
        return False
    unsafe = GRAPHQL_UNSAFE_RE.search(cmd)
    for seg in CMD_SPLIT_RE.split(cmd):
        if not (API_RE.search(seg) and API_WRITE_RE.search(seg)):
            continue
        if not unsafe and GRAPHQL_READ_RE.search(seg):
            continue
        return True
    return False


# ------------------------------------------------------------------ WhatsApp (CONTRACT §5, §5d)
# Every WhatsApp decision is allow or deny, never ask: in bypassPermissions mode an ask would pass. Paths are
# compared casefolded (macOS file systems ignore case), commands after stripping quotes and backslashes.

WA_PASS_SUBS = ("read", "chats", "draft", "list", "drop", "status", "pending", "link", "unlink", "help", "-h",
                "--help")
WA_ID = r"w[a-z2-9]{3}"
_WA_BIN = (r"(?:python3?[ \t]+)?(?:\"\$\{CLAUDE_PLUGIN_ROOT\}/bin/mother\"|\$\{CLAUDE_PLUGIN_ROOT\}/bin/mother"
           r"|\"[^\"$`\\\n]*/mother\"|'[^'\n]*/mother'|(?:[\w.~+-]*/)*mother)")
# One argument of a passing subcommand: plain text, $VAR / ${VAR}, or a quoted string without $( ) or backticks.
_WA_ARG = r"""(?:[^\s;&|<>`$()\\'"]|\$\{?[A-Za-z_]\w*\}?|"(?:[^"`$\\\n]|\$\{?[A-Za-z_]\w*\}?)*"|'[^'\n]*')+"""
_WA_REDIR = r"[ \t]*(?:[0-9]?(?:<<<|<<-?|<|>>?|>\|)|&>>?)[ \t]*%s|[ \t]*[0-9]?[<>]&[ \t]*(?:[0-9]+|-)" % _WA_ARG
WA_SEND_RE = re.compile(r"^%s[ \t]+whatsapp[ \t]+send[ \t]+(%s)\Z" % (_WA_BIN, WA_ID))
WA_PROMPT_RE = re.compile(r"^%s[ \t]+whatsapp[ \t]+prompt((?:[ \t]+%s)+)\Z" % (_WA_BIN, WA_ID))
WA_PASS_RE = re.compile(r"^%s[ \t]+whatsapp(?:[ \t]+(?:%s)(?:[ \t]+%s)*)?(?:%s)*\Z" % (
    _WA_BIN, "|".join(re.escape(x) for x in WA_PASS_SUBS), _WA_ARG, _WA_REDIR))
WA_ENV_PREFIX_RE = re.compile(r"([A-Za-z_]\w*)=(?:%s)?[ \t]+" % _WA_ARG)  # a value as a passing argument
WA_ENV_CODE_RE = re.compile(r"(?:PATH|ENV|BASH_ENV|PYTHON\w*|DYLD_\w*|LD_\w*|PERL5\w*|NODE_OPTIONS)\Z", re.I)
WA_READ_RE = re.compile(r"^%s[ \t]+whatsapp[ \t]+read(?:[ \t]+[^\s;&|<>`$()\\'\"]+)*\Z" % _WA_BIN)
WA_STATE_RE = re.compile(r"bridge\.sock|\.mother/whatsapp\b|whatsapp/(?:approved|drafts|session)(?=/|$|[\s;&|)])"
                         r"|whatsapp/(?:sent|prompts)\.jsonl|whatsapp/grant\.key", re.I)
WA_STATE_FILES = ("sent.jsonl", "bridge.sock", "session", "grant.key")
WA_WS_FILES = ("drafts", "approved", "prompts.jsonl")


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
    """The folders only `mother whatsapp` writes: <state>/whatsapp (and the default ~/.mother/whatsapp) and
    <ws>/.mother/whatsapp."""
    ds = [os.path.join(wa_state_root(), "whatsapp"), os.path.join(os.path.expanduser("~"), ".mother", "whatsapp")]
    if ctx.ws:
        ds.append(os.path.join(ctx.ws, ".mother", "whatsapp"))
    return ds


def _cf(path):
    return os.path.realpath(path).casefold()


def wa_inside(path, ctx):
    rp = _cf(path)
    return any(inside(rp, _cf(d)) for d in wa_dirs(ctx))


def wa_protected(ctx):
    """Every protected WhatsApp path, as written and resolved, casefolded, for glob matching."""
    found = set()
    for d in wa_dirs(ctx):
        kids = WA_WS_FILES if d.endswith(os.path.join(".mother", "whatsapp")) and ctx.ws and \
            d == os.path.join(ctx.ws, ".mother", "whatsapp") else WA_STATE_FILES
        for p in [d] + [os.path.join(d, k) for k in kids]:
            found |= {os.path.abspath(p).casefold(), os.path.realpath(p).casefold()}
    return found


# -- Reading a shell line (T5-h): the gate keys on shapes, not spellings. A command name the guard can't compute
# is denied; every argument has its unresolved expansions read as wildcards and is matched against the protected
# paths; destructive or copying commands are also matched against the folders that hold them. A deliberately
# evasive agent can still get past a reader like this one: the bridge re-checks the yes itself (CONTRACT §5d).

WA_RESERVED = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac"}
WA_WRAPPERS = {  # commands that run the rest of their arguments as a command; the options that take a value
    "env": ("-u", "--unset", "-C", "--chdir", "-P"), "command": (), "exec": ("-a",), "nohup": (), "time": (),
    "sudo": ("-u", "-g", "-h", "-p", "-C", "-D", "-U", "-r", "-t", "-T"), "doas": ("-u", "-C"), "nice": ("-n",),
    "timeout": ("-s", "-k", "--signal", "--kill-after"), "gtimeout": ("-s", "-k", "--signal", "--kill-after"),
    "xargs": ("-I", "-n", "-L", "-P", "-s", "-E", "-d", "-a", "-J", "-R", "-S"), "builtin": (),
    "stdbuf": ("-i", "-o", "-e"), "caffeinate": ("-t", "-w"), "unbuffer": (), "chronic": (),
    "watch": ("-n", "--interval"), "ionice": ("-c", "-n", "-p"), "taskpolicy": ("-c", "-d", "-g", "-b")}
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
_WA_SEND_WORDS = re.compile(r"(?:^|[\s/])mother\s+whatsapp\s+(?:send|prompt)\b")


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
            self.hit("compound", "a whatsapp send or prompt that is not a command of its own")
        if self.runner:
            n = wa_norm(self.cmd)
            if _WA_SEND_WORDS.search(n):
                self.hit("compound", "a whatsapp send or prompt fed to a shell or interpreter")
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
            if vals[1].casefold() in ("send", "prompt"):
                self.hit("compound", "a whatsapp send or prompt that is not a command of its own")
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
                re.search(r"\.mother|whatsapp|bridge\.sock|grant\.key", lits, re.I):
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


def wa_act(op, what, **kw):
    cat = {"state": "whatsapp_state", "computed": "computed_command"}.get(op, "any_message_to_others")
    return act("whatsapp", cat, what, op=op, **kw)


def classify_whatsapp(cmd, cwd, ctx):
    """A WhatsApp action for a Bash command, or None when the command is not WhatsApp's business or is one of
    the passing subcommands (read, chats, draft, list, …). Ops: send, prompt (decided on proof); state,
    compound, unknown, computed (always denied). A send or prompt must be the exact shape on the raw text; any
    other line is read by _WaScan (CONTRACT §5, §5d). Reasons never quote the command: guard.log holds no
    message text."""
    if cwd and wa_inside(cwd, ctx):
        return wa_act("state", "a shell command run inside WhatsApp's state folders")
    line = cmd.strip()
    m = WA_SEND_RE.match(line)
    if m:
        return wa_act("send", "a WhatsApp send of draft " + m.group(1), draft=m.group(1))
    m = WA_PROMPT_RE.match(line)
    if m:
        ids = m.group(1).split()
        return wa_act("prompt", "a WhatsApp self-chat prompt for " + " ".join(ids), draft=",".join(ids), drafts=ids)
    try:
        _WaScan(cmd, cwd, ctx).run()
    except _WaHit as h:
        return h.action
    return None


def wa_module():
    """lib/whatsapp.py, imported only here (it imports this file lazily too). Raises ImportError."""
    lib = os.path.join(os.path.dirname(HERE), "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import whatsapp
    return whatsapp


def wa_sent(path):
    """sent.jsonl records ([] when missing), or None when it can't be read or a line is malformed."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    recs = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            return None
        if not isinstance(r, dict):
            return None
        recs.append(r)
    return recs


def classify_bash(cmd, cwd, ctx):
    wa = classify_whatsapp(cmd, cwd, ctx)
    if wa:
        return wa
    # Edits to the guard's own state through the shell. Reads (cat, grep) pass.
    if SHELL_WRITE_RE.search(cmd):
        names = ["paused", "ledger.json", "guard.log", "guard.py", "hooks.json", ".mother/workspace"]
        low = cmd.casefold()
        hit = any(n in low for n in names)
        if ctx.ws and not hit:
            roots = {ctx.ws, os.environ.get("MOTHER_WORKSPACE", "").strip()} - {""}
            here = _cf(cwd) if cwd else ""
            hit = any(os.path.join(r, "config.json").casefold() in low for r in roots) \
                or ("config.json" in low and here == _cf(ctx.ws))
        if hit:
            return act("config", "guard_config", "a shell command that could change the guard's own files")
    short = cmd.strip()[:160]
    if GH_CRED_RE.search(cmd):
        return act("credential", "credentials", "storing a credential on the code host: " + short)
    if GH_DELETE_RE.search(cmd) or (API_RE.search(cmd) and API_DELETE_RE.search(cmd)):
        return act("deletion", "deletions", "a code-host deletion: " + short)
    if GH_WRITE_RE.search(cmd) or api_write(cmd):
        return act("code", "code_host_comments", "a code-host write: " + short, text=cmd)
    return None


def classify_mcp(tool, inp):
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


def classify(tool, inp, cwd, ctx):
    if tool == "Bash":
        return classify_bash(str(inp.get("command", "")), cwd, ctx)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if fp:
            fp = os.path.realpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(fp)))
            if wa_inside(fp, ctx):
                return wa_act("state", "an edit to WhatsApp's drafts, approvals or bridge state: " + fp)
            if fp.casefold() in protected_paths(ctx):
                return act("config", "guard_config", "an edit to the guard's own files: " + fp)
        return None
    if not tool.startswith("mcp__"):
        return None
    if any(SENSITIVE_KEYS.match(str(k)) for k in inp.keys()):
        return act("credential", "credentials", "a tool call carrying a password or card field (%s)" % tool)
    a = classify_mcp(tool, inp)
    if a and a["kind"] not in ("draft", "deletion") and SECRET_RE.search(json.dumps(inp)):
        return act("credential", "credentials", "an outbound call whose content looks like a credential (%s)" % tool)
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


def run_grant_scope(a, ctx, sc, now=None, only=None):
    """`only` = (source key, sha256, signature check): consume that one signed grant and no other (a WhatsApp
    proof names it)."""
    scope, target, n = sc
    scopes, max_age = grant_limits(ctx.cfg)
    if scope == "whatsapp":
        if not only or not re.match(r"^%s$" % WA_ID, str(target or "")) or scope not in scopes:
            return None
    elif scope not in scopes or not target or target not in allowed_targets(scope, ctx.cfg):
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
            if only and (key != only[0] or g.get("sha256") != only[1] or not only[2](g)):
                continue
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


def paused(ctx):
    if not ctx.ws:
        return None
    p = os.path.join(ctx.ws, ".mother", "PAUSED")
    return p if os.path.exists(p) else None


def decide_whatsapp(a, ctx, transcript=None):
    """allow or deny, never ask (CONTRACT §5). On an allowed send, writes approved/<id>.json for the bridge."""
    op, what = a.get("op"), a["what"]
    if op == "state":
        return "deny", "%s. Only `mother whatsapp` commands touch it; the bridge is reached only through " \
                       "`mother whatsapp send <id>`." % what
    if op == "unknown":
        return "deny", "%s. See `mother whatsapp help`." % what
    if op == "compound":
        return "deny", "%s. Run `mother whatsapp send <id>` (or `prompt <ids>`) alone." % what
    if op == "computed":
        return "deny", "%s: the guard can't tell what it runs. Write the command name out plainly (a variable " \
                       "is fine as an argument, or set it earlier in the same line)." % what
    p = paused(ctx)
    if p:
        return "deny", "Outbound is PAUSED (%s exists). Blocked: %s." % (p, what)
    if not ctx.ok:
        return "deny", "%s: no valid config (%s)." % (what, ctx.problem)
    if a["category"] in policy_lists(ctx.cfg)[1]:
        return "deny", "%s: policy.never lists %s." % (what, a["category"])
    try:
        wa = wa_module()
    except Exception:
        return "deny", "%s: whatsapp module unavailable." % what
    cfg = ctx.cfg
    w = cfg.get("whatsapp") if isinstance(cfg.get("whatsapp"), dict) else {}
    if not (w.get("enabled") is True and w.get("send", True) is True):
        return "deny", "%s: whatsapp sending is off (whatsapp.enabled / whatsapp.send)." % what
    import contextlib
    import io
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            wa.require_home(cfg)
    except SystemExit:
        return "deny", "%s: WhatsApp runs only on workspace.home_machine." % what
    if op == "prompt":
        missing = [i for i in a.get("drafts") or [] if not wa.load_draft(ctx.ws, i)]
        if missing or not a.get("drafts"):
            return "deny", "%s: not an open draft: %s." % (what, ", ".join(missing) or "none named")
        return "allow", "%s: every id is an open draft; the prompt reaches only the manager." % what
    if op != "send":
        return "deny", "%s: not a WhatsApp action the guard knows." % what
    i = a.get("draft")
    d = wa.load_draft(ctx.ws, i)
    if not d:
        return "deny", "%s: no open draft %s (missing or expired)." % (what, i)
    chat = d.get("chat")
    allowed = {x.get("jid") for x in w.get("allow") or [] if isinstance(x, dict)}
    if not chat or chat not in allowed or chat == w.get("self_jid"):
        return "deny", "%s: its chat is not in whatsapp.allow." % what
    if not wa.has_history(cfg, chat):
        return "deny", "%s: no cold contact (no history with that chat in the local database)." % what
    recs = wa_sent(os.path.join(wa_dirs(ctx)[0], "sent.jsonl"))
    if recs is None:
        return "deny", "%s: the bridge's sent.jsonl can't be read, so the daily cap can't be checked." % what
    cap = w.get("max_per_day", 20)
    cap = min(cap, 50) if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else 20
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    n = sum(1 for r in recs if r.get("kind") == "send" and str(r.get("at") or "")[:10] == today)
    if n >= cap:
        return "deny", "%s: today's cap is reached (%d of whatsapp.max_per_day %d)." % (what, n, cap)
    if any(r.get("kind") == "send" and r.get("draft") == i and r.get("sha256") == d.get("sha256") for r in recs):
        return "deny", "%s: already sent (one yes sends once)." % what
    base = os.path.join(ctx.ws, ".mother", "whatsapp", "approved", i)
    if os.path.lexists(base + ".used"):
        return "deny", "%s: already sent (approved/%s.used: one yes sends once)." % (what, i)
    if os.path.lexists(base + ".json"):
        return "deny", "%s: send for %s already approved/in flight (approved/%s.json)." % (what, i, i)
    proof = wa.prove(ctx.ws, cfg, i, transcript_path=transcript)
    if not proof or not proof.get("sha256") or proof.get("path") not in ("session", "self_chat", "slack_dm"):
        return "deny", "%s: no proven yes. Show the draft and get the manager's `ok %s` on an enabled path." % (
            what, i)
    if proof["path"] == "slack_dm":
        key = str(proof.get("evidence") or "")[len("slack_dm:"):]
        if not run_grant_scope(a, ctx, ("whatsapp", i, 1), only=(key, proof["sha256"], wa.grant_sig_ok)):
            return "deny", "%s: the manager's DM grant for it is already used." % what
    path = base + ".json"
    rec = {"id": i, "chat": chat, "sha256": proof["sha256"], "path": proof["path"],
           "evidence": proof.get("evidence"), "at": now_iso()}
    try:  # created exclusively, never through a symlink: a second allow for the same id can't race this one
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f, indent=1)
    except FileExistsError:
        return "deny", "%s: send for %s already approved/in flight." % (what, i)
    except OSError as e:
        return "deny", "%s: could not write the approval record (%s)." % (what, type(e).__name__)
    return "allow", "%s: proven yes via %s; approval record %s." % (what, proof["path"], path)


def decide(a, ctx, transcript=None):
    if a["kind"] == "whatsapp":
        return decide_whatsapp(a, ctx, transcript)
    if a["kind"] == "config":
        return "ask", "%s. The agent must not change its own guard; confirm this is you." % a["what"]
    p = paused(ctx)
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
               "target": (a.get("channel") or a.get("draft") or ",".join(sorted(str(x) for x in (a.get("projects") or []) if x))
                          or None) if a else None,
               "decision": decision, "reason": reason}
        with open(os.path.join(d, "guard.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ channel reply sessions

CHANNEL_PRIVATE = ["people", "journal", "memory", "todos", ".mother", "bridge"]
CHANNEL_NOTE_DIRS = ("todos", "memory", "projects", "teams", "people")
CHANNEL_NO_TOOLS = {"Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"}


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
    if tool in CHANNEL_NO_TOOLS:
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
READER_BASH = {"gh", "glab", "date"}
DRY_RUN_ENV = "MOTHER_DRY_RUN"


def mother_subagent(payload):
    t = str(payload.get("agent_type") or "").strip()
    if not t:
        return None
    if t.startswith("mother:"):
        t = t[len("mother:"):]
    return t if t in SUBAGENT_TOOLS else None


def unquoted(cmd):
    return re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", cmd)


def subagent_session(kind, tool, inp, cwd, ctx):
    """A `mother:reader` gathers one source and hands back JSON; a `mother:critic` reads a draft and answers
    PASS or fixes. Both are read-only: MOTHER is the single writer. Every classified write is denied — the
    manager's own DM and drafts included — and so is any tool outside the agent's list. Returns
    (decision, reason) or None (not outbound: normal permissions)."""
    who = "the %s sub-agent" % kind
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
        bare = unquoted(cmd)
        first = os.path.basename(bare.split()[0]) if bare.split() else ""
        if first not in READER_BASH or re.search(r"[;&|`<>]|\$\(", bare):
            return "deny", "%s runs only single gh/glab read commands, not: %s" % (who, cmd[:120])
        a = classify_bash(cmd, cwd, ctx)
        if a is not None:
            return "deny", "%s writes nothing (%s)." % (who, a["what"])
    return None


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
        a = classify(tool, inp, payload.get("cwd"), ctx)
        if a is None:
            return None
        if os.environ.get(DRY_RUN_ENV, "").strip():
            r = "deny", "%s: dry run (%s is set), nothing leaves the machine." % (a["what"], DRY_RUN_ENV)
        else:
            r = stamp_block(tool, a, ctx) or decide(a, ctx, payload.get("transcript_path"))
        # The manager chose bypass mode for this session: an ask becomes no decision, so Claude Code's own
        # mode applies. Denies (PAUSED, the never-list, dry run) and the guard's self-edit check still hold.
        # WhatsApp never takes this branch: its decisions are allow or deny only (anything else becomes deny).
        if a["kind"] == "whatsapp" and r[0] not in ("allow", "deny"):
            r = "deny", "%s: no WhatsApp decision (%s), so deny." % (a["what"], r[0])
        if r[0] == "ask" and a["kind"] not in ("config", "whatsapp") \
                and payload.get("permission_mode") == "bypassPermissions":
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
