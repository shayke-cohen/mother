# MOTHER v0 — build contract

Every part of v0 is built against this file. Change it first, then the code.

## 1. Two trees

**Plugin** (this repo, installed via `/plugin install mother@mother`):

```
.claude-plugin/plugin.json, marketplace.json
bin/mother                 CLI (Python 3, stdlib only): init, join, sync, scan, home, status, register, runs, items, eval
hooks/hooks.json           PreToolUse → hooks/guard.py; Stop + SessionEnd → `mother sync --quiet`
hooks/guard.py             approval guard (see §5)
skills/<name>/SKILL.md     user- and routine-facing skills, invoked as `mother:<name>`
agents/<name>.md           read-only sub-agents MOTHER dispatches: `mother:reader`, `mother:critic` (see §5c)
routines/<id>.md           routine definitions: what a scheduled run does (reads config, calls skills)
lib/                       channels.py (§5b), items.py (reader evidence check), evals.py (A/B harness)
assets/deck/               meeting deck template.html + build.py
rules/                     default policy text skills cite: outbound.md, privacy.md, memory.md, todos.md, projects.md, teams.md
templates/workspace/       files `mother init` copies into a new workspace
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
.mother/                   machine-local state, gitignored: grants.json, ledger.json, ledger.lock, health.lock, guard.log, last-sync, PAUSED, eval/ (A/B pairs)
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
| `people[]` (per team) | | `name, email, chat, code`, optional `one_on_one {cadence_days (3-90, default 14), title}` for 1:1 prep and drift |
| `channels[]` (per team) | | `id, name, mode: off|watch|mention|draft|auto, trigger: manager|team|anyone, external: bool, role: standup|team|product|support|announcements, quiet_hours ("HH:MM-HH:MM", default manager.quiet_hours), max_replies_per_hour (default 6), auto_topics: [slug] (auto mode)` |
| `agent.aliases` | list, optional | other names that address the agent in a channel (e.g. `["mother"]`) |
| `routines.brief.audio` | bool or map, optional | spoken brief: `true`, or `{voice, voice_he, rate (90-360 wpm), language: en|he, save_to, engine: say|kokoro, kokoro_voice (default af_heart)}`; rendered by `mother speak` with macOS `say` or, for English with `engine: kokoro`, local Kokoro (`mother tts setup` → `~/.mother/tts`; falls back to `say` for Hebrew or when missing) into `.mother/audio/` (last 14 kept), copied to `save_to` or iCloud Drive `Mother/` when present |
| `routines.brief.draft_replies` | bool, optional | default false; true = the brief routine saves mail **drafts** for To-respond mail it triaged as `prep` (never sends) |
| `routines.commitments` | map, optional | `at`, `days`, `no_reply_days` (1-30, default 3), `lookback_days` (1-60, default 14), `draft_nudges` (bool, default false: mail drafts for no-reply asks only on the manager's `nudge <n>`) |
| `routines.channels` | map, optional | reply-loop knobs, no `at`: `poll_seconds` 60, `idle_poll_seconds` 180, `model`, `thread_ttl_hours` 48, `draft_ttl_hours` 72, `session_timeout_minutes` 20 |
| `whatsapp` | map, optional | WhatsApp read and send for allowlisted work chats (§5d). `enabled` (bool), `self_jid` (the manager's own JID; required when `enabled`), `allow[]` (`{jid, name, team?, group?}`; `jid` is `<digits>@s.whatsapp.net` or `<digits>@g.us`), `approve_via` (non-empty subset of `slack_dm`, `self_chat`, `session`; default all three), `max_per_day` (1-50, default 20), `min_gap_seconds` (>= 0, default 5), `max_prompts_per_day` (1-100, default 10), `read`, `send` (bool). Unknown keys fail. JIDs are phone numbers: they live in `config.json` only, never in memory, journal or people files. Policy may tighten `approve_via` and the cap, never widen. |
| `routines.<id>` | map | `at` (HH:MM), `days` (`workdays` / `all` / a day / list of days / `monthly:<1-28>`), plus routine-specific options; absent = off |
| `policy` | map | defaults in `rules/outbound.md`; per-user overrides only tighten |

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

**`whatsapp`** is a tool-based connector with no MCP server: `{"tool": "mother whatsapp", "read": "ok",
"send": "untested", "checked_at": "…"}`. The doctor tests `read` for real (opens the local WhatsApp database
read-only, runs the schema check, counts the allowlisted chats; `error:schema` if the tables changed,
`denied` without Full Disk Access). For `send` it only checks the bridge is running and linked
(`untested` if linked, `error:unlinked` if not); it never sends a test message unprompted. A write capability,
so `mother health pick` treats `send` like the others.

Optional extra capabilities: `tracker.comment`, `mail.draft`, `mail.send`, `code.write`, and a `directory`
connector (org lookups). A routine that hits an auth failure mid-run records it with `mother health record`
(status `expired`), so later runs that day don't retry it blindly.

Values are `ok`, `untested`, `denied`, `missing_scope:<scope>`, `expired`, or `error:<short>`. A routine reads
this first and names any capability it needs that isn't `ok` in its output — never "nothing found".
`untested` is not an open health issue in `mother status`.

## 5. Approval guard (`hooks/guard.py`)

Port of the reference deployment's `guard-outbound.py`, made config-driven:
- Matches outbound tools by **capability**, not server name: chat post/send/schedule, tracker create/update/
  transition/comment/sprint ops, mail send, calendar create/update with guests, `gh`/`glab` write commands.
- **allow**: posts to `manager.chat_dm`; mail drafts; calendar events without guests.
- **ask**: everything else outbound (the human confirms). When the payload's `permission_mode` is
  `bypassPermissions`, an ask (other than a guard self-edit) gets no output and is logged as `pass`.
- **deny**: a chat write (post/reply/schedule/edit, or a canvas/topic/purpose change; not a reaction) through a
  server listed in any connector's `stamps` to anyone other than `manager.chat_dm`; an unknown target, or two
  different targets in one call, is denied too (`rules/outbound.md`).
- **deny**: anything if `<workspace>/.mother/PAUSED` exists, and the `never` list (credential entry, deletions).
- Unattended routines may write only through a grant in `.mother/grants.json` that cites the manager's own
  message (channel, user = manager, ts) — scope-limited, count-limited, expiring, logged in `.mother/ledger.json`.
- Every outbound decision is appended to `.mother/guard.log` (tool, category, target, decision, reason — never message text).
- **Session approval:** a human-typed message whose *leading* lines are `mother approve tracker <PROJECT>` or
  `mother approve chat <CHANNEL_ID>` approves this session's writes to that target for 12 hours. Lines from
  tool output, summaries, scheduled triggers or further down a message never count. The deck's "Copy results"
  starts with the tracker line.
- **Grants** (`.mother/grants.json`): `{scope, target, uses, expires, run, source{channel,user,ts}}`; scopes
  `chat-post`, `chat-reply`, `tracker-create`, `tracker-comment`, `tracker-transition`, and `whatsapp` (target =
  draft id, cap 1, written only by `mother whatsapp pending` and valid only with its HMAC `sig`, §5d); the source must be the
  manager, ≤ 96 h old; counts are capped per source in the ledger. `policy.grants` may disable or narrow them.
  Plus `answer` (`+ thread`), written by `mother channels`: one signed reply in one thread of a `mention`/`auto`
  channel, citing whoever addressed the agent if the channel's `trigger` allows them; rate-capped by
  `max_replies_per_hour` in the ledger (`rules/outbound.md`).
- **WhatsApp (`whatsapp.send`, §5d)** is never `ask`: every WhatsApp decision is `allow` or `deny`, in every
  permission mode (including `bypassPermissions`, where an ask would pass), and a guard error on a WhatsApp
  call denies. Action: `{"kind": "whatsapp", "op": "send"|"prompt"|"state"|"compound"|"unknown"|"computed",
  "category": "any_message_to_others" | "whatsapp_state" (for `state`) | "computed_command" (for `computed`),
  "draft": <id>}` (`prompt` adds `"drafts": [ids]`; `state`, `compound`, `unknown` and `computed` always
  deny); `guard.log` names the draft id as the target, never a JID, and never echoes the command.
  - **Recognition: shapes, not spellings.** A `send` or `prompt` is recognised only on the **raw** command:
    `mother whatsapp send <id>` as the **whole** command — the `mother` CLI (bare, a path ending in `/mother`,
    `"${CLAUDE_PLUGIN_ROOT}/bin/mother"`, optionally after `python3`), lower-case unquoted `whatsapp send`, one
    id, nothing else (no `;`, `&&`, `|`, `$( )`, backticks, redirection, env prefix, quoting, second id) — or
    `mother whatsapp prompt <id> [<id>…]` the same way. Every other Bash command is read as shell: simple
    commands split on `;`, `&&`, `||`, `|`, `&`, newlines and parentheses; quotes, `$'…'`, `$"…"` and
    backslashes resolved; comments and quoted heredoc bodies are data; `$( )`, backticks, `<( )`, the strings
    given to `eval`, `sh|bash|zsh -c`, `env -S` and `find -exec`, and a heredoc fed to a shell are read as
    commands too. Variables resolve from `HOME`, `MOTHER_STATE_DIR`, `MOTHER_WORKSPACE`, `CLAUDE_PLUGIN_ROOT`
    (the plugin root), `NAME=…` set earlier in the line, and `$(cat ~/.mother/workspace)` / `$(mother path)`
    (the workspace); everything else is unresolved. Then:
    - a command name (after `env`, `command`, `exec`, `xargs`, `sudo`, `nohup`, `time`, `timeout`, `nice` and
      the like) is first resolved as above and then from the guard's own environment (`$EDITOR`). One that
      still holds an unresolved variable, a substitution or an unquoted glob or brace is **denied** as
      `computed` only when (a) with each unresolved part as `*` its last path part could be `mother`
      (`mo$(true)ther`, `bin/mo?her`, `${D}/mother`; a name that is only one expansion, `$X`, doesn't count
      here), or (b) some word in the line, with expansions as `*` and not a lone `*`, could be `whatsapp`
      (`$X whatsapp send …`, `$X wh${a}atsapp …`). Otherwise it gets no decision (`$EDITOR notes.md`,
      `"$(git rev-parse --show-toplevel)"/x.sh`). An `eval`, `sh -c`, `env -S` or inline-code string the guard
      can't compute is treated the same way, and is also denied when its literal text names `mother` or
      `whatsapp` (`eval "$(ssh-agent -s)"` passes). `$CH plan` resolves when `CH=…` is set earlier in the line;
    - a `mother` command (resolved) whose first argument is `whatsapp` and whose subcommand is `send` or
      `prompt` is `compound` (deny); any other `whatsapp` subcommand must be a passing one, with its raw
      simple command plain, `$VAR` or quoted arguments and redirections (`>`, `>>`, `<`, `<<`, `2>&1`, `&>`; no
      `$( )` or backticks), run directly (not under a wrapper), else `unknown` (deny). Leading `NAME=value`
      prefixes with such a value read like `export NAME=value;` (`MOTHER_STATE_DIR=/tmp/x mother whatsapp
      status` passes), except `PATH`, `ENV`, `BASH_ENV`, `PYTHON*`, `DYLD_*`, `LD_*`, `PERL5*` and
      `NODE_OPTIONS`, which change what code runs (`unknown`); a prefixed `send` or `prompt` is still `compound`
      (it is not the exact shape). A computed first
      argument or subcommand, or `mother` under `xargs`, is `unknown`;
    - unquoted words spelling `mother whatsapp send|prompt` anywhere in the line (`mother whatsapp⏎send w3kx`),
      or that text anywhere in a line where a shell or interpreter reads code from a pipe or heredoc, are
      `compound`; inline code (`python3 -c`, `node -e`, `awk '…'`, …) naming the state paths below, or
      `mother … whatsapp`, or `whatsapp` with `AF_UNIX`, `UNIX-CONNECT` or `.sock`, is denied; so is a script
      under a `bridge/` folder on a line that mentions `whatsapp`, and `nc`, `ncat`, `socat` or
      `curl --unix-socket` on a line that mentions `whatsapp` or `bridge` or has an unresolved argument.
    A repo path, `grep` pattern or commit message that merely contains the words (`git -C …/mother log --grep
    whatsapp`, `git commit -m "whatsapp: …"`) is not a mention: the rules key on command names and resolved
    paths.
  - `send` is allowed only when all hold, else denied: not PAUSED; `config.json` valid; `policy.never` lacks
    `any_message_to_others`; `lib/whatsapp.py` imports (else deny "whatsapp module unavailable");
    `whatsapp.enabled` and `whatsapp.send`; this is `workspace.home_machine`; the draft is open (exists,
    unexpired); its chat is in `whatsapp.allow` and is not `self_jid`; the chat has history (no cold contact);
    `<state>/whatsapp/sent.jsonl` is readable (missing = empty) and has fewer `kind: "send"` lines dated today
    (UTC date of `at`; `prompt` lines don't count) than `whatsapp.max_per_day` (default 20, ceiling 50), and no
    `send` line for this draft id with this text hash (once); and `prove()` finds a yes on an enabled path.
    Also: no `approved/<id>.used` (deny "already sent") and no `approved/<id>.json` (deny "send for <id>
    already approved/in flight"); an `approved/<id>.failed` does not block a fresh approval (it is re-proved).
    A `slack_dm` proof consumes the proving grant's one use in `.mother/ledger.json` (that exact grant: same
    source, text hash and a valid `sig`); a spent grant denies. On allow the guard creates
    `<ws>/.mother/whatsapp/approved/<id>.json` = `{"id", "chat", "sha256", "path", "evidence", "at"}`
    exclusively (`O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600; an existing file or symlink denies), with the hash
    `prove()` returned, and the reason names that file.
  - `mother whatsapp prompt <id> [<id>…]` (a single command, ids only) is allowed when every id is an open draft
    and the PAUSED/config/enabled/home checks above pass (it reaches only the manager's own self chat), else
    denied. `mother whatsapp read|chats|draft|list|drop|status|pending|verify|link|unlink|help` pass the guard (not
    outbound; `link` shows a local QR, `drop` and `unlink` only reduce), alone or in a pipeline.
  - **State is off-limits to the agent.** The protected folders are `<ws>/.mother/whatsapp/`,
    `<state>/whatsapp/` (`<state>` = `$MOTHER_STATE_DIR` or `~/.mother`) and `~/.mother/whatsapp/`; every path
    comparison is case-insensitive (casefolded after `realpath`), here and for the guard's own files below.
    A Bash command is **denied** (`state`) — reads included — when its cwd lies inside a protected folder, or
    when an argument, a redirection target, an assignment value or a piece of one (split at `=`, `:`, `,`,
    quotes and parentheses, so `--file=PATH`, `UNIX-CONNECT:PATH` and `open('PATH')` count) names one:
    - a word with no unresolved part, after `~` and the variables above, resolved against the cwd and every
      `cd`, `pushd`, `-C` and `--directory` in the line, that lies inside a protected folder; or a word
      without spaces that contains `bridge.sock`, `.mother/whatsapp`, `whatsapp/{approved,drafts,session}`,
      `whatsapp/{sent,prompts}.jsonl` or `whatsapp/grant.key`;
    - a word with wildcards: each unresolved expansion and each brace group (`{a,b}`) becomes `.*` (it may
      hold a `/`), each `*` `?` `[…]` a glob within one folder (quoted ones too). It is denied when it, or a
      prefix of it ending at a `/`, matches a protected path case-insensitively (`~/.mo${E}ther/wh*`,
      `"$W"/.mot${E}her/whats${E}app/…`, `~/.mother/*`). A word, or prefix, that is only wildcards and `/`
      names nothing in particular and is skipped, here and in the destructive check below (`cd "$W"`,
      `rm -rf "$D"/*`, `cp -R "$A"/* "$B"/`). After a `cd` to an unresolved folder, later relative words are
      matched as `.*/word`. Matching takes time linear in the word and the path, however many wildcards it
      holds (runs of them collapse to one);
    - a word with an unresolved part next to the text `.mother`, `whatsapp`, `bridge.sock` or `grant.key` (no
      spaces in it): `cat $UNKNOWN/.mother/x`.
    Destructive or copying commands (`rm`, `rmdir`, `unlink`, `mv`, `cp`, `rsync`, `tar`, `ditto`, `zip`,
    `unzip`, `ln`, `chmod`, `chown`, `chflags`, `shred`, `trash`, and `find` with `-delete`/`-exec`/`-ok`)
    are also **denied** when an argument names a folder that holds the state: `~/.mother`, `<state>`, `<ws>`,
    `<ws>/.mother`; and, for a recursive one (`rm`, `rmdir`, `mv`, `tar`, `zip`, `unzip`, `rsync`, `ditto`,
    `shred`, `trash`, `find`, and `cp`/`chmod`/`chown`/`chflags` with `-r`/`-R`/`-a`), any folder above them
    except `/` (`find ~ … -delete`, `tar cf x.tar .` in the workspace, `cp -r <state> /tmp`). `git clean`
    (not `-n`/`--dry-run`) is recursive the same way: denied when it runs in any of those folders (its cwd,
    a `cd`, `-C`, `--work-tree` or a pathspec: `git -C <ws> clean -fdx`, `git clean -fdx` in `<state>`). Reading and
    listing those folders, and work inside the workspace, pass (`ls ~/.mother`, `cd "$W" && git status`,
    `cp a.md "$W"/memory/`).
    Write/Edit/MultiEdit/NotebookEdit anywhere under a protected folder is **denied** (not asked). Only
    `mother whatsapp` subcommands write there. The Read tool may read drafts.
    Residuals, stated plainly: see §5d "What the locks stop".
  - Channel sessions (`MOTHER_CHANNEL_ROLE`) run no Bash at all, so no `mother whatsapp` command. The
    `mother:reader` sub-agent may run only a single `mother whatsapp read …` command (no shell operators);
    every other `mother whatsapp` command is denied to sub-agents. `MOTHER_DRY_RUN=1` denies `send` and `prompt`.
- **Paused** denies drafts too. Policy overrides only tighten (the auto list can shrink, the never list can grow).
- The hook matcher includes `Write|Edit|MultiEdit|NotebookEdit`, so edits to the guard's own files
  (config, ledger, log, PAUSED, hooks) ask, matched case-insensitively (`Config.json`, `.mother/paused`).

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
  limited to single `gh`/`glab`/`date` commands with no chaining. A critic gets no MCP tools. Other sub-agents
  (not MOTHER's) keep the normal rules.
- `mother items check` turns a reader's reply into the only input a skill may use. Items without a link
  (URL, ticket key, `owner/repo#N`, workspace path) are dropped. A `state: noise` tally cited by the query that reproduces it is kept as a count, never a finding. A reader whose items were all uncited
  counts as `couldnt_check`, and so does unreadable output (exit 2). Never treat it as a quiet source.
- `MOTHER_DRY_RUN=1` makes the guard deny every outbound call. `mother eval brief` sets it for both runs of
  an A/B pair. A new gather mode becomes the default only when `mother eval report` says `ship`: at least
  10 judged pairs; per-source coverage and uncited lines no worse than baseline; no new false-quiet sources; the manager
  picked it or `same` in ≥ 60% of pairs; and cost ≤ 3× baseline.

## 5d. WhatsApp (`lib/whatsapp.py`, `mother whatsapp`, `bridge/whatsapp/`)

Read and send for the chats in `whatsapp.allow`, from the manager's own number. Two independent parts: a
read-only reader of the local WhatsApp database, and a send-only bridge (a linked device). Everything is
home-machine-only: every `mother whatsapp` command except `status` refuses to run anywhere but
`workspace.home_machine`.

- **Read.** `mother whatsapp read --since <iso> [--chat <jid>]…` opens the macOS WhatsApp database
  (`$MOTHER_WHATSAPP_DB`, else `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`)
  with `mode=ro`, filters by the allowlisted JIDs (plus the self chat for approvals) in SQL, and prints reader
  items (`mother items check` applies). Text messages only; anything else is `[media]` with no content. A
  locked database is retried once, then the source is `couldnt_check`; a missing table or column is
  `couldnt_check` with `schema_changed`. Never a partial list. Citation: `whatsapp://<chat-jid>/<stanza-id>`.
  Read-only, so the guard lets it through. No chat bodies are written to the workspace.
  `mother whatsapp chats` lists recent chats (jid, name, last message date, group flag) from the session table
  only, never a message; setup uses it to propose the allowlist. Both commands refuse off the home machine
  (exit 2; exit 3 if no home is set) and print `couldnt_check` with a `reason` (`schema_changed`, `locked`,
  `denied`, `missing`) instead of a partial answer. Text types are `ZMESSAGETYPE` 0 and 7 (7 = link preview).
- **Drafts.** A send is always draft, then approved send. `mother whatsapp draft --chat <jid> --text-file <path>`
  writes `<ws>/.mother/whatsapp/drafts/<id>.json` (`id`, chat, text, text sha256, created, `run`). Ids are `w`
  plus 3 characters of `[a-z2-9]`. Drafts are machine-local, never synced, and expire after 72 hours. An edit is
  a new draft with its own yes. A draft may only target an allowlisted chat. Routines may draft; they never
  send (frontmatter `sends: [whatsapp.draft]`). `draft` also refuses (exit 2) a chat with no history in the
  database ("no cold contact"), prints the draft as JSON, and exits 1 on empty text. `mother whatsapp list [--json]`
  shows open drafts (an expired one is deleted when read); `mother whatsapp drop <id>` deletes one (exit 1 if
  none). `read` and `chats` exit 2 "whatsapp reading is off" when `whatsapp.enabled` or `whatsapp.read` is false;
  `draft` exits 2 "whatsapp sending is off" when `enabled` or `send` is false. In a group, the sender is the
  member's contact name, else first name, else "a member"; never a phone number.
- **Three approval paths** (`whatsapp.approve_via`), each shown the same wording, `WhatsApp -> <chat name>:
  "<text>" - reply ok <id>`:

  | Path | Manager says yes | Proof the guard checks |
  |---|---|---|
  | `slack_dm` | `ok <id>` in `manager.chat_dm` | a grant citing the manager's own message (channel, user = manager, ts), as for run grants |
  | `self_chat` | `ok <id>` in "Message yourself" | the local database holds a `ZISFROMME` text `ok <id>` in the self chat after the draft was shown, whose stanza id is **not** in the bridge's `sent.jsonl` (MOTHER cannot approve itself) |
  | `session` | `ok <id>` in the Claude session | the guard's `session_approvals` (genuine user messages only) |

  Grammar: `ok <id> [<id>…]`, `ok all`, `no <id>`. One yes approves one draft: one exact text, one chat, once.
  First valid yes wins. `no <id>` on any path drops the draft. A session may name explicit ids only; `ok all`
  counts only on `self_chat` and `slack_dm`, where a batched prompt exists to resolve it.
  The whole message must be the command (case-insensitive; `ok w3kx but change it` is not a yes). Every proof
  must come after the draft's `created` time, so an old reply can't approve a later draft that reuses the id.
  **Bound to the text:** the draft file keeps `sha256` of its text; a proof needs `sha256(text)` to still equal
  it, and the self-chat and DM proofs need a prompt that showed the draft with that same hash. The proof
  carries the hash (`prove()` returns `{path, evidence, sha256}`); the guard writes that hash into
  `approved/<id>.json`. `prompts.jsonl` lines (written when a prompt goes out) are
  `{"at", "path": "self_chat"|"slack_dm", "ids": [...], "sha256": {"<id>": "<text sha256>"}}`; a line without
  the `sha256` map shows nothing. `ok all` resolves to the latest prompt for that path sent at or before the
  reply, and covers a draft only if that prompt came after the draft and carries its current hash.
  `session`: a genuine human message (the guard's `user_text`) in the transcript, at most 72 h old, explicit ids.
  `self_chat`: an `ok`/`no` counts only after a self-chat prompt that showed the draft with its current hash (no
  prompt, no self-chat proof). A message with an empty stanza id never counts. A yes needs
  `<state>/whatsapp/sent.jsonl` (`{"at", "kind": "send"|"prompt", "chat", "draft"?, "sha256", "stanza"}`) to
  exist and parse: if it is missing, unreadable, or any line is malformed or lacks a stanza id, the self chat
  proves nothing. A well-formed line whose `stanza` is JSON `null` (the bridge got no id back) is skipped.
  When `self_chat` is in `approve_via` and the self chat can't be read from the database, no
  path proves (a `no` there might be hidden). `slack_dm`: the grant's source channel must be a
  `manager.chat_dm` id (not `manager.chat_user`); with no `chat_dm` the DM path is unavailable. A `no` on any
  enabled path beats a yes: the draft is not sendable. Drafts never go to `whatsapp.self_jid` (`draft` exits 2).
- **`mother whatsapp pending`** reads stdin `{"dm": [chat messages]}` (the manager's DM messages the
  `/mother:channels` loop already fetched: `user`, `ts`, `text`). Only the manager's own human messages count
  (not bots, apps, subtypes, or the agent's own signed posts). For each `ok` naming an open draft created before
  the reply, whose current text a `slack_dm` prompt showed before the reply, it appends a grant to
  `.mother/grants.json` under an exclusive lock on `.mother/grants.lock` (`scope: "whatsapp"`, `target: <id>`,
  `sha256` of the text, `uses: 1`, `run: "whatsapp"`, `expires` = reply + 72 h, `source` = first
  `manager.chat_dm`, manager, reply `ts`, and `sig` = HMAC-SHA256 hex over `scope\ntarget\nts\nsha256` keyed
  by `<state>/whatsapp/grant.key` (32 random bytes as hex, created 0600 with `O_EXCL` on first use); the same
  reply never writes a second grant). `grants.json` stays agent-writable (the digest writes run grants with
  agent tools), so a `whatsapp` grant without a valid `sig` (missing, wrong, not a string, non-ASCII) proves
  nothing and is never consumed; a malformed one never stops the others being checked. Each `no` drops the
  draft. Then it drops drafts with a `no` in the self chat and prints
  `{"send": [ids with a proof on an enabled path], "dropped": [ids]}`. With `whatsapp.enabled` or
  `whatsapp.send` false it prints empty lists. It sends nothing itself.
- **Send.** `mother whatsapp send <id>` goes through the guard (§5), which loads the draft, checks the chat is
  allowlisted and the draft unexpired, the daily cap and that it was not already sent, finds a proof by an
  enabled path, and writes `<ws>/.mother/whatsapp/approved/<id>.json` (`{"id", "chat", "sha256", "path",
  "evidence", "at", "transcript"?}`: path that proved it, evidence key, text hash, and the session transcript
  the guard read when it had one). The bridge checks again: allowlist, approval file whose hash matches the
  draft, daily cap, and the yes itself: before every send it runs `python3 <plugin>/bin/mother whatsapp verify
  <id> --sha256 <hex>` (no shell) and refuses with `no_approval` unless it exits 0. **`mother whatsapp verify
  <id> [--sha256 <hex>]`** passes the guard and is read-only: it never deletes or changes a file (an expired
  draft is exit 1 "expired", not removed). It loads `approved/<id>.json`, re-runs the proof **on the path the
  record names only** with the evidence it records (the recorded transcript for `session`, the local database
  for `self_chat`, the signed grant for `slack_dm`, whose use the guard already consumed), and exits 0 only if
  that path holds a yes (and no enabled path a `no`) and the proof's sha256 equals the approval's, `sha256` of
  the draft's current text, and `--sha256` when given; else exit 1 with the reason on stderr (exit 2 when
  sending is off, like `draft`). **Approval lifecycle:** the guard
  creates `approved/<id>.json` (exclusive); the bridge renames it to `approved/<id>.used` after a send and to
  `approved/<id>.failed` on a definite failure. `.json` present = in flight (the guard denies another send);
  `.used` = sent (denied for good); `.failed` = a fresh `send` re-proves and approves again. `mother whatsapp
  draft` never reuses an id that has any file in `approved/`. The guard is the first lock, the
  bridge the second. Unattended sends happen only in the `/mother:channels` poll, which runs
  `mother whatsapp pending` (open drafts with a proven yes) and sends each.
- **Bridge** (`bridge/whatsapp/`, Node 20+, Baileys pinned exactly in `package.json` and `package-lock.json`;
  optional, nothing else depends on it). `node bridge.mjs` serves on `<state>/whatsapp/bridge.sock` (0600, in
  a 0700 folder; a stale socket is replaced, a live one refused); `--link` prints the QR and exits once
  linked; `--unlink` logs out and deletes `session/`. It resolves the workspace as `bin/mother` does
  (`$MOTHER_WORKSPACE`, else `<state>/workspace`). One JSON request per connection, one JSON reply:
  `{"op": "status"}` → `{"ok": true, "linked", "me", "sent_today", "prompts_today"}`;
  `{"op": "send", "draft": "<id>"}` → `{"ok": true, "stanza"}`; `{"op": "prompt", "text"}` → the same, sent
  to `whatsapp.self_jid` only. A refusal is `{"ok": false, "error"}`, one of `not_allowlisted`, `no_approval`,
  `hash_mismatch`, `cap`, `gap`, `unlinked`, `no_draft`, `already_sent`, `off`, `paused`, `wrong_account`,
  `no_workspace`, `send_failed`, `log_failed` (prompt only), `bad_request`, `internal`. Messages go out one at
  a time. A send needs, in order: `approved/<id>.json` exists (else `no_approval` and nothing else); sending
  on, no `PAUSED`; the draft's chat in `whatsapp.allow` and not `self_jid`; the approval for that id and chat;
  `sha256(text)` equal to the draft's and the approval's hash; no `sent.jsonl` send of that id and hash; fewer
  than `max_per_day` sends and (for a prompt) `max_prompts_per_day` prompts on today's UTC date of `at`; at
  least `min_gap_seconds` × a random 0.7–1.3 (never below `min_gap_seconds`) since the last message of either
  kind, **waited out inside the request** when 30 s or less, else refused `gap`; an unreadable `sent.jsonl`
  refuses as `cap`; linked; the linked account (device suffix stripped) equal to `self_jid` (else
  `wrong_account`, for prompts too); then `mother whatsapp verify <id> --sha256 <hash of the text in memory>`
  exit 0. It shows "typing…" for `min(8 s, 40 ms × characters)` plus up to 1 s (not for a prompt), sends, and
  appends one `sent.jsonl` line (one `O_APPEND` write, `at` UTC ISO-8601 with `Z`; a prompt has no `draft`;
  `"stanza": null` when WhatsApp returned no id). **Once the approval file exists every outcome settles it:**
  `.used` after a send or on `already_sent`; `.failed` on every refusal or failure (so a fresh `send`
  re-proves the same yes). A crash between the send and the log line leaves the approval `.json` and the
  outcome unrecorded; the guard then denies a resend, which is the safe side. A prompt whose text (or any line
  of it) reads as a reply (`ok <id>…`, `ok all`, `no <id>…`) is `bad_request`; a prompt whose `sent.jsonl`
  line can't be written returns `log_failed` with its `stanza` and a `detail` saying it went out unlogged.
  Incoming events have no handler: nothing is stored, logged or forwarded. Logs (stderr) carry draft ids,
  stanza ids and error codes only, never text or a JID. Everything it creates is the manager's alone (umask
  077; session files 0600).
- **Commands that talk to the bridge** (all refuse off the home machine except `status`; `send` and `prompt` exit 2
  "whatsapp sending is off" when `enabled` or `send` is false). The client is `whatsapp.bridge_call(req,
  timeout=None, start=True)`: it never raises, it returns `{"ok": false, "error": …}` with `bridge_down` (not
  running and not started, or no socket within 15 s), `bridge_not_installed` (no `node` on PATH or no
  `bridge/whatsapp/node_modules`; carries a `hint` to run `npm ci --ignore-scripts --omit=dev` there),
  `bridge_start_failed`, `bridge_timeout` or `bridge_bad_reply`. Default timeout 60 s for `send`/`prompt` (the bridge
  may wait 30 s for the gap, then type and verify), 5 s for `status`.
  - **`mother whatsapp send <id>`** exits 2 without calling the bridge unless `approved/<id>.json` exists. On a
    bridge `ok` it sets the draft's `status` to `sent` (a sent draft is no longer listed, prompted or
    returned by `pending`) and prints `{"ok": true, "stanza"}`; any bridge error prints the reply as JSON and
    exits 2 with the code. It never touches `approved/`: the bridge settles the approval.
  - **`mother whatsapp prompt <id> [<id>…]`** (ids only: the guard accepts nothing else) needs every id to be
    an open draft (exit 2 otherwise) and runs every path in `whatsapp.approve_via` that can prompt, as one
    batched text. `self_chat`: the bridge `prompt` op. `slack_dm`: no call; the output carries
    `{"slack_dm": {"ok": true, "channel": <first manager.chat_dm>, "text"}}` for the skill to post (no
    `chat_dm`: `{"ok": false, "error": "no_chat_dm"}`). Output `{"ids", "self_chat"?, "slack_dm"?}`; exit 2 when no
    path delivered. Each path that delivered appends one `prompts.jsonl` line (`{"at", "path", "ids", "sha256"}`,
    one `O_APPEND` write; a bridge `log_failed` counts as delivered because the message went out; a bridge refusal
    records nothing). During `manager.quiet_hours` it prints `{"held": true}`, records nothing and calls nothing. The
    text is `<id> WhatsApp -> <chat name>: "<text>"` per draft (continuation lines of the text quoted with `> `),
    a blank line, then `Reply with: ok <ids>  |  ok all  |  no <id>`; no line can read as an approval reply, which the
    bridge would refuse as `bad_request`.
  - **`mother whatsapp link` / `unlink`** run `node bridge/whatsapp/bridge.mjs --link|--unlink` in the foreground,
    inheriting the terminal (the QR shows there). **`status`** prints `{"bridge": <status reply or error>, "read":
    {"db_reachable": bool}}`; it never starts the bridge and never reads a message.
  - **Bridge supervision.** No launchd agent. When the socket is missing or refuses a connection, `bridge_call`
    starts `node bridge.mjs` detached (new session, stdin closed, stdout and stderr appended to
    `<state>/whatsapp/bridge.log`, which holds ids and codes only) and waits up to 15 s for the socket. The bridge exits
    cleanly (socket removed) after 30 minutes with no request (`$MOTHER_BRIDGE_IDLE_MS` overrides it, for tests), so
    the first send after a quiet spell takes a few seconds longer. The guard already classifies `send` (proof
    required), `prompt` (ids only, each an open draft) and `link`, `unlink`, `status` (pass).
- **Limits.** `max_per_day` (default 20, ceiling 50) counts every send; `min_gap_seconds` (default 5, jittered)
  between sends; one recipient per call; no first message to a JID with no prior message in the local
  database. Self-chat prompts go to the manager's own number: they do not count toward `max_per_day`; they count only toward
  `max_prompts_per_day` (default 10), are batched to one prompt per routine run per path, and are held during
  `manager.quiet_hours`.
- **Machine-local state.** Bridge: `<state>/whatsapp/` (`<state>` = `$MOTHER_STATE_DIR` or `~/.mother`):
  `session/` (device keys, 0600), `bridge.sock` (Unix socket, 0600, no TCP), `bridge.log` (ids and codes only) and `sent.jsonl` (time, chat JID,
  draft id, text hash, stanza id). Workspace, git-ignored: `.mother/whatsapp/drafts/`, `approved/`,
  `prompts.jsonl`. `mother whatsapp link` shows the QR; `unlink` logs the device out and deletes the keys.
  `.mother/PAUSED` stops all sends.
- **What the locks stop.** The guard and the bridge stop mistakes and injected instructions: an agent that
  misreads a yes, a message or page that tells it to send, a command written in an odd way. They do not stop
  a deliberately evasive agent running as the manager on the same macOS account. Such an agent can write a
  script file and run it (the guard sees `python3 x.py`, not what it does), build a path in a way no shell
  reader resolves, or read what the account can read — including the bridge's session keys in
  `<state>/whatsapp/session/` — and act outside MOTHER altogether. Two narrower residuals on the `slack_dm`
  path: `mother whatsapp pending` signs whatever DM messages the agent passes it on stdin, and
  `<state>/whatsapp/grant.key` is readable by the account (the guard denies it to Bash, but the Read tool
  runs unchecked outside channel sessions). So the Slack path trusts the agent's report of the manager's DM.
  A send built entirely from variables set outside the Bash call (`$A $B $C $D`, every word computed, none
  that could read as `mother` or `whatsapp`) gets no guard decision: that is the evasive case too, and only
  the bridge's own checks stand in its way.
  The bridge re-checking the original yes itself narrows this; it does not remove it. The `session` and
  `self_chat` proofs read the transcript and the local database, not agent input.
- **Never.** Reading or storing a chat that is not on the allowlist; sending text the manager did not approve
  word for word; sending by any path the guard cannot see; assessing a person from their messages.

## 6. Sync (`bin/mother sync`)

- No remote → commit locally only. Remote → `git add -A`, run `mother scan` (abort the whole commit and write
  `QUARANTINE.md` on any secret hit), commit `chore(sync): <ISO>`, `pull --rebase`, push; retry ×3 on reject;
  stop on conflict and say so. Never force-push.
- `mother home set|check|move` manages `workspace.home_machine`; `mother:routine` calls `home check` first
  and exits with a `runs.md` line when this isn't the home machine.

## 7. Skills in v0

`setup`, `doctor` (also routine `doctor`, daily; heals unattended with retry, reconnect and fall back only, never write checks or sign-ins, and alerts once per issue per day), `routine` (dispatcher), `brief`, `digest` (team standup + decision queue), `dots`
(connect-the-dots), `meeting` (prep deck · meeting mode · apply results · recap), `reflect`, `todo`, `project`,
`team`, `remember`. Descriptions stay short and specific: they are matched against every request.

## 8. Writing rules for everything in this repo

- No company-specific names, IDs or hostnames. Examples use `example.com`, `PLAT`, `C00000001`.
- Python 3 stdlib only; shell only for one-liners. Tests for every CLI command and the guard.
- Outbound behaviour always goes through the guard; no skill posts around it.
