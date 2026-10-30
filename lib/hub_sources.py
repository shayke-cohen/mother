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
MAX_ISSUES = 50
MAX_HISTORY = 100
MAX_NEW_THREADS = 10  # unfollowed threads read per watched channel per pass
MAIL_LINK = "https://mail.google.com/mail/u/0/#all/%s"
THREAD_DAYS = 7
SEARCH_OVERLAP = 600


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", (s or "").replace("Z", "+00:00"))
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
    def gap(cls, cfg: dict) -> Optional[str]:
        """A reason this source cannot be polled until the manager configures something, else None."""
        return None

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
        marks = [m for m in me.get("marks") or [] if m]
        lines, partial = [], []
        seen = list(cursor.get("seen") or [])
        followed = [t for t in cfg.get("follow") or [] if isinstance(t, str)]
        for t in cursor.get("followed") or []:  # unfollowed since the last pass: stop polling it
            if t not in followed:
                threads.pop(t, None)
        for t in followed:
            threads.setdefault(t, since)

        def add(ch, msg, link="", floor=since, dm=False):
            ts = msg.get("ts")
            if not ts or float(ts) <= floor or "%s:%s" % (ch, ts) in seen:
                return
            seen.append("%s:%s" % (ch, ts))  # every path (history, replies, search) shares this set
            who = msg.get("user") or msg.get("username") or ""
            first = (msg.get("text") or "").lstrip().split("\n", 1)[0]
            if marks and any(k in first for k in marks) and (not msg.get("user") or who == mine or msg.get("bot_id")):
                return  # MOTHER's own signed post: neither news nor the manager's reply
            tts = msg.get("thread_ts")
            # a DM conversation is one thing: the manager's reply resolves it
            key = hub.thing_key("chat", {"channel": ch, "ts": "dm" if dm else ts, "thread_ts": None if dm else tts})
            text = msg.get("text") or ""
            if mine and who == mine:
                if not dm:
                    threads.setdefault(key, float(ts))
                lines.append(hub.make_line("chat", "resolved", key, ts_iso(ts), title=text[:80], who=who,
                                           link=link, summary=text, uid=ts))
                return
            lines.append(hub.make_line("chat", "update", key, ts_iso(ts), title=text[:80], who=who, link=link,
                                       summary=text, also=hub.extract_also(text), uid=ts))

        for ch in cfg.get("channels") or []:
            res = client.call("get_channel_history", {"channel_id": ch, "oldest": "%.6f" % since,
                                                      "limit": MAX_HISTORY + 1})
            got = (res or {}).get("messages") or []
            if len(got) > MAX_HISTORY:
                partial.append("%s: more than %d new messages" % (ch, MAX_HISTORY))
            for m in got:
                add(ch, m)
            # Replies in threads nobody follows: a parent with replies newer than the cursor (less the search
            # overlap). History lists parents by their own time, so an older parent needs a lookback read.
            heads = {m["ts"]: m for m in got if m.get("ts")}
            try:
                back = (client.call("get_channel_history", {
                    "channel_id": ch, "oldest": "%.6f" % (at.timestamp() - THREAD_DAYS * 86400),
                    "latest": "%.6f" % since, "limit": MAX_HISTORY}) or {}).get("messages") or []
            except McpError:
                back = []
            for m in back:
                if m.get("ts"):
                    heads.setdefault(m["ts"], m)
            active = []
            for tts, m in heads.items():
                try:
                    newest, n = float(m.get("latest_reply") or 0), int(m.get("reply_count") or 0)
                except (TypeError, ValueError):
                    continue
                if n > 0 and newest > since - SEARCH_OVERLAP and "%s/%s" % (ch, tts) not in threads:
                    active.append((newest, tts))
            active.sort(reverse=True)
            if len(active) > MAX_NEW_THREADS:
                partial.append("more than %d active threads in %s" % (MAX_NEW_THREADS, ch))
            for _, tts in active[:MAX_NEW_THREADS]:
                res = client.call("get_thread_replies", {"channel_id": ch, "thread_ts": tts})
                for m in (res or {}).get("messages") or []:
                    if m.get("ts") != tts:
                        add(ch, dict(m, thread_ts=tts), floor=since - SEARCH_OVERLAP)

        for key, tseen in list(threads.items())[:MAX_THREADS * 2]:
            if "/" not in key:
                continue
            ch, tts = key.split("/", 1)
            if float(tseen) < at.timestamp() - THREAD_DAYS * 86400:
                del threads[key]
                continue
            res = client.call("get_thread_replies", {"channel_id": ch, "thread_ts": tts})
            for m in (res or {}).get("messages") or []:
                if m.get("ts") == tts:
                    continue
                if float(m.get("ts", 0)) > float(tseen):
                    add(ch, dict(m, thread_ts=tts), floor=float(tseen))
                    threads[key] = max(float(threads[key]), float(m["ts"]))

        # Search indexes late and `after` is exclusive by day: look back a day, overlap 10 minutes, dedupe by ts.
        floor = since - SEARCH_OVERLAP
        day = (datetime.fromtimestamp(since, UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        for label, text in (("mentions", "<@%s>" % mine), ("DMs", "is:dm")):
            if label == "mentions" and not mine:
                continue
            try:
                res = client.call("search-messages", {"searchText": text, "after": day}) or {}
            except McpError as e:
                low = str(getattr(e, "message", e)).lower()
                scope = e.kind == "missing_scope" or re.search(r"missing[_ ]scope|insufficient scope", low)
                partial.append("%s: %s" % (label, "missing scope" if scope else e.kind))
                continue
            box = res.get("messages", res)
            for m in (box.get("matches") if isinstance(box, dict) else None) or []:
                ch = (m.get("channel") or {}).get("id") or m.get("channel_id") or ""
                if not ch:
                    continue
                add(ch, m, link=m.get("permalink") or "", floor=floor, dm=(label == "DMs"))
        keep = dict(list(threads.items())[-MAX_THREADS:])
        return _result(lines, {"ts": "%.6f" % at.timestamp(), "threads": keep, "seen": seen[-300:],
                                                                     "followed": followed}, partial)


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
        lines, partial = [], []
        listed = client.call("list-emails", {"query": q, "maxResults": MAX_MAIL + 1}) or {}
        refs = listed.get("messages") or []
        if len(refs) > MAX_MAIL:
            partial.append("more than %d new messages" % MAX_MAIL)
        for ref in refs[:MAX_MAIL]:
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
            tid = m.get("threadId") or ref.get("threadId")
            key = hub.thing_key("mail", {"thread_id": tid})
            lines.append(hub.make_line("mail", "update", key, when, title=subj[:120], who=sender, link=MAIL_LINK % tid,
                                       summary=m.get("snippet") or "", also=hub.extract_also(subj)))
        if email:  # the manager's own sent mail resolves its thread, stamped when it was sent
            sent = client.call("list-emails", {"query": "from:%s after:%d" % (email, since),
                                               "maxResults": MAX_MAIL}) or {}
            for ref in sent.get("messages") or []:
                if not ref.get("threadId"):
                    continue
                stamp = ref.get("internalDate")
                if stamp is None:  # the list gives ids only: one read for the time (nothing else is kept)
                    stamp = (client.call("read-email", {"messageId": ref["id"]}) or {}).get("internalDate")
                try:
                    when = ts_iso(int(stamp) / 1000.0)
                except (TypeError, ValueError):
                    when = iso(at)
                lines.append(hub.make_line("mail", "resolved", "mail:" + ref["threadId"], when, who=email,
                                           link=MAIL_LINK % ref["threadId"]))
        return _result(lines, {"since": int(at.timestamp()), "seen": seen[-200:]}, partial)


# ------------------------------------------------------------------------------------------------- tracker
class TrackerJira(Adapter):
    SOURCE = "tracker"
    READ_TOOLS = {"get-issues", "get-issue-changelog"}
    WATCHED = ("status", "assignee", "priority")
    AUTO_FAILS = 2  # consecutive failing passes before an auto-followed project is dropped
    FIELDS = ["summary", "status", "assignee", "reporter", "priority", "updated", "created", "comment", "labels"]

    @staticmethod
    def _projects(cfg: dict) -> List[str]:
        return list(dict.fromkeys(p for p in cfg.get("projects") or [] if isinstance(p, str) and p))

    @classmethod
    def gap(cls, cfg):
        return None if cls._projects(cfg) else "no tracker projects configured"

    @classmethod
    def _jql(cls, cfg: dict, project: str, minutes: int) -> str:
        scope = ["assignee = currentUser()", "reporter = currentUser()", "watcher = currentUser()",
                 "project = %s" % project]
        jql = "project = %s AND (%s) AND updated >= -%dm" % (project, " OR ".join(scope), minutes)
        if cfg.get("extra_jql"):
            jql += " AND (%s)" % cfg["extra_jql"]
        return jql + " ORDER BY updated DESC"

    @classmethod
    def _fetch(cls, client, cfg, minutes, n):
        """(issues, failed): the read tool needs a projectKey, so one call per project; a failing project is
        skipped (named in `failed`, and in `bad` by project) and the others still poll."""
        issues, failed, bad = [], [], []
        for project in cls._projects(cfg):
            try:
                res = client.call("get-issues", {"projectKey": project, "jql": cls._jql(cfg, project, minutes),
                                                 "fields": cls.FIELDS, "maxResults": n}) or {}
            except McpError as e:
                failed.append("%s: %s" % (project, e.kind))
                bad.append(project)
                continue
            got = res.get("issues") or []
            issues.append((project, got))
        return issues, failed, bad

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
                issues = [i for _, got in cls._fetch(client, cfg, 7 * 24 * 60, 100)[0] for i in got]
                for i in issues:
                    fp[i["key"]] = dict(cls._fp(i.get("fields") or {}), u=(i.get("fields") or {}).get("updated", ""))
            except McpError:
                pass
            return _result([], {"since": iso(at), "fp": fp})
        since = parse_iso(cursor["since"])
        auto = set(cfg.get("auto") or [])
        fails = {p: n for p, n in (cursor.get("fails") or {}).items() if p in auto and isinstance(n, int)}
        fps = dict(cursor.get("fp") or {})
        minutes = int(math.ceil((at - since).total_seconds() / 60.0)) + 2
        email = (me.get("email") or "").lower()
        account = me.get("tracker_account") or ""
        lines, partial = [], []
        pages, failed, bad = cls._fetch(client, cfg, minutes, MAX_ISSUES + 1)
        drop = []
        for project in cls._projects(cfg):  # consecutive failures of an auto-followed project
            if project not in auto:
                continue
            if project in bad:
                fails[project] = fails.get(project, 0) + 1
                if fails[project] >= cls.AUTO_FAILS:
                    fails.pop(project)
                    drop.append(project)
                    lines.append(hub.make_line("tracker", "error", "watcher:tracker:%s" % project, iso(at),
                                               title="stopped following tracker project %s" % project,
                                               summary="it failed %d passes in a row; `mother hub follow %s` to retry"
                                                       % (cls.AUTO_FAILS, project)))
            else:
                fails.pop(project, None)
        got = []
        for project, page in pages:
            if len(page) > MAX_ISSUES:
                partial.append("more than %d updated issues" % MAX_ISSUES)
            got.extend(page[:MAX_ISSUES])
        partial.extend("project %s" % f for f in failed)
        partial = list(dict.fromkeys(partial))
        for i in got:
            key, f = i["key"], i.get("fields") or {}
            new = cls._fp(f)
            old = fps.get(key)
            fps[key] = dict(new, u=f.get("updated", ""))
            if old is None:
                # first sight (newly assigned, newly in scope, or created): a candidate if touched since the cursor
                if not f.get("updated") or parse_iso(f["updated"]) <= since:
                    continue
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
            elif any(k in cls.WATCHED for k in changed) and (email or account) \
                    and ("comments" not in changed or (email and author == email)) \
                    and cls._own_change(client, key, email, account):
                kind = "resolved"
            lines.append(hub.make_line("tracker", kind, hub.thing_key("tracker", {"key": key}),
                                       iso(parse_iso(f["updated"])) if f.get("updated") else iso(at),
                                       title=(f.get("summary") or "")[:120], who=who, link=link,
                                       summary="changed: " + ", ".join(changed) + "; status " + new["status"],
                                       also=hub.extract_also(f.get("summary") or "")))
        # prune the least recently updated, not the oldest inserted
        keep = dict(sorted(fps.items(), key=lambda kv: kv[1].get("u", ""))[-500:])
        out = _result(lines, {"since": iso(at), "fp": keep, "fails": fails}, partial)
        if drop:
            out["drop_auto"] = drop
        return out

    @classmethod
    def _changelog_tail(cls, client, key) -> list:
        """The newest changelog entries (the tool pages oldest first): one read for `total`, then the last 50.
        Without a `total`, pages are read until `isLast` is true, at most 5;
        past the cap, or when `isLast` is missing, nothing is returned."""
        name = "get-issue-changelog"
        head = client.call(name, {"issueKey": key, "startAt": 0, "maxResults": 1})
        head = head if isinstance(head, dict) else {}
        total = head.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            res = client.call(name, {"issueKey": key, "startAt": max(0, total - 50), "maxResults": 50})
            vals = res.get("values") if isinstance(res, dict) else None
            return vals if isinstance(vals, list) else []
        out = []
        for n in range(5):
            res = client.call(name, {"issueKey": key, "startAt": n * 100, "maxResults": 100})
            res = res if isinstance(res, dict) else {}
            vals = res.get("values") if isinstance(res.get("values"), list) else []
            out.extend(vals)
            if res.get("isLast") is True:
                return out
        return []  # cap reached, empty page or no `isLast`: the newest entries were not seen, so claim nothing

    @classmethod
    def _own_change(cls, client, key, email, account) -> bool:
        """True when the latest change to status / assignee / priority of `key` was made by the manager. Two
        changelog reads (see `_changelog_tail`); any failure or unreadable answer is False (the caller reports an update)."""
        try:
            hist = cls._changelog_tail(client, key)
        except (McpError, ForbiddenTool):  # also a server that lacks the tool: never silence the line
            return False
        best = None
        for h in hist:
            if not isinstance(h, dict) or not h.get("created"):
                continue
            if not any(isinstance(it, dict) and str(it.get("field", "")).lower() in cls.WATCHED
                       for it in h.get("items") or []):
                continue
            try:
                when = parse_iso(h["created"])
            except (ValueError, TypeError):
                continue
            if best is None or when > best[0]:
                best = (when, h.get("author"))
        who = (best[1] if best else None) or {}
        return bool(best) and isinstance(who, dict) and (
            bool(account and who.get("accountId") == account)
            or bool(email and str(who.get("emailAddress", "")).lower() == email))


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
                if (ev.get("organizer") or {}).get("self"):
                    pass  # the manager's own change
                elif my in ("accepted", "declined", "tentative") and prev in (None, "needsAction"):
                    lines.append(hub.make_line("calendar", "resolved", key, iso(upd), title=title, who=who,
                                               link=link, summary="responded: " + my))
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
            raise McpError("unreachable", str(e))
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
    def _rr_path(login):
        return "search/issues?per_page=30&sort=updated&q=is:pr+is:open+review-requested:%s" % login

    @staticmethod
    def _repo(item: dict) -> str:
        return (item.get("repository_url") or "").split("/repos/", 1)[-1]

    @classmethod
    def poll(cls, client, cursor, cfg, me, at):
        if cursor is None:
            rr = {}
            if me.get("code"):
                try:  # seed the review-requested set so a PR leaving it can be told from one never seen
                    for it in (client.call("get", {"path": cls._rr_path(me["code"])}) or {}).get("items") or []:
                        rr[hub.thing_key("code", {"repo": cls._repo(it), "number": it["number"]})] = it.get("title", "")
                except McpError:
                    pass
            return _result([], {"since": iso(at), "ci": {}, "branch": {}, "rr": rr})
        since = parse_iso(cursor["since"])
        prev_rr = dict(cursor.get("rr") or {})
        rr = {}
        ci = dict(cursor.get("ci") or {})
        branches = dict(cursor.get("branch") or {})
        login = me.get("code") or ""
        lines = []

        def search(q):
            return (client.call("get", {"path": "search/issues?per_page=30&sort=updated&q=" + q}) or {}).get(
                "items") or []

        if login:
            for it in search("is:pr+is:open+review-requested:%s" % login):
                key = hub.thing_key("code", {"repo": cls._repo(it), "number": it["number"]})
                rr[key] = it.get("title", "")
                if parse_iso(it["updated_at"]) > since:
                    lines.append(hub.make_line("code", "update", key, it["updated_at"], title=it.get("title", "")[:120],
                                               who=(it.get("user") or {}).get("login", ""),
                                               link=it.get("html_url", ""), summary="review requested"))
            for k, title in prev_rr.items():  # left the set: reviewed, merged, closed or request withdrawn
                if k not in rr:
                    lines.append(hub.make_line("code", "resolved", k, iso(at), title=title[:120],
                                               summary="no longer awaiting your review"))
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
            elif not red and runs and ci.get(repo) and all(r.get("status") == "completed" for r in runs):
                ci[repo] = ""
                lines.append(hub.make_line("code", "resolved", key, iso(at), title="CI green on %s" % br))
        return _result(lines, {"since": iso(at), "ci": ci, "branch": branches, "rr": rr})


ADAPTERS = {
    "chat:slack": ChatSlack,
    "mail:gmail": MailGmail,
    "tracker:jira": TrackerJira,
    "calendar:google": CalendarGoogle,
    "code:github": CodeGithub,
}  # type: Dict[str, Type[Adapter]]
