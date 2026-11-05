# AGENTS.md — working on the MOTHER plugin

The single source of truth for any coding agent (Claude Code, Codex, Cursor, Copilot, Gemini) changing **this
repo**. `CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md` and `.cursor/rules/mother.mdc` are pointers
to this file and hold no rules of their own. Change rules here.

This is not the persona file. The agent that serves a manager reads `templates/workspace/AGENTS.md`, which
`mother init` copies into each private workspace. Don't edit that template when you mean this file.

## What this repo is

MOTHER is a Claude Code plugin: a chief of staff for people managers. A watcher with no model polls the
manager's chat, tracker, code host, mail, calendar and WhatsApp into a local inbox file. One visible desktop
session leads: it triages each inbox line with one short model call and records the decision in code. The Chief
pane (`/chief`) shows what needs the manager. Pressing an action opens a case session that owns that item until
it is closed. Scheduled routines (brief, digest, dots, …) run as desktop scheduled tasks. An approval guard hook
decides, in code, what may reach another person.

The repo is **public** and holds code only. Each manager's data lives in a separate private workspace.

Read next, as the task needs:

1. [ARCHITECTURE.md](ARCHITECTURE.md): code map, diagrams, how a run and an inbox item flow.
2. [docs/CONTRACT.md](docs/CONTRACT.md): the build contract. **Binding. Change it first, then the code.**
3. [docs/DESIGN.md](docs/DESIGN.md): the product reasoning behind the contract.
4. [docs/ROADMAP.md](docs/ROADMAP.md): what's built, what's next, what is out of scope on purpose.
5. [docs/GUIDE.md](docs/GUIDE.md) and [README.md](README.md): how a manager installs and operates it.

## Map: one job per directory

| Path | Its one job |
|---|---|
| `.claude-plugin/` | The plugin manifest and the marketplace entry. The version lives here and in `bin/mother` |
| `bin/mother` | The CLI: owns workspace state, sync and git; dispatches verbs to `lib/` |
| `lib/` | Deterministic engines behind the CLI (hub, chief, floor, now, health, channels, WhatsApp). Python 3.9 stdlib |
| `hooks/guard.py`, `hooks/guard_fast.py` | The approval guard: allow, ask or deny for every tool call that reaches someone |
| `hooks/register.tsx` and the other `.ts` / `.tsx` files in `hooks/` | The hooks module (mod API): the Chief pane, the Now panel, cases, relay, the floor |
| `hooks/hooks.json` | Wires the module, the guard (PreToolUse) and `mother sync` (Stop, SessionEnd) |
| `types/` | Types for the module's state and the JSON the CLI prints to it |
| `skills/` | What the model does for a request (`mother:<name>`); Markdown |
| `routines/` | What one scheduled run does; Markdown with frontmatter |
| `agents/` | Read-only sub-agents: `reader`, `critic` |
| `rules/` | Policy text the skills cite. When a rule and the guard disagree, the guard wins |
| `assets/` | The meeting deck and sprint plan templates, the bundled portrait |
| `templates/workspace/` | The skeleton `mother init` copies into a new workspace, the persona `AGENTS.md` included |
| `bridge/whatsapp/` | The optional send-only WhatsApp bridge (Node) |
| `tests/` | stdlib `unittest` for the Python side; fixtures |
| `docs/` | CONTRACT, DESIGN, ROADMAP, GUIDE; `docs/assets/` holds the README's mock images |

## Running tests fast

```bash
claude plugin validate .                    # manifests, hooks.json, the module's static scan (seconds)
claude plugin test .                        # the module's tests: hooks/chief.test.tsx, hooks/register.test.tsx
python3 -m unittest tests.test_docs tests.test_packaging   # under 2 s; run after any doc or new-file change
python3 -m unittest tests.test_guard        # one module, while iterating
python3 -m unittest tests.test_guard.TestGrants            # one class (or .TestGrants.test_name)
python3 -m unittest discover tests          # everything: over 1,000 tests, many minutes. Run it last, once
```

Pick the module that covers what you touched:

| You changed | Run | Speed |
|---|---|---|
| A doc, or added or moved a file | `tests.test_docs`, `tests.test_packaging` | seconds |
| `lib/chief.py`, `lib/floor.py` | `tests.test_chief`, `tests.test_floor` | seconds |
| `hooks/chief.tsx`, `hooks/pane_ui.tsx`, `hooks/pane_tabs.tsx`, `hooks/cos.ts`, `hooks/floor.ts`, `hooks/register.tsx` | `claude plugin test .` | under a minute |
| `lib/hub_sources.py`, `lib/hub_mcp.py` | `tests.test_hub_sources` | ~10 s |
| `lib/hub.py` | `tests.test_hub`, `tests.test_hub_e2e` | slow |
| `lib/now.py` | `tests.test_now` | ~15 s |
| `lib/inbox.py`, `lib/items.py`, `lib/evals.py` | `tests.test_inbox`, `tests.test_items`, `tests.test_eval` | seconds |
| `lib/health.py` | `tests.test_health` | slow |
| `lib/channels.py`, `lib/whatsapp.py` | `tests.test_channels`, `tests.test_whatsapp` | slow |
| `bin/mother` | `tests.test_cli`, `tests.test_cli_contract` | slow |
| `hooks/guard.py`, `hooks/guard_fast.py`, `hooks/hooks.json` | `tests.test_guard`, `tests.test_packaging`, then `tests.test_redteam` | slow; redteam alone is over 2 minutes |
| `assets/deck/`, `assets/plan/` | `tests.test_deck`, `tests.test_plan` | seconds |

A throwaway workspace for manual runs. `MOTHER_STATE_DIR` keeps `init` from repointing the real
`~/.mother/workspace`:

```bash
export MOTHER_STATE_DIR="$(mktemp -d)"
python3 bin/mother init "$MOTHER_STATE_DIR/ws"
python3 bin/mother status
```

There is no build step, no package manager and no linter config. To try an unreleased change end to end:
`claude --plugin-dir /path/to/this/checkout`.

## Rules that bite

- **Public repo: nothing real.** No company names, people, ids, hostnames, internal URLs or vendor-specific MCP
  prefixes in code, skills, rules, tests, docs or examples. Use `example.com`, `example-org/example-api`,
  `PLAT-123`, `C00000001`, `Agnes`, `Sam`, `Dana`.
- **Runtime text says "the manager".** Skills, routines, rules, prompts and CLI output never name a person; the
  user is "the manager" (docs addressed to the reader may say "you").
- **Python 3.9, stdlib only.** The hook and CLI run on the stock macOS Python. No `pip install`, no `match`, no
  `X | Y` type unions, no `tomllib`. Config is JSON for this reason.
- **The contract is binding.** Any change to a file layout, a config key, a CLI command, a guard decision or a
  `capabilities.json` value updates `docs/CONTRACT.md` in the same change.
- **The docs are tested.** `tests/test_docs.py` fails when AGENTS.md or ARCHITECTURE.md names a repo path that
  doesn't exist, when ARCHITECTURE.md misses a skill, a routine or a `lib/` module, or when `CLAUDE.md` loses
  its `@AGENTS.md` line.
- **Every runtime file is tracked.** `tests/test_packaging.py` fails if anything under `agents/ bin/ lib/ hooks/
  skills/ routines/ rules/ assets/ templates/ types/ bridge/ .claude-plugin/` isn't in git. `git add` new files
  by path.
- **Vendor-neutral.** Skills talk in capabilities (`chat.read`, `tracker.create`), never a product name; the
  connector is resolved from `capabilities.json` at run time. Product-specific tool names live only in
  `lib/hub_sources.py`.
- **Nothing goes around the guard.** No skill, routine or script posts, sends or writes to a tracker by a path
  the guard can't see. New outbound behaviour means a guard rule and a guard test.
- **Untrusted text is data.** Anything read from a source, a session tail or a model reply goes into a prompt as
  JSON under a fixed note, is parsed strictly, cleaned (`safe`, `oneLine` in `hooks/shared.ts`) and drawn as
  plain text. A model's verdict never decides anything by itself: code does.
- **People, not scores.** Nothing that assesses, scores or reads the mood of a person. See ROADMAP's "Out of
  scope, on purpose".
- **`capabilities.json` is written only by `mother health`.** Skills and routines call `mother health record` /
  `merge`; they never edit the file.
- **Skill descriptions are routing.** A `SKILL.md` `description` is matched against every request in every
  session where the plugin is on. Keep it short, specific, phrased "Use when / Use for …".

### Mod API gotchas (the `.ts` / `.tsx` files in `hooks/`)

Each of these cost a past change real time. The API is early access; the engine's static scan is strict.

- **One unmatched hook per event per module.** The engine takes a single hook without a matcher for each event
  (`session.start`, `prompt.submit`, …). `hooks/register.tsx` owns `session.start` and calls `startChief`; add
  to an existing handler instead of registering a second one.
- **`read($, atom)` needs the atom declared in the reading file.** The scan resolves an atom from a `const` in
  the file that uses it, so the same atom is declared again in `hooks/chief.tsx`, `hooks/pane_tabs.tsx` and
  `hooks/floor.ts`. Importing the atom is not enough.
- **Drawing is pure.** The engine refuses a state write made while `ui.render` is dispatched. Load and update
  on the timer pass (`chiefPass`), a button press or a command; never inside a draw.
- **A comment inside JSX children renders as text.** Put comments above the element, not between its children.
- **`$` can't be passed through callbacks.** The scan follows `$` only into functions declared at a file's top
  level (`function f($, …)`, not an arrow stored in a variable, not a closure handed to a timer). This is why
  `hooks/register.tsx` owns the timer and calls `chiefPass`.
- **Tests register one `prompt.submit` handler.** A test file's harness has a single `prompt.submit` hook
  beneath the module's; extend it, don't add another (`hooks/chief.test.tsx`, top of file).
- **Detach slow work from a turn.** Model calls from `turn.complete` or `prompt.submit` are `void …catch()`:
  they must never delay or fail the manager's turn.

## Gotchas while you work here

- **The guard is live.** If MOTHER is installed, `hooks/guard.py` runs on your own tool calls in this repo. A
  shell command that could write the *installed* guard's files (under `.claude/plugins/`) or the workspace's
  `config.json` / `.mother/` state will ask. Any `gh … delete` is denied; ask the user to run it.
- **Two copies of the plugin.** The installed plugin lives under `~/.claude/plugins/…`; your checkout is
  separate. Editing the checkout changes nothing a running routine sees until a release is installed, or you
  run with `--plugin-dir`.
- **Untracked folders are the user's.** `docs/drafts/` and `docs/superpowers/` hold private notes. Never
  `git add -A` / `git add .` here; add files by path.
- **Never touch the real workspace.** Tests and manual runs set `MOTHER_STATE_DIR` (and drop
  `MOTHER_WORKSPACE`) to a temp dir, as `CliTest.setUp` does. Otherwise `mother init` / `join` repoint
  `~/.mother/workspace`, and `sync` commits and pushes the manager's real workspace.
- **Workspace data is not test data.** Don't copy anything from a real workspace into fixtures, even redacted.

## Checklists

### Add a skill (example: `skills/todo/SKILL.md`)

1. Create the skill's folder under `skills/` with a `SKILL.md`: frontmatter `name` and a "Use when …"
   `description`, then plain second-person steps. Files it creates in a workspace go in a `templates/` folder
   beside it.
2. Add the name to the skills list in `ARCHITECTURE.md` (tested), to CONTRACT §7 and to the README's Skills table.
3. If it sends anything: a guard rule and a test in `tests/test_guard.py`.
4. `git add` the files, then `python3 -m unittest tests.test_docs tests.test_packaging`.

### Add a routine (example: `routines/commitments.md`)

1. Create the routine's file under `routines/` with frontmatter `id`, `team_scoped`, `description`, `config`,
   `default`, `needs`, `writes`, `sends`, then the step list. A scheduled run starts with no memory of any
   conversation, so the file must carry everything it needs.
2. Its config key: `validate()` in `bin/mother`, CONTRACT §2 and §3, `templates/workspace/config.example.json`.
3. Every shell command it runs must pass the guard's allow-list unasked: add them to
   `tests/fixtures/routine_commands.txt` (checked by `tests/test_redteam.py`).
4. Add the id to the routines list in `ARCHITECTURE.md` (tested) and the README's Routines table.
5. `python3 -m unittest tests.test_docs tests.test_packaging tests.test_cli`.

### Add a CLI verb (example: `lib/floor.py`, the smallest module)

1. A new top-level command: a `cmd_<name>` in `bin/mother`, an entry in `COMMANDS`, a line in the docstring at
   the top of the file. A verb of an existing command: that module's `VERBS` / `USAGE` and its `main`.
2. State is written atomically (temp file, then rename) and under the hub lock when another process reads it.
3. CONTRACT (the section for that module), `docs/GUIDE.md` command table, a test in the module's test file.
4. If the module is new: a row in ARCHITECTURE's code map (tested).

### Add a pane tab (example: the Health tab, `healthSection` in `hooks/pane_tabs.tsx`)

1. Add the id to `TabId` and `TAB_IDS` in `hooks/pane_ui.tsx`.
2. Write the section as a top-level `function` in `hooks/pane_tabs.tsx` that takes what it needs and returns
   elements. It reads no file and writes no state while drawing.
3. Add one row to the `tabs` list in `paneTree` (`hooks/chief.tsx`) and a count in `tabCounts`.
4. Add the id to the `/chief tab …` usage text and the command description in `hooks/chief.tsx`.
5. New data comes through a `mother …` verb and its JSON shape in `types/index.d.ts`, loaded on the timer pass.
6. Tests in `hooks/chief.test.tsx`; CONTRACT §5g; the README's pane section.

### Where other changes go

| You want to… | Touch | And |
|---|---|---|
| Change what may go out | `hooks/guard.py` | `rules/outbound.md`; CONTRACT §5; `tests/test_guard.py`; `tests/test_redteam.py` |
| Add a config key | `validate()` in `bin/mother` | CONTRACT §3; `templates/workspace/config.example.json` |
| Change triage, cases, relay or the floor | `hooks/chief.tsx`, `hooks/cos.ts`, `hooks/floor.ts`, `lib/chief.py`, `lib/floor.py` | CONTRACT §5g; `skills/cos/SKILL.md` if the session's contract changes |
| Change what the watcher reads | `lib/hub_sources.py` | CONTRACT §5e; `tests/test_hub_sources.py` |
| Change connector health or alerts | `lib/health.py` | CONTRACT §4; `tests/test_health.py` |
| Add a sub-agent | a file under `agents/` | the guard's `agent_type` handling; CONTRACT §5c |
| Change a policy the agent cites | a file under `rules/` | the guard, if the rule is enforced there |

## Writing style for skills, routines and rules

Read two neighbouring files before writing a new one and match them: plain second-person instructions, short
sections, tables for anything with more than three cases, `${CLAUDE_PLUGIN_ROOT}` for plugin paths,
`"${CLAUDE_PLUGIN_ROOT}/bin/mother" path` for the workspace (its output written out literally: in the workspace
the guard's shell allow-list asks before any `$` expansion, `$(…)`, glob or unlisted program, CONTRACT §5).

## Commits, PRs and releases

- Branch from `main` as `feat/…` or `fix/…`; PRs merge with a merge commit.
- Commit subject: `<area>: <what changed, in plain words>` in lower case, e.g.
  `guard: let read-only gh/glab api graphql queries through`.
- **A release bumps the version in two places:** `version` in `.claude-plugin/plugin.json` and `VERSION` in
  `bin/mother`. The PR title carries the headline and the version, e.g. `Chief pane: Health and Team tabs
  (0.20.0)`. No git tags.
- Don't push, open a PR or release unless the user asks.

## What not to do

- **Don't rebuild hidden workers or a long-lived chief session.** Until 0.18.0 a background session kept alive
  by launchd and tmux, headless `claude -p` workers and a decision batch to the manager's DM did the work. They
  were removed on purpose: work happens in desktop sessions the manager can see. The watcher stays model-free.
- **Don't put workspace data in this repo.** No config, people, journal, memory or todos from any manager, not
  as fixtures, not as examples. A workspace never lives inside the plugin repo.
- **Don't let a model decide.** A model call returns data; code records the decision and falls back to keeping
  the item in front of the manager.
- **Don't widen what goes out unasked** without a contract change, a guard rule and a red-team row.
- **Don't add a dependency.** Python stays stdlib-only; the module imports only `claude-code` and its own files.
- **Don't edit `templates/workspace/AGENTS.md` to change how agents work on this repo,** or this file to change
  the manager's persona.
