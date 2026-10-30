# MOTHER v0 — build contract

Every part of v0 is built against this file. Change it first, then the code.

## 1. Two trees

**Plugin** (this repo, installed via `/plugin install mother@mother`):

```
.claude-plugin/plugin.json, marketplace.json
bin/mother                 CLI (Python 3, stdlib only): init, join, sync, scan, home, status, register, runs, items, eval, now
hooks/hooks.json           PreToolUse → hooks/guard.py (every other tool via hooks/guard_fast.py); Stop + SessionEnd → `mother sync --quiet`
hooks/guard.py             approval guard (see §5)
hooks/guard_fast.py        the guard's fast path: every tool the main matcher doesn't take; escalates to guard.py for
                           channel roles, MOTHER's sub-agents, hub and hub-fed sessions and the grant key (see §5)
skills/<name>/SKILL.md     user- and routine-facing skills, invoked as `mother:<name>`
agents/<name>.md           read-only sub-agents MOTHER dispatches: `mother:reader`, `mother:critic` (see §5c)
routines/<id>.md           routine definitions: what a scheduled run does (reads config, calls skills)
lib/                       channels.py (§5b), items.py (reader evidence check), evals.py (A/B harness), now.py (tick's Now list)
assets/deck/               meeting deck template.html + build.py
rules/                     default policy text skills cite: outbound.md, privacy.md, memory.md, todos.md, projects.md, teams.md
templates/workspace/       files `mother init` copies into a new workspace
mods/now/                  the Now panel, a second plugin in the same marketplace (`/plugin install mother-now@mother`):
                           .claude-plugin/plugin.json (userConfig `mother_bin`), hooks/register.tsx (+ tests), types/index.d.ts;
                           opens with `/now-panel` (the engine reserves `/now` for the mother:now skill);
                           reads `.mother/now.json` and the hub's `.mother/hub/hub.json`, `sessions.json`, `watcher.json` (§5e), writes only through
                           `mother now done|snooze|top` and `mother hub done|snooze|session seen`, no network or MCP calls;
                           a Hub section (health, Sessions, Waiting for you to start, Hub updates, Recently routed) and `hub ●|◌ stale|✗ · N waiting` on the status line (✗: `watcher.json` missing or its `at` older than max(10 min, 2x `every_seconds`); ◌ stale, only while `active`: `at` older than 2x `every_seconds`, or `last_append_at` later than `hub.json` `last_wake` (or no wake) and older than 2x `every_seconds`)
tests/                     stdlib unittest; `python3 -m unittest discover tests`
```

**Workspace** (one per manager, private; layout proven by the reference deployment):

```
config.json                §3 (JSON: the hook and CLI run on stock Python 3.9, which has no YAML/TOML parser)
capabilities.json          written by mother:doctor and `mother health` (§4)
health.md                  one line per connector failure or fix, appended by `mother health record` (no ids, no content)
AGENTS.md / CLAUDE.md      the agent's persona + personal rules; CLAUDE.md is a pointer to AGENTS.md
memory/                    one fact per file + MEMORY.md index (frontmatter: name, description, metadata.type)
todos/open.md, archive.md  open loops, sections Yours / Mine / Blocked; items tagged `+<slug>`, origin `~manager@date` or `~agent/<src>@date`
projects/<slug>/PROJECT.md + files/     indexed by projects/PROJECTS.md
teams/<slug>/TEAM.md, roadmap.md, sprints/, meetings/, repos/<owner>__<repo>.md, intake.md, metrics.md, runs.md   indexed by teams/TEAMS.md
people/<slug>.md           work items only, never assessments
one-on-ones/<slug>/        next.md (queued topics) + YYYY-MM-DD.md (prep, then notes: topics and commitments only)
journal/YYYY-MM-DD.md      nightly reflection; journal/dots/YYYY-MM-DD.md for connect-the-dots; journal/commitments/YYYY-MM-DD.md for the commitment tracker; journal/brief/YYYY-MM-DD.md for the brief's triage list
runs.md                    one line per routine run: `- <ISO> <routine> <ok|partial|failed|skipped> — <note>` (`skipped` = not the home machine, not a scheduled day, paused); `mother runs` checks it against the schedule (a slot with no line after 2h = `missed`)
.mother/                   machine-local state, gitignored: grants.json, ledger.json, ledger.lock, health.lock, guard.log, last-sync, PAUSED, eval/ (A/B pairs), tick-register.json (the offset the tick cron was built with, §2),
                           now.json (the tick's Now list: items, Top 3, coverage; ids and links, no message bodies, no per-person counts),
                           now-state.json (the manager's done/snooze/Top 3 edits), now.lock (serialises both); hub/ (the watcher and hub state, §5e); all written atomically. Each item's `actions[]` is `{id (S<n>), label, prompt, task}`: `prompt` is always `mother now do <id>` (what the panel pastes); `task` is the routine's original instruction (`dots do A3`, `digest ok 12`, `draft a nudge to …`) that `mother now do` prints for the `mother:now` skill. Top 3 entries are `{rank, title, why, link, item_id, set_by (agent|manager), set_at, state}`: `set_at` is the ISO time the list was set (the tick's `generated_at` when it first proposed it, carried on later ticks; the edit time for `mother now top set|swap`, kept as `at` in now-state.json). Only `top set` and `top swap` make the list the manager's (`set_by: manager` on every entry, and no swap hint); `top done` marks one entry done and keeps every entry's `set_by` and `set_at`, so an agent list stays the agent's (its swap hint too). The tick sends a fresh proposal on the first tick of each local day; merge ignores it while a manager list for today exists. Which toasts fired is per-device, in the mod's store (`toasted`, keyed `<item id>@<first_seen>`, so an item asked for again toasts again), not in the workspace. `swap_hint` is null or `{title, why, link, item_id?}`; merge keeps `item_id` only when it names an item of this run, else takes the one item with the same `link`, else drops it (the hint stays). A Top 3 entry without an `item_id` takes the id of the one item with the same `link`. Items that share a `(signal, link)` pair through `link` or `also` are one item: merge lists it once, and keeps an earlier item's id when the same work comes back under another link, so done and snooze still apply. Merge drops a candidate without a `why`, and any item whose `since` is older than `routines.tick.max_age_days` unless its signal's source is the workspace. Its stdout (the runs.md note) ends `couldn't check: <sources|none>`, then `; partial: <sources>` when some were only partly read
```

## 2. Finding each other

- `~/.mother/workspace` — one line: absolute path of the active workspace. Written by `mother init` / `join`.
  The hook, the CLI and every skill resolve the workspace from `$MOTHER_WORKSPACE`, else this file.
- Skills reference plugin files via `${CLAUDE_PLUGIN_ROOT}`; routines never hard-code a path.
- A registered scheduled task's prompt is a **thin pointer**: `cd "<workspace>"`, `git pull --rebase --autostash -q` when there
  is a remote (on failure continue with the local copy and say so), then run the mother:routine skill with id
  `<id>`. Behaviour lives in `routines/<id>.md`.
- `mother register` prints `{"home_machine": …, "tasks": [{taskId, title, cronExpression, prompt}]}`. Crons are
  in the **machine's local time** (the scheduler's convention); setup warns when that differs from
  `manager.timezone`, and converts `at` times by hand.
- **Interval routines** (only `tick`) have `every` (minutes) and `active_hours` instead of `at`. Their slots are
  minute :05 (and :35 for `every: 30`) of each step hour inside `active_hours`, in **`manager.timezone`** — the
  clock `lib/now.py`, the dispatcher's active-hours gate and `mother runs` all use. `mother register` converts
  them to the machine's clock with today's offset between the two zones, minute included (a half-hour offset
  turns :05 into :35): `5 6-21 * * 1,2,3,4,5` for the defaults when the zones agree, `35 11-22 …` for
  `06:00-18:00` in London on a +05:30 machine. With `every: 30` and a half-hour offset the cron can fire once
  more at either end; the dispatcher skips that fire as outside active hours. A window that would cross the
  machine's midnight is not a config error (`validate` doesn't depend on the machine's zone): `mother register`
  leaves tick out, still prints every other routine, adds `"errors": ["routines.tick.active_hours crosses
  midnight in this machine's time zone; …"]` to its JSON, says so on stderr and exits 2 (days are never
  shifted). `mother runs` shows tick `failed` with that note (exit 2, as for an invalid tick config), and
  `mother status` prints it as a `tick: WARNING` line with `config: valid`. The tick task entry carries
  `offset_minutes`, also kept in `.mother/tick-register.json`; when today's offset differs from it (a DST
  change in one of the two zones), `mother runs` shows tick `failed` with `re-run mother register (time-zone
  offset changed)` instead of missed slots. `mother runs` judges `tick` in `manager.timezone`: missed only inside active hours on a
  scheduled day, once 2x `every` has passed after the day's first slot with no `runs.md` line in 2x `every`.
- A routine whose frontmatter says `sync: false` (`tick`) is machine-local: its pointer **omits** the
  `git pull --rebase` (`_pointer(…, pull=False)`: just `cd "<workspace>"`, then the mother:routine skill), and
  the dispatcher skips the final `mother sync`. The Stop/SessionEnd hooks' `mother sync --quiet` still runs when
  that session ends, so it skips the commit and push when the only change in the workspace is `runs.md` lines
  appended for routine `tick`; the next sync with anything else (or any `mother sync` without `--quiet`) carries
  them.
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
| `manager.tracker_account` | str, optional | the manager's tracker account id; the hub watcher uses it (and `manager.email`) to tell the manager's own tracker edits from others' |
| `agent.name`, `agent.emoji`, `agent.manager`, `agent.manager_short` | str | signature `"{emoji} *{name} (AI)* · on behalf of {manager_short}"` |
| `manager.email`, `manager.timezone`, `manager.workdays`, `manager.chat_dm` | | `chat_dm` = the manager's DM channel/user id: the only always-allowed outbound target |
| `workspace.git_remote` | str or null | null = local only |
| `workspace.home_machine` | str or null | hostname allowed to run routines; null = routines refuse to run until set |
| `connectors.{chat,tracker,mail,calendar,code}` | str | `slack`/`teams`, `jira`/`linear`/`monday`/`none`, … |
| `teams[]` | list | `slug, name, tracker{project,board}, repos[], meetings{}, sprint{length,starts}, channels[], people[]` |
| `people[]` (per team) | | `name, email, chat, code`, optional `one_on_one {cadence_days (3-90, default 14), title}` for 1:1 prep and drift |
| `channels[]` (per team) | | `id, name, mode: off|watch|mention|draft|auto, trigger: manager|team|anyone, external: bool, role: standup|team|product|support|announcements, quiet_hours ("HH:MM-HH:MM", default manager.quiet_hours), max_replies_per_hour (default 6), auto_topics: [slug] (auto mode)` |
| `agent.aliases` | list, optional | other names that address the agent in a channel (e.g. `["mother"]`) |
| `routines.brief.audio` | bool or map, optional | spoken brief: `true`, or `{voice, voice_he, rate (90-360 wpm), language: en|he, save_to, engine: say|kokoro, kokoro_voice (default af_heart)}`; rendered by `mother speak` with macOS `say` or, for English with `engine: kokoro`, local Kokoro (`mother tts setup` → `~/.mother/tts`; falls back to `say` for Hebrew or when missing) into `.mother/audio/` (last 14 kept), copied to `save_to` or iCloud Drive `Mother/` when present |
| `routines.brief.draft_replies` | bool, optional | default false; true = the brief routine saves mail **drafts** for To-respond mail it triaged as `prep` (never sends) |
| `routines.commitments` | map, optional | `at`, `days`, `no_reply_days` (1-30, default 3), `lookback_days` (1-60, default 14), `draft_nudges` (bool, default false: mail drafts for no-reply asks only on the manager's `nudge <n>`) |
| `routines.tick` | map, optional | the hourly pulse behind the Now list (`mother now`); absent = off. `every` (minutes, one of 30, 60, 120, 180, 240; default 60), `active_hours` (`"HH:MM-HH:MM"`, start before end, default `"06:00-22:00"`, in `manager.timezone`; no tick runs outside it), `days` (`workdays` / `all` / a day / list of days; not `monthly:`), `thresholds` (map signal -> `Nh`, `Nd` or `0`, counted in working time; default per signal), `signals` (map signal -> `true`/`false`; all on by default, and a signal whose source connector is `none` is off), `notify` (`none` default, or `dm` = one DM to `manager.chat_dm` for a new on-you item; never while `now.json.quiet`), `slow_every` (integer 1-6, default 3: every Nth slot scans every signal, the others only the on-you signals and `default_branch_red`), `max_age_days` (integer 0-365, default 30: work whose `since` is older is left off the list, except workspace signals; `0` = no cap). Default thresholds: `review_requested` 4h, `unanswered_mention` 2h, `decision_open` 1d, `mail_needs_reply` 4h, `pr_no_reviewer` 1d, `pr_approved_unmerged` 2d, `pr_changes_silent` 3d, `default_branch_red` 2h, `ticket_stalled` 3d, `bug_unowned` 1d, `blocker_cleared` 1d, `outside_question` 4h, `action_item_no_ticket` 1d, the rest 0. Signals: on you `review_requested`, `unanswered_mention`, `decision_open`, `ticket_waiting_on_you`, `mail_needs_reply`, `invite_unanswered`, `promise_due`, `suggestion_waiting`; neglected `pr_no_reviewer`, `pr_approved_unmerged`, `pr_changes_silent`, `default_branch_red`, `ticket_stalled`, `bug_unowned`, `blocker_cleared`, `sprint_at_risk`, `outside_question`, `action_item_no_ticket`, `one_on_one_drift`. Unknown keys or signals fail `mother status`. |
| `routines.channels` | map, optional | reply-loop knobs, no `at`: `poll_seconds` 60, `idle_poll_seconds` 180, `model`, `thread_ttl_hours` 48, `draft_ttl_hours` 72, `session_timeout_minutes` 20 |
| `whatsapp` | map, optional | WhatsApp read and send from the manager's own number (§5d). `enabled` (bool), `self_jid` (the manager's own JID, `<digits>@s.whatsapp.net`; required when `enabled`), `max_per_day` (1-50, default 20), `min_gap_seconds` (>= 0, default 5), `read`, `send` (bool). Other unknown keys fail. The keys of the earlier design, `allow`, `approve_via` and `max_prompts_per_day`, are accepted with any value and ignored; `mother whatsapp setup` drops them the next time it writes the block, and they are removed one release after WhatsApp ships. The block is written by `mother whatsapp setup`, run by the manager in their own terminal (§5d); the agent never edits it. JIDs are phone numbers: they live in `config.json` and tool output only, never in memory, journal or people files. |
| `hub` | map, optional | The watcher and hub (§5e). `enabled` (bool, default false), `every_seconds` (int 30-3600, default 120: the watcher's interval in active hours), `idle_every_seconds` (int 0-86400, default 600; 0 = no polling outside active hours), `model` (str or null: the hub session's model), `max_cards_per_wake` (int 1-3, default 3), `sweep_minutes` (int 10-240, default 30), `update_ttl_hours` (int 1-168, default 24, counted in working hours), `connections` (map from capability `chat|mail|tracker|calendar|code` to `{command: str, args: [str], env: {str: str}}` or `{cli: "gh"}`; one of `command` or `cli` is required), `watch` (`chat.channels` [id], `mail.ignore_senders` [str], `mail.ignore_labels` [str], `tracker.projects` [str], `tracker.extra_jql` str or null, `calendar.lead_minutes` int, `code.repos` [str]). Unknown keys fail `mother status`. |
| `routines.<id>` | map | `at` (HH:MM), `days` (`workdays` / `all` / a day / list of days / `monthly:<1-28>`), plus routine-specific options; absent = off |
| `policy` | map | defaults in `rules/outbound.md`; per-user overrides only tighten |
| `policy.bypass_passes_asks` | bool, optional | default `false`: in a session whose cwd is the workspace, a guard ask stays an ask in `bypassPermissions` mode. `true` restores the pass (§5). Any other type fails `mother status` |

The CLI validates this (`mother status`) and fails loudly on unknown keys or a wrong type.

## 4. `capabilities.json`

```json
{ "checked_at": "2026-10-23T08:00:00Z", "host": "home-mac",
  "connectors": {
    "slack":   { "server": "mcp__Acme_Slack__slack",
                 "read": "ok", "post": "ok", "search": "ok",
                 "servers": { "read":   ["mcp__Acme_Slack__slack", "mcp__<claude.ai slack>"],
                              "search": ["mcp__<claude.ai slack>"],
                              "post":   ["mcp__Acme_Slack__slack", "mcp__<claude.ai slack>"] },
                 "health":  { "mcp__Acme_Slack__slack": {
                                "_gateway": "mcp.acme.internal",
                                "read": {"status": "error:unreachable", "class": "unreachable",
                                         "checked_at": "2026-10-25T07:30:00Z"},
                                "search": {"status": "missing_scope:search:read", "class": "missing_scope",
                                           "checked_at": "2026-10-25T07:30:00Z"} },
                              "mcp__<claude.ai slack>": {
                                "read": {"status": "ok", "checked_at": "2026-10-25T07:30:00Z"} } },
                 "stamps":  ["mcp__<claude.ai slack>"],
                 "token":   { "mcp__Acme_Slack__slack": "user", "mcp__<claude.ai slack>": "user" } },
    "jira":    { "server": "...", "read": "ok", "create": "ok|untested|denied", "transition": "...", "sprint": "..." },
    "calendar":{ "server": "...", "read": "ok" }, "mail": { ... }, "code": { "tool": "gh", "read": "ok" } },
  "notes": [ "tracker: sprint update is a full replace; names max 30 chars" ] }
```

Per connector:

| Key | Meaning |
|---|---|
| `server` | The main server prefix. A **tool-based** connector (`code`: `"tool": "gh"`) has no `server` or `servers`; its capability values and string `token` are its own. `tool`, `notes` and `host` are not capabilities: only keys holding a status value are. |
| `<cap>` (`read`, `post`, …) | The **rollup** for that capability: `ok` when any server in its list is `ok`, else the first (preferred) server's status. Routines that read only this keep working. |
| `servers.<cap>` | Ordered list of server prefixes. First = preferred, rest = tested backups. The order is a preference, not a cache: setup and the doctor write it, preferring a server with no stamp and the manager's identity. A failure never reorders it; `mother health pick` takes the first server whose entry is `ok`, so a recovered server is used again. For a **write** capability (`post`, `post_dm`, `create`, `comment`, `transition`, `sprint`, `draft`, `send`, `write`), which the doctor never probes unattended, it then takes the first `untested` one and says `"verified": false`; failing statuses are never picked. `pick` on a tool-based connector returns `{"server": null, "tool": "gh", "status": …}`. |
| `health.<server>.<cap>` | One server's result: `status` (value list below), `class`, `checked_at` (ISO UTC), and when alerted `alerted_at`, `alerted_class`, `pending`. |
| `health.<server>._gateway` | The gateway host the server shares with others (Acme). Keys starting `_` are metadata, not capabilities. Servers on one gateway that fail with a network error are one `unreachable` issue. |
| `stamps` | Server prefixes that add their own attribution to chat writes. The guard reads this (§5). |
| `token` | `{server: user\|bot}` — the identity each server writes as. A write to anyone but the manager falls back (`pick --target other`) only to a server with no stamp whose token is known and equal to the preferred server's. |

**Failure classes** (what fixes it): `unreachable` (network or timeout, a whole gateway at once; switch
server), `dropped` (server `failed`, "connection closed"; reconnect), `invalidated` (401, `needs_auth`,
"connection invalidated"; human sign-in), `missing_scope` and `denied` (no healing; use the server that has
it), `other`. `mother health classify --error TEXT` maps an error message to a status and class.

**Alerts.** At most one per issue per manager-local day, held during `manager.quiet_hours` unless urgent
(`mother health alert`, `mother health pending`). An issue is one (server, capability), except that every
entry on one `_gateway` with the same class is one outage: `alert --sent` stamps them all, and any of them
alerted today makes the rest `skip`. `ok` and `untested` never alert. `alerted_at` survives an `ok` record
(`pending` is cleared) and is dropped only when the entry fails again with a different class
(`alerted_class` records which class was alerted).

**All runtime writes go through `mother health`** (`record`, `merge`, `alert`; `pick` and `summary` read).
It takes `.mother/health.lock`, writes atomically, and appends a line to `health.md` for each failure or fix:

```
One line per connector failure or fix, appended by `mother health record`.
- <ISO> <kind>.<cap> <server short name> <class> → <retry|reconnect|fallback|you|—|open> [@<gateway>]
```

Routines and the doctor never edit the file by hand. The doctor's fresh result is merged in
(`mother health merge`), keeping list order and every `alerted_at`. A connector the fresh result leaves out
is kept as it was; the doctor's `server` stands while it is still listed (else `servers.read[0]`, else the
first listed); an entry with no `checked_at` takes the date of the run that wrote it (the fresh file's, or the old
file's for a connector the run left out), and so does a tool connector's own `checked_at`. Merge logs an `open` line for each entry
that moved into a failing status or class, and its `changed` list names rollups that went from `ok` to a
failing status (not to `untested`). `record --fixed-by fallback` on a backup logs the preferred server's
class `→ fallback`, whatever the backup's own previous status.

**Still read, no longer written:** a bare-string `servers` value; the top-level `servers` map
(`"<kind>.<cap>": "<prefix>"`); a string `token` (read as `{server: value}` for the connector's main
server); a connector without `health` (seeded from its top-level capability values).

Optional extra capabilities: `tracker.comment`, `mail.draft`, `mail.send`, `code.write`, and a `directory`
connector (org lookups). A routine that hits an auth failure mid-run records it with `mother health record`
(status `expired`), so later runs that day don't retry it blindly.

Values are `ok`, `untested`, `denied`, `missing_scope:<scope>`, `expired`, or `error:<short>`. A routine reads
this first and names any capability it needs that isn't `ok` in its output — never "nothing found".
`untested` is not an open health issue in `mother status`.

## 5. Approval guard (`hooks/guard.py`)

Port of the reference deployment's `guard-outbound.py`, made config-driven:
- **Hook registration** (`hooks/hooks.json`): the guard sees only the tools Claude Code hands it, so every tool
  reaches it, in every session, through exactly one of two PreToolUse entries. The tools it has a rule for in
  every session go to `guard.py`: `Bash Write Edit MultiEdit NotebookEdit WebFetch WebSearch Task Agent Skill
  Monitor CronCreate CronDelete TaskStop SendMessage ListAgents` and every `mcp__…` tool (an anchored matcher).
  Every other tool (the complement matcher `^(?!(?:…)$)(?!mcp__).*`: `Read Grep Glob LS`, `ToolSearch`,
  `TodoWrite`, `CronList`, and whatever the host adds, such as `Artifact` or `RemoteTrigger`) goes to
  `hooks/guard_fast.py`. The fast path hands the call to `guard.py` unchanged when `MOTHER_CHANNEL_ROLE` is set,
  `agent_type` names one of MOTHER's sub-agents (`reader`, `critic`, with or without `mother:`), the session id
  appears in `.mother/hub/hub-session.json`, `sessions.json` or `fed-sessions.json` (a hub or hub-fed session),
  a Read/Grep/Glob/LS might touch the grant key (casefolded), or anything in the payload is odd (bad JSON, a
  non-object input, a non-string `cwd` or `agent_type`); otherwise it exits with no decision. It never decides
  itself, so the sub-agents' tool lists and the hub's deny-by-default cover every tool. `tests/test_packaging.py`
  collects every tool name in a comparison on `tool` in `guard.py` (named sets resolved) and fails unless each,
  and any other name, matches exactly one entry, and each one with a rule outside the fast path's cases goes to
  `guard.py`. `guard_fast.py` is one of the guard's own files (edits ask, like `guard.py`).
- Matches outbound tools by **capability**, not server name: chat post/send/schedule, tracker create/update/
  transition/comment/sprint ops, mail send, calendar create/update with guests, `gh`/`glab` write commands.
- **allow**: posts to `manager.chat_dm`; mail drafts; calendar events without guests.
- **ask**: everything else outbound (the human confirms). When the payload's `permission_mode` is
  `bypassPermissions`, an ask gets no output and is logged as `pass` only when the session's cwd is outside the
  workspace, or `policy.bypass_passes_asks` is `true` (an invalid config counts as `false`). An ask for the guard's
  own files, `grants.json` or a persona file never passes.
- **ask, every session and mode** (`kind: "grants"`, `op: "cli"`): a shell command whose text, with quotes and
  backslashes dropped and casefolded, has `grant add` (or `grant` followed by a word holding `$` or a backtick):
  `mother grant add` in any form. Never passed by bypass, whatever `policy.bypass_passes_asks` says; **deny** under
  PAUSED and `MOTHER_DRY_RUN`. `mother grant remove|list` get no decision, except in a hub-fed session (below).
- **ask, every mode** (`kind: "grants"`): a Write/Edit/MultiEdit/NotebookEdit of `<workspace>/.mother/grants.json`;
  a shell command that writes (the self-edit write test, plus `tar`, `unzip`, `ditto`, `patch`, `cpio`, `pax`,
  `mkdir`, and `git checkout|restore|reset|apply|am|stash|clone|pull|merge|cherry-pick|rebase|worktree`) and whose
  normalised text (heredoc bodies of plain commands blanked, quotes and backslashes dropped, casefolded) names
  `grants.json` or `.mother` (not after a home directory: `~/`, `$HOME/`, `/Users/<x>/`), when it reaches the
  workspace (run from inside it and not first `cd`-ing to an absolute or home path, or naming the workspace path,
  `$W/` or `$MOTHER_WORKSPACE`). **deny** under PAUSED. `mother grant add|remove` is the one writer (below).
- **ask, every mode** (`kind: "persona"`): an edit (Write/Edit/MultiEdit/NotebookEdit) of the workspace's
  `AGENTS.md`, `.mcp.json`, `memory/MEMORY.md`, any `CLAUDE.md` in it, or anything under a `.claude/` folder in it
  (casefolded); a shell write, under the same normalisation and reach test, naming `agents.md`, `claude.md`,
  `.mcp.json`, `.claude` (not after a home directory), `memory.md`, or a glob or `$` right after `memory/`.
  PAUSED leaves it an ask. A channel session for the manager asks for `memory/MEMORY.md` too. `mother memory
  index` is the unprompted writer of the index (§7).
- **deny** (`kind: "approve-line"`): an in-app session tool (`start_session`, `send_message`, `SendMessage`, …)
  whose input has, in any string, a line that starts `mother approve` (any case, any or no whitespace between the
  words, Unicode format characters dropped; CR, VT, FF, U+001C-U+001E, U+0085, U+2028 and U+2029 end a line too).
  Only the manager types approvals.
- **ask** (`kind: "outbound_unknown"`), only in a session whose cwd is the workspace or under it (any role; outside
  the workspace these get no decision, as before): a shell command (Bash, a `Monitor` `command`, or an MCP
  `run_in_terminal` `command`; heredoc bodies of plain commands and of a `mother` invocation are data) that has
  `/dev/tcp/` or `/dev/udp/` anywhere, or any simple command (split on `;&|()` and newlines, `$( )`, `<( )` and
  backticks included, words after assignments, redirects and the wrappers `env sudo doas command builtin exec
  nohup time nice xargs timeout watch caffeinate parallel stdbuf …` and keywords `if then do else …` skipped)
  whose program word starts with `$` (but not `${CLAUDE_PLUGIN_ROOT}/…` unless the line assigns it) or holds
  `$(` or a backtick; or whose program, quotes and backslashes dropped, is `curl`, `wget`, `nc`, `ncat`, `netcat`,
  `socat`, `ssh`, `scp`, `sftp`, `rsync`, `ftp`, `telnet`, `http`, `https`, `httpie` or `xh` (also as the word
  after `-exec`/`-execdir`/`-ok`, or behind xargs/parallel/watch/env/sudo); `sh|bash|zsh|… -c TEXT` and `eval`
  with TEXT classified the same way; `python*`, `node`, `perl`, `ruby`, `php`, `deno`, `bun`, `swift` or
  `osascript` with a `://` argument, `-m` of a network module (`http.server`, `urllib`, `smtplib`, …), or inline
  code (`-c`, `-e`, `-E`, `-r`, `-p`, `--eval`, `--print`, `-`, a heredoc) whose text matches a network or process
  marker (`socket`, `urllib`, `http`, `requests`, `httpx`, `fetch`, `subprocess`, `system`, `popen`, `pty`,
  `/dev/tcp`, `curl`, `wget`, `smtplib`, `ftplib`, `telnet`, `net::`, `lwp`, `open-uri`, `child_process`,
  `spawn`, `exec`, a backtick; osascript inline always counts), never when the script is the `mother` CLI;
  `git push` with a URL or `user@host:` argument, `git remote add|set-url`; `open` with a URL; `nslookup`,
  `dig`, `host` or `drill` with `$(` or a backtick; `gh`/`glab api` with `--hostname` or a `://` endpoint;
  `WebFetch`; `WebSearch` whose query holds a URL (`://`, `www.`, or `host.tld/…`); an MCP tool whose name has a
  `navigate` part, or a `…batch` tool whose input names one; an MCP tool whose name has a `javascript`,
  `evaluate`, `eval` or `js` part; an MCP tool whose name has a `fetch` part and whose input has a url-like field
  or a `://` value; a `Monitor` with a `ws` target; and an MCP call the domain rules give no decision whose operation is not a read
  verb but has a write verb (the write list plus `copy`, `batch`, `save`, `store`, `import`, `sync`, `deploy`,
  `notify`, `broadcast`, `duplicate`, `clone`, `emit`, `dispatch`, `enqueue`, `commit`, `release`) and whose
  server is not the app's own (`mcp__ccd_…`). PAUSED and `MOTHER_DRY_RUN` deny it.
- **Monitor** in every session: its `command` is classified exactly like a Bash command (§5 rules above).
- **The workspace shell allow-list** (`shell_unlisted`). In a session whose cwd is the workspace or under it, other
  than the hub (its own templates), a channel session and MOTHER's sub-agents (their own rules), a Bash command, a
  Monitor `command` and an MCP `run_in_terminal` `command` get **no decision** only when the strict reader takes
  every stage. Order: WhatsApp rules; `mother grant add` (above); the allow-list; when it refuses, the other Bash
  rules (guard files, protected names, code-host writes), then `outbound_unknown` for a network program, then the
  kind of protected path it named (`grants` for `.mother/` or `~/.mother/`, `persona`, `home_settings` for a write
  under `~/.claude`), else **ask** `shell_unlisted` naming the first part refused. `shell_unlisted` asks under
  PAUSED, is denied by `MOTHER_DRY_RUN`, and passes in bypass only with `policy.bypass_passes_asks`.
  - **Reader.** Stages split on newline, `;`, `&&`, `||`, `|`. Refused anywhere: `$` other than
    `$CLAUDE_PLUGIN_ROOT`, `${CLAUDE_PLUGIN_ROOT}` and `$HOME` (expanded; a line that assigns either is refused);
    backticks; `*?[]{}!` outside quotes (a lone `[`/`]` word excepted); a `#` comment; `(`/`)` (subshells,
    process substitution); a lone `&`; `|&`; `<<<`; `<>`; `>|`; a zsh numeric glob `<N-M>`; any backslash outside
    single quotes (inside double quotes too: the shell deletes `\`+newline there); a word starting with `=` (zsh
    expands `=cmd` to a program path); `~` not followed by `/`; an unclosed quote; an assignment word; a program
    given by path (except the plugin's `bin/mother` and the two plugin scripts below); a heredoc unless into
    `mother` with an operator exactly `<<'WORD'`, `<<"WORD"`, `<<-'WORD'` or `<<-"WORD"`, WORD
    `[A-Za-z_][A-Za-z0-9_]*`, followed by whitespace or the end of the line (its body is skipped exactly to its end
    line, so it hides nothing after it).
    Redirections: `/dev/null`, `N>&M`, or a literal path; output only inside the workspace and not protected; an
    input (`<`) source gets the same path checks as an operand, hidden-path rule included.
  - **Programs.** `mother` (bare, the plugin's `bin/mother` by path or `${CLAUDE_PLUGIN_ROOT}`, or `python3` +
    that path) with a verb from `path version help status runs scan sync now hub health items memory speak
    channels whatsapp grant home eval register`, except `grant` other than `remove|list|help` (→ `grants`,
    `op: cli`), `hub install|uninstall`, `home` other than `check|show`, `eval` with `--plugin-dir` or
    `--skip-permissions`, and `register` with any argument; any other verb (`init`, `join`, `link`, `tts`, …)
    asks. `claude mcp list` (exactly). `python3 <plugin>/assets/plan/build.py …` and `…/assets/deck/build.py …`
    with literal arguments (an `.html` operand is a write target). Read-only: `ls cat head tail wc sort uniq cut tr grep egrep rg jq diff cmp stat file
    basename dirname realpath date pwd echo printf true false test [ sleep which`, not `date -s`. `printf` and `echo`
    take **no options**: a first argument starting with `-` asks (`printf -v HOME …` would set a variable for the
    stages after it). No builtin that can set a variable is ever on the list, and naming one asks: `read declare
    typeset export set local let getopts mapfile readarray vared zle unset alias source . eval print readonly
    integer float shift unalias hash builtin command emulate setopt`. **Options:** these
    take only the options listed (a short cluster is read letter by letter; a value-taking letter takes the rest of
    the cluster or the next word; anything else asks):
    `rg` `-n -i -l -c -w -v -F -S --files --hidden --json --no-heading` (and their long names) and `-e -g --glob
    -t --type -A -B -C -m --max-count --regexp` with a value (so no `--pre`, `-z`, `--search-zip`, `--hostname-bin`,
    `--hyperlink-format`, `-f`, `--follow`); `grep`/`egrep` `-n -i -l -c -w -v -E -F -r -R -h -H -o -q -s` and
    `-e -A -B -C -m --include --exclude -f` (`-f` only with a workspace path); `sed` `-n -E` and `-e SCRIPT`, with
    each script part an address plus `p`, `d`, `q`, `=` or `s///` with flags from `gpIi0-9` (no `w W r R e`); `sort`
    `-n -r -u -f -V -k -t`; `jq` `-r -c -s -n -e -S` (and long names), `--arg`/`--argjson NAME VALUE`, with a
    filter that doesn't name `$ENV`, `env`, `import`, `include` or `input_filename`; `cut` `-s -f -d -c -b`; `tr`
    `-d -s -c -C`; `head` `-n -c -N`; `tail` `-r -n -c -N` (no `-f`); `wc` `-l -w -c -m`; `uniq` `-c -d -u -i -f -s`
    with at most one file; `diff` `-u -q -r -N -i -w -b -B -a --brief -U`; `file` `-b -i -I -L -h --brief --mime
    --mime-type`. `find`: start paths, then only `-name -iname -path -ipath -type -maxdepth -mindepth -mtime -mmin
    -newer -size` (each with a value; `-newer`'s is a path) and `-empty -print -print0 -prune -not ! -o -or -a -and
    ( ) -depth` (no `-exec`, `-delete`, `-rm`, `-fprint*`, `-files0-from`, …). The operands of these programs
    (files, not the grep/rg pattern, the sed script or the jq filter) are paths (below).
    `cd` only as the first stage, to a literal existing folder inside the workspace, and joined to the rest only
    by `&&` (later relative paths resolve from there). File ops `mkdir` (`-p -v`), `touch` (`-c`), `cp`/`mv`
    (`-f -i -n -v -p`), `rm` (`-r -R -f -i -v`) with every operand inside the workspace and not protected (a
    `cp`/`mv` into a folder checks folder/name), no recursive `cp`/`mv`, and `rm -r` only of a folder that holds no
    protected path (at most 2000 entries). **No folder swaps:** no source, destination (as written and as
    folder/name) or, for `mkdir`, folder it would create may be a protected path, a folder above one (`memory`, the
    root) or a folder that holds one on disk (at most 2000 entries), so `mv memory memory.old && mv m3 memory`
    asks. `gh`/`glab`: the reader's reads (§5c) and `auth status`. `git`: global options only `-C`, `--no-pager`,
    `--no-optional-locks`, and it runs **only at the workspace root** (the stage's cwd after any `cd` and `-C` must
    be the root: a `.git` below it brings its own config, such as `core.fsmonitor`); subcommands `status log diff
    show add commit pull push fetch rev-parse branch ls-files blame` and `stash list`; not `commit -F/--file/-t/
    --template/--amend`, `blame --contents`, `diff|log|show -o`; no argument starting `-c`, `--exec`,
    `--upload-pack`, `--receive-pack`, `--repo`, `--output`, `--ext-diff`, `--textconv`, `--config`, holding `://`
    or shaped `user@host:`; `push|pull|fetch` with no remote or a remote named in the workspace's `.git/config`;
    `push` without `-f`/`-d` (in any short cluster), `--force`, `--force-with-lease`, `--force-if-includes`,
    `--mirror`, `--all`, `--branches`, `--tags`, `--delete`, `--prune`, or a refspec starting `+` or `:`;
    `pull|fetch` without `-f -p -P -u` (in any short cluster), `--force`, `--prune`, `--prune-tags`, `--refmap`,
    `--update-head-ok`, or a refspec starting `+` or holding `:`. Plain `git push`, `git push origin`, `git pull`
    stay quiet.
  - **Paths.** Every operand of every allowed program (not the text of `echo`, `printf`, `test`, `[`, `sleep`,
    `date`, `true`, `false`, `which`, `tr`, `cut`, nor a grep or sed script) and every redirection target is
    resolved from the stage's cwd (symlinks followed) and refused when it is in `~/.mother` (reading
    `~/.mother/workspace` excepted), in `~/.claude` or is `~/.claude.json`, or is protected in the workspace:
    `.mother/`, any `.git` or `.claude` path component, any `.gitconfig`, `.gitattributes` or `.gitmodules` path
    component, any `.config/git` pair of components (git reads these through `HOME` or `XDG_CONFIG_HOME`), any
    `CLAUDE.md`, `.mcp.json` or `grants.json`, `AGENTS.md`
    and `memory/MEMORY.md`, `config.json` and `capabilities.json` at the top (any case), and the workspace root
    itself as a write target. A write to `config.json` or `capabilities.json` is `kind: config`.
    **Hidden paths:** for every word of every allowed program, the word, the text after its first `=`, and the
    text after a leading short-option cluster (`-f…`, `-m…`) are each a path when they start with `/` or `~` or
    hold `..`; such a path must pass the checks above and resolve inside the workspace, the plugin or the system
    temp folders (`tempfile.gettempdir()`, `/tmp`, `/var/tmp`), or be `/dev/null`.
- **ask, every mode** (`kind: "config"`): a Write/Edit/MultiEdit/NotebookEdit of the workspace's
  `capabilities.json` (written only by `mother health`), like `config.json`.
- **ask, every mode** (`kind: "persona"`): a Write/Edit/MultiEdit/NotebookEdit of anything with a `.git`,
  `.gitconfig`, `.gitattributes` or `.gitmodules` path component, or under a `.config/git` folder, in the
  workspace, at any depth (git runs hooks and config such as `core.fsmonitor` from them).
- **Hub-fed sessions** (`kind: "grants"`, `op: "hub_fed"`; checked first for a Bash, Monitor or `run_in_terminal`
  command, in or out of the workspace): a session whose id is in `.mother/hub/sessions.json` (any state), in
  `hub-session.json`, or in `.mother/hub/fed-sessions.json` (the hook adds the id, before deciding, whenever a
  command names `mother hub`) reads text from people who can message the manager. In it, `mother grant` (every
  verb), `mother channels` with any verb but `plan status mute unmute stop pause prune help`, and any
  `channels approvals|triage|run|answer|done|send` **ask**; PAUSED and `MOTHER_DRY_RUN` deny; bypass never passes
  it. The channels loop runs in its own session (§5b). The app's tools that carry the conversation into another
  session, `mcp__ccd_session__hand_off_to_session`, `mcp__ccd_session__move_to_cloud` and
  `mcp__ccd_session__start_session` with `context: "fork"`, are `kind: "manager_only"` in a hub-fed session (ask in
  every mode, PAUSED included; the hub session itself still denies them).
- **The channels loop's session** (`kind: "grants"`, `op: "channels_session"`; checked right after hub-fed, in or
  out of the workspace): `mother channels approvals|triage|run|done` (and any `channels answer|send`) run
  without a question only in a session listed in `.mother/channels-session.json`. The hook lists a session only
  when all of these hold: the Bash command is exactly one plain `mother channels plan …` (`mother`, the plugin's
  `bin/mother` by path or `${CLAUDE_PLUGIN_ROOT}`, or `python3` of it; plain or single-quoted words, nothing
  chained, piped, redirected, expanded or commented), the guard gives it no decision, its cwd is in the workspace,
  it runs as the session itself (no `agent_type`), the session is not hub-fed, and the first message a human typed
  in the session (`transcript_path`; a slash command, only in its own format at the very start of the message,
  read as `/name args`) is `/mother:channels …` or `/loop [interval] /mother:channels …`. A command that only names `plan` (a grep, a comment, a chain) records
  nothing. The file keeps at most 3 ids, newest first, each with its time in `recorded`; a new id evicts the
  oldest one recorded more than 2 hours ago, and is not added when all three are more recent. Anywhere else these
  verbs **ask**; PAUSED and `MOTHER_DRY_RUN` deny; bypass never passes it.
- **ask, every session and mode** (`kind: "manager_only"`, PAUSED included; never passed by bypass;
  `MOTHER_DRY_RUN` denies): `mother channels resume` and `mother channels start`.
- **ask, every session and mode** (`kind: "home_settings"`): a Write/Edit/MultiEdit/NotebookEdit of
  `~/.claude/settings.json`, `~/.claude/settings.local.json`, `~/.claude.json` or anything under
  `~/.claude/plugins/` (they decide which plugins and hooks run). Never passed by bypass. The same for a session
  transcript: the payload's `transcript_path` or any `*.jsonl` under `~/.claude/projects/` (the channels-session
  rule reads the first prompt there; the auto-memory `*.md` notes there are not transcripts), by a file tool or by a shell command that writes and names `.claude/projects`,
  the transcript's path or file name, or runs from inside `~/.claude/projects`.
- **deny, every session** (`kind: "grant-key"`, checked before any role): `Read`, `Edit`, `Write`, `MultiEdit`,
  `NotebookEdit` of the grant key (`<state>/grant.key`, state = `$MOTHER_STATE_DIR` or `~/.mother`), and `Grep`,
  `Glob`, `LS` whose root is the key or a folder holding it (paths compared casefolded).
- **MCP web tools** (`outbound_unknown`, workspace sessions, any server but `mcp__ccd_…`, when the domain rules
  give no decision): a tool name with a `url`, `http`, `https`, `fetch`, `download`, `web`, `scrape`, `page`,
  `request`, `browse`, `navigate` or `crawl` part; an input field `url`, `uri`, `target`, `href`, `endpoint`,
  `link`, their plurals, `host(s)`, `server(s)`, `hook(s)`, `webhook(s)`, or `…_url(s)`/`…_uri(s)`/`…_link(s)`
  (nested too; a list value counts when any element does) whose value looks like a host
  (`[scheme://]name.tld[:port][/…]`) or holds `://`; a tool named `form_input`, and `computer` with `action` `type` or `key`.
- **deny**: a chat write (post/reply/schedule/edit, or a canvas/topic/purpose change; not a reaction) through a
  server listed in any connector's `stamps` to anyone other than `manager.chat_dm`; an unknown target, or two
  different targets in one call, is denied too (`rules/outbound.md`).
- **deny**: the desktop app's own session tools in a reader/critic sub-agent or a channel session (§5c).
- **allow**: the desktop app's own session tools, by exact name, when they stay inside what the hub manages (§5e):
  `list_sessions`, `dismiss_task` and `ListAgents` always; `send_message` (`ccd_session_mgmt`) only when its
  `session_id` is the `session_id` of a `live` entry in `<workspace>/.mother/hub/sessions.json`; `SendMessage` when
  its `to` is one of those live ids, or (outside the workspace) is not a session id (a session id is `local_…` or a
  UUID); in a session whose cwd is the workspace or under it, `SendMessage` by name only to `main` or the `name`
  of a `live` entry (with or without a ` [ref]` suffix);
  `start_session` (`ccd_session`) only with `initiation: "own_initiative"` or `proposal_id` alone (with
  `wake_when_done` at most). Look-alikes on other servers are classified normally.
- **ask**: any other use of those session tools (a message into a session the hub does not route to, a `SendMessage`
  by any other name from the workspace, a missing or
  unreadable `sessions.json`, a `start_session` that starts a session now). This ask is not turned into a deny by
  `PAUSED` or the never-list; `bypassPermissions` passes it like any other ask.
- **deny**: nearly everything in the hub session (§5e). It is a session whose id is in `session_ids` of
  `<workspace>/.mother/hub/hub-session.json` (newest first, at most 5; a newer `tail` never unfences an older id), which the hook writes (atomically, under `hub.lock`; a failure is
  swallowed and changes no decision) when a Bash call from the workspace (cwd inside it, not from a sub-agent) is
  one plain `mother hub tail …` command (`mother`, or the absolute path of this plugin's `bin/mother`). Sub-agent
  and channel checks run first. In the hub session the hook allows only: Bash (and a Monitor `command`) that matches an exact template: one line
  of tokens separated by single spaces, each token plain characters (`A-Za-z0-9_.:/@=,+-`, and `#` only inside a
  token, as in `owner/repo#45`) or a single-quoted string with no `'`, newline or tab; no other character (no
  `"`, `$`, backtick, `;&|<>(){}\`, `*?[]~!`), and no control character other than newline and tab nor Unicode space
  or format character anywhere. The commands: `mother hub <verb>` (not `install`, `uninstall` or `watch`),
  `mother home check`, `mother now show`, `mother path`, `pwd`, `date` (a `+format` argument only); the one stdin
  form, the first line exactly `mother hub decide <<'HUBEOF'`, a body, and a last line exactly `HUBEOF` (no body
  line equals it after stripping spaces and tabs); and `gh pr view|checks <N>`, `gh issue view <N>`,
  `gh run view <N>` (`mr` for glab), then at most once each `--repo owner/repo`, `--json fields` and (view of a
  pr, issue or mr) `--comments`, as separate long-form tokens. `gh api` and every other `gh`/`glab` form are
  denied. A Monitor also needs no url or websocket field. `Read`/`Grep`/`Glob`/`LS` whose real path is inside the workspace or the plugin; MCP calls the guard does not
  classify as writes whose operation starts with a read verb, whose name has no fetch, download, upload, export,
  url, http, browse or navigate part and whose input has no `url`, `uri`, `href`, `path`, `file`, `dest` or
  `destination` field; `set_session_model` and `set_session_effort` on the session itself; the in-app session
  tools, which keep the rules above (so a send to a non-hub session still asks, a line starting `mother approve`
  is denied, and in bypass an ask stays an ask in the workspace); `Skill`, `CronCreate` (only a `prompt` exactly equal to
  one of `HUB_CRON_PROMPTS` in the guard: `/mother:hub`, or the skill's cron prompt `mother hub tail --once — run
  it and handle the lines; if it prints nothing, end the turn at once`), `CronDelete`, `CronList`, `TaskStop`, `ToolSearch`, `TodoWrite`. Everything else (`Write`, `Edit`, `WebFetch`,
  `WebSearch`, `Agent`, other commands, other MCP calls) is denied with "the hub session can only do hub work; ask
  the manager to open a session for this". Other sessions are unchanged.
- **deny**: anything if `<workspace>/.mother/PAUSED` exists, and the `never` list (credential entry, deletions).
- Unattended routines may write only through a grant in `.mother/grants.json` that cites the manager's own
  message (channel, user = manager, ts) — scope-limited, count-limited, expiring, logged in `.mother/ledger.json`.
- **`mother grant`** (`lib/grants.py`) is the only writer of grants (besides `mother channels`' own answer and
  reply grants): `add <scope> <target> --by <user-id> --cites <link|ts> [--channel ID] [--uses N] [--hours H
  (default 24)] [--run ID] [--thread TS]` builds `{scope, target, uses: min(N, ceiling), expires: min(now + H,
  ts + max age), run (default manual), source{channel, user, ts}}` and writes it only if `valid_grant` would honour
  it now (exit 1 and nothing written otherwise, for `answer`, while PAUSED, or when `grants.json` is unreadable,
  which it never overwrites). `--cites` is a message link holding `/<CHANNEL>/p<16 digits>` or a ts (channel from
  `--channel`, else the first `manager.chat_dm`). `remove --run ID` drops that run's grants; `list [--json]`.
  Atomic, under `.mother/grants.lock`. Exit 2 on usage errors. Every write (theirs and `mother channels'`) is
  **signed**: `sig` = HMAC-SHA256, hex, over the `grants` list serialised as JSON with sorted keys, `,`/`:`
  separators and UTF-8 kept, keyed with the bytes of `<state>/grant.key` (32 random bytes hex-encoded, made 0600
  on first use; state = `$MOTHER_STATE_DIR` or `~/.mother`, outside the workspace). The guard honours no grant
  in a file whose `sig` is missing or doesn't verify: unsigned grants written before this change are ignored.
  Every writer (`mother grant add|remove`, and `mother channels`' answer grant, reply grant and revoke) verifies
  the file it is about to change first: unsigned or badly signed grants are dropped (it starts from an empty
  set and never re-signs them), with one `decision: "reset"` line in `guard.log`; an unparseable file is
  refused, never overwritten.
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
- **WhatsApp (`mother whatsapp send`, §5d)** works like a chat message. Action: `{"kind": "whatsapp", "op":
  "send"|"state"|"compound"|"unknown"|"computed"|"terminal", "category": "own_dm" (a send to the manager's own
  chat) | "any_message_to_others" (any other send, and `compound`/`unknown`) | "whatsapp_state" (for `state`) |
  "computed_command" (for `computed`) | "whatsapp_setup" (for `terminal`), "to", "text"}`. `state`, `compound`,
  `unknown`, `computed` and `terminal` always deny, in every mode, and a guard error on a WhatsApp call denies.
  `guard.log` names the recipient as given (a number or JID is logged as `a number`), never the text, never
  the command.
  - **A send.** A Bash command that is exactly one `mother whatsapp send …` command — the plugin's own CLI
    (a path, or a bare `mother` found on `PATH`, optionally after `python3`, whose realpath is `<root>/bin/mother`
    where `<root>/.claude-plugin/plugin.json` names this plugin and `<root>/lib/whatsapp.py` exists: the versioned
    cache copy, the marketplace clone `mother link` points at, or a `--plugin-dir` checkout; any other executable
    named `mother` is `unknown`, deny), then literal `whatsapp send`, then `--to WHO` and `--text TEXT` (`--opt=value` too), and
    nothing else on the line except a `2>&1`-style redirection (no `;`, `&&`, `|`, `&`, other redirection,
    heredoc, env prefix or comment). Every word must be literal text: quotes, `$'…'` and backslashes are fine,
    a `$VAR`, `$( )` or backtick in any word but the CLI path is not (deny: "write the text literally, in
    single quotes"). A recipient or text holding a character the prompt can't show is denied, so the prompt
    shows exactly what is sent: any character whose Unicode category (Python's `unicodedata`) is `Cc` (except
    `\n` and `\t`), `Cf` (format: bidi controls, zero-width spaces, word joiners, tag characters, soft hyphen;
    except U+200D, the zero-width joiner emoji sequences use), `Co` (private use), `Cs` (surrogates) or `Cn`
    (unassigned in the guard's Python, except in the emoji blocks U+1F000–U+1FAFF, where it is an emoji newer than
    the guard's Unicode tables; the England, Scotland and Wales flags — U+1F3F4, the tag letters `gbeng`/`gbsct`/`gbwls`, U+E007F — pass as a
    whole, and no other tag sequence; variation selectors other than U+FE0E/U+FE0F (U+FE00–U+FE0D,
    U+E0100–U+E01EF) and the Mongolian ones U+180B–U+180F are denied, since they render as nothing), plus the Hangul fillers U+115F, U+1160, U+3164 and U+FFA0, U+00AD,
    U+034F (combining grapheme joiner) and the line and paragraph separators U+2028, U+2029. Variation
    selectors U+FE0E/U+FE0F are marks, not format characters, and pass. So is a form the shell would rewrite: an unquoted `~`, glob (`*`, `?`, `[`)
    or brace list, and a `$'…'` escape for a raw byte (`\x80`–`\xff`, octal 200–377) or `\c`. A text shaped like a credential is on the never-list. Then: PAUSED denies; an
    invalid `config.json` denies; `whatsapp.enabled` or `whatsapp.send` false denies ("sending is off"); the
    category in `policy.never` denies. `--to me`, `--to <self_jid>` or the manager's own number (only while
    `self_jid` is set) is `own_dm`: **allow** while `own_dm` is in the auto list, else ask. Any other recipient
    is **ask**, with the reason `WhatsApp to <WHO as JSON>: <text as JSON>` (a number or JID gets ` (number)`
    after it; the guard looks nothing up) and, unless the text already holds `<agent.name> (AI)`, a note that
    MOTHER adds its signature line.
  - **The send ticket.** On every ask or allow of a send, before answering, the guard writes
    `<state>/whatsapp/tickets/<hex>`, `hex` = sha256 of the recipient as typed, a NUL, and the text, both as
    the CLI receives them (UTF-8), hex-encoded; content `{"at": <epoch seconds>}`. The folder is created 0700
    (a symlink there denies); the file is written to a temp name with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600,
    then renamed over any old ticket of that name. If it can't be written the send is denied with a plain
    reason. The bridge needs a fresh ticket for every send (§5d), so a send that never got a guard decision
    is refused. A ticket left by a prompt the manager declined expires after 600 s (10 minutes, counted from the prompt, so a slow yes still sends). In `bypassPermissions`
    the ask passes like any other (logged `pass`), the ticket still written: the same caveat as chat, stated
    in `rules/outbound.md`.
  - **Everything else is read as shell.** Simple commands split on `;`, `&&`, `||`, `|`, `&`, newlines and
    parentheses; quotes, `$'…'`, `$"…"` and backslashes resolved; comments and quoted heredoc bodies are data;
    `$( )`, backticks, `<( )`, the strings given to `eval`, `sh|bash|zsh -c`, `env -S` and `find -exec`, and a
    heredoc fed to a shell are read as commands too. Variables resolve from `HOME`, `MOTHER_STATE_DIR`,
    `MOTHER_WORKSPACE`, `CLAUDE_PLUGIN_ROOT` (the plugin root), `NAME=…` set earlier in the line, and
    `$(cat ~/.mother/workspace)` / `$(mother path)` (the workspace); everything else is unresolved. Then:
    - a command name (after `env`, `command`, `exec`, `xargs`, `sudo`, `nohup`, `time`, `timeout`, `nice` and
      the like) is first resolved as above and then from the guard's own environment (`$EDITOR`). One that
      still holds an unresolved variable, a substitution or an unquoted glob or brace is **denied** as
      `computed` only when (a) with each unresolved part as `*` its last path part could be `mother`
      (`mo$(true)ther`, `bin/mo?her`, `${D}/mother`; a name that is only one expansion, `$X`, doesn't count
      here), or (b) some word in the line, with expansions as `*` and not a lone `*`, could be `whatsapp`
      (`$X whatsapp send …`). Otherwise it gets no decision (`$EDITOR notes.md`). An `eval`, `sh -c`, `env -S`
      or inline-code string the guard can't compute is treated the same way, and is also denied when its
      literal text names `mother` or `whatsapp` (`eval "$(ssh-agent -s)"` passes). `$CH plan` resolves when
      `CH=…` is set earlier in the line;
    - a `mother` command (resolved) whose first argument is `whatsapp` and whose subcommand is `send` (not the
      exact shape above) is `compound` (deny); `setup`, `link` or `unlink` is `terminal` (below); any other
      subcommand must be `read`, `chats`, `status`, `install`, `stop` or `help`, with its raw simple command
      plain, `$VAR` or quoted arguments and redirections (`>`, `>>`, `<`, `<<`, `2>&1`, `&>`; no `$( )` or
      backticks), run directly (not under a wrapper), else `unknown` (deny). Leading `NAME=value` prefixes
      with such a value read like `export NAME=value;`, except `PATH`, `ENV`, `BASH_ENV`, `PYTHON*`, `DYLD_*`,
      `LD_*`, `PERL5*` and `NODE_OPTIONS`, which change what code runs (`unknown`). A computed first argument
      or subcommand, or `mother` under `xargs`, is `unknown`;
    - unquoted words spelling `mother whatsapp send` anywhere in the line (`mother whatsapp⏎send …`), or that
      text anywhere in a line where a shell or interpreter reads code from a pipe or heredoc, are `compound`;
      inline code (`python3 -c`, `node -e`, `awk '…'`, …) naming the state paths below, or `mother …
      whatsapp`, or `whatsapp` with `AF_UNIX`, `UNIX-CONNECT` or `.sock`, or `baileys`, or `whatsapp` imported
      as a module (`import whatsapp`, `from whatsapp import`, `__import__`/`import_module` on a line naming
      it), is denied; so is a
      script under a `bridge/` folder on a line that mentions `whatsapp`, and `nc`, `ncat`, `socat` or
      `curl --unix-socket` on a line that mentions `whatsapp` or `bridge` or has an unresolved argument.
    A repo path, `grep` pattern or commit message that merely contains the words (`git -C …/mother log --grep
    whatsapp`, `git commit -m "whatsapp: …"`) is not a mention: the rules key on command names and resolved
    paths.
  - `mother whatsapp setup|link|unlink` (a resolved `mother` command whose subcommand is one of these, in any
    position on the line) is `terminal`: denied, in every mode, with "run it in your own terminal". They link
    the device and write `self_jid`. Commands that give another command a terminal are read as wrappers:
    `script [opts] [file [command…]]` (and `script -c CMD`) is unwrapped and its command judged as usual (a
    wrapped `mother whatsapp` command is denied, like `nohup`); `expect`, `tmux`, `screen`, `dtach`, `abduco`,
    `ptyexec`, `pty`, `rlwrap`, `empty` on a line that names `mother` and `whatsapp` deny.
  - **The `whatsapp` block of `config.json` is the manager's.** A Write, Edit, MultiEdit or NotebookEdit of
    `<ws>/config.json` (realpath, casefolded) whose result changes the parsed `whatsapp` value (absent = absent;
    any change, narrowing too) is `terminal`: denied in every mode. Edit/MultiEdit are applied in memory to the
    current text in order (`replace_all` honoured); a result that doesn't parse counts as a change when the
    file has a block today; an Edit whose old text isn't in the file, a Write without `content` and an edit
    that keeps the block keep the existing self-edit ask. A shell write that names `config.json` (the self-edit
    rule below) is `terminal` (deny) while the file holds a `whatsapp` block, and the old ask otherwise.
  - **The bridge and its state are off-limits to the agent.** The protected folders are `<state>/whatsapp/`
    (`<state>` = `$MOTHER_STATE_DIR` or `~/.mother`) and `~/.mother/whatsapp/`: the socket, the session keys,
    `sent.jsonl` and the installed bridge in `app/`. Every path comparison is case-insensitive (casefolded
    after `realpath`), here and for the guard's own files below. A Bash command is **denied** (`state`) —
    reads included — when its cwd lies inside a protected folder, or when an argument, a redirection target,
    an assignment value or a piece of one (split at `=`, `:`, `,`, quotes and parentheses, so `--file=PATH`,
    `UNIX-CONNECT:PATH` and `open('PATH')` count) names one:
    - a word with no unresolved part, after `~` and the variables above, resolved against the cwd and every
      `cd`, `pushd`, `-C` and `--directory` in the line, that lies inside a protected folder; or a word
      without spaces that contains `bridge.sock`, `.mother/whatsapp`, `whatsapp/session`, `whatsapp/app` or
      `whatsapp/sent.jsonl`;
    - a word with wildcards: each unresolved expansion and each brace group (`{a,b}`) becomes `.*` (it may
      hold a `/`), each `*` `?` `[…]` a glob within one folder (quoted ones too). It is denied when it, or a
      prefix of it ending at a `/`, matches a protected path case-insensitively (`~/.mo${E}ther/wh*`,
      `~/.mother/*`). A word, or prefix, that is only wildcards and `/` names nothing in particular and is
      skipped, here and in the destructive check below (`cd "$W"`, `rm -rf "$D"/*`). After a `cd` to an
      unresolved folder, later relative words are matched as `.*/word`. Matching takes time linear in the word
      and the path, however many wildcards it holds;
    - a word with an unresolved part next to the text `.mother`, `whatsapp` or `bridge.sock` (no spaces in
      it): `cat $UNKNOWN/.mother/x`.
    Destructive or copying commands (`rm`, `rmdir`, `unlink`, `mv`, `cp`, `rsync`, `tar`, `ditto`, `zip`,
    `unzip`, `ln`, `chmod`, `chown`, `chflags`, `shred`, `trash`, and `find` with `-delete`/`-exec`/`-ok`)
    are also **denied** when an argument names a folder that holds the state: `~/.mother`, `<state>`, `<ws>`,
    `<ws>/.mother`; and, for a recursive one (`rm`, `rmdir`, `mv`, `tar`, `zip`, `unzip`, `rsync`, `ditto`,
    `shred`, `trash`, `find`, and `cp`/`chmod`/`chown`/`chflags` with `-r`/`-R`/`-a`), any folder above them
    except `/`. `git clean` (not `-n`/`--dry-run`) is recursive the same way. Reading and listing those
    folders, and work inside the workspace, pass (`ls ~/.mother`, `cd "$W" && git status`,
    `cp a.md "$W"/memory/`).
    Write/Edit/MultiEdit/NotebookEdit anywhere under a protected folder is **denied** (not asked). Only
    `mother whatsapp`, the bridge and the guard (tickets) write there. Residuals, stated plainly: see §5d "What the guard stops".
  - Channel sessions (`MOTHER_CHANNEL_ROLE`) run no Bash at all, so no `mother whatsapp` command. The
    `mother:reader` sub-agent may run only a single `mother whatsapp read …` command (plain or quoted
    arguments, no shell operators); every other `mother whatsapp` command is denied to sub-agents.
    `MOTHER_DRY_RUN=1` denies `send`.
- **Paused** denies drafts too. Policy overrides only tighten (the auto list can shrink, the never list can grow).
- The hook matcher includes `Write|Edit|MultiEdit|NotebookEdit`, so edits to the guard's own files
  (config, ledger, log, PAUSED, hooks) ask.
- A shell command asks as a self-edit only when it can write (a redirect other than `/dev/null` or `2>&1`,
  `rm`/`mv`/`cp`/`sed -i`/…, or a script that opens a file for writing) **and** it reaches the running copy:
  it names `guard.py`/`guard_fast.py`/`hooks.json` together with the installed plugin's path (`${CLAUDE_PLUGIN_ROOT}`,
  `.claude/plugins/`), or `config.json`/`PAUSED`/`ledger.json`/`guard.log` together with the workspace's
  path (`$MOTHER_WORKSPACE`, `$(cat ~/.mother/workspace)`, the path itself), or it runs from inside either
  without a `cd`; or it writes `~/.mother/workspace`. The same names in a checkout of this repo, another
  project or a test workspace (a command that sets its own `MOTHER_WORKSPACE`/`MOTHER_STATE_DIR`) pass.
- `gh`/`glab` text the shell won't run passes: quoted strings and heredoc bodies that feed a plain command
  (commit messages, PR bodies, prompts). Quotes count when the command hands them to an interpreter
  (`bash -c`, `python3 -c`, `ssh`, `xargs` …); heredoc bodies count when they feed one or the command runs a
  script it wrote (`chmod`, `./x.sh`). `gh api -X GET …` and `-F query=@file` whose file holds a query and no
  mutation are reads. Connector calls `executeRead` and `*_monitor`/`*_watch` are not outbound.

- **Accepted risks.** These are known and left open on purpose:
  - A session outside the workspace keeps Claude Code's normal permissions. The guard's shell allow-list applies
    only in the workspace, so there `python3 lib/channels.py …` or a `python3 -c` that calls `grants.main` runs
    with no question from the guard; Claude Code's own permission prompt is the only check there. Likewise a
    session there can still rewrite its own transcript through the shell when the command hides the path (a
    variable, a computed name, a script) from the guard's text check.
  - From a hub-fed session (other than the hub session itself, which denies them), `Agent` with remote
    isolation, `Skill schedule`, `CronCreate`, `mcp__ccd_session_mgmt__clear_session` /
    `unarchive_session` and `set_session_permission_mode` are not fenced beyond the hub role: they keep their
    normal rules.
  - A channels loop session that hasn't run `plan` for more than 2 hours can be evicted from
    `channels-session.json` by three other sessions the manager started with `/mother:channels`; it then asks
    before its reply verbs until it runs `plan` again.
  - Outside the sessions the fast path hands on (§5, hook registration), a tool the guard has no rule for gets
    Claude Code's normal permissions.

## 5b. Channel reply loop (`lib/channels.py`, `mother channels`, skill `mother:channels`)

- The skill does chat I/O only; the CLI decides. `plan` → what to poll; `triage` (stdin: one channel's new
  messages + followed threads) → actions; `run --action` → one headless `claude -p` session per answer →
  `answer` (grant written) | `draft` (to the manager's DM) | `skip`; `approvals` (stdin: replies in each draft's DM
  thread) → `send` actions; `done` records each post.
- Sessions run in the workspace with `--permission-mode default` and `MOTHER_CHANNEL_ROLE=manager|team|anyone`.
  **The guard enforces the limits from that variable**, whatever the user's allow rules say: a channel session
  makes no outbound call at all (not even green ones — the loop posts), no `Bash`/`Monitor`/web/subagents, and reads only
  the workspace and the plugin. A teammate's session also can't write, can't read `people/ journal/ memory/ todos/
  .mother/ bridge/`, grep the workspace root or use `..`/absolute search patterns, and may call only tools under the
  prefixes in `MOTHER_CHANNEL_SERVERS` — the tracker and code-host prefixes *with their product segment*
  (`mcp__gw__jira`), so a gateway that also carries chat or mail stays shut; a prefix shared outright with chat,
  mail or calendar is refused. The manager's session may write only notes: `*.md` under `todos/ memory/ projects/
  teams/ people/` (an allow-list: `.mcp.json`, `skills/`, `scripts/` and pointer files are code a later session
  would run). Every tool this needs is registered for the guard in every session (§5, hook registration).
  The `--allowedTools`/`--disallowedTools` flags repeat the same limits as a first fence. One session per thread
  and role (`--session-id`, then `--resume`); a teammate never joins a session the manager drove.
  `MOTHER_NO_SYNC=1` makes `mother sync` a no-op inside them.
- **The loop runs in its own session, never a hub-fed one** (§5). `approvals` and `triage` read replies on stdin
  as the manager's and `run`/`answer` write grants, so in a session that reads hub lines (a card the hub routes to,
  the hub itself, or any session that ran `mother hub …`) the guard asks before each of them. The loop's first
  step is `mother channels plan`: the guard records the session that runs it (as one plain command, in the
  workspace, not a sub-agent, not hub-fed, in a session the manager started with `/mother:channels` or
  `/loop … /mother:channels`; §5) in `.mother/channels-session.json` (`{session_ids, at, recorded: {id: time}}`,
  newest first, at most 3, none recorded in the last 2 hours evicted), and
  `approvals`, `triage`, `run` and `done` ask in any session not listed there, such as a brief that read mail. `mother channels
  start` and `resume` always ask: only the manager turns the loop back on.
- State: `.mother/channels.json` (cursors, threads, pending actions, drafts, streaks, demotions, stop/pause);
  audit: `.mother/channels.log` (jsonl, one line per post, draft, skip, mute, stop, error). Both machine-local.
- A first look at a channel only sets its cursor to now. Quiet hours and rate limits hold messages (the cursor
  stops); stop/pause drop them. Earned autonomy: 10 unedited approvals on one channel+topic → one proposal to the
  manager; a 👎 on a reply mutes the thread and demotes its topic.

## 5c. Sub-agents (`agents/`, `mother items`, `mother eval`)

- **One writer.** MOTHER (the routine or interactive session) judges, writes and posts. Sub-agents only read
  and hand back. Agents never message each other, and a sub-agent's hand-back is data: it approves nothing
  (§5, `rules/outbound.md`).
- `mother:reader` reads one source (calendar, mail, chat, tracker, code) and returns one JSON object,
  `{source, coverage{status ok|partial|couldnt_check, reason, opened, unopened}, items[{kind, title, who, when,
  link, state, note}]}`. `mother:critic` reads one outbound draft and its cited items and returns `PASS` or
  `FIX` + a numbered list. Neither may spawn agents (frontmatter `disallowedTools` / `tools`).
- **The guard enforces read-only** from the hook payload's `agent_type` (`mother:reader` or `reader`, likewise
  critic), because plugin agents can't carry their own hooks. Every classified write is denied, including
  green ones. Write/Edit and web are denied, and so is any tool outside the agent's list. A reader's Bash is
  limited to one plain command (words of plain characters, single-quoted strings, or double-quoted strings with
  no `$`, backtick, backslash or `!`; no chaining, redirect, glob, newline or env prefix): `date` (not `-s`/`--set`),
  or `gh`/`glab` as the hub allows (§5e) or one of `gh pr list|view|checks|status|diff`, `gh issue
  list|view|status`, `gh search prs|issues`, `gh run list|view`, `gh repo view`, `gh release list|view`, `gh api
  <relative endpoint>` (not `graphql`), `glab mr list|view`, `glab issue list|view`, `glab ci list|view|status`,
  `glab repo view`, `glab release list|view`, `glab api …`; never with `--hostname`, `-H`/`--header`, `--web`,
  `--input`, `-f`/`-F`/`--field`/`--raw-field`, `-X`/`--method`, an argument holding `://`, or a `--repo`/`-R`
  that is not `owner/repo`. A reader may also use `ToolSearch` (it loads a deferred MCP tool's schema). A critic
  gets no MCP tools. Other sub-agents
  (not MOTHER's) keep the normal rules.
- `mother items check` turns a reader's reply into the only input a skill may use. Items without a link
  (URL, ticket key, `owner/repo#N`, workspace path) are dropped. A `state: noise` tally cited by the query that reproduces it is kept as a count, never a finding. A reader whose items were all uncited
  counts as `couldnt_check`, and so does unreadable output (exit 2). Never treat it as a quiet source.
- `MOTHER_DRY_RUN=1` makes the guard deny every outbound call. `mother eval brief` sets it for both runs of
  an A/B pair. A new gather mode becomes the default only when `mother eval report` says `ship`: at least
  10 judged pairs; per-source coverage and uncited lines no worse than baseline; no new false-quiet sources; the manager
  picked it or `same` in ≥ 60% of pairs; and cost ≤ 3× baseline.

## 5d. WhatsApp (`lib/whatsapp.py`, `mother whatsapp`, `bridge/whatsapp/`)

Read and send from the manager's own number, like chat: reading is local and free, every send to someone else
asks in Claude's normal permission prompt (§5). Two independent parts: a read-only reader of the local macOS
WhatsApp database, and a send-only bridge (a linked device). Every `mother whatsapp` command except `status` and
`help` (or no argument) refuses to run anywhere but `workspace.home_machine` (exit 2; exit 3 if no home is set).
The guard finds the workspace as for every other rule (`$MOTHER_WORKSPACE`, else `~/.mother/workspace`); the
bridge and the CLI use `$MOTHER_WORKSPACE`, else `<state>/workspace`. They differ only when `MOTHER_STATE_DIR`
is set without `MOTHER_WORKSPACE`, as in throwaway test workspaces.

- **Which chats.** Any chat in the local database may be read. A send may go to any chat that already has a
  message with the manager in the database (no cold contact: never a stranger), and to the manager's own
  chat. No allowlist.
- **Naming a chat (WHO).** `me` (= `whatsapp.self_jid`), a JID, a phone number (`+`, spaces, `-`, `(`, `)`
  dropped; 6-15 digits → `<digits>@s.whatsapp.net`), or a chat's exact name. A JID or number must be a chat in
  `ZWACHATSESSION`. A name must equal one `ZPARTNERNAME` exactly, case-insensitively, both sides trimmed: no
  partial matches. None is exit 2 "no chat is named …", listing up to 5 chats whose names contain it; more
  than one is exit 2 listing up to 5 of them (name, DM/group, last date, JID). Broadcast and status chats are
  never matched.
- **`mother whatsapp chats [--query TEXT] [--limit N]`** lists recent chats (default 30) from
  `ZWACHATSESSION` only, never a message: JSON `[{"name", "type": "dm"|"group", "last", "jid",
  "hidden_id"?}]`, newest first. WhatsApp stores a pinned chat's `ZLASTMESSAGEDATE` far in the future (it
  shows as a year like 7026): a date more than a day ahead of now is shown as `"last": "pinned"` and sorted
  first. `--query` keeps chats whose name or JID contains TEXT. A chat keyed by a private `@lid` id carries
  `"hidden_id": true`: it can be read by name, not sent to.
- **`mother whatsapp read [--chat WHO] [--since ISO] [--limit N]`** opens the database
  (`$MOTHER_WHATSAPP_DB`, else `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`)
  with `mode=ro` and prints `{"status": "ok", "items": [{"chat", "chat_name", "from_me", "sender", "text",
  "at", "link"}]}`, oldest first: with `--chat`, that chat's last N messages (default 100) since `--since` if
  given; without it, the last N messages across all chats since `--since` (default 24 hours ago). Text
  types are `ZMESSAGETYPE` 0 and 7 (7 = link preview); anything else is `[media]` with no content. In a
  group the sender is the member's contact name, else first name, else "a member", never a number. A locked
  database is retried once, then `{"status": "couldnt_check", "reason"}` with `locked`; a missing table or
  column is `schema_changed`; `denied` (no Full Disk Access) and `missing` likewise. Never a partial list.
  Citation: `whatsapp://<chat-jid>/<stanza-id>`. Nothing is written anywhere. `read` and `chats` exit 2
  "whatsapp reading is off" when `whatsapp.enabled` or `whatsapp.read` is false.
- **`mother whatsapp send --to WHO --text TEXT`** goes through the guard first (§5: ask, or allow for `me`;
  either way the guard writes the send ticket). Then it exits 2 "whatsapp sending is off" when `enabled` or `send` is false; exits 2
  with a plain sentence for an empty text, a text over 4096 characters, an ambiguous or unknown WHO, a hidden
  `@lid` chat ("WhatsApp hides this chat's number, so MOTHER can't address it"), a chat with no message in
  the database ("no cold contact": a chat counts only once it holds a text message or one the manager sent;
  system rows such as the encryption notice don't count), or a JID that is neither `<digits>@s.whatsapp.net`
  nor `<digits>@g.us`. The text is sent as received (nothing stripped). To anyone but `self_jid` the
  disclosure signature (`rules/outbound.md`) on its own line after a blank one: `agent.signature`, else `*<name> (AI)* ·
  on behalf of <manager_short>` led by `agent.emoji`, converted from Slack formatting (WhatsApp shows it raw):
  `<url|label>` becomes `label`, `<url>` the url, a known `:shortcode:` its emoji (`robot_face` 🤖, `sparkles`
  ✨, …), an unknown one is left out; it goes along as the `signature` the bridge appends, on its own line after a blank
  one; none when the text already holds `<name> (AI)`. Then the bridge `send` op with `to` as typed, and
  JSON `{"ok": true, "to": <jid>, "name", "id": <message id or null>}`; a bridge refusal prints its reply
  and exits 2 with the code, saying nothing was sent. When the CLI gets no answer (`bridge_timeout`, or
  `bridge_bad_reply` from a dropped connection) it can't tell: it prints the reply and exits 2 with "the bridge
  may still send it; check `mother whatsapp status` before retrying", never "nothing was sent". One recipient
  per call; nothing is queued or retried.
- **Bridge** (`bridge/whatsapp/`, Node 20+, Baileys pinned exactly in `package.json` and `package-lock.json`;
  optional, nothing else depends on it). `node bridge.mjs` serves on `<state>/whatsapp/bridge.sock` (0600, in
  a 0700 folder; a stale socket is replaced, a live one refused). One JSON request per connection, one JSON
  reply: `{"op": "status"}` → `{"ok": true, "linked", "me", "sent_today", "last_sent_at", "connecting"?,
  "logged_out"?}` (`last_sent_at`: the `at` of the newest `sent.jsonl` line, or null; `logged_out`: WhatsApp
  logged this device out); `{"op": "stop"}` →
  `{"ok": true}`, then it exits; `{"op": "send", "to", "jid", "text", "signature"?}` → `{"ok": true, "stanza"}`
  or `{"ok": false, "error"}`, one of `bad_request` (a JID that is not a user or group JID, an empty text or a
  signed text over 4096 characters, a `to` that isn't a string), `wrong_recipient` (`to` is `me` but `jid`
  isn't `self_jid`, or `to` is a JID or number that isn't `jid`; a name can't be checked here), `no_ticket`,
  `ticket_expired`, `off`, `paused`, `no_workspace`, `cap`, `sent_log_unreadable` (`sent.jsonl` has a torn or
  malformed line, so today's sends can't be counted; the manager fixes or moves the file), `gap`, `unlinked`,
  `wrong_account`,
  `self_mismatch`, `send_failed`, `internal`. Messages go out one at a time. A send needs, in order: a well
  formed request; the workspace's `config.json` (else `no_workspace`); **a ticket** at `<state>/whatsapp/tickets/<sha256(to NUL text)>` that is a regular file (not
  a symlink) whose `at` is less than 600 s old, deleted before anything else happens, so it is used once (a
  missing one is `no_ticket`, an old one, or one dated more than 60 s ahead, `ticket_expired`; every send also
  deletes tickets older than 600 s, skipping names that start with `.`, which are the guard's temp files);
  the recipient check above; sending on and no
  `PAUSED` (it reads the workspace's `config.json`, resolved like `bin/mother`); a readable `sent.jsonl` with
  fewer than `max_per_day` (default 20, ceiling 50) sends on today's UTC date of `at`; at least
  `min_gap_seconds` × a random 0.7–1.3 (never below `min_gap_seconds`) since the last send, **waited out
  inside the request** when 30 s or less, else `gap`; linked (a paired session still connecting is waited
  for, up to 20 s); the linked account (device suffix stripped) equal to `self_jid` (else `wrong_account`);
  and a JID on the manager's own number only as `self_jid` itself (else `self_mismatch`). It shows "typing…"
  for `min(8 s, 40 ms × characters)` plus up to 1 s (not in the manager's own chat), sends, and appends one
  `sent.jsonl` line `{"at", "kind": "send", "chat", "sha256", "stanza"}` (one `O_APPEND` write, `at` UTC
  ISO-8601 with `Z`, `"stanza": null` when WhatsApp returned no id; a failed append is logged, the send
  stands). The sent text is `text`, plus a blank line and `signature` when one is given. Incoming events have no handler: nothing is stored, logged or forwarded. Logs (stderr, to
  `bridge.log`) carry stanza ids and error codes only, never text or a JID; every `console.*` call is
  dropped (libsignal prints key material there). Everything it creates is the manager's alone (umask 077).
  `--link` prints the QR and exits once linked (`--link --phone <digits>` shows an 8-character pairing code
  instead); every connection announces the live WhatsApp Web version (10 s timeout; WhatsApp refuses to pair
  a client that is too old), else the library's built-in one; the bridge counts as paired only once
  `creds.json` holds a confirmed `account`, and `--link` first deletes a `session/` that isn't paired, so each
  attempt starts clean. `--unlink` logs out and deletes `session/`.
- **The client** is `whatsapp.bridge_call(req, timeout=None, start=True)`: it never raises, it returns `{"ok":
  false, "error"}` with `bridge_down`, `bridge_not_installed` (no Node, or no install; carries a `hint` with
  an absolute path), `node_too_old` (below 20), `bridge_start_failed`, `bridge_timeout` or `bridge_bad_reply`.
  Timeout 120 s for `send` (the bridge's worst case: a send queued behind another, up to 30 s for the gap, 20 s
  for a reconnect, 9 s of typing, then WhatsApp's own reply), 5 s for `status`. When the socket
  is missing or refuses, it starts `node <state>/whatsapp/app/bridge.mjs` detached (new session, stdin closed,
  output appended to `<state>/whatsapp/bridge.log`, rotated to `bridge.log.1` past 1 MiB) under an exclusive
  `flock` on `<state>/whatsapp/start.lock`, waits up to 15 s for the socket, then up to 20 s while `status`
  says `"connecting": true`. The bridge exits after 30 minutes with no request (`$MOTHER_BRIDGE_IDLE_MS`
  overrides it) or on `stop`.
- **Other commands.** `install` copies `package.json`, `package-lock.json` and the non-test `*.mjs` of
  `bridge/whatsapp/` into `<state>/whatsapp/app/` (outside the plugin, which an update replaces) and runs
  `npm ci --ignore-scripts --omit=dev` there; exit 2 with `npm_failed`, `bridge_not_installed` (no Node) or
  `node_too_old`. Node is looked up on PATH, then `/opt/homebrew/bin`, `/usr/local/bin`, then the newest
  `~/.nvm/versions/node/*/bin`. `stop` asks a running bridge to exit and prints `{"ok": true, "stopped"}`.
  `status` prints `{"bridge": <status reply or error>, "read": {"db_reachable", "reason"?, "hint"?},
  "reinstall_needed"?}` without starting the bridge or reading a message, even when WhatsApp is off;
  `reinstall_needed` is true when the plugin's bridge files differ (sha256) from the installed copy.
  `link` and `unlink` stop a running bridge, then run the installed `bridge.mjs --link|--unlink` in the
  foreground (link: the same QR/phone choice and retry loop as setup).
- **`mother whatsapp setup`** (with `link` and `unlink`: the manager's own terminal only; exit 2 "run `mother
  whatsapp <sub>` in your own terminal" unless stdin and stdout are TTYs, and the guard denies them to the
  agent) is one guided flow, safe to re-run: (a) install when not installed or `reinstall_needed` (a Node
  problem stops with its hint), then stop a running bridge; (b) ask the bridge whether it is linked; if not,
  "Link by scanning a QR code (Enter), or type p to link with your phone number instead:" and link, offering a
  new QR or code after `qr_expired`, `timeout` or `network`, stopping with a plain sentence on `replaced` or
  `no_account`. When WhatsApp logged the device out (the manager removed it under Linked devices, or it expired:
  a 401, or `status` says `logged_out`), `bridge.mjs --link` deletes `session/` and the same run says "WhatsApp
  logged this device out; showing a fresh code" and links again without asking; a second `logged_out` in a row
  stops. `link` does the same; (c) `self_jid` is the linked account, suffix stripped; when WhatsApp gives only a
  private `@lid` id, setup asks for the manager's number (unless they typed it to link by phone); (d) one
  config write: `enabled: true`, `self_jid`, every other key kept except `allow`, `approve_via` and
  `max_prompts_per_day`, which are dropped; (e) probe the database: `denied` prints the Full Disk Access
  instruction and stops (exit 2), `missing` says to install and sign in to WhatsApp desktop; (f) a summary
  and the next step. Every write re-reads `config.json`, runs the full `validate()` under an exclusive `flock`
  on `<ws>/.mother/config.lock`, and writes only a valid config, atomically (temp file + rename, mode kept,
  `indent=2`, `ensure_ascii=False`, key order kept). Exit 0 done, 2 a step stopped, 130 on Ctrl-C ("setup
  stopped; nothing more was changed").
- **Machine-local state.** `<state>/whatsapp/`: `session/` (device keys, 0700/0600), `bridge.sock`,
  `bridge.log`, `sent.jsonl` (time, chat JID, text hash, message id), `app/` (the installed bridge),
  `tickets/` (the guard's one-use send tickets, 600 s), `start.lock`. Nothing WhatsApp lives in the workspace. `.mother/PAUSED` stops every send.
- **What the guard stops, and what it doesn't.** The guard asks before every send to someone else and keeps
  the agent off the bridge socket, its state and the setup commands. It stops mistakes and injected
  instructions. It does not stop a deliberately evasive agent running as the manager on the same macOS
  account: such an agent can write a script file and run it (the guard sees `python3 x.py`, not what it
  does), build a command from variables set outside the Bash call, or read what the account can read,
  including the session keys, and act outside MOTHER altogether. The guard's reading of a command line is
  lexical: it does not read script files or what a Write puts in one. The send ticket is the second lock:
  the bridge sends only what the guard just decided on, once, within 600 s, so a script or inline code that
  reaches the socket or calls `lib/whatsapp.py` without a guard decision is refused. An agent that forges a
  ticket with its own code (the guard denies it the folder, but not a script it writes and runs) is out of
  scope, by the manager's threat model: the locks are for mistakes and injected instructions. The bridge
  checks that `to` matches `jid` for `me`, numbers and JIDs, not for a name. In `bypassPermissions` a send
  asks nothing, as for chat.
- **Never.** Storing a chat body in the workspace; a first message to someone with no history; sending by any
  path the guard cannot see; assessing a person from their messages.

## 5e. Hub (`lib/hub*.py`, `mother hub`)

A machine-local watcher polls the manager's sources read-only and appends source updates to an inbox; a hub
Claude session routes them into the manager's sessions. Config: the `hub` block (§3). Everything below lives in
`<workspace>/.mother/hub/`, is machine-local and gitignored, and is written atomically (temp file +
`os.replace`) under the `fcntl` lock `hub.lock`. `inbox.jsonl` is append-only by the watcher, and both appending
and rotating take `hub.lock`, so a rotation never races an append.
Nothing holds a message body beyond a summary of at most 300 characters, and nothing counts per person.

- **Files.** `cursors.json` (per-source poll cursors), `inbox.jsonl` (the updates the watcher found),
  `watcher.json` (the watcher's heartbeat: last poll, per-source state, plus `every_seconds`, the current
  effective interval, and `active`, whether it is inside active hours now, so a reader can judge staleness
  without reading config), `hub.json` (what the hub session last did and what is pending), `sessions.json`
  (the manager's sessions the hub may route to), `hub-session.json` (`{session_id, session_ids, at}`: the sessions that ran
  `mother hub tail` in the workspace, newest first, at most 5; the guard fences every id in `session_ids`, so a
  newer `tail` adds a hub session and never unfences an older one, §5), `fed-sessions.json` (`{session_ids}`: the
  sessions that ran any `mother hub …` command, newest first, at most 50; written by the guard under `hub.lock`, §5
  hub-fed sessions), `hub.lock`. (`.mother/channels-session.json`, the channels loop's sessions, is §5b's.) Readers
  treat a missing file as "nothing pending" or
  "the hub never woke".
- **Formats.** A thing key is `PLAT-123` (tracker), `owner/repo#45` (code), `<channel>/<thread ts, else message ts>`
  (chat), `mail:<thread id>`, `cal:<event id>`. An `inbox.jsonl` line is one JSON object `{id, at, source,
  kind (update|resolved|error|sweep|manual), key, also, title, who, link, summary}` (`title` and `who` are the sender's
  text: `make_line` drops C0/C1 control characters and Unicode format and line/paragraph separator characters,
  and turns tab, CR and newline into a space), `id` = `<source>:<key>:<at>` (chat
  uses the message's raw `ts` in place of `at`, so two messages in one second keep distinct ids), `also` = other
  keys found in the text. When lines for one key are collapsed (`coalesce`, and `tail`), the latest `at` wins, but
  a `resolved` line never replaces an `update` whose `at` is the same or later. Appends and rotation both run under `hub.lock`. Rotation moves the inbox to `inbox-YYYY-MM-DD.jsonl` and folds any leftover `inbox.jsonl.*.rotating`
  file from a crash into the dated file, and deletes dated files 7 or more days old.
- **Verbs.** `mother hub watch` (run the watcher), `install` / `uninstall` (its launchd job), `check` (one read per
  connection), `status`, and the hub session's verbs `tail`, `decide`, `session`, `pending`, `done`, `snooze`, `show` (below) and
  the manager's verbs `add`, `follow`, `unfollow`, `mute`, `unmute`, `watching` (watch list, below).
  `mother hub help` works without a workspace. `--json` gives machine output; without it the output is short text.
- **launchd job.** Label `com.mother.hub`, plist `~/Library/LaunchAgents/com.mother.hub.plist`.
  `ProgramArguments` = absolute python, absolute `bin/mother`, `hub`, `watch`; `EnvironmentVariables` =
  `MOTHER_WORKSPACE` and a `PATH` of the directories of every resolved connection program (`cli` wins over
  `command`) plus `/usr/bin:/bin`; `KeepAlive` `{SuccessfulExit: false}` (a crash is restarted, a clean exit
  is not), `ThrottleInterval` 60 and `RunAtLoad` true; stdout and stderr go to `.mother/hub/watcher.log`, which the
  watcher keeps under 1 MB (past that it is copied to `watcher.log.1`, replacing it, and emptied in place).
  `install` exits 2 or 3 off the home machine (as `home check`), exits 1 when `hub.enabled` is not `true`, naming
  any program `shutil.which` cannot find, or when `hub.connections` is empty, then writes the plist and runs
  `launchctl bootstrap gui/<uid>` (a `bootout` first, so a reinstall replaces the job); `uninstall` is `bootout`
  plus removing the plist. `check` runs under that same `PATH`: an MCP connection must start and list every
  tool on its adapter's read allow-list, each exactly once (`tools/list` is the read; no tool is called; a failure
  names the missing ones), `gh` must answer `gh api
  user`; it prints `cap | ok/✗ | reason` and exits 1 if any fails. `status` prints the `watcher.json` and
  `hub.json` (`last_wake`) heartbeats and exits 2 only when the watcher never ran or its `at` is older than 2x
  `watcher.json` `every_seconds` while `active`. The hub line is printed with `STALE` (the exit code is unchanged)
  when, while `active`, `watcher.json` `last_append_at` is later than `hub.json` `last_wake` (or the hub never
  woke) and older than 2x `every_seconds`: the hub has not caught up with a line it must decide. An old
  `last_wake` with nothing appended since is a quiet day, not stale; a missing `hub.json` prints `hub: never woke`.
- **Watch list and `manual` lines.** `watch.json` (machine-local, atomic, under `hub.lock`) is
  `{version: 1, follow: {projects, channels, threads, repos}, mute: {keys, people, channels}, auto: {projects:
  [{key, from, at}]}, unfollowed: [project]}`; a missing or damaged file is empty. Each polling pass merges it with `hub.watch`
  (`merged_watch`: `chat.channels` + followed channels, `chat.follow` + followed threads, `tracker.projects` +
  followed and auto projects, `code.repos` + followed repos, plus the `mute` block) and hands the adapters that
  effective config; `hub.watch` itself is never rewritten. Verbs, all writing only `.mother/hub/`: `follow <x>` /
  `unfollow <x>` classify `x` as a project (`[A-Z][A-Z0-9]+`), a channel (`C…`/`G…` id), a thread
  (`<channel>/<ts>`) or a repo (`owner/repo`), else exit 2 (`unfollow` of a project also drops it from `auto` and records it in `unfollowed`, which `follow` clears);
  `mute <x>` / `unmute <x>` take a chat user id (`U…`) or an email (person), a channel id, or anything else as a
  key; `watching [--json]` prints the merged list and the mutes; `add <link-or-text> [--key K]` appends one
  `manual` line (source `manual`; key = `--key`, else a chat message link's thread key, else the first tracker or
  PR key in the text, else `manual:<sha1 prefix>`; `link` is the first URL; id carries a hash so two adds in a
  second stay distinct). Before appending, the watcher drops a line whose key or `also` key is a muted key, whose
  `who` is a muted person (a user id or email equal to `who`, or to the address inside a trailing `<…>`; never a substring), or whose chat key is in a muted channel. A `manual`
  line is never dropped by mute (mute does drop watcher `resolved` lines), never produced by the watcher, never collapsed by `coalesce`, never resolved
  (a `manual` line in a batch suppresses the `resolved` handling of that key in the same batch), and `tail` always prints it; the hub
  routes or proposes it, never only logs it. `add` strips control and format characters from the text, key and
  link (a link not matching `^https?://\S+$` is left out and stays in the text); its id ends in a random suffix.
  A bare uppercase token given to `follow` is a project; it is a channel only when it is `C`/`G` plus 8 or more
  characters and already a known channel, or with `--channel`; `--project|--channel|--thread|--repo` force the type.
  The chat adapter remembers which threads came from the follow list (`followed` in its cursor) and drops one when
  it leaves the list, so `unfollow` stops polling it; threads the adapter tracks itself stay.
- **The watcher** (`hub.watch(ws, cfg)`, started by `mother hub watch`). It writes one line to its log and
  returns, so the process exits 0 and launchd leaves it down, when this is not the home machine
  (`workspace.home_machine` names another machine or none), when `hub.enabled` is not `true`, or when the config
  is unusable (`hub.every_seconds` not a number >= 1; `idle_every_seconds`, `sweep_minutes` or
  `update_ttl_hours` not a number >= 0; active hours or timezone that will not parse); all are checked once at
  start. It also returns on SIGTERM. A failed inbox rotation is logged and retried at the next day change.
  Each pass (`hub.poll_once`) polls every capability in `connectors` that has an adapter, appends the coalesced
  lines to the inbox, and only then saves that source's cursor in `cursors.json` (`{<cap>: cursor, _last_sweep}`), so
  a crash repeats lines rather than losing them; a failing source keeps its cursor. It sleeps `every_seconds` inside
  active hours (`routines.tick.active_hours` on `manager.workdays`, in `manager.timezone`) and `idle_every_seconds`
  outside; at 0 it does not poll outside them and sleeps until the next window. It rotates the inbox when the local
  day changes (the file is named for the day that ended).
- **`watcher.json`.** `{at, every_seconds, active, last_append_at?, sources: {<cap>: {status: ok|partial|couldnt_check, reason?,
  since?}}, errors: [str]}`. `partial` carries the adapter's sub-read failures as `reason`; `couldnt_check` carries
  why (`no connection` when `hub.connections` has no entry for the capability, or the failure), `since` is when
  the status began, and `errors` lists `<cap>: <reason>` for every source not `ok`. `last_append_at` is the poll
  time of the last pass that appended a line the hub must decide (`update`, `error` or `sweep`; a `resolved` line
  is never printed to the hub, so it does not count, and a key whose winning line in that pass, by the same rule
  `tail` collapses with, is `resolved` does not count either); it is carried over from the previous heartbeat otherwise.
- **Error transition.** On the first failing pass of a source since it last worked, the watcher appends one `error`
  line (key `watcher:<cap>`); `cursors.json` `_errored` ([cap]) remembers that it was emitted, so further failing
  passes add nothing and a restart does not repeat it; a successful pass clears it. The watcher never writes
  `capabilities.json` and never calls `mother health record`: `watcher.json` and the `error` line are its record, and
  `pick` / the doctor are untouched. A missing connection is reported in `watcher.json` only. A `ForbiddenTool` (a
  tool name outside the adapter's `READ_TOOLS`, a programming error) is never caught: it stops the watcher, and
  launchd restarts it after `ThrottleInterval`.
- **Sweep.** A `sweep` line (source `hub`, key `sweep`) is appended when `hub.sweep_minutes` have passed since
  `cursors.json` `_last_sweep` (or there is none) and something is pending: `hub.json` `pending` is non-empty, or a
  `sessions.json` session with `state: live` has `unseen > 0` and a `last_routed_at` within `hub.update_ttl_hours`
  working hours (older unseen news no longer wakes the hub). Both files are read defensively; a missing or
  unreadable one counts as nothing pending.
- **`hub.json`.** `{version: 1, last_wake, inbox_cursor (an inbox line id), next_update (counter behind the U ids),
  routed: [at most 50 {line_id, key, decision, session_id?, at}], pending: [{key, title, link, read, next_step,
  also: [key] (at most 50), proposed_at, raised_at (null until a card is raised), task_id (the card's id, or
  null), last_update_at (the last `propose` for the key), name}], updates: [{id (U<n>), key, title, why, link, at, state: open|done|snoozed, snooze_until?}]}`.
  Readers must tolerate any field missing.
- **`pending raised <key> --name <slug> [--task <task_id>]`.** Stamps `raised_at` (now), `name` and `task_id` on
  that `pending` entry; exit 1 when there is no such pending key, 2 on bad usage. **`pending drop <key>`** removes
  the entry and prints its `task_id` (so the hub can `dismiss_task` the card); exit 1 when there is none.
  **Expiry:** on every write a `pending` entry goes when it is older than `hub.update_ttl_hours` working hours,
  counted from `raised_at`, else `proposed_at`. `decide` `propose` leaves `raised_at` null (keeping an earlier stamp for the same key); the hub raises a card for each entry without `raised_at`, oldest `proposed_at` first, within its card budget, then stamps it. An unraised entry keeps the watcher's sweep firing. The sweep matches started cards by the stamped `name`.
- **`sessions.json`.** `{version: 1, sessions: {<key>: {session_id, name, title, link (a `claude://` URL or null,
  stored as given), also: [key], opened_at, last_routed_at, unseen (int), state: live|dropped}}}`. A key maps to a
  session when it, or any key in the line's `also`, is a session key or is in a session's `also`; only `live`
  sessions receive anything.
- **`tail [--once] [--follow]`.** Prints, one JSON line per item and flushed, the inbox lines after
  `hub.json` `inbox_cursor`, reading the rotated `inbox-*.jsonl` files and then `inbox.jsonl` in order. `update`
  lines are collapsed to one per key (`coalesce`); `sweep` and `error` lines pass through as they are. A key whose
  latest line is `resolved` prints nothing and its `pending` and `updates` entries are dropped. With no cursor it
  starts at the oldest kept line; if the cursor line was pruned (older than 7 days) it does the same and says so
  on stderr. `--follow` (what the hub's Monitor runs) keeps polling the files every 1.5 s, remembers printed line
  ids and so survives a rotation. `tail` never moves the cursor; only `decide` does. `--once` (the default) exits
  after one pass.
- **`decide`.** stdin is one object or a list `{line_id, key, decision: log|now|route|propose|error, title?, why?,
  link?, read?, next_step?, name?, also?}` (exit 2 on bad input, nothing written). Under `hub.lock` it appends to
  `routed` (last 50), moves `inbox_cursor` to `line_id` (never backwards), and sets `last_wake`. `now` creates (or
  refreshes, per key) an `updates` entry with the next `U<n>`; `also` may be one key (a string) or a list
  of keys; anything else exits 2. `propose` replaces the `pending` entry for the key
  (keeping `proposed_at`, `raised_at`, `task_id` and `name`, merging `also`, and setting `last_update_at`);
  `route` adds 1 to `unseen`, sets `last_routed_at` on the matching live session, and adds the line's key and its
  `also` keys to that session's `also` (at most 50, oldest dropped), so a later line in the same thread finds the
  session even without the key it was first routed by. A `line_id` already in `routed` is not applied again: its
  result is `{line_id, key, already_decided: true}`, and a call where every item is already decided writes
  nothing. A `route` with no live session
  is stored as `propose`; the result says so (`note`). A `now` for a key that has a live session is recorded as a
  `route` instead (an update would be dropped at once), and the result says so. A `line_id` that is not in the kept
  inbox files, or whose inbox line has a different `key` than the item's, exits 2 with nothing written. The batch is applied in inbox order whatever order it was given in, and
  the cursor moves to the furthest decided line, where a decided `update` line stands for every `update` line of
  its key after the old cursor with no later `at` (the lines `tail` collapsed into it), wherever they sit; a later `decide` for a line before the cursor is still recorded
  but never moves the cursor back, so the hub must decide every printed line, in printed order. `title` and `link` default to the inbox line's. Output: one
  `{line_id, key, decision (as stored), session_id?, update_id?, note?}` per item.
- **`session`.** `add <key> --id ID --name N --title T [--link URL] [--also K,K]` (only when `<key>` has a `pending`
  entry with `raised_at` set, or is already in `sessions.json`; else exit 1 and nothing written; a key that is live
  under another session id is repointed only when it also has that raised pending entry, else exit 1; live, `unseen` 0; removes the
  key and its `also` from `pending`; when that pending entry's `last_update_at` is later than its `raised_at`, the
  output carries `catch_up: {key, read, next_step, link, at}`, the one message the hub sends into the new session), `drop <key>` (state `dropped`), `seen <key>` (`unseen` 0; exit 1 for an
  unknown key), `for <key>` (prints the live session id, or exits 1).
- **`done <U-id>`, `snooze <U-id> 1h|tomorrow|next-week`, `show [--json]`.** Snooze uses `mother now`'s
  `snooze_until`; an ended snooze is open again at the next write. Exit 1 for an unknown id, 2 for a bad word.
- **Update expiry.** On every write an update goes when it is older than `hub.update_ttl_hours` **working** hours
  (`now.working_hours`, counted from the later of `at` and `snooze_until`) or when its key has a live session.
- **Guard.** The desktop app's own session tools reach only the manager's sessions, but a message can carry
  untrusted source text, so the guard lets them through only inside what the hub manages (§5): `list_sessions`,
  `dismiss_task` and `ListAgents` always; `mcp__ccd_session_mgmt__send_message` only to the `session_id` of a
  `live` `sessions.json` entry; `SendMessage` to such an id, and by name (not a session id, `local_…` or a UUID)
  only to `main` or a `live` entry's `name` (a ` [ref]` suffix is ignored) when the session's cwd is the workspace
  or under it, so text injected into a source the hub reads cannot steer it to another of the manager's sessions
  (outside the workspace any name passes); `mcp__ccd_session__start_session` only with `initiation: "own_initiative"` (a card the manager starts) or
  `proposal_id` alone. Anything else asks. Exact names only: a same-named tool on another server (`mcp__other__send_message`) is classified like any other tool. The credential-shaped-content check (`SECRET_RE`) still applies to their input, so a token pasted into a message or card brief is denied. A reader or critic sub-agent and a channel session (either role) are denied all six.
- **Read-only by construction.** The watcher calls only tools on its adapters' read allow-list. It never sends,
  posts or writes to a tracker.
  Adapters (`lib/hub_sources.py`) are keyed `<cap>:<connector>` (`chat:slack`, `mail:gmail`, `tracker:jira`,
  `calendar:google`, `code:github`) and declare `READ_TOOLS`, tool-name suffixes whose first word is a read verb.
  The client (`lib/hub_mcp.py`) resolves them against the server's `tools/list` and wraps it in `ReadOnlyClient`,
  which raises `ForbiddenTool` for a name outside `READ_TOOLS` before anything is sent. An allowed suffix that the
  server does not offer, or offers more than once, raises `McpError` instead: that source is `partial` (an
  optional sub-read) or `couldnt_check`, and the watcher goes on. GitHub goes through `gh api` with one
  pseudo-tool, `get`, limited to the path prefixes `search/issues?` and `repos/` and no method or field flags.
  `poll` returns `{lines, cursor, partial}`; the first poll (no cursor) returns no lines. The manager's own
  activity yields a `resolved` line for its key, never an `update`. A code CI line uses key `owner/repo#ci-<branch>`.
  A DM conversation is one thing, keyed `<channel>/dm`, so the manager's reply resolves it; an unthreaded reply in a
  watched channel does not resolve the message it answers, the manager's own tracker comment, status, assignee and priority edits resolve (see Own tracker edits), and replies in threads the
  watcher does not follow (ones the manager has not posted in) are not read (both accepted gaps; a comment by
  the manager does resolve).
  **Tracker auto-follow.** After a pass's lines are appended, a tracker key (`PROJ-n`) in any line's `key` or `also`
  whose project is not configured, followed, auto-followed, muted (as a key) or `unfollowed` is added to
  `auto.projects` (`{key, from, at}`; at most 20, the oldest dropped), under `hub.lock` and separately from the
  inbox append; the next pass polls it. A project in `auto` that fails two passes in a row is removed, recorded in
  `unfollowed` and reported by one `error` line (`watcher:tracker:<PROJ>`); configured and followed projects only
  mark the source `partial`, as before. **Own tracker edits.** For each issue whose status, assignee or priority
  changed, the tracker adapter reads `get-issue-changelog` (paged oldest first: one read for `total`, then the last 50
  entries; without a `total`, up to 5 pages until `isLast` is true, and past the cap or with no `isLast` the line stays an `update`; offsets like `+0300` are normalised); when the latest change to those fields was made by
  the manager (`manager.tracker_account` as `accountId`, or `manager.email`) the line is `resolved`; a changelog
  failure or an unreadable answer (only the `values` shape is read) leaves it an `update`. `mother hub check`
  requires `get-issue-changelog` on the tracker server (a missing tool is a missing read tool).
  A code PR leaving the review-requested set resolves its key; CI is green only when every check run is completed.
  The manager is recognised by `manager.email`, a chat user id (the first `U…` id in `manager.chat_dm`, a string
  or a list; else the `chat` of the `people` entry with the manager's email; a `D…` DM channel id is not a user),
  and the code login. A chat message whose first line carries MOTHER's signature (`<agent.name> (AI)` or
  `agent.signature`) and was posted as the manager, without a user, or by a bot is MOTHER's own post: it yields
  no line at all. A mail `resolved` line (the manager's sent reply) is stamped with the sent message's own
  `internalDate` (one `read-email` when the list carries no date), so newer incoming mail in the same poll still
  shows. Mail lines link to the web thread (`https://mail.google.com/mail/u/0/#all/<thread id>`). Each list is read
  one past its limit (mail 20 new messages, tracker 50 updated issues, chat history 100 messages per channel);
  more than the limit marks the source `partial` (`more than N …`) and only the limit is used.
  The tracker read tool (`get-issues`) requires `projectKey`, so the tracker adapter polls once per project key: the
  union of `hub.watch.tracker.projects` and every `teams[].tracker.project`, de-duplicated, order kept, each call
  carrying `projectKey` and the JQL scoped to that project (`extra_jql` still applies). No project keys is a config
  gap: the source is `couldnt_check` with reason `no tracker projects configured` (no error line). One project
  failing (e.g. an unknown key) is skipped and marks the source `partial` (`project <KEY>: <kind>`); the other
  projects still poll and their cursor and fingerprints advance. Accepted gap: issues assigned to the manager in
  projects not listed are not watched. A chat search sub-read that fails for a missing scope says
  `mentions: missing scope` (or `DMs: …`) in the `partial` reason.
  The calendar list tool has no recurrence-expansion argument (its inputSchema is calendarId, timeMin, timeMax), so
  starting-soon sees recurring meetings only when the tool returns the expanded instance.
  Errors carry a `health.CLASSES` kind: a timeout is `unreachable`, a closed server `dropped`.

## 6. Sync (`bin/mother sync`)

- `--quiet` (the hook path) with nothing changed but appended `tick` lines in `runs.md` → no commit, no push,
  `last-sync` untouched (§2).
- No remote → commit locally only. Remote → `git add -A`, run `mother scan` (abort the whole commit and write
  `QUARANTINE.md` on any secret hit), commit `chore(sync): <ISO>`, `pull --rebase`, push; retry ×3 on reject;
  stop on conflict and say so. Never force-push.
- `mother home set|check|move` manages `workspace.home_machine`; `mother:routine` calls `home check` first
  and exits with a `runs.md` line when this isn't the home machine.

**`mother memory index <memory-file> [--title T] [--hook H]`** is the unprompted writer of `memory/MEMORY.md`:
for an existing `memory/<name>.md` (not `MEMORY.md`; name of letters, digits, `.`, `_`, `-`) it appends one line
`- [Title](<name>.md) — hook` (title from `--title`, else the file's frontmatter `name`, else the file stem; hook from
`--hook`, else `description`; both one line, control and format characters dropped, `[ ]` in the title become
`( )`, `[`, `]`, `(`, `)` removed from the hook, at most 80 / 100 characters), creating the index from the workspace
template when missing. An already-indexed file returns `{changed: false, line: null}` before any other check.
`mother memory check` prints `{unindexed: [names], missing: [names]}`: memory files with no index line, and index
links (outside code spans) to files that don't exist. Idempotent: no
change when a line already links `(<name>.md)`. Atomic, under `.mother/memory.lock`. Exit 1 for anything else,
2 for usage. Prints `{ok, changed, line}`.

## 7. Skills in v0

`setup`, `doctor` (also routine `doctor`, daily; heals unattended with retry, reconnect and fall back only, never write checks or sign-ins, and alerts once per issue per day), `routine` (dispatcher), `brief`, `digest` (team standup + decision queue), `dots`
(connect-the-dots), `meeting` (prep deck · meeting mode · apply results · recap), `reflect`, `todo`, `project`,
`team`, `remember`, `whatsapp` (read chats, send through `mother whatsapp send`, which asks; §5d), `hub` (the manager's pinned hub session: wakes on the watcher's inbox through a Monitor on `mother hub tail --follow`, or a 3-minute cron running `tail --once`; decides each line with `mother hub decide`, routes it with the in-app message tool, raises cards with `start_session` `own_initiative`, and reconciles sessions on a `sweep` line; §5e), `now` (shows the tick's list; `mother now do <S-id>` runs one suggestion on the manager's typed request, handing dots and digest items to their own handlers; done, snooze and Top 3 edits). Descriptions stay short and specific: they are matched against every request.

## 8. Writing rules for everything in this repo

- No company-specific names, IDs or hostnames. Examples use `example.com`, `PLAT`, `C00000001`.
- Python 3 stdlib only; shell only for one-liners. Tests for every CLI command and the guard.
- Outbound behaviour always goes through the guard; no skill posts around it.
