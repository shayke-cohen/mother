#!/usr/bin/env python3
"""MOTHER approval guard: a PreToolUse hook that enforces rules/outbound.md in code.

Prompt rules can be lost to context compaction or overridden by injected content; a hook cannot. For every
tool call that sends, posts or writes somewhere another person can see, the guard answers:

  deny   the kill switch <workspace>/.mother/PAUSED exists, or the call is on the never-list
         (credential entry, deletions, and any category the manager moved to `policy.never`)
  allow  a green action: a chat message to `manager.chat_dm`, a mail draft, a calendar event with no
         guests; a chat/tracker write covered by a `mother approve tracker|chat <target>` line at the top
         of a genuine user message in this session (12 h); or a valid run grant (.mother/grants.json)
  ask    every other outbound call: the human confirms it in the UI

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
                     "standing_rules", "docs_writes"]
ALWAYS_NEVER = ["credentials", "deletions", "payments"]

# Grant scopes an unattended run may hold, with the ceiling on uses per cited source message.
GRANT_SCOPES = {"chat-post": 3, "chat-reply": 3, "tracker-create": 5, "tracker-comment": 5,
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
    return c


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
               "transfer", "mark", "react", "convert", "start", "end", "invoke", "trigger", "run", "write"}

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
    return {os.path.realpath(p) for p in ps}


def classify_bash(cmd, cwd, ctx):
    # Edits to the guard's own state through the shell. Reads (cat, grep) pass.
    if SHELL_WRITE_RE.search(cmd):
        names = ["PAUSED", "ledger.json", "guard.log", "guard.py", "hooks.json", ".mother/workspace"]
        hit = any(n in cmd for n in names)
        if ctx.ws and not hit:
            cfgp = os.path.join(ctx.ws, "config.json")
            here = os.path.realpath(cwd) if cwd else ""
            hit = cfgp in cmd or ("config.json" in cmd and here == ctx.ws)
        if hit:
            return act("config", "guard_config", "a shell command that could change the guard's own files")
    short = cmd.strip()[:160]
    if GH_CRED_RE.search(cmd):
        return act("credential", "credentials", "storing a credential on the code host: " + short)
    if GH_DELETE_RE.search(cmd) or (API_RE.search(cmd) and API_DELETE_RE.search(cmd)):
        return act("deletion", "deletions", "a code-host deletion: " + short)
    if GH_WRITE_RE.search(cmd) or (API_RE.search(cmd) and API_WRITE_RE.search(cmd)):
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
        channel = str(first(inp, "channel_id", "channelId", "channel", "chat_id", "chatId", "conversation_id",
                            "user_id", "userId", "channels") or "")
        thread = first(inp, "thread_ts", "threadTs", "thread_id", "reply_to_id", "parent_id")
        if "reaction" in verbs or "react" in verbs or "reactions" in verbs:
            return act("chat", "any_message_to_others", "a chat reaction in %s" % (channel or "?"),
                       op="reaction", channel=channel, thread=thread)
        posting = lead in ("post", "send", "reply", "schedule", "update", "edit", "share", "upload", "complete",
                           "forward") and not ({"canvas", "topic", "purpose"} & verbs)
        if posting:
            sub = "schedule" if lead == "schedule" else "edit" if lead in ("update", "edit") else \
                "reply" if (thread or lead == "reply") else "post"
            return act("chat", "any_message_to_others", "chat %s to %s" % (short, channel or "?"),
                       op=sub, channel=channel, thread=thread, broadcast=bool(inp.get("reply_broadcast")),
                       text=text_of(inp))
        return act("chat", "any_message_to_others", "a chat change (%s)" % short, op="other", channel=channel,
                   thread=None)

    if dom == "mail":
        if {"filter", "filters", "forwarding", "vacation", "auto", "autoreply", "settings", "delegate",
                "rule", "rules"} & verbs:
            return act("standing-rule", "standing_rules", "a mail rule or setting change (%s)" % short)
        if lead in ("send", "reply", "forward", "submit") or "send" in verbs:
            return act("mail", "any_message_to_others", "sending mail to %s" % (first(inp, "to", "recipients")
                                                                                 or "?"), text=text_of(inp))
        return None  # labels, archive, mark-as-read, trash: no one else sees them

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
            if fp in protected_paths(ctx):
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


def green(a, ctx, auto):
    if a["kind"] == "chat" and a.get("channel") and a["channel"] in dm_ids(ctx.cfg) and "own_dm" in auto:
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


def valid_grant(g, scope, target, cfg, scopes, max_age, now):
    """The grant's (source key, cap) if it is well-formed and covers (scope, target); else None."""
    if not isinstance(g, dict) or g.get("scope") != scope or str(g.get("target")) != str(target):
        return None
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
    if str(user) != str(owner_id(cfg)):
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
    scope, target, n = sc
    scopes, max_age = grant_limits(ctx.cfg)
    if scope not in scopes or not target or target not in allowed_targets(scope, ctx.cfg):
        return None
    if scope in ("chat-post", "chat-reply", "tracker-comment"):
        agent = ctx.cfg.get("agent") or {}
        marks = ["%s (AI)" % agent["name"]] if agent.get("name") else []
        if isinstance(agent.get("signature"), str) and agent["signature"].strip():
            marks.append(agent["signature"].strip())
        if not any(m in (a.get("text") or "") for m in marks):
            return None  # unattended writes must carry the disclosure signature
    data, ok = read_json(os.path.join(ctx.ws, ".mother", "grants.json"))
    if not ok or not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        return None
    now = time.time() if now is None else now
    with Ledger(ctx.ws) as led:
        book = led.read()
        if book is None:
            return None
        for g in data["grants"]:
            v = valid_grant(g, scope, target, ctx.cfg, scopes, max_age, now)
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
            return "run grant %s on %s citing the manager's message %s (%d/%d used)" % (
                scope, target, key, used + n, cap)
    return None


# ------------------------------------------------------------------ decision


def paused(ctx):
    if not ctx.ws:
        return None
    p = os.path.join(ctx.ws, ".mother", "PAUSED")
    return p if os.path.exists(p) else None


def decide(a, ctx, transcript=None):
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


def log(ctx, tool, a, decision, reason):
    if not ctx.ws:
        return
    try:
        d = os.path.join(ctx.ws, ".mother")
        os.makedirs(d, exist_ok=True)
        rec = {"at": now_iso(), "tool": tool, "kind": a.get("kind") if a else None,
               "category": a.get("category") if a else None,
               "target": (a.get("channel") or ",".join(sorted(str(x) for x in (a.get("projects") or []) if x))
                          or None) if a else None,
               "decision": decision, "reason": reason}
        with open(os.path.join(d, "guard.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


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
        a = classify(tool, inp, payload.get("cwd"), ctx)
        if a is None:
            return None
        r = decide(a, ctx, payload.get("transcript_path"))
    except Exception as e:  # fail closed on anything that might be outbound
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
