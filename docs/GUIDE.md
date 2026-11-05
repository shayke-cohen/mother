# Operating guide

The day-to-day detail the [README](../README.md) leaves out: the watcher, the Chief pane's commands, cases and
relay, the Now panel, team-channel replies, WhatsApp and every CLI command. The exact rules are in
[CONTRACT.md](CONTRACT.md); this page is how to use them. Examples use invented names and ids.

- [The watcher](#the-watcher)
- [The Chief pane](#the-chief-pane)
- [Cases and relay](#cases-and-relay)
- [The Now panel](#the-now-panel)
- [The terminal hub](#the-terminal-hub)
- [Replying in team channels](#replying-in-team-channels)
- [Commands](#commands)
- [Pausing, stopping, uninstalling](#pausing-stopping-uninstalling)

## The watcher

The watcher polls your sources read-only and appends what changed to `<workspace>/.mother/hub/inbox.jsonl`. It
has no model. It runs on the home machine only.

Add a `hub` block to `config.json` with `enabled: true` and one connection per source you want watched:

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
mother hub refresh    # reconnect now: restarts the watcher and polls every source at once
mother hub status     # watcher heartbeat and each source's interval; exit 2 if stale
```

### Intervals

Each source can be checked at its own pace. The watcher wakes on the shortest interval and polls a source only
when its own is due. Set `every_seconds` (working hours, 30-86400) and `idle_every_seconds` (outside them; `0`
means only during working hours) under `hub.watch.<source>`, and leave a source out to keep the global
`hub.every_seconds` / `hub.idle_every_seconds`:

```json
"watch": {
  "mail":     {"every_seconds": 3600},
  "calendar": {"every_seconds": 900, "lead_minutes": 15},
  "tracker":  {"every_seconds": 600, "projects": ["PLAT"]},
  "code":     {"every_seconds": 600}
}
```

`mother hub status` marks a source STALE when its last poll is more than twice its interval old; `mother hub
check` warns when the calendar is checked less often than `lead_minutes`, because starting-soon alerts would
arrive late.

### DMs and your self-DM

The watcher's chat server can't list your DMs, so it reads a DM only when you map it. List your DMs with your
chat connector in a normal session, match the user ids to the people in `config.json`, then run
`mother hub dms set U00000001=D00000001 U00000002=D00000002` (only people in config are accepted, not your own
id; `mother hub dms show` lists the map, `mother hub dms drop U00000001` removes one). A message from that person
reaches the inbox; your own reply in the DM clears it.

To use the DM with yourself as an inbox, run `mother hub dms inbox <its D… id>`: every note you type there
becomes an inbox line, like `mother hub add` (posts made by apps, MOTHER's included, are skipped).

### WhatsApp as a source

With `whatsapp.enabled` and `whatsapp.read` on, add `"whatsapp": {"local": "whatsapp"}` under
`hub.connections`. The watcher then reads the local WhatsApp database (no bridge, nothing sent) for new direct
messages to you, for groups you list in `hub.watch.whatsapp.groups` or follow with
`mother hub follow --whatsapp <jid>`, and for any group that @-mentions you; your own reply in the chat clears
it. `mother hub mute <jid>` silences a chat.

### Disk access for the watcher

The launchd watcher is a bare `python3` process, not your terminal, so reading the WhatsApp database needs its
own Full Disk Access grant (System Settings > Privacy & Security > Full Disk Access > `+`). macOS charges the
access to the **app that contains** the Python the job runs, not to the binary in the plist. With Xcode's Python
(`/Applications/Xcode.app/Contents/Developer/usr/bin/python3`, the stock `python3` on a Mac with Xcode) that app
is **Xcode** itself: add `Xcode.app`. Adding its `Python.app` or the `python3` launcher does nothing (seen on
macOS 26: the privacy log names `subject=com.apple.dt.Xcode`). A python.org Python re-launches itself as its own
`Python.app`, and that is the one to add. To see who macOS checks, run the watcher and then
`log show --last 2m --predicate 'process == "tccd"' | grep -A2 python3` and read the `AUTHREQ_SUBJECT` line. Then
run `mother hub refresh`. A passing `mother hub smoke` run from your terminal does not prove the watcher has this
grant, because the terminal has its own. Without it the Health tab shows the source as unreadable and
`mother hub status` says `couldnt_check (denied)`.

### Steering what it watches

`mother hub follow --project PLAT`, `--channel`, `--thread`, `--repo example-org/example-api` or `--whatsapp`
adds to the watch list; `unfollow` removes. `mother hub mute <key | link | channel id>` drops a thing's lines
(`--for 1w` for a week, `--scope channel` for a thread's whole channel, `--words w1,w2` for a rule matched
without a model). `mother hub watching` prints the merged list. The pane's **Follow** and **Ignore…** buttons on
an expanded Inbox card run the same verbs.

## The Chief pane

Open it with `/chief` in the desktop app. In a terminal session `/chief` prints `mother chief status` instead.

| Command | What |
|---|---|
| `/chief` or `/chief open` | Open the pane |
| `/chief tab needs\|inbox\|sessions\|today\|team\|health\|all` | Show one tab |
| `/chief expand`, `/chief collapse` | Open or close every card |
| `/chief status` | Who leads, today's spend, pinned sessions, the inbox |
| `/chief pause`, `/chief resume` | The chief-only switch: stops the tick and triage, never touches `.mother/PAUSED` |
| `/chief relay on\|off\|status` | Whether follow-ups are delivered into case sessions by themselves |

**Leading.** Any open desktop session can lead; exactly one does at a time. It runs `mother chief tick` every
minute. If you close it, another open desktop session takes over within two minutes. The header shows
`led here`, `led by another session` or, in red, `nobody leading`.

**Budget.** Triage, the session summaries and question detection are model calls counted against
`chief.daily_budget_usd` (default 20). Over budget none are made: lines become Inbox cards untriaged and cards
keep their last summary. `mother chief status` prints the figure.

**Pinned sessions.** Pin a desktop session in the app and it appears on the Sessions tab with a two-line summary,
a chat box, a recommended reply you still press Send on, and Open. Every message the pane sends into a session
is asked by the guard.

**Portrait.** `<workspace>/.mother/portrait.png` (a PNG of at most 2 MiB) replaces the picture; add a small
`portrait.jpg` for the desktop app. A wide picture becomes a banner, a square one a spot beside the title.

## Cases and relay

A case is one Inbox item owned by one session until it is closed.

1. Press an action button (or **Open session**) on an Inbox card. A new desktop session opens in the workspace
   with the brief typed and not sent:

   ```
   Mother case: Dana asks who owns the checkout migration
   Key: C00000001/1700000000.000100
   Source: https://chat.example.com/archives/C00000001/p1700000000000100
   Why it reached the manager: Dana needs an owner named before Thursday's planning.

   Use the mother:cos skill for how to act. Read the thread and PLAT-123, then recommend an owner.
   When this is finished, run: mother hub done U12
   ```

2. You read it and press Enter. The session claims the key, so the card gets a `case` badge and later lines on
   that thread are routed to this session.
3. When the session stops on a question, it shows up under **Needs you** with its options as buttons, whether or
   not it is pinned.
4. New activity on the thread arrives in the session as one message that starts `Mother relay:`, at most once
   per 5 minutes and 6 times an hour, at most 10 lines each. A session that is closed or asleep gets nothing;
   its card shows `N new`.
5. `/chief relay off` holds deliveries back. A card then shows **Deliver now**, which delivers once.
6. The session closes the case with `mother hub done <id>`. After that, new activity on the thread is a new
   Inbox item.

A case session reads other people's words, so the guard treats it more strictly than a fresh session: the
routine-work shortcut of `policy.autonomy: "functional"` is off there, and each outbound action asks.

## The Now panel

The hourly `tick` routine writes `<workspace>/.mother/now.json`: what is blocked on you, what teams are
neglecting, suggestions waiting for a yes, and your Top 3. Three ways to read it: the Chief pane's Today and Team
tabs, `/mother:now` in the chat, or the Now panel (`/now-panel`), which adds a status line
(`◆ 2 on you · 1 neglected`).

The panel is two panes. **Home** ranks and routes: Top 3 today, the suggested asks, then one line per item
grouped by what it asks of you (**Decide**, **Reply**, **Review**, **On you**), **Sessions**, **Suggestions** and
**Neglected by teams**. Click a row and the **Inspector** shows that one thing: why it is on the list, one primary
next move plus alternatives, a chip for each source, and Done (`d`), Snooze (`s`), Ignore…, Make top 3 (`t`) and
Tell Mother… at the foot. Mail also gets **Archive** and **Mark read**, done through the watcher's mail
connection with no model (`mother inbox`, still checked by the guard). Every action only fills the prompt; you
press Enter.

Its one setting, `mother_bin` (the plugin's userConfig), is the path to the `mother` script it calls. Left empty
it runs this plugin's own `bin/mother`.

If you installed the panel when it was a separate plugin (`mother-now`), remove that one once:
`/plugin uninstall mother-now@mother`.

## The terminal hub

`/mother:hub` is the older way to decide the inbox: a session that wakes on each new line and logs it, routes it
into a session that follows the thread, or proposes a new session. It refuses to start while a desktop session
leads (a heartbeat under two minutes old), so use it only where the desktop app isn't running. Run it once in a
session in the workspace, in normal permission mode, and leave it open. Set `hub.model` to a small, fast model:
each wake is a model turn. It never messages anyone else.

## Replying in team channels

Each channel in `config.json` has a reply mode: `off`, `watch` (read for digests only), `mention` (answers in
the thread when someone addresses the agent by name, e.g. "Agnes, is the deploy green?"), `draft` (drafts
replies to your DM; you answer `send`, `send: <edit>` or `no`) or `auto` (answers the topics you list in
`auto_topics` itself and drafts the rest). Who may trigger a `mention` channel is its `trigger`: `manager`,
`team` or `anyone`. Start the loop in a Claude Code session, on a cheap model:

```
/loop /mother:channels
```

Every reply is signed, stays in its thread, and is checked by the guard. When a teammate asks, the answer is
written by a read-only session that can't see your mail, calendar, DMs or private notes. People in the channel
can say `Agnes, mute` (this thread), `Agnes, stop` (this channel) or `Agnes, pause` (every channel), or 👎 a
reply. Only you turn it back on. After 10 unedited drafts on one topic it proposes answering that topic itself;
you decide by editing the config.

## Commands

In your own terminal, after `mother link`. `mother <command> help` prints more for `hub`, `chief`, `now`,
`health`, `channels`, `whatsapp`, `floor` and `eval`.

| Command | What |
|---|---|
| `mother status` | Validate `config.json` and show workspace, doctor and home-machine state |
| `mother path` | Print the active workspace path |
| `mother init <path>`, `mother join <git-url>` | Create a workspace; clone an existing one on another machine |
| `mother home show\|set\|check\|move` | Which machine runs the routines |
| `mother sync` | Commit and push the workspace now (also runs at the end of each turn) |
| `mother scan` | Secret-scan changed workspace files; exit 1 on a hit |
| `mother guard off\|on\|status` | Turn every guard rule off or back on. While off, the model alone decides; `PAUSED` still wins |
| `mother register` | Print the scheduled-task definitions for the enabled routines |
| `mother runs` | Watchdog: which routines missed their last slot or failed |
| `mother health` | Connector health: which server serves each capability, the failure log, a 7-day summary |
| `mother grant add\|remove\|list` | Guard grants for unattended runs; the only writer of `.mother/grants.json`. The guard asks before each `add` |
| `mother memory index <file>`, `mother memory check` | Add one index line to `memory/MEMORY.md`; list drift |
| `mother now show\|done\|snooze\|top` | The Now list from the last tick and your edits to it |
| `mother inbox archive\|read <mail:ID \| link>...` | Archive or mark read whole mail threads with no model, through the watcher's mail connection; guard-checked and logged |
| `mother hub install\|uninstall\|check\|smoke\|refresh\|status\|watch` | The watcher and its launchd job |
| `mother hub add\|follow\|unfollow\|mute\|unmute\|watching\|dms` | Hand it a link or note; the watch and mute lists; the DM map |
| `mother hub done\|snooze <U-id>` | Close or snooze an inbox item (the pane's Done and Snooze) |
| `mother chief status\|pause\|resume\|relay` | Who leads and today's spend; the chief-only switch; the relay switch |
| `mother floor list --json` | Each session's card: did it stop on a question |
| `mother channels status\|pause\|resume` | The reply loop: paused channels, pending drafts; stop or restart every channel |
| `mother whatsapp setup` | Guided WhatsApp setup, in your own terminal only: installs and links the bridge, checks database access |
| `mother whatsapp chats\|read` | Recent chats and one chat's messages, from the local database |
| `mother whatsapp send --to WHO --text …` | Send one message from your number; the guard asks first, except for `--to me` |
| `mother whatsapp status\|link\|unlink\|install\|stop` | The bridge's state and lifecycle |
| `mother speak`, `mother tts setup` | Render the spoken brief; install the optional Kokoro neural voice (~330 MB in `~/.mother/tts`, English only) |
| `mother eval brief\|verdict\|report` | A/B the brief's gather mode on the same inputs |

## Pausing, stopping, uninstalling

- **Kill switch:** `touch "$(mother path)/.mother/PAUSED"` blocks every outbound call until you delete the
  file. Routines still read and still write to the workspace.
- **Stop the pane's model calls:** `mother chief pause`.
- **Stop one routine:** remove it from `config.json` → `routines`, then ask `/mother:setup` to update the
  scheduled tasks (it disables the task rather than deleting it).
- **Stop the watcher:** `mother hub uninstall`.
- **Uninstall:** disable the `Mother:` tasks in the scheduled tasks list, then
  `/plugin uninstall mother@mother` and `rm ~/.local/bin/mother`. Your workspace folder is untouched.
