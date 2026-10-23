---
name: doctor
description: Use for "/mother:doctor", "mother doctor", "check my connectors", "why didn't the brief see Slack", "is my tracker connected", or the weekly doctor routine. Live-tests every connector the agent depends on — one real read each, write checks only on explicit opt-in — and writes capabilities.json plus a table of fixes.
---

# Doctor

A tool being listed is not a tool that works. Sign-ins expire independently per connector, tokens miss
scopes, admins gate features. The doctor finds out by **calling**, records the answer in
`<workspace>/capabilities.json` (CONTRACT §4), and every routine reads that file first so a broken source is
reported as "couldn't check", never as "nothing found".

Re-runnable at any time; scheduled weekly. Each run replaces `capabilities.json` wholesale.

## 1. Discover servers by tool shape

**Never assume a server name.** Names differ per install, and the same capability may be offered by two
servers (a gateway and a native connector). Find candidates by what the tools do, with `ToolSearch`
keyword queries, then group results by server prefix (`mcp__<server>__…`):

| Kind | Look for tools that… | Capabilities to test |
|---|---|---|
| chat | read channel history / thread replies; post or send a message; search messages; look up a user | `read`, `post`, `search`, `private_history`, `users` |
| tracker | get/search issues or items; create; update; transition/change status; comment; list/update sprints | `read`, `create`, `comment`, `transition`, `sprint` |
| mail | list/search messages; read one; create a draft; send | `read`, `draft`, `send` |
| calendar | list/search events; check availability; create/update an event | `read`, `create` |
| code | the `gh` or `glab` CLI (`gh auth status`), or an MCP code-search server | `read`, `write` |
| directory (optional) | look up an employee / list reports | `read` |

Also run `claude mcp list` if available: a server marked as needing authentication is `expired` without
a call. Use `config.json` `connectors.*` to know which kinds matter; a kind set to `none` is skipped and
not reported as a gap.

When two servers offer one kind, test both and record, per capability, which one works. It is common for
one to post but not search and the other to search but not post.

## 2. One real read per connector

Make the smallest real call that returns data, scoped to config when it exists:

- **chat** — history of the manager's DM (`manager.chat_dm`) and of one configured channel (1–5 messages);
  one search for a common word; history of one private channel if config has one (`private_history`).
- **tracker** — one query for the manager's open items with an **explicit page size of 5** (some tools
  default to a page of 1, which silently looks like "one item"). If the tool requires a project parameter,
  check whether it is honoured or ignored when a query is supplied, and record that in `notes`.
- **mail** — list 3 recent messages (metadata only; don't read bodies).
- **calendar** — today's events in a short window with an explicit offset (whole-week windows can be huge).
- **code** — `gh auth status` (or `glab auth status`), then list one configured repo.

Never print message bodies, ticket descriptions or email content in the report — counts and status only.

## 3. Classify the result

Map each outcome to a CONTRACT §4 value from the error text, not from guesswork:

| Seen | Value |
|---|---|
| Data came back | `ok` |
| "missing_scope", "not allowed", "insufficient scope", with a scope name | `missing_scope:<scope>` |
| "sign in", "token expired", "re-authenticate", "invalid_auth", 401, or the server opened a sign-in itself | `expired` |
| 403 / "permission" / "admin must enable" | `denied` |
| Capability exists but was not exercised (write checks declined) | `untested` |
| Anything else | `error:<first ~40 chars, no ids>` |

**Token type**, where knowable: a "who am I"/auth-test call that reports a bot or app user → `bot`;
otherwise, if posts and reads resolve to the manager's own user → `user`. For `gh`, record the token kind
and scopes from `gh auth status`. If unknowable, omit the field. A `user` token means **every write
appears as the manager** — the signature is what tells people it was the agent.

## 4. Write checks — opt-in only

Ask once: "Run write checks? I'll create one clearly-labelled test item, comment on it and close it. Nothing
is deleted." Default is **no**; every write capability then stays `untested`. On yes, each write still goes
through the approval guard and the manager approves it there.

| Kind | Check | Leaves behind |
|---|---|---|
| chat `post` | Post `"[<agent> test] doctor check — ignore"` to the manager's own DM | One DM |
| tracker `create` → `comment` → `transition` | In a project the **manager names** (suggest a personal or sandbox one; never a team's live board by default): create `"[<agent> test] doctor check"`, add one comment, transition to the done/closed state (read available transitions first) | One closed item, labelled |
| tracker `sprint` | Only on a sandbox board the manager names. Read the sprint list first. Sprint updates can be a **full replace** — send every field (name, state, start, end, goal) copied from the read. Names may be capped at 30 characters | Nothing, or one test sprint |
| mail `draft` | Create a draft to the manager, subject `"[<agent> test] doctor check"` | One draft for the manager to discard |
| calendar `create` | Skipped unless the manager asks: an event without guests on their own calendar | One event |
| mail `send`, calendar with guests | **Never tested** — always `untested` | — |

**Never delete** anything, including the test item; deleting is on the never list. Record the test item's
key in `notes` so the next run reuses it (comment + reopen/close) instead of creating another.

## 5. Write `capabilities.json`

```json
{ "checked_at": "<UTC ISO>", "host": "<hostname -s>",
  "connectors": {
    "<connectors.chat value>": { "server": "<prefix that read>", "read": "ok", "post": "untested",
                                 "search": "missing_scope:<scope>", "private_history": "ok", "token": "user",
                                 "servers": { "search": "<other prefix>" }, "notes": ["…"] },
    "<connectors.tracker value>": { "server": "…", "read": "ok", "create": "untested", "comment": "untested",
                                    "transition": "untested", "sprint": "untested" },
    "<connectors.mail value>": { … }, "<connectors.calendar value>": { … },
    "<connectors.code value>": { "tool": "gh", "read": "ok", "write": "untested" } } }
```

Keys under `connectors` are the `config.json` connector values. `servers` maps a capability to a different
server prefix when it isn't the main one. `capabilities.json` lives in the private workspace, so recording
local server prefixes there is fine; they never go into the plugin.

## 6. Report

A table, worst first:

| Connector | Capability | Status | Fix |
|---|---|---|---|
| chat | search | missing_scope:search:read | Ask the chat workspace admin to add the scope, or use the other server that can search |
| mail | read | expired | Sign in again — see below |

Fixes by status:
- `expired` → name the connector and how to sign in again (the server's auth tool, `/mcp`, the app's
  connector settings, or `claude mcp login <server>`). A **signed sign-in URL is a credential**: show it in
  the session, never write it to a file, memory, log or commit. Scheduled runs cannot complete a sign-in —
  say a human must.
- `missing_scope:<s>` → which scope, who can grant it, and which routine output is degraded meanwhile.
- `denied` → which admin setting or role is missing.
- `untested` → "run `/mother:doctor` and accept write checks" — only if a routine needs that write.

End with one line on impact: which routines will run `partial` until each fix lands.

When run as the weekly routine (unattended): read checks only, no write checks, send the table to the
manager's DM only if something got worse since the previous `capabilities.json`.
