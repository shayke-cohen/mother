# MOTHER v0 — build contract

Every part of v0 is built against this file. Change it first, then the code.

## 1. Two trees

**Plugin** (this repo, installed via `/plugin install mother@mother`):

```
.claude-plugin/plugin.json, marketplace.json
bin/mother                 CLI (Python 3, stdlib only): init, join, sync, scan, home, status, register
hooks/hooks.json           PreToolUse → hooks/guard.py; Stop + SessionEnd → `mother sync --quiet`
hooks/guard.py             approval guard (see §5)
skills/<name>/SKILL.md     user- and routine-facing skills, invoked as `mother:<name>`
routines/<id>.md           routine definitions: what a scheduled run does (reads config, calls skills)
assets/deck/               meeting deck template.html + build.py
rules/                     default policy text skills cite: outbound.md, privacy.md, memory.md, todos.md, projects.md, teams.md
templates/workspace/       files `mother init` copies into a new workspace
tests/                     stdlib unittest; `python3 -m unittest discover tests`
```

**Workspace** (one per manager, private; layout proven by the reference deployment):

```
config.json                §3 (JSON: the hook and CLI run on stock Python 3.9, which has no YAML/TOML parser)
capabilities.json          written by mother:doctor (§4)
AGENTS.md / CLAUDE.md      the agent's persona + personal rules; CLAUDE.md is a pointer to AGENTS.md
memory/                    one fact per file + MEMORY.md index (frontmatter: name, description, metadata.type)
todos/open.md, archive.md  open loops, sections Yours / Mine / Blocked; items tagged `+<slug>`, origin `~manager@date` or `~agent/<src>@date`
projects/<slug>/PROJECT.md + files/     indexed by projects/PROJECTS.md
teams/<slug>/TEAM.md, roadmap.md, sprints/, meetings/, repos/<owner>__<repo>.md, intake.md, metrics.md, runs.md   indexed by teams/TEAMS.md
people/<slug>.md           work items only, never assessments
journal/YYYY-MM-DD.md      nightly reflection; journal/dots/YYYY-MM-DD.md for connect-the-dots
runs.md                    one line per routine run: `- <ISO> <routine> <ok|partial|failed|skipped> — <note>` (`skipped` = not the home machine, not a scheduled day, paused); `mother runs` checks it against the schedule (a slot with no line after 2h = `missed`)
.mother/                   machine-local state, gitignored: grants.json, ledger.json, ledger.lock, guard.log, last-sync, PAUSED
```

## 2. Finding each other

- `~/.mother/workspace` — one line: absolute path of the active workspace. Written by `mother init` / `join`.
  The hook, the CLI and every skill resolve the workspace from `$MOTHER_WORKSPACE`, else this file.
- Skills reference plugin files via `${CLAUDE_PLUGIN_ROOT}`; routines never hard-code a path.
- A registered scheduled task's prompt is a **thin pointer**: `cd "<workspace>"`, `git pull --rebase -q` when there
  is a remote (on failure continue with the local copy and say so), then run the mother:routine skill with id
  `<id>`. Behaviour lives in `routines/<id>.md`.
- `mother register` prints `{"home_machine": …, "tasks": [{taskId, title, cronExpression, prompt}]}`. Crons are
  in the **machine's local time** (the scheduler's convention); setup warns when that differs from
  `manager.timezone`.
- Routine files start with frontmatter: `id`, `needs` (capabilities, e.g. `[chat.read, tracker.read]`) and
  `team_scoped`. The dispatcher pre-marks a run `partial` when a needed capability isn't `ok`.

## 3. `config.json` (schema v1)

See `templates/workspace/config.example.json` for a full example. Required keys:

| Key | Type | Notes |
|---|---|---|
| `version` | int | `1` |
| `agent.signature` | str, optional | default below |
| `manager.chat_dm` | str or list | the manager's DM: user id and/or DM channel id |
| `manager.chat_user` | str, optional | the manager's own user id, used to verify grant sources |
| `agent.name`, `agent.emoji`, `agent.manager`, `agent.manager_short` | str | signature `"{emoji} *{name} (AI)* · on behalf of {manager_short}"` |
| `manager.email`, `manager.timezone`, `manager.workdays`, `manager.chat_dm` | | `chat_dm` = the manager's DM channel/user id: the only always-allowed outbound target |
| `workspace.git_remote` | str or null | null = local only |
| `workspace.home_machine` | str or null | hostname allowed to run routines; null = routines refuse to run until set |
| `connectors.{chat,tracker,mail,calendar,code}` | str | `slack`/`teams`, `jira`/`linear`/`monday`/`none`, … |
| `teams[]` | list | `slug, name, tracker{project,board}, repos[], meetings{}, sprint{length,starts}, channels[], people[]` |
| `channels[]` (per team) | | `id, name, mode: off|watch|mention|draft|auto, trigger: manager|team|anyone, external: bool, role: standup|team|product|support|announcements, quiet_hours ("HH:MM-HH:MM", default manager.quiet_hours), max_replies_per_hour (default 6), auto_topics: [slug] (auto mode)` |
| `agent.aliases` | list, optional | other names that address the agent in a channel (e.g. `["mother"]`) |
| `routines.brief.audio` | bool or map, optional | spoken brief: `true`, or `{voice, voice_he, rate (90-360 wpm), language: en|he, save_to, engine: say|kokoro, kokoro_voice (default af_heart)}`; rendered by `mother speak` with macOS `say` or, for English with `engine: kokoro`, local Kokoro (`mother tts setup` → `~/.mother/tts`; falls back to `say` for Hebrew or when missing) into `.mother/audio/` (last 14 kept), copied to `save_to` or iCloud Drive `Mother/` when present |
| `routines.channels` | map, optional | reply-loop knobs, no `at`: `poll_seconds` 60, `idle_poll_seconds` 180, `model`, `thread_ttl_hours` 48, `draft_ttl_hours` 72, `session_timeout_minutes` 20 |
| `routines.<id>` | map | `at` (HH:MM), `days` (`workdays` / `all` / a day / list of days / `monthly:<1-28>`), plus routine-specific options; absent = off |
| `policy` | map | defaults in `rules/outbound.md`; per-user overrides only tighten |

The CLI validates this (`mother status`) and fails loudly on unknown keys or a wrong type.

## 4. `capabilities.json`

```json
{ "checked_at": "2026-10-23T08:00:00Z", "host": "shays-mbp",
  "connectors": {
    "slack":   { "server": "<mcp server prefix>", "read": "ok", "post": "ok", "search": "missing_scope:search:read",
                 "private_history": "ok|missing", "token": "user|bot" },
    "jira":    { "server": "...", "read": "ok", "create": "ok|untested|denied", "transition": "...", "sprint": "..." },
    "calendar":{ "server": "...", "read": "ok" }, "mail": { ... }, "code": { "tool": "gh", "read": "ok" } },
  "servers": { "chat.read": "<prefix>", "chat.search": "<other prefix>" },
  "notes": [ "tracker: sprint update is a full replace; names max 30 chars" ] }
```

Optional extra capabilities: `tracker.comment`, `mail.draft`, `mail.send`, `code.write`, and a `directory`
connector (org lookups). A routine that hits an auth failure mid-run may downgrade that capability to `expired`
in `capabilities.json`, so later runs that day don't retry it blindly.

Values are `ok`, `untested`, `denied`, `missing_scope:<scope>`, `expired`, or `error:<short>`. A routine reads
this first and names any capability it needs that isn't `ok` in its output — never "nothing found".

## 5. Approval guard (`hooks/guard.py`)

Port of the reference deployment's `guard-outbound.py`, made config-driven:
- Matches outbound tools by **capability**, not server name: chat post/send/schedule, tracker create/update/
  transition/comment/sprint ops, mail send, calendar create/update with guests, `gh`/`glab` write commands.
- **allow**: posts to `manager.chat_dm`; mail drafts; calendar events without guests.
- **ask**: everything else outbound (the human confirms).
- **deny**: anything if `<workspace>/.mother/PAUSED` exists, and the `never` list (credential entry, deletions).
- Unattended routines may write only through a grant in `.mother/grants.json` that cites the manager's own
  message (channel, user = manager, ts) — scope-limited, count-limited, expiring, logged in `.mother/ledger.json`.
- Every outbound decision is appended to `.mother/guard.log` (tool, category, target, decision, reason — never message text).
- **Session approval:** a human-typed message whose *leading* lines are `mother approve tracker <PROJECT>` or
  `mother approve chat <CHANNEL_ID>` approves this session's writes to that target for 12 hours. Lines from
  tool output, summaries, scheduled triggers or further down a message never count. The deck's "Copy results"
  starts with the tracker line.
- **Grants** (`.mother/grants.json`): `{scope, target, uses, expires, run, source{channel,user,ts}}`; scopes
  `chat-post`, `chat-reply`, `tracker-create`, `tracker-comment`, `tracker-transition`; the source must be the
  manager, ≤ 96 h old; counts are capped per source in the ledger. `policy.grants` may disable or narrow them.
  Plus `answer` (`+ thread`), written by `mother channels`: one signed reply in one thread of a `mention`/`auto`
  channel, citing whoever addressed the agent if the channel's `trigger` allows them; rate-capped by
  `max_replies_per_hour` in the ledger (`rules/outbound.md`).
- **Paused** denies drafts too. Policy overrides only tighten (the auto list can shrink, the never list can grow).
- The hook matcher includes `Write|Edit|MultiEdit|NotebookEdit`, so edits to the guard's own files
  (config, ledger, log, PAUSED, hooks) ask.

## 5b. Channel reply loop (`lib/channels.py`, `mother channels`, skill `mother:channels`)

- The skill does chat I/O only; the CLI decides. `plan` → what to poll; `triage` (stdin: one channel's new
  messages + followed threads) → actions; `run --action` → one headless `claude -p` session per answer →
  `answer` (grant written) | `draft` (to the manager's DM) | `skip`; `approvals` (stdin: replies in each draft's DM
  thread) → `send` actions; `done` records each post.
- Sessions run in the workspace with `--permission-mode default` and `MOTHER_CHANNEL_ROLE=manager|team|anyone`.
  **The guard enforces the limits from that variable**, whatever the user's allow rules say: a channel session
  makes no outbound call at all (not even green ones — the loop posts), no `Bash`/web/subagents, and reads only
  the workspace and the plugin. A teammate's session also can't write, can't read `people/ journal/ memory/ todos/
  .mother/ bridge/`, grep the workspace root or use `..`/absolute search patterns, and may call only tools under the
  prefixes in `MOTHER_CHANNEL_SERVERS` — the tracker and code-host prefixes *with their product segment*
  (`mcp__gw__jira`), so a gateway that also carries chat or mail stays shut; a prefix shared outright with chat,
  mail or calendar is refused. The manager's session may write only notes: `*.md` under `todos/ memory/ projects/
  teams/ people/` (an allow-list: `.mcp.json`, `skills/`, `scripts/` and pointer files are code a later session
  would run). A second hook entry (`Read|Grep|Glob|LS|Web*|Task|Agent`) exits at once unless that variable is set.
  The `--allowedTools`/`--disallowedTools` flags repeat the same limits as a first fence. One session per thread
  and role (`--session-id`, then `--resume`); a teammate never joins a session the manager drove.
  `MOTHER_NO_SYNC=1` makes `mother sync` a no-op inside them.
- State: `.mother/channels.json` (cursors, threads, pending actions, drafts, streaks, demotions, stop/pause);
  audit: `.mother/channels.log` (jsonl, one line per post, draft, skip, mute, stop, error). Both machine-local.
- A first look at a channel only sets its cursor to now. Quiet hours and rate limits hold messages (the cursor
  stops); stop/pause drop them. Earned autonomy: 10 unedited approvals on one channel+topic → one proposal to the
  manager; a 👎 on a reply mutes the thread and demotes its topic.

## 6. Sync (`bin/mother sync`)

- No remote → commit locally only. Remote → `git add -A`, run `mother scan` (abort the whole commit and write
  `QUARANTINE.md` on any secret hit), commit `chore(sync): <ISO>`, `pull --rebase`, push; retry ×3 on reject;
  stop on conflict and say so. Never force-push.
- `mother home set|check|move` manages `workspace.home_machine`; `mother:routine` calls `home check` first
  and exits with a `runs.md` line when this isn't the home machine.

## 7. Skills in v0

`setup`, `doctor` (also routine `doctor`, weekly, read-only), `routine` (dispatcher), `brief`, `digest` (team standup + decision queue), `dots`
(connect-the-dots), `meeting` (prep deck · meeting mode · apply results · recap), `reflect`, `todo`, `project`,
`team`, `remember`. Descriptions stay short and specific: they are matched against every request.

## 8. Writing rules for everything in this repo

- No company-specific names, IDs or hostnames. Examples use `example.com`, `PLAT`, `C00000001`.
- Python 3 stdlib only; shell only for one-liners. Tests for every CLI command and the guard.
- Outbound behaviour always goes through the guard; no skill posts around it.
