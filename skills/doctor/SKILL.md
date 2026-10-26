---
name: doctor
description: Use for "/mother:doctor", "mother doctor", "check my connectors", "why didn't the brief see Slack", "is my tracker connected", or the daily doctor routine. Live-tests every connector the agent depends on — one real read each, write checks only on explicit opt-in — and writes capabilities.json plus a table of fixes.
---

# Doctor

A tool being listed is not a tool that works. Sign-ins expire independently per connector, tokens miss
scopes, admins gate features. The doctor finds out by **calling**, records the answer in
`<workspace>/capabilities.json` (CONTRACT §4), and every routine reads that file first so a broken source is
reported as "couldn't check", never as "nothing found".

Re-runnable at any time; scheduled daily. The doctor never edits `capabilities.json` by hand: it writes a
fresh result to a temp file and applies it with `mother health merge`, which keeps each capability's server
order and every `alerted_at` and replaces the rest. Single results go through `mother health record`.
All calls below are `${CLAUDE_PLUGIN_ROOT}/bin/mother health …`.

**Modes.** `/mother:doctor fix` (the default for interactive runs) tests, then runs the healing ladder (§6).
`/mother:doctor check` only reads and classifies; it changes nothing but the health record. The daily
routine runs unattended (§9).

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

**Test every server of a kind, not only the main one.** Two servers often serve one kind (a corporate
gateway plus claude.ai connectors); record, per capability, which of them work. It is common for one to
post but not search and the other to search but not post. Take each server's URL host from `claude mcp list`
(for grouping, §3).

**Listed or "connected" is not working.** `claude mcp list` and the app's connector status can say
connected while a real call fails with "connection to this connector was invalidated … reconnect it from
connector settings". Only a real call counts.

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

Map each outcome to a CONTRACT §4 value from the error text, not from guesswork. For a failure, run
`mother health classify --error "<error text, no ids>"` -> `{status, class}`; the class (`unreachable`,
`dropped`, `invalidated`, `missing_scope`, `denied`, `other`) says what fixes it:

| Seen | Value |
|---|---|
| Data came back | `ok` |
| "missing_scope", "not allowed", "insufficient scope", with a scope name | `missing_scope:<scope>` |
| "sign in", "token expired", "re-authenticate", "invalid_auth", 401, or the server opened a sign-in itself | `expired` |
| 403 / "permission" / "admin must enable" | `denied` |
| Capability exists but was not exercised (write checks declined) | `untested` |
| Anything else | `error:<first ~40 chars, no ids>` |

| Class | Shows up as | Fix |
|---|---|---|
| `unreachable` | network or timeout error; every server on one gateway fails together | switch server; "probably VPN" only if nothing else covers it |
| `dropped` | server status `failed`, "connection closed" | reconnect, re-test |
| `invalidated` | "invalidated", "reconnect from connector settings", 401, `needs_auth` | human sign-in (assisted, §7); switch meanwhile |
| `missing_scope` / `denied` | as above | no healing; use the server that has it |

**Gateway grouping.** Two or more servers that share a URL host and are all `unreachable` are **one**
issue, not several: record each with `--gateway <host>` so they collapse into one row and one alert.

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

## 5. Write the result

Build the fresh result as JSON in a temp file (not in the workspace), then `mother health merge --file <tmp>`
-> `{changed: [kind.cap, …]}`. Per-server results can also be stored one at a time:
`mother health record --kind K --cap C --server S --status V [--class X] [--gateway H] [--fixed-by F]`
-> `{previous, rollup}`. Never edit `capabilities.json` yourself.

```json
{ "checked_at": "<UTC ISO>", "host": "<hostname -s>",
  "connectors": {
    "<connectors.chat value>": { "server": "<preferred prefix>", "read": "ok", "post": "untested",
                                 "search": "missing_scope:<scope>", "private_history": "ok",
                                 "servers": { "read": ["<a>", "<b>"], "post": ["<a>", "<b>"], "search": ["<b>"] },
                                 "stamps": ["<server that appends its own signature>"],
                                 "token": { "<a>": "user", "<b>": "user" }, "notes": ["…"] },
    "<connectors.tracker value>": { "server": "…", "read": "ok", "create": "untested", "comment": "untested",
                                    "transition": "untested", "sprint": "untested" },
    "<connectors.mail value>": { … }, "<connectors.calendar value>": { … },
    "<connectors.code value>": { "tool": "gh", "read": "ok", "write": "untested" } } }
```

Keys under `connectors` are the `config.json` connector values. `servers.<capability>` is an **ordered
list**: first is preferred, the rest are tested backups. Order it with a server that has **no stamp and
the manager's identity first**; a failure never reorders it (routines pick by health with `mother health
pick`). `stamps` lists servers that add their own signature (claude.ai Slack: "Sent using @Claude"); `token`
records `user`/`bot` per server. Writes to anyone but the manager never go through a stamping server
(rules/outbound.md, "One signature, not two"); only `manager.chat_dm` may. `capabilities.json` lives in the
private workspace, so recording local server prefixes there is fine; they never go into the plugin.

## 6. Heal (fix mode)

Run once per capability that is not `ok`, in order, and record each step with
`mother health record … --fixed-by retry|reconnect|fallback|you`:

1. **Re-test** with the same probe. Brief network errors clear here (`retry`).
2. **Reconnect**, if the server is a claude.ai connector in `failed`. Load `session_connectors_status` and
   `reconnect_session_connector` with ToolSearch. Reconnect cannot fix `needs_auth` (that needs the
   manager). It takes effect only **after the turn ends**: record `reconnect requested`, end the turn, and
   re-test on resume (`reconnect`).
3. **Switch server.** Test the next server in `servers.<cap>`; record its health. If it is `ok`, the
   capability is covered (`fallback`). Writes to others follow the stamp rule in §5.
4. **Human step**: assisted sign-in (§7). Unattended: alert (§9).
5. **Re-test** after the human step and report fixed (`you`) or still broken (`—`).

## 7. Assisted sign-in (interactive only)

The aim is a single click from the manager, not signing in for them. **Never** type a password, 2FA code
or token, never click a consent/Allow screen, never toggle a VPN or system setting.

- **claude.ai connector** (`invalidated`, `needs_auth`): load `anthropic-skills:built-in-browser` first.
  Open the app's connector settings, find the connector, click Reconnect. In the account picker choose the
  manager's own Google account. **Stop** at a password, 2FA or consent screen and say "click Allow in the
  browser pane"; then re-test.
- **Browser path fails or the page is unreachable**: give the manual path (Settings -> Connectors -> the
  connector -> Reconnect, or `/mcp` in a terminal) and wait for "done".
- **Gateway server `unreachable`** (e.g. Willow): say "<gateway> is unreachable, probably VPN. Connect and
  say done." Do not touch the VPN.
- **Gateway server `needs_auth`**: use that server's own `authenticate` tool. A signed sign-in URL is a
  credential: show it in the session only, never in a file, log, memory or commit.

## 8. Report

A table, worst first:

| Connector | Capability | Status | Fixed by | Fix |
|---|---|---|---|---|
| chat | search | missing_scope:search:read | — | Ask the chat workspace admin to add the scope, or use the other server that can search |
| mail | read | expired | you | Signed in again — see §7 |
| calendar | read | error (unreachable) | fallback | Covered by the other server; gateway down, probably VPN |

`Fixed by` is `retry`, `reconnect`, `fallback`, `you` or `—`.

Fixes by status:
- `expired` → name the connector and how to sign in again (the server's auth tool, `/mcp`, the app's
  connector settings, or `claude mcp login <server>`). A **signed sign-in URL is a credential**: show it in
  the session, never write it to a file, memory, log or commit. Scheduled runs cannot complete a sign-in —
  say a human must.
- `missing_scope:<s>` → which scope, who can grant it, and which routine output is degraded meanwhile.
- `denied` → which admin setting or role is missing.
- `untested` → "run `/mother:doctor` and accept write checks" — only if a routine needs that write.

End with one line on impact: which routines will run `partial` until each fix lands.

Check the plugin itself: `${CLAUDE_PLUGIN_ROOT}/bin/mother status` prints `plugin: DISABLED` when
`enabledPlugins["mother@mother"]` is false in the user's Claude Code settings. Then the approval guard and the
sync hook don't load in any new session, so put it at the **top** of the report, above the connector table:
the fix is `claude plugin enable mother@mother` (or the app's plugin settings) and a restart. Never flip the
setting yourself; it's the manager's.

Under the table print `${CLAUDE_PLUGIN_ROOT}/bin/mother health summary` (7-day failures and fixes, so the
manager can tell gateway from connector from schedule). Then run `${CLAUDE_PLUGIN_ROOT}/bin/mother runs` and append its `MISSED` / `FAILED` rows under the table:
a routine that never logged a run is a plumbing failure (the scheduled task is gone, disabled, or the app
was closed for days), not a connector one. Fix: re-register it via `/mother:setup`, or open the app.

## 9. Unattended (the daily routine)

Read checks only, ladder steps 1-3 only, no write checks, no sign-ins. Update the record with `mother
health merge`/`record`. For each issue **not covered by a fallback**:

1. `mother health alert --kind K --cap C --server S [--urgent]` -> `{action: send|pending|skip}`.
2. On `send`: PushNotification, if that tool is present in the run (check with ToolSearch; skip silently
   if absent), and a DM to the manager through the server from `mother health pick --kind chat --cap post
   --target dm`. Name the connector, what is degraded, and the single step to take. Then
   `mother health alert … --sent`.
3. On `pending` (quiet hours): do nothing now; a later routine's pre-flight sends it. On `skip`: nothing.

A healthy day sends nothing. Also send the table if `mother runs` reports a missed or failed routine.
