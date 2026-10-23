# MOTHER

**M**anager's **O**perations, **T**asks, **H**andoffs, **E**xecution & **R**eporting.

A chief of staff for people managers, built on [Claude Code](https://claude.com/claude-code).

> Yes, it's named after the ship's computer in *Alien*. Unlike that one, this Mother never treats the crew as
> expendable: it works for the manager, in the open, and anything another person sees carries its signature.

> **Status: pre-alpha, one pilot.** All the pieces exist: CLI, sync, approval guard, setup, doctor,
> routines and the meeting deck, covered by 60 automated tests. One manager's workspace has run on it since
> 2026-10-23; nobody else has yet. Expect rough edges, and please report them. See
> [docs/CONTRACT.md](docs/CONTRACT.md), [docs/DESIGN.md](docs/DESIGN.md) and [docs/ROADMAP.md](docs/ROADMAP.md).

## What it is

MOTHER gives a manager of any kind (engineering, product, operations, support) **their own named agent**, for
example "Agnes (AI) · on behalf of Dana". The agent:

- **Knows your world.** A setup interview *discovers* your people, teams, projects, channels, trackers, repos
  and recurring meetings from your connectors, then shows you a draft to confirm. You don't have to type it in.
- **Proves its tools work.** A connector *doctor* makes live read and (opt-in) write calls, then records what
  actually works. Routines check that record before they run, so a broken Slack connection shows up as
  "couldn't check", never as "all quiet".
- **Runs your routines.** A daily brief, a team digest that opens with a short *decision queue* you answer
  in one line, a weekly team-meeting deck with a **meeting mode** (owners, decisions, tickets from the room)
  that writes outcomes back to your tracker, a recap, and a daily *connect-the-dots* pass that suggests
  actions for you and for itself.
- **Remembers.** People, commitments, decisions and open loops live as plain files in a private workspace,
  and a nightly reflection keeps that memory current.

## Principles

1. **Plugin = code, workspace = data.** The plugin is shared and versioned. Each manager's workspace is
   private, local by default, with an optional git remote.
2. **Disclosure, always.** The agent acts through the manager's own accounts, so every outbound message,
   comment or ticket is signed: `✨ Agnes (AI) · on behalf of Dana`.
3. **Approvals live in code, not just in the prompt.** A pre-tool hook decides what may go out without
   asking (your own DM, drafts), what needs your yes (anything another person sees), and what never happens
   (credentials, deletions, payments). It also provides a kill switch.
4. **Evidence or silence.** Answers cite a source. With no source, it asks you instead of guessing.
5. **Earned autonomy.** Channels start at `draft`. The agent can *propose* more autonomy after a track record,
   but only the manager can grant it.
6. **People, not scores.** It reports work items, never assessments, never mood. The team should know the
   agent exists and what it reads.

## Before you install

- **Claude Code** (the desktop app if you want scheduled routines: they run only while the app is open,
  and a missed run fires on the next launch).
- **Python 3.9+** and **git** on your PATH. `gh` if you want GitHub in the brief.
- **Connectors** for whatever you use: chat (Slack or Teams), mail, calendar, a tracker (Jira, Linear,
  Monday, or none) and a code host (optional). MOTHER doesn't ship connectors. Setup finds the ones you have,
  and the doctor tests them.
- A **private** git repo for your workspace if you want backup or a second machine. Never a public one:
  the workspace holds your notes about people and open loops.

## What installing changes

The approval guard is a hook, so it runs in **every** Claude Code session, not only in the workspace:

- Any call that reaches another person (a chat post, an email send, a tracker write, a `gh` write) asks for
  your confirmation, even in an unrelated repo. Outside the workspace's green list nothing is pre-approved.
- Credential entry, payments and deletions are refused outright, including `gh … delete` and
  `gh api -X DELETE` in any repo. Run those yourself in a terminal.
- Calls that aren't outbound are untouched; your normal permission rules apply.

The Stop hook runs `mother sync`, which only acts on your workspace. Before you have one, it does nothing.

## Install

In Claude Code:

```
/plugin marketplace add shayke-cohen/mother
/plugin install mother@mother
/mother:setup
```

Setup asks where to put the workspace, discovers your people, teams, channels and meetings, shows you a
draft config, and writes nothing until you confirm. Then it runs the doctor, sets this machine as the home
machine, registers the routines, and runs `mother link` so the `mother` command works in your own terminal.
To do that step by hand:

```
python3 ~/.claude/plugins/marketplaces/mother/bin/mother link
```

It symlinks `mother` into `~/.local/bin`. Pass another directory if that one isn't on your PATH.

**Another computer, same workspace:** install the plugin, run `mother link` as above, then
`mother join <your-private-repo-url>` and `/mother:doctor`. Only the *home* machine runs routines
(`mother home show`), so don't register them on the second one.

## Using it

Day to day you mostly read what it sends you and answer in one line. The default schedule is set in
`config.json` → `routines`, in your timezone:

| Routine | When (default) | What you get |
|---|---|---|
| brief | 08:45 workdays | Today's calendar, what's waiting on you, overdue open loops |
| digest | 09:00 workdays | Per team, opening with the decision queue |
| dots | 13:00 workdays | Connections across threads, and suggested actions for you and for the agent |
| reflect | 21:30 daily | Updates memory and open loops from the day; flags stale items |
| doctor | weekly | Re-tests connectors, so a broken one shows as "couldn't check" |
| meeting prep / recap | around each team's weekly meeting | The deck, then a recap draft |

You can also just ask: "brief me", "what's open", "remember that …", "add a todo …", "take notes on the
design review", "set up a new project". Each one routes to a skill: `brief`, `todo`, `remember`,
`meeting`, `project`, `team` and so on.

Useful commands in your terminal:

| Command | What |
|---|---|
| `mother status` | Validate `config.json` and show workspace, doctor and home-machine state |
| `mother home show` | Which machine runs the routines |
| `mother sync` | Commit and push the workspace now (also runs automatically at the end of each turn) |
| `mother register` | Print the scheduled-task definitions for the enabled routines |

### Pausing, stopping, uninstalling

- **Kill switch:** `touch "$(mother path)/.mother/PAUSED"` blocks every outbound call until you delete the
  file. Routines still read and still write to the workspace.
- **Stop one routine:** remove it from `config.json` → `routines`, then ask `/mother:setup` to update the
  scheduled tasks (it disables the task rather than deleting it).
- **Uninstall:** disable the `Mother:` tasks in the scheduled tasks list, then
  `/plugin uninstall mother@mother` and `rm ~/.local/bin/mother`. Your workspace folder is untouched.

## What's inside

| Path | What |
|---|---|
| `bin/mother` | CLI: `init`, `join`, `sync`, `scan`, `home`, `status`, `register`, `path`, `link` (Python 3.9 stdlib) |
| `hooks/guard.py` | Approval guard: allow / ask / deny per policy, kill switch, grants for unattended runs |
| `skills/` | `setup`, `doctor`, `routine`, `brief`, `digest`, `dots`, `meeting`, `reflect`, `todo`, `project`, `team`, `remember` |
| `routines/` | What each scheduled run does |
| `assets/deck/` | Weekly meeting deck with meeting mode |
| `rules/` | Default policies: outbound, privacy, memory, todos, projects, teams |
| `templates/workspace/` | A new workspace's skeleton |

## Development

```
python3 -m unittest discover tests
```

## License

MIT
