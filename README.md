# MOTHER

**M**anager's **O**perations, **T**asks, **H**andoffs, **E**xecution & **R**eporting.

A chief of staff for people managers, built on [Claude Code](https://claude.com/claude-code).

> Yes, it's named after the ship's computer in *Alien*. Unlike that one, this Mother never treats the crew as
> expendable: it works for the manager, in the open, and anything another person sees carries its signature.

> **Status: pre-alpha, one pilot.** All the pieces exist: CLI, sync, approval guard, setup, doctor,
> routines, the meeting deck and the sprint plan, covered by 154 automated tests. One manager's workspace has
> run on it since 2026-10-23; nobody else has yet. Expect rough edges, and please report them. See
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
- Calls that aren't outbound are untouched; your normal permission rules apply. (A second hook on reads exists
  only for channel reply sessions and exits immediately everywhere else.)

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
| brief | 08:45 workdays | Today's calendar, mail sorted into to-respond / FYI / noise, overdue open loops, and a numbered triage (`me` / `prep` / `you`) you hand back with "go 1 3". With `"draft_replies": true` it also leaves reply drafts in your mail |
| digest | 09:00 workdays | Per team, opening with the decision queue |
| dots | 13:00 workdays | Connections across threads, and suggested actions for you and for the agent |
| brief, spoken | with the brief, if `routines.brief.audio` is on | A 60–90 second audio version (macOS voices, or the Kokoro neural voice with `"engine": "kokoro"` after `mother tts setup`; `"language": "he"` for Hebrew) |
| commitments | 16:30 workdays | What you promised, what you're waiting on, and asks with no reply after 3 days; answer `nudge 3` for a draft follow-up |
| one-on-one | 07:30 workdays | Prep for each of today's 1:1s (a shareable agenda kept apart from private prep) and a note when a report's 1:1s have lapsed |
| reflect | 21:30 daily | Updates memory and open loops from the day; flags stale items |
| doctor | weekly | Re-tests connectors, so a broken one shows as "couldn't check" |
| meeting prep / recap | around each team's weekly meeting | The deck, then a recap draft |

You can also just ask: "brief me", "what's open", "remember that …", "add a todo …", "take notes on the
design review", "prep my 1:1 with Sam", "what did I promise", "set up a new project". Each one routes to a
skill: `brief`, `todo`, `remember`, `one-on-one`, `commitments`,
`meeting`, `project`, `team` and so on.

### Replying in team channels

Each channel in `config.json` has a reply mode: `off`, `watch` (read for digests only), `mention` (answers in
the thread when someone addresses it by name, e.g. "Agnes, is the deploy green?"), `draft` (drafts replies to
your DM; you answer `send`, `send: <edit>` or `no`) or `auto` (answers the topics you list in `auto_topics`
itself and drafts the rest). Who may trigger a `mention` channel is its `trigger`: `manager`, `team` or
`anyone`. Start the loop in a Claude Code session, on a cheap model:

```
/loop /mother:channels
```

Every reply is signed, stays in its thread, and is checked by the guard. When a teammate asks, the answer is
written by a read-only session that can't see your mail, calendar, DMs or private notes. People in the channel
can say `Agnes, mute` (this thread), `Agnes, stop` (this channel) or `Agnes, pause` (every channel), or 👎 a
reply. Only you turn it back on. After 10 unedited drafts on one topic it proposes answering that topic itself;
you decide by editing the config.

Useful commands in your terminal:

| Command | What |
|---|---|
| `mother status` | Validate `config.json` and show workspace, doctor and home-machine state |
| `mother home show` | Which machine runs the routines |
| `mother sync` | Commit and push the workspace now (also runs automatically at the end of each turn) |
| `mother register` | Print the scheduled-task definitions for the enabled routines |
| `mother runs` | Watchdog: which routines missed their last slot or failed (the brief and doctor flag these too) |
| `mother tts setup` | Install the optional Kokoro neural voice for the spoken brief (~330 MB in `~/.mother/tts`, English only) |
| `mother channels status` | Reply loop: paused or stopped channels, pending drafts, streaks |
| `mother channels pause` / `resume` | Stop or restart replies in every channel |

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
| `skills/` | `setup`, `doctor`, `routine`, `brief`, `digest`, `dots`, `meeting`, `channels`, `reflect`, `todo`, `project`, `team`, `remember` |
| `lib/channels.py` | `mother channels`: the reply-mode engine behind `mother:channels` |
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
