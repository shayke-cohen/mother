# Architecture

A map of the code, for whoever changes it next. *Why* lives in [docs/DESIGN.md](docs/DESIGN.md); the exact
rules (file formats, config keys, guard decisions) live in [docs/CONTRACT.md](docs/CONTRACT.md). This page
says where things are and how they connect.

## The big idea

MOTHER is a Claude Code **plugin**. Most of its behaviour is Markdown the model follows (skills, routines,
rules). The parts that must hold even when the model forgets, gets confused or reads a hostile email are
**Python**, and they run outside the model's control: a hook that judges every outbound call, a CLI that owns
state and git, and small engines whose JSON answers the skills must use.

```
            plugin repo (shared, versioned)                    workspace (one per manager, private)
 ┌─────────────────────────────────────────────┐      ┌──────────────────────────────────────────┐
 │ Markdown the model follows                  │      │ config.json        who, teams, routines  │
 │   skills/   routines/   rules/   agents/    │ ───▶ │ capabilities.json  per-server health     │
 │                                             │reads │ memory/ todos/ people/ teams/ projects/  │
 │ Python that holds regardless                │writes│ journal/ one-on-ones/  runs.md           │
 │   hooks/guard.py  bin/mother  lib/*.py      │      │ .mother/  machine-local state (gitignored)│
 └─────────────────────────────────────────────┘      └──────────────────────────────────────────┘
```

Plugin = code, workspace = data. The plugin never stores anything about a manager; the workspace never
holds code a later session would run.

## Code map

| Path | What it is | Notes |
|---|---|---|
| `.claude-plugin/` | Plugin manifest and marketplace entry | Version lives here and in `bin/mother` |
| `hooks/hooks.json` | Wires the hooks | PreToolUse → guard; Stop and SessionEnd → `mother sync --quiet` |
| `hooks/guard.py` | **Approval guard**: allow / ask / deny for every outbound call | Recognises tools by capability, not server name. Fails closed. Reads `stamps` from `capabilities.json` to block writes to others through a connector that adds its own attribution. CONTRACT §5 |
| `bin/mother` | The CLI: workspace lifecycle, sync, scan, home machine, status, register, runs watchdog, speak/tts | Dispatches `channels`, `items`, `eval`, `health` to `lib/` |
| `lib/health.py` | Per-server connector health (`mother health`): record, pick a server, classify an error, merge the doctor's result, once-a-day alerts | **The only writer of `capabilities.json`** (file lock, atomic write); appends `health.md`. CONTRACT §4 |
| `lib/channels.py` | Reply-mode engine behind `mother:channels` | The skill does chat I/O; this decides who, when, and what. CONTRACT §5b |
| `lib/whatsapp.py` | `mother whatsapp`: chats and read from the local WhatsApp database, send through the bridge, setup and link | Home machine only; the guard asks before each send to someone else. CONTRACT §5d |
| `bridge/whatsapp/` | Send-only WhatsApp bridge (Node, Baileys linked device) on a 0600 Unix socket | Optional; `checks.mjs` = pure send rules (cap, gap), `bridge.mjs` = socket + WhatsApp. CONTRACT §5d |
| `lib/grants.py` | `mother grant add\|remove\|list`: the one writer of `.mother/grants.json` for unattended runs | Checks each grant with the guard's own `valid_grant` before writing; the guard asks for any other write. CONTRACT §5 |
| `lib/items.py` | Evidence check on a reader sub-agent's reply | Drops uncited items; bad output = `couldnt_check`. CONTRACT §5c |
| `lib/now.py` | Deterministic core of the hourly tick and Now panel (working hours, signal thresholds, merge into `now.json`, the `mother now` verbs) | Python 3.9 stdlib only |
| `lib/hub.py` | State core of the watcher and hub: thing keys, inbox lines, `.mother/hub/` files (atomic, locked) | Python 3.9 stdlib only |
| `lib/hub_mcp.py` | Minimal MCP stdio client plus the read-only wrapper the watcher uses | Allow-list enforced before send. CONTRACT §5e |
| `lib/hub_sources.py` | Per-capability poll adapters and the deterministic prefilter | Product-specific tool names live only here. CONTRACT §5e |
| `lib/evals.py` | Blind A/B harness for a routine change (`mother eval`) | Both runs dry (`MOTHER_DRY_RUN=1`) |
| `lib/tts_kokoro.py` | Optional neural voice for the spoken brief | Runs in its own venv under `~/.mother/tts` |
| `skills/<name>/SKILL.md` | What the model does for a request, invoked as `mother:<name>` | `templates/` beside a skill = files it creates in the workspace |
| `routines/<id>.md` | What one scheduled run does | Frontmatter: `id`, `needs`, `writes`, `sends`, `config`, `default` |
| `agents/` | Read-only sub-agents: `reader` (one source → cited JSON), `critic` (draft → PASS/FIX) | Read-only is enforced by the guard, not the frontmatter alone |
| `rules/` | Policy text the skills cite: outbound, privacy, memory, todos, projects, teams | When `outbound.md` and the guard disagree, the guard wins |
| `assets/deck/`, `assets/plan/` | Self-contained HTML templates + `build.py` (JSON in → HTML out) | Meeting deck with meeting mode; sprint plan page |
| `templates/workspace/` | Skeleton `mother init` copies into a new workspace | Includes the **persona** `AGENTS.md` for the manager's agent |
| `tests/` | stdlib `unittest`, one module per Python component plus packaging | `test_packaging` checks every runtime file is tracked |
| `docs/` | CONTRACT (spec), DESIGN (why), ROADMAP (stages) | `docs/drafts/`, `docs/superpowers/` are untracked personal notes |

Skills: `setup`, `doctor`, `routine` (the dispatcher), `brief`, `now`, `hub`, `digest`, `dots`, `commitments`,
`one-on-one`, `meeting`, `channels`, `reflect`, `todo`, `project`, `team`, `remember`, `whatsapp`.

Routines: `brief` (+ `brief-ab`), `digest`, `dots`, `tick`, `commitments`, `one-on-one`, `meeting-prep`,
`meeting-recap`, `reflect`, `doctor`, `weekly-snapshot`, `monthly-refresh`.

## How a scheduled run flows

```
Claude Code scheduler (desktop app, home machine only, local-time cron from `mother register`)
  └─ thin pointer prompt: cd <workspace>; git pull --rebase; run mother:routine with id <id>
      └─ skills/routine  ── mother home check · stop on .mother/PAUSED · mother status · day check
          ├─ pre-flight (~60 s, never fatal): send `mother health pending` alerts; for each `needs`
          │     capability `mother health pick` → probe a stale read, heal (reconnect, backup server),
          │     record and alert once a day; writes are never probed. Unhealed = "couldn't check", `partial`
          └─ routines/<id>.md  ── step list, calling skills (mother:brief, mother:todo, …)
              ├─ gather: connectors directly, or one mother:reader per source → `mother items check`
              │     a call that fails mid-run: `health classify` + `record`, `pick` again, retry once there
              ├─ judge and write: workspace files (journal/, todos/, teams/…)
              ├─ check: mother:critic on any draft another person will see
              ├─ send: MCP / gh call ──▶ hooks/guard.py decides allow · ask · deny, logs to .mother/guard.log
              └─ one line in runs.md
  └─ Stop hook: mother sync → secret scan → commit → pull --rebase → push
```

An interactive request ("brief me") enters at the skill and skips the dispatcher.

The daily `doctor` routine runs before the first routine of the day: it re-tests every connector read-only,
heals what it can without a human (re-test, reconnect, switch server), merges the result through
`mother health merge`, and alerts the manager once per broken connector. Sign-ins and write checks happen
only in an interactive `/mother:doctor`.

## How a channel reply flows

```
/loop /mother:channels   (a Claude session, cheap model)
  ├─ mother channels plan          → what to poll
  ├─ skill reads chat              → mother channels triage (stdin JSON) → actions
  ├─ mother channels run --action  → headless `claude -p` with MOTHER_CHANNEL_ROLE=manager|team|anyone
  │                                   the guard narrows that session: no outbound, limited reads
  ├─ answer → guard grant written; skill posts the signed reply │ draft → manager's DM │ skip
  └─ mother channels done          → state in .mother/channels.json, audit in .mother/channels.log
```

## How the hub flows

```
launchd: mother hub watch   (lib/hub.py + hub_mcp.py + hub_sources.py, no model, read-only allow-list)
  ├─ per connection: read-only MCP over stdio (or `gh`) → adapter → deterministic prefilter
  ├─ appends one line per changed thing to .mother/hub/inbox.jsonl; heartbeat in watcher.json
  └─ records nothing in capabilities.json: watcher.json plus one inbox `error` line are the record
pinned desktop session (/mother:hub): Monitor on `tail --follow` wakes it on each new line; the cron fallback wakes it every 3 minutes and ends at once when `tail --once` prints nothing
  ├─ mother hub decide   → log / Now row only │ send_message into the thing's session │ proposal card
  ├─ session map: .mother/hub/sessions.json (thing → session; DMs keyed per conversation)
  ├─ overflow proposals wait in `pending`, raised on later wakes (`mother hub pending raised`); pending and unseen expire after update_ttl_hours working hours
  └─ in-app session tools pass the guard only inside what the hub manages (messages into live sessions.json sessions, own_initiative cards); anything else asks; sub-agents and channel sessions are denied
Now panel: Hub section (status, sessions, updates); the Open button fills the prompt to open the session and marks it seen; the manager sends it (desktop draws only https links)
```

Accepted gaps (also in CONTRACT §5e):

- The tracker adapter misses edits to fields other than status, assignee, priority and comments (labels, summary, description), and without a changelog it cannot tell the manager's own changes to those four from anyone else's: they come through as updates (a comment by the manager does resolve).
- An unthreaded reply in a watched channel does not resolve the message it answers; only a reply in the thread (or in a DM) does.
- Replies in threads the watcher does not follow (ones the manager has not posted in) are not read.

## Invariants the code relies on

- **One writer.** Only MOTHER (the routine or interactive session) judges, writes and posts. Sub-agents and
  channel sessions read and hand back data; their output approves nothing.
- **Capabilities before claims.** A routine checks each capability it needs before using it. A source it
  couldn't reach is reported as "couldn't check", never as quiet.
- **One writer for connector state.** Only `mother health` writes `capabilities.json` and `health.md`.
  Skills and routines call it; they never edit the file. The server order in `servers.<cap>` is a
  preference that a failure never reorders, so a recovered server is used again.
- **Evidence or silence.** Anything shown to the manager cites a link; `lib/items.py` enforces this for
  readers.
- **Fail closed.** No workspace, a bad config or a crash in the guard means *ask*, never *allow*. The guard
  never answers *allow* for a call it doesn't recognise.
- **Tighten only.** Workspace policy and personal rules may narrow the plugin's defaults, never widen them.
- **Machine-local vs synced.** `.mother/` (grants, ledger, guard log, channel state, audio, eval pairs) is
  gitignored and per machine; everything else in the workspace syncs.

## Finding things

- The workspace path: `$MOTHER_WORKSPACE`, else `$MOTHER_STATE_DIR/workspace` (default `~/.mother/workspace`).
- Plugin files from a skill: `${CLAUDE_PLUGIN_ROOT}/…`. Routines never hard-code a path.
- Environment switches: `MOTHER_DRY_RUN` (guard denies all outbound), `MOTHER_NO_SYNC` (sync is a no-op),
  `MOTHER_CHANNEL_ROLE` / `MOTHER_CHANNEL_SERVERS` (channel session limits); for tests, `MOTHER_HOSTNAME`
  (this machine's name), `MOTHER_NOW` (a fixed clock for `runs` and `health`) and `MOTHER_SAY_BIN` (the
  speech binary).
