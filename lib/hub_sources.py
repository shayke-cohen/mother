"""Source adapters and the deterministic prefilter for the hub watcher (read-only, no model).

Each adapter polls one capability through a `hub_mcp.ReadOnlyClient` (code: through `GhClient`) and returns
`{lines, cursor, partial}`. Tool names are matched by suffix, so the gateway prefix does not matter. Product
specifics live here and nowhere else. `cursor=None` is the first poll: it returns no lines and a cursor at `at`.
"""
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Type

import health
import hub
from hub_mcp import ForbiddenTool, McpError

UTC = timezone.utc
MAX_THREADS = 25
MAX_MAIL = 20
THREAD_DAYS = 7


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    s = (s or "").replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        d = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def ts_iso(ts) -> str:
    return iso(datetime.fromtimestamp(float(ts), UTC))


def _result(lines: List[dict], cursor: dict, partial: Optional[List[str]] = None) -> dict:
    return {"lines": hub.coalesce(lines), "cursor": cursor, "partial": partial or []}


class Adapter:
    SOURCE = ""
    READ_TOOLS = set()  # tool-name suffixes this adapter may call

    @classmethod
    def poll(cls, client, cursor: Optional[dict], cfg: dict, me: dict, at: datetime) -> dict:
        raise NotImplementedError


def _first(*vals):
    for v in vals:
        if v:
            return v
    return ""


# --------------------------------------------------------------------------------------------------- chat
class ChatSlack(Adapter):
    SOURCE = "chat"
    READ_TOOLS = {"get_channel_history", "get_thread_replies", "search-messages"}

    @classmethod
    def poll(cls, client, cursor, cfg, me, at):
        if cursor is None:
            return _result([], {"ts": "%.6f" % at.timestamp(), "threads": {}})
        since = float(cursor["ts"])
        threads = dict(cursor.get("threads") or {})
        mine = me.get("chat") or ""
        lines, partial = [], []
        for t in cfg.get("follow") or []:
            threads.setdefault(t, since)

        def add(ch, msg, link="", floor=since):
            ts = msg.get("ts")
            if not ts or float(ts) <= floor:
                return
            tts = msg.get("thread_ts")
            key = hub.thing_key("chat", {"channel": ch, "ts": ts, "thread_ts": tts})
            text = msg.get("text") or ""
            who = msg.get("user") or msg.get("username") or ""
            if mine and who == mine:
                threads.setdefault(key, float(ts))
                lines.append(hub.make_line("chat", "resolved", key, ts_iso(ts), title=text[:80], who=who,
                                           link=link, summary=text))
                return
            lines.append(hub.make_line("chat", "update", key, ts_iso(ts), title=text[:80], who=who, link=link,
                                       summary=text, also=hub.extract_also(text)))

        for ch in cfg.get("channels") or []:
            res = client.call("get_channel_history", {"channel_id": ch, "oldest": "%.6f" % since, "limit": 100})
            for m in (res or {}).get("messages") or []:
                add(ch, m)

        for key, seen in list(threads.items())[:MAX_THREADS * 2]:
            if "/" not in key:
                continue
            ch, tts = key.split("/", 1)
            if float(seen) < at.timestamp() - THREAD_DAYS * 86400:
                del threads[key]
                continue
            res = client.call("get_thread_replies", {"channel_id": ch, "thread_ts": tts})
            for m in (res or {}).get("messages") or []:
                if m.get("ts") == tts:
                    continue
                if float(m.get("ts", 0)) > float(seen):
                    add(ch, dict(m, thread_ts=tts), floor=float(seen))
                    threads[key] = max(float(threads[key]), float(m["ts"]))

        day = datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%d")
        for label, text in (("mentions", "<@%s>" % mine), ("DMs", "is:dm")):
            if label == "mentions" and not mine:
                continue
            try:
                res = client.call("search-messages", {"searchText": text, "after": day}) or {}
            except McpError as e:
                partial.append("%s: %s" % (label, e.kind))
                continue
            box = res.get("messages", res)
            for m in (box.get("matches") if isinstance(box, dict) else None) or []:
                ch = (m.get("channel") or {}).get("id") or m.get("channel_id") or ""
                if ch:
                    add(ch, m, link=m.get("permalink") or "")
        keep = dict(list(threads.items())[-MAX_THREADS:])
        return _result(lines, {"ts": "%.6f" % at.timestamp(), "threads": keep}, partial)


# --------------------------------------------------------------------------------------------------- mail
def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


class MailGmail(Adapter):
    SOURCE = "mail"
    READ_TOOLS = {"list-emails", "read-email"}

    @classmethod
    def poll(cls, client, cursor, cfg, me, at):
        if cursor is None:
            return _result([], {"since": int(at.timestamp()), "seen": []})
        since = int(cursor["since"])
        seen = list(cursor.get("seen") or [])
        email = (me.get("email") or "").lower()
        q = "in:inbox after:%d" % since
        if email:
            q += " -from:%s" % email
        for lab in cfg.get("ignore_labels") or []:
            q += ' -label:"%s"' % lab
        lines = []
        listed = client.call("list-emails", {"query": q, "maxResults": MAX_MAIL}) or {}
        for ref in listed.get("messages") or []:
            if ref["id"] in seen:
                continue
            seen.append(ref["id"])
            m = client.call("read-email", {"messageId": ref["id"]}) or {}
            sender = _header(m, "From")
            low = sender.lower()
            if email and email in low:
                continue
            if any(s.lower() in low for s in cfg.get("ignore_senders") or []):
                continue
            labels = {l.lower() for l in m.get("labelIds") or []}
            if any(l.lower() in labels for l in cfg.get("ignore_labels") or []):
                continue
            subj = _header(m, "Subject")
            try:
                when = ts_iso(int(m.get("internalDate")) / 1000.0)
            except (TypeError, ValueError):
                when = iso(at)
            key = hub.thing_key("mail", {"thread_id": m.get("threadId") or ref.get("threadId")})
            lines.append(hub.make_line("mail", "update", key, when, title=subj[:120], who=sender, link="",
                                       summary=m.get("snippet") or "", also=hub.extract_also(subj)))
        if email:  # the manager's own sent mail resolves its thread; ids only, no reads
            sent = client.call("list-emails", {"query": "from:%s after:%d" % (email, since),
                                               "maxResults": MAX_MAIL}) or {}
            for ref in sent.get("messages") or []:
                if ref.get("threadId"):
                    lines.append(hub.make_line("mail", "resolved", "mail:" + ref["threadId"], iso(at),
                                               who=email))
        return _result(lines, {"since": int(at.timestamp()), "seen": seen[-200:]})


# ------------------------------------------------------------------------------------------------- tracker
class TrackerJira(Adapter):
    SOURCE = "tracker"
    READ_TOOLS = {"get-issues"}
    FIELDS = ["summary", "status", "assignee", "reporter", "priority", "updated", "created", "comment", "labels"]

    @classmethod
    def _jql(cls, cfg: dict, minutes: int) -> str:
        scope = ["assignee = currentUser()", "reporter = currentUser()", "watcher = currentUser()"]
        projects = cfg.get("projects") or []
        if projects:
            scope.append("project in (%s)" % ", ".join(projects))
        jql = "(%s) AND updated >= -%dm" % (" OR ".join(scope), minutes)
        if cfg.get("extra_jql"):
            jql += " AND (%s)" % cfg["extra_jql"]
        return jql + " ORDER BY updated DESC"

    @classmethod
    def _fetch(cls, client, cfg, minutes, n):
        projects = cfg.get("projects") or [""]
        res = client.call("get-issues", {"projectKey": projects[0], "jql": cls._jql(cfg, minutes),
                                         "fields": cls.FIELDS, "maxResults": n}) or {}
        return res.get("issues") or []

    @staticmethod
    def _fp(f: dict) -> dict:
        c = f.get("comment") or {}
        return {"status": (f.get("status") or {}).get("name", ""),
                "assignee": (f.get("assignee") or {}).get("accountId", ""),
                "priority": (f.get("priority") or {}).get("name", ""),
                "comments": c.get("total", len(c.get("comments") or []))}

    @classmethod
    def poll(cls, client, cursor, cfg, me, at):
        if cursor is None:
            fp = {}
            try:  # seed fingerprints so the first real poll can tell a field change from a label edit
                for i in cls._fetch(client, cfg, 7 * 24 * 60, 100):
                    fp[i["key"]] = cls._fp(i.get("fields") or {})
            except McpError:
                pass
            return _result([], {"since": iso(at), "fp": fp})
        since = parse_iso(cursor["since"])
        fps = dict(cursor.get("fp") or {})
        minutes = int(math.ceil((at - since).total_seconds() / 60.0)) + 2
        email = (me.get("email") or "").lower()
        lines = []
        for i in cls._fetch(client, cfg, minutes, 50):
            key, f = i["key"], i.get("fields") or {}
            new = cls._fp(f)
            old = fps.get(key)
            fps[key] = new
            if old is None:
                if not f.get("created") or parse_iso(f["created"]) < since:
                    continue  # first sight of an old issue: nothing observable changed
                old = {}
            changed = [k for k in ("status", "assignee", "priority", "comments") if old.get(k) != new[k]]
            if not changed:
                continue  # label, description or other non-watched field
            comments = (f.get("comment") or {}).get("comments") or []
            last = comments[-1] if comments else {}
            author = (last.get("author") or {}).get("emailAddress", "").lower()
            link = re.sub(r"/rest/api/.*$", "", i.get("self") or "") + "/browse/" + key if i.get("self") else ""
            who = (last.get("author") or {}).get("displayName") or (f.get("assignee") or {}).get("displayName", "")
            kind = "update"
            if changed == ["comments"] and email and author == email:
                kind = "resolved"
            lines.append(hub.make_line("tracker", kind, hub.thing_key("tracker", {"key": key}),
                                       iso(parse_iso(f["updated"])) if f.get("updated") else iso(at),
                                       title=(f.get("summary") or "")[:120], who=who, link=link,
                                       summary="changed: " + ", ".join(changed) + "; status " + new["status"],
                                       also=hub.extract_also(f.get("summary") or "")))
        keep = dict(list(fps.items())[-500:])
        return _result(lines, {"since": iso(at), "fp": keep})


# ------------------------------------------------------------------------------------------------ calendar
class CalendarGoogle(Adapter):
    SOURCE = "calendar"
    READ_TOOLS = {"list-events"}

    @staticmethod
    def _start(ev: dict) -> Optional[datetime]:
        s = (ev.get("start") or {}).get("dateTime")
        return parse_iso(s) if s else None  # all-day events have no start time to count down to

    @classmethod
    def poll(cls, client, cursor, cfg, me, at):
        if cursor is None:
            return _result([], {"since": iso(at), "resp": {}, "notified": []})
        since = parse_iso(cursor["since"])
        resp = dict(cursor.get("resp") or {})
        notified = list(cursor.get("notified") or [])
        lead = int(cfg.get("lead_minutes", 10))
        email = (me.get("email") or "").lower()
        res = client.call("list-events", {"calendarId": "primary", "timeMin": iso(at - timedelta(hours=2)),
                                          "timeMax": iso(at + timedelta(days=7))}) or {}
        lines = []
        for ev in res.get("items") or []:
            eid = ev.get("id")
            if not eid:
                continue
            key = hub.thing_key("calendar", {"event_id": eid})
            me_att = next((a for a in ev.get("attendees") or []
                           if a.get("self") or (email and (a.get("email") or "").lower() == email)), {})
            my = me_att.get("responseStatus", "")
            prev = resp.get(eid)
            resp[eid] = my
            title = (ev.get("summary") or "")[:120]
            link = ev.get("htmlLink") or ""
            who = (ev.get("organizer") or {}).get("email", "")
            upd = parse_iso(ev["updated"]) if ev.get("updated") else None
            if upd and upd > since:
                if prev == "needsAction" and my and my != "needsAction":
                    lines.append(hub.make_line("calendar", "resolved", key, iso(upd), title=title, who=who,
                                               link=link, summary="responded: " + my))
                elif (ev.get("organizer") or {}).get("self"):
                    pass  # the manager's own change
                elif ev.get("status") == "cancelled":
                    lines.append(hub.make_line("calendar", "update", key, iso(upd), title=title, who=who,
                                               link=link, summary="cancelled"))
                else:
                    new = my == "needsAction" and prev is None
                    lines.append(hub.make_line("calendar", "update", key, iso(upd), title=title, who=who,
                                               link=link, summary="new invite" if new else "invite changed"))
            st = cls._start(ev)
            tag = "%s@%s" % (eid, (ev.get("start") or {}).get("dateTime"))
            if (st and ev.get("status") != "cancelled" and my != "declined" and at < st <= at + timedelta(minutes=lead)
                    and tag not in notified):
                notified.append(tag)
                lines.append(hub.make_line("calendar", "update", key, iso(at), title=title, who=who, link=link,
                                           summary="starts at %s" % iso(st)))
        return _result(lines, {"since": iso(at), "resp": dict(list(resp.items())[-300:]),
                               "notified": notified[-100:]})


# ---------------------------------------------------------------------------------------------------- code
GH_PREFIXES = ("search/issues?", "repos/")
_GH_PATH = re.compile(r"^[A-Za-z0-9_./:@%+=&,<>?#-]+$")


class GhClient:
    """`gh api` as a read-only pseudo-MCP client with one tool, `get`. The path must start with an allow-listed
    prefix; no method, field or header flag is ever passed, so every call is a GET."""
    tools = ["get"]

    def __init__(self, runner=None, timeout: float = 30):
        self._runner = runner or self._run
        self.timeout = timeout

    def _run(self, path: str):
        try:
            p = subprocess.run(["gh", "api", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise McpError("other", str(e))
        if p.returncode != 0:
            raise McpError(health.classify(p.stderr or "gh failed")[1], (p.stderr or "")[:300])
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def call(self, name: str, arguments: dict):
        path = (arguments or {}).get("path", "")
        if name != "get" or not path.startswith(GH_PREFIXES) or ".." in path or not _GH_PATH.match(path):
            raise ForbiddenTool("gh path not allowed: %r" % path)
        return self._runner(path)

    def close(self) -> None:
        pass


class CodeGithub(Adapter):
    SOURCE = "code"
    READ_TOOLS = {"get"}

    @staticmethod
    def _repo(item: dict) -> str:
        return (item.get("repository_url") or "").split("/repos/", 1)[-1]

    @classmethod
    def poll(cls, client, cursor, cfg, me, at):
        if cursor is None:
            return _result([], {"since": iso(at), "ci": {}, "branch": {}})
        since = parse_iso(cursor["since"])
        ci = dict(cursor.get("ci") or {})
        branches = dict(cursor.get("branch") or {})
        login = me.get("code") or ""
        lines = []

        def search(q):
            return (client.call("get", {"path": "search/issues?per_page=30&sort=updated&q=" + q}) or {}).get(
                "items") or []

        if login:
            for it in search("is:pr+is:open+review-requested:%s" % login):
                if parse_iso(it["updated_at"]) > since:
                    key = hub.thing_key("code", {"repo": cls._repo(it), "number": it["number"]})
                    lines.append(hub.make_line("code", "update", key, it["updated_at"], title=it.get("title", "")[:120],
                                               who=(it.get("user") or {}).get("login", ""),
                                               link=it.get("html_url", ""), summary="review requested"))
            for it in search("is:pr+is:open+author:%s" % login):
                if parse_iso(it["updated_at"]) <= since:
                    continue
                repo, n = cls._repo(it), it["number"]
                key = hub.thing_key("code", {"repo": repo, "number": n})
                for kind_path in ("issues", "pulls"):
                    cs = client.call("get", {"path": "repos/%s/%s/%d/comments?since=%s&per_page=30"
                                             % (repo, kind_path, n, iso(since))}) or []
                    for c in cs:
                        if parse_iso(c["created_at"]) <= since:
                            continue
                        who = (c.get("user") or {}).get("login", "")
                        lines.append(hub.make_line("code", "resolved" if who == login else "update", key,
                                                   c["created_at"], title=it.get("title", "")[:120], who=who,
                                                   link=c.get("html_url", ""), summary=c.get("body") or ""))
        for repo in cfg.get("repos") or []:
            br = branches.get(repo)
            if not br:
                br = (client.call("get", {"path": "repos/%s" % repo}) or {}).get("default_branch") or "main"
                branches[repo] = br
            runs = (client.call("get", {"path": "repos/%s/commits/%s/check-runs?per_page=100" % (repo, br)})
                    or {}).get("check_runs") or []
            red = [r for r in runs if r.get("conclusion") in ("failure", "timed_out")]
            key = hub.thing_key("code", {"repo": repo, "number": "ci-" + br})
            sha = (runs[0].get("head_sha") if runs else "") or ""
            if red and ci.get(repo) != sha:
                ci[repo] = sha
                lines.append(hub.make_line("code", "update", key, iso(at), title="CI red on %s" % br,
                                           link=red[0].get("html_url", ""),
                                           summary="%s failing on %s" % (red[0].get("name", "check"), br)))
            elif not red and runs and ci.get(repo):
                ci[repo] = ""
                lines.append(hub.make_line("code", "resolved", key, iso(at), title="CI green on %s" % br))
        return _result(lines, {"since": iso(at), "ci": ci, "branch": branches})


ADAPTERS = {
    "chat:slack": ChatSlack,
    "mail:gmail": MailGmail,
    "tracker:jira": TrackerJira,
    "calendar:google": CalendarGoogle,
    "code:github": CodeGithub,
}  # type: Dict[str, Type[Adapter]]
