# AGENTS.md — working on the MOTHER plugin

This file is for coding agents (Claude Code, Codex, Cursor, …) changing **this repo**, the MOTHER plugin.
It is not the persona file: the agent that serves a manager reads `templates/workspace/AGENTS.md`, which
`mother init` copies into each private workspace. Don't confuse the two, and don't edit that template when
you mean this file.

Read next, in this order, as the task needs:

1. [ARCHITECTURE.md](ARCHITECTURE.md): code map, how a run flows, where each kind of change goes.
2. [docs/CONTRACT.md](docs/CONTRACT.md): the build contract. **Change it first, then the code.**
3. [docs/DESIGN.md](docs/DESIGN.md): the product reasoning behind the contract.
4. [docs/ROADMAP.md](docs/ROADMAP.md): what's built, what's next, and what is out of scope on purpose.

## Commands

```bash
python3 -m unittest discover tests          # whole suite: ~240 tests, ~4-5 minutes (channels, guard, health, cli are the slow ones)
python3 -m unittest tests.test_guard        # one module, while iterating
python3 -m unittest tests.test_guard.TestGrants   # one class (or .TestGrants.test_name for one test)

# A throwaway workspace. MOTHER_STATE_DIR keeps `init` from repointing the real ~/.mother/workspace.
export MOTHER_STATE_DIR="$(mktemp -d)"
python3 bin/mother init "$MOTHER_STATE_DIR/ws"
python3 bin/mother status
```

There is no build step, no package manager and no linter config. Python 3.9 stdlib only.

To try an unreleased change end to end in Claude Code: `claude --plugin-dir /path/to/this/checkout`
(`mother eval brief --plugin-dir …` does the same for an A/B run).

## Rules that are easy to break

- **Contract first.** Any change to a file layout, a config key, a CLI command, a guard decision or a
  `capabilities.json` value updates `docs/CONTRACT.md` in the same change.
- **Stdlib only, Python 3.9.** The hook and CLI run on the stock macOS Python. No `pip install`, no
  `match`, no `X | Y` type unions, no `tomllib`. Config is JSON for this reason.
- **No real names.** No company names, people, IDs, hostnames or vendor-specific MCP prefixes in code,
  skills, rules, tests or examples. Use `example.com`, `PLAT`, `C00000001`, `Agnes` / `Dana`.
- **Vendor-neutral.** Skills talk in capabilities (`chat.read`, `tracker.create`), never "Slack" or "Jira";
  the connector is resolved from `capabilities.json` at run time.
- **Nothing goes around the guard.** No skill, routine or script posts, sends or writes to a tracker by any
  path the guard can't see. New outbound behaviour means a guard rule and a guard test.
- **People, not scores.** Nothing that assesses, scores or reads the mood of a person. See ROADMAP's
  "Out of scope, on purpose" before proposing a feature in that area.
- **`capabilities.json` is written only by `mother health`.** A skill or routine that learns something
  about a connector calls `mother health record` / `merge`; it never edits the file, which other runs
  read under a lock.
- **Every runtime file is tracked.** `tests/test_packaging.py` fails if anything under `agents/ bin/ lib/
  hooks/ skills/ routines/ rules/ assets/ templates/ .claude-plugin/` isn't in git. `git add` new files.
- **Skill descriptions are routing.** A `SKILL.md` `description` is matched against every user request in
  every session where the plugin is on. Keep it short, specific, and phrased as "Use when / Use for …".

## Gotchas

- **The guard is live while you work.** If MOTHER is installed, `hooks/guard.py` runs on your own tool
  calls in this repo. A shell command that could write the *installed* guard's files (under
  `.claude/plugins/` or `${CLAUDE_PLUGIN_ROOT}`) or the workspace's `config.json` / `.mother/` state will
  **ask**; the same names in this checkout pass. With `--plugin-dir` this checkout *is* the running copy, so
  edit with the Edit/Write tools. Any `gh … delete` is denied outright; ask the user to run it.
- **Two copies of the plugin.** The installed plugin lives under `~/.claude/plugins/…`; your checkout is
  separate. Editing the checkout changes nothing a running routine sees until a release is installed, or
  you run with `--plugin-dir`.
- **Untracked folders are the user's.** `docs/drafts/` and `docs/superpowers/` hold private notes and
  plans. Never `git add -A` / `git add .` here; add files by path.
- **Never touch the real workspace.** Tests and manual runs set `MOTHER_STATE_DIR` (and drop
  `MOTHER_WORKSPACE`) to a temp dir, as `CliTest.setUp` does. Otherwise `mother init` / `join` repoint
  `~/.mother/workspace`, and `sync` commits and pushes the manager's real workspace.
- **Workspace data is not test data.** Don't copy anything from a real workspace (config, people, journal)
  into fixtures, even redacted.

## Where a change goes

| You want to… | Touch | And |
|---|---|---|
| Add or change what a scheduled run does | `routines/<id>.md` | frontmatter `needs`/`writes`/`sends`; CONTRACT §2 |
| Add a user-facing ability | `skills/<name>/SKILL.md` (+ `templates/`) | README "Using it"; CONTRACT §7 |
| Add a CLI command | `bin/mother` (or a `lib/` module it dispatches to) | docstring at top of `bin/mother`; `tests/test_cli.py`; README command table |
| Change what may go out | `hooks/guard.py` | `rules/outbound.md`; CONTRACT §5; `tests/test_guard.py` |
| Add a config key | `validate()` in `bin/mother` | CONTRACT §3; `templates/workspace/config.example.json` |
| Change connector health, server choice or alerts | `lib/health.py` | CONTRACT §4; `tests/test_health.py`; the pre-flight in `skills/routine/SKILL.md` and `skills/doctor/SKILL.md` if callers change |
| Add a sub-agent | `agents/<name>.md` | guard's `agent_type` handling; CONTRACT §5c |
| Change a policy the agent cites | `rules/<name>.md` | the guard, if the rule is enforced there |

## Writing style for skills, routines and rules

Read two neighbouring files before writing a new one and match them: plain second-person instructions,
short sections, tables for anything with more than three cases, `${CLAUDE_PLUGIN_ROOT}` for plugin paths,
`"${CLAUDE_PLUGIN_ROOT}/bin/mother" path` for the workspace (its output written out literally: in the workspace the guard's shell allow-list asks before any `$` expansion, `$(…)`, glob or unlisted program, CONTRACT §5). A scheduled run starts with no
memory of any conversation, so a routine file must carry everything it needs.

## Commits, PRs and releases

- Branch from `main` as `feat/…` or `fix/…`; PRs merge with a merge commit.
- Commit subject: `<area>: <what changed, in plain words>` in lower case, e.g.
  `guard: let read-only gh/glab api graphql queries through`. Areas in use: `brief`, `guard`, `eval`,
  `deck`, `plan`, `meeting`, `chat`, `watchdog`, `commitments`, `one-on-one`, `roadmap`, `fix`.
- A release is its own commit on `main`, `release X.Y.Z: <headline>`, that bumps the version in **three**
  places: `.claude-plugin/plugin.json`, `VERSION` in `bin/mother`, and the test count in `README.md`'s status
  line (from `Ran N tests`). The body says what changed for the manager, citing PR numbers.
- Don't push, tag or release unless the user asks.
