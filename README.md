# MOTHER

**M**anager's **O**perations, **T**asks, **H**andoffs, **E**xecution & **R**eporting.

A chief of staff for people managers, built on [Claude Code](https://claude.com/claude-code).

> Yes, it's named after the ship's computer in *Alien*. Unlike that one, this Mother never treats the crew as
> expendable: it works for the manager, in the open, and anything another person sees carries its signature.

> **Status: pre-alpha, one pilot.** All the pieces exist: CLI, sync, approval guard, setup, doctor,
> routines, the meeting deck, the sprint plan and WhatsApp, covered by 834 automated tests. One manager's workspace has
> run on it since 2026-10-23; nobody else has yet. Expect rough edges, and please report them. See
> [docs/CONTRACT.md](docs/CONTRACT.md), [docs/DESIGN.md](docs/DESIGN.md) and [docs/ROADMAP.md](docs/ROADMAP.md).

## What it is

MOTHER gives a manager of any kind (engineering, product, operations, support) **their own named agent**, for
example "Agnes (AI) · on behalf of Dana". The agent:

- **Knows your world.** A setup interview *discovers* your people, teams, projects, channels, trackers, repos
  and recurring meetings from your connectors, then shows you a draft to confirm. You don't have to type it in.
- **Proves its tools work.** A connector *doctor* makes live read and (opt-in) write calls, then records what
  actually works, per server. When one breaks it retries, reconnects or switches to a backup server, and
  walks you through a sign-in when only you can fix it. Routines check that record before they run, so a broken
  Slack connection shows up as "couldn't check", never as "all quiet".
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
- The guard is a Python process started on every tool call, so each call costs about a tenth of a second more.
- Edits to `~/.claude/settings.json`, `settings.local.json`, `~/.claude.json` or anything under
  `~/.claude/plugins/` ask in **every** session, since they could switch the guard off.
- A session that ran `mother hub tail` is treated as a hub: it stays fenced to hub work (see below) even after
  a newer hub starts (the guard remembers the last five), so use a fresh session for anything else.

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

**The Now panel.** The tick's list can sit beside the chat, with a status line
(`◆ 2 on you · 1 neglected`) and Done, Snooze, Make top 3 and Do it buttons. It ships inside the `mother` plugin
(a hooks module beside the guard), so there is nothing more to install. Open it with `/now-panel` (`/now` is
MOTHER's chat skill, which prints the same list in the conversation). It reads `.mother/now.json` on the machine
the tick runs on and makes no network calls.
Its one setting, `mother_bin` (userConfig), is the path to the `mother` script it calls for `mother path` and
the `mother now` edits. Left empty (the default) it runs this plugin's own `bin/mother`, the same version the
hooks run; set it only to point the panel at another copy (a leading `~` is your home folder).

If you installed the panel before it moved into `mother` (the separate `mother-now` plugin), remove that one
once, so the panel isn't loaded twice:

```
/plugin uninstall mother-now@mother
```

**Turn on the hub (optional).** The hub watches your sources and routes updates into your sessions (see "The hub"
below). On the home machine, add a `hub` block to `config.json` with `enabled: true` and one connection per source
you want watched:

```json
"hub": {
  "enabled": true,
  "connections": {
    "chat":     {"command": "npx", "args": ["-y", "mcp-remote", "https://gateway.example.com/chat/mcp"]},
    "mail":     {"command": "npx", "args": ["-y", "mcp-remote", "https://gateway.example.com/mail/mcp"]},
    "tracker":  {"command": "npx", "args": ["-y", "mcp-remote", "https://gateway.example.com/tracker/mcp"]},
    "calendar": {"command": "npx", "args": ["-y", "mcp-remote", "https://gateway.example.com/calendar/mcp"]},
    "code":     {"cli": "gh"},
    "whatsapp": {"local": "whatsapp"}
  }
}
```

The first four are MCP servers the watcher starts itself (here through a gateway proxy; use whatever command
starts yours), `gh` is the code host's command line, and `whatsapp` reads the local WhatsApp database (it needs
`whatsapp.enabled` and `whatsapp.read`). Leave out any you don't want. Then:

```
mother hub smoke      # reads each source twice, 20 s apart, in a throwaway copy; prints status and counts, never content
mother hub install    # runs the watcher under launchd on this machine
```

**Disk access for the watcher.** The launchd watcher is a bare `python3` process, not your terminal, so reading the
WhatsApp database needs its own Full Disk Access grant (System Settings > Privacy & Security > Full Disk Access >
`+`). Add the interpreter the job runs: it is the first entry of `ProgramArguments` in
`~/Library/LaunchAgents/com.mother.hub.plist` (`/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' ~/Library/LaunchAgents/com.mother.hub.plist`;
it is the `python3` that `mother hub install` found, often `/usr/bin/python3`, and you may have to press Cmd+Shift+G in
the file picker to type the path), then run `mother hub install` again so the job restarts. A passing `mother hub
smoke` run from your terminal does not prove the watcher has this grant, because the terminal has its own.
Without it the Now panel shows `whatsapp ✗` and `mother hub status` says the source is `couldnt_check (denied)`.

Finally run `/mother:hub` once in a pinned session, in normal permission mode (not bypass), and leave it open: it
wakes on each update and routes it. `mother hub status` shows whether the watcher and hub are keeping up.

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
| doctor | daily | Re-tests every connector, heals what it can (retry, reconnect, switch to a backup server) and alerts once per broken connector |
| tick | hourly 06:05-21:05 workdays | A short Now list, kept quiet: what is blocked on you, what the team is neglecting, suggestions waiting for a yes, and your Top 3 today. Read it with `/mother:now` or the Now panel (`/now-panel`, see Install); it sends nothing unless `routines.tick.notify` is `dm` |
| meeting prep / recap | around each team's weekly meeting | The deck, then a recap draft |

You can also just ask: "brief me", "what's open", "remember that …", "add a todo …", "take notes on the
design review", "prep my 1:1 with Sam", "what did I promise", "set up a new project", "what's on me now" (`/mother:now`: the tick's list; say "mother now do S3" to run one suggestion, or "snooze that"). Each one routes to a
skill: `brief`, `now`, `todo`, `remember`, `one-on-one`, `commitments`,
`meeting`, `project`, `team` and so on. With WhatsApp set up (`mother whatsapp setup`), "what did Sam say on
WhatsApp" or "WhatsApp Dana that I'm late" go to the `whatsapp` skill: it reads any chat, and each message to
someone else waits for your yes in the permission prompt.

The hub is optional and runs on your home machine. Set the `hub` block in `config.json` (with `enabled: true`), install the watcher with
`mother hub install` (it polls your sources read-only and appends what changed to a local inbox), then run
`/mother:hub` once in a pinned session. That session wakes when a line arrives (or, without a Monitor, checks every 3 minutes and stops at once when nothing is new): it sends an update into the
session already working on that thing, or raises a New task card for you to start. It never messages anyone else.

Each wake is a model turn, and the Monitor that wakes the hub lasts at most 30 minutes, so the hub re-arms it every
30 minutes inside your active hours even on a quiet day. Set `hub.model` to a small, fast model
so those turns cost little. Outside active hours it doesn't re-arm: it schedules one `/mother:hub` run for the
start of your next active window and goes quiet until then.

**DMs and your self-DM.** The watcher's Slack server can't list your DMs, so it reads a DM only when you map it. To
fill the DM map, list your DMs with the Slack connector (`slack_list_user_channels`, types im) in a normal MOTHER
session, match the user ids to the people in `config.json`, then run `mother hub dms set …`, for example
`mother hub dms set U00000001=D00000001 U00000002=D00000002` (only people in config are accepted; `mother hub dms
show` lists the map, `mother hub dms drop U00000001` removes one). A message from that person reaches the hub; your
own reply in the DM clears it. To use your self-DM (the DM with yourself) as an inbox, run `mother hub dms inbox
<its D… id>`: every note you post there becomes a thing the hub routes or proposes, like `mother hub add`.

WhatsApp can be one of its sources: with `whatsapp.enabled` and `whatsapp.read` on, add `"whatsapp": {"local": "whatsapp"}` under `hub.connections`. The watcher then reads the local WhatsApp database (no bridge, nothing sent) for new direct messages to you, for groups you list in `hub.watch.whatsapp.groups` or follow with `mother hub follow --whatsapp <jid>`, and for any group that @-mentions you; your own reply in the chat clears it. `mother hub mute <jid>` silences a chat. It needs the same Full Disk Access as reading WhatsApp chats does.

You can steer it by typing to it ("follow PLAT", "mute that channel", "what are you watching", "open a session for this"; a session you ask for still needs your yes) or from the Now panel: **Send to hub** on any item with a link, **Follow** and **Mute** on a hub session or hub update. It can't fetch the web or edit files; for those it proposes a session.

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
| `mother health` | Connector health: which server serves each capability, the failure log and a 7-day summary (`mother health help`) |
| `mother grant add` | Add a guard grant for an unattended run, checked exactly as the guard will (`remove --run ID`, `list`); the only writer of `.mother/grants.json`; the guard asks you before each `add`; grants are signed with `~/.mother/grant.key` |
| `mother memory index <file>` | Add one index line for `memory/<file>.md` to `memory/MEMORY.md` (idempotent); the only unprompted writer of the index. `memory check` lists drift |
| `mother runs` | Watchdog: which routines missed their last slot or failed (the brief and doctor flag these too) |
| `mother now show` | The Now list from the last tick (`--json` for the raw file); `mother now done <id>`, `snooze <id> 1h\|tomorrow\|next-week`, `top set\|swap\|done` edit your view of it |
| `mother hub install` | Home machine: run the hub watcher under launchd (`uninstall` removes it); `check` makes one read per connection, `status` shows the watcher and hub heartbeats (exit 2 if stale), `smoke` runs `check` and two polling passes 20 s apart into a throwaway copy of the state and prints per-source status and line counts (never content; exit 2 if a source cannot be read), `watch` runs it in the foreground; `add <link-or-text>` hands the hub something by hand, `follow`/`unfollow`/`mute`/`unmute` edit its machine-local watch list, `watching` prints it; `dms set|drop|show|inbox` maps DMs with people in config and the self-DM inbox |
| `mother tts setup` | Install the optional Kokoro neural voice for the spoken brief (~330 MB in `~/.mother/tts`, English only) |
| `mother channels status` | Reply loop: paused or stopped channels, pending drafts, streaks |
| `mother channels pause` / `resume` | Stop or restart replies in every channel |
| `mother whatsapp setup` | One guided WhatsApp setup, in your own terminal: installs and links the bridge (a QR, or a code typed on your phone), turns WhatsApp on for your account and checks database access. Safe to re-run |
| `mother whatsapp chats` / `read` | Your recent chats (`--query` to find one), and the messages of one chat or of the last day. Read from WhatsApp desktop's local database; nothing is copied anywhere |
| `mother whatsapp send --to WHO --text …` | Send one message from your number to a chat you've already used. MOTHER runs it; the guard asks you first, showing who and the text, except for `--to me` |
| `mother whatsapp install` | Install the optional WhatsApp bridge into `~/.mother/whatsapp/app` (Node 20+, runs `npm ci`; repeat after a plugin update when `status` says `reinstall_needed`) |
| `mother whatsapp link` / `unlink` | Pair the bridge again, in your own terminal / log it out and delete its keys; both stop a running bridge first (`mother whatsapp stop`) |
| `mother whatsapp status` | Is the bridge linked, and is the local WhatsApp database readable (no messages shown) |

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
| `bin/mother` | CLI: `init`, `join`, `sync`, `scan`, `home`, `status`, `register`, `runs`, `path`, `link`, `speak`, `tts`, `channels`, `items`, `eval`, `health`, `now` (Python 3.9 stdlib) |
| `hooks/guard.py` | Approval guard: allow / ask / deny per policy, kill switch, grants for unattended runs |
| `skills/` | `setup`, `doctor`, `routine`, `brief`, `now`, `digest`, `dots`, `commitments`, `one-on-one`, `meeting`, `channels`, `reflect`, `todo`, `project`, `team`, `remember` |
| `agents/` | Read-only sub-agents: `reader` (one source) and `critic` (one draft) |
| `lib/` | `channels.py` (reply-mode engine), `now.py` (the Now list), `health.py` (per-server connector health, the only writer of `capabilities.json`), `items.py` (reader evidence check), `evals.py` (A/B harness) |
| `routines/` | What each scheduled run does |
| `assets/deck/`, `assets/plan/` | Weekly meeting deck with meeting mode; sprint plan page |
| `rules/` | Default policies: outbound, privacy, memory, todos, projects, teams |
| `templates/workspace/` | A new workspace's skeleton |

## Development

```
python3 -m unittest discover tests
```

Start with [AGENTS.md](AGENTS.md) (rules, commands, release steps) and [ARCHITECTURE.md](ARCHITECTURE.md)
(code map and how a run flows). Both are written for coding agents and humans alike.

## License

MIT
