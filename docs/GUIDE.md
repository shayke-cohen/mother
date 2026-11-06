# Operating guide

The day-to-day detail the [README](../README.md) leaves out: the watcher, every Chief pane command and button, cases and
relay, waits and habits, the Now panel, team-channel replies, WhatsApp, every CLI command and what to do when something is
off. The exact rules are in [CONTRACT.md](CONTRACT.md); this page is how to use them. Examples use invented names and ids.

- [The watcher](#the-watcher)
- [The Chief pane](#the-chief-pane)
- [The life of an item](#the-life-of-an-item)
- [Cases and relay](#cases-and-relay)
- [Waiting, chase and done](#waiting-chase-and-done)
- [Habits and quieting](#habits-and-quieting)
- [Notifications](#notifications)
- [The fast loop](#the-fast-loop)
- [Health and sign-in cards](#health-and-sign-in-cards)
- [The Now panel](#the-now-panel)
- [The terminal hub](#the-terminal-hub)
- [Replying in team channels](#replying-in-team-channels)
- [Commands](#commands)
- [Troubleshooting](#troubleshooting)
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

### `/chief` commands

| Command | What |
|---|---|
| `/chief`, `/chief open` | Open the pane and start its fast refresh |
| `/chief tab needs\|inbox\|sessions\|today\|team\|health\|all` | Show one tab |
| `/chief expand`, `/chief collapse` | Open or close every card |
| `/chief refresh` | Re-read every session's card now, ignoring the intervals (never the budget or the 6 calls a minute) |
| `/chief status` | Who leads, today's spend, pinned sessions, the inbox |
| `/chief pause`, `/chief resume` | The chief-only switch: stops the tick and triage, never touches `.mother/PAUSED` |
| `/chief relay on\|off\|status` | Whether follow-ups are delivered into case sessions by themselves |
| `/chief habits on\|off\|status` | Whether triage uses what you did before, and whether anything is quieted |
| `/chief floor on\|off\|status` | This session only: whether its system prompt asks for the `Waiting on:` line |
| `/chief notify on\|off\|status` | Desktop notifications (on by default) |
| `/chief skin dark\|dark-app\|plain\|status` | The pane's look, kept on this machine |

### The header

From the top: the banner or portrait; the **mood line**, the first that holds of sign-in problems, questions, urgent
items, Health problems, waits to chase, sessions done, sessions working, inbox waiting, all clear; the **what just
changed** line (a coloured dot and one event, for 15 minutes or until something at least as important replaces it);
then spend against budget, who leads (`led` plus `here` or `by another session`, a dim `starting…`, or a red
`nobody leading`), a red `guard off` or `paused` pill, and the heartbeat's age. Under that, red lines for a spent
budget, an unreadable spend file or a pause, and for 10 seconds an **Undo** row after an Archive.

### The Ask box

`Ask Mother…`, then **Start** or Enter. An empty box says `Type what you need first`. The answer draws in a card under
the field, with `Route: <route> · read in code` or `· one model call`, and **Dismiss**. Every route is logged as `ask`.

| You type | What happens |
|---|---|
| `tell <session> …`, `ask <session> …` | The longest leading words that name exactly one session (a case key, a CLI id's first characters, its title or words of it) are the target; the rest is sent to it |
| `status of <session>`, `how is …`, `where is …` | Its stored card, in one answer, then a refresh of that card. Nothing is typed into the session |
| `hide <session>`, `show <session>` | Takes it off the floor, or puts it back |
| `open <key or session>` | Opens that session |
| `pause`, `resume` | The chief-only switch |
| `why <key>` | The verdicts that key got |
| Anything else | One model call (`chief.triage_model`, counted; none over budget) over a summary of the floor: it answers, tells a session, reports on one, or opens a case |

A message to a session carrying a risky word (merge, master, main, prod, deploy, release, rollout, rollback, delete,
drop, force, send, post, publish, email, payment) or a link to anything but a local or `.test` host waits on **Send
anyway?** with Send and Cancel. A case always starts from your own words, never the model's: it opens a new session on
the case brief, with the session claiming the key on its first prompt. A failed or odd model reply is a case too.

### Tabs and their buttons

A tab shows its count; a coral dot marks one with something new, a red dot marks Health when something is wrong. The
tab shown is your pick, else the first that holds something. On the dark skin a card is three rows: caret and title;
badges, label, age, source link; the pills, with exactly one coral. Pressing a card's button selects it (a bold border).

**Needs you**, in this order. The tab's count holds the first two.

| Card | What it says | Buttons |
|---|---|---|
| Sign-in or network | `Sign-in needed: <source>` or `Network: <source> unreachable`, the short reason | **Retry** runs `mother hub check <source>`, then `mother hub refresh` when it answers; **How to fix** unfolds a line or two |
| Question | The question, recap, repo and branch, why it needs you, age, PR links; what each button sends | The session's answers (coral only on the one its recommendation names), a pill for a recommendation no answer says, **Open**, **Hold**, **Deliver now** when relayed lines wait, and a `Tell this session…` field with **Send** |
| Chase | `Waiting N days on <who>: <what>` | **Draft a nudge** asks the session for a draft, never a send; **Keep waiting** (3 more days); **Drop it** (no chase for this wait) |
| Done | `Done: <title>` | **Archive**, **Keep** (until the session next moves) |

A press re-reads the list first; if the question changed you get `That question has moved on` and nothing is sent.
**Hold** hides a question until it changes; the tab says how many are on hold. Answering hides the card until the next
read.

**Inbox**: what triage kept, by urgency then age, at most 10 drawn. Badges are the source, `decide` / `reply` / `act`
(none for `fyi`), `urgent`, and `case` when a session owns it. Compact: the verdict's actions (or two suggestions fixed
per source) and **Done**. When a live session owns the item, or exactly one session's title, summary or case names its
ticket or PR, **Send to \<session\>** is the coral pill: the item goes there framed as other people's words, and later
activity follows it. Expanded adds the reason, every link, `kind · on … · urgency`, why it reached you, a habit note,
what each action sends, **Open session**, **Done**, **Follow**, **Ignore…** (this, for a week, or a chat's channel) and
**Snooze** (`1h`, `tomorrow`, `next-week`).

**Sessions**: your pinned sessions, then `Cases N` (case sessions that aren't pinned), sorted waiting on you, stuck,
working, waiting on others, idle, done. A state badge, plus `check due`, `case` with its label and `N new`. Compact:
reply chips (a press fills the chat box, never sends), **Check now**, **Open**; on a done one **Archive** and **Keep**. Expanded adds the summary, `Since the
last card: …`, `updated Nm ago`, links, a `Tell this session…` field with **Send**, **Deliver now**, **Refresh**, and on a
done case **Close case**. This session's own card says `this session` and has no Open.

**Today**: Suggested, today's open Top 3 (badge `top`), then what is on you, from the tick's `now.json`. The tick's first
action, **Ask Mother** (fills a prompt), **Done**. Expanded adds **Open**, **Snooze**, **Ignore…**, every fact the item
carries, and for a dots action a `What this does` block. No `now.json`, or a stale one, is said in one dim line.

**Team**: the tick's neglected items under a heading per team, 7 a group then `Show N more`. Same buttons as Today.

**Health**: see [Health and sign-in cards](#health-and-sign-in-cards).

**All**: every section stacked under its heading.

### Leading, budget, pinned sessions, picture

**Leading.** Any open desktop session can lead; exactly one does at a time. It runs `mother chief tick` every minute. If
you close it, another open desktop session takes over within two minutes.

**Budget.** Triage, session cards, the question fallback and Ask routing are model calls counted against
`chief.daily_budget_usd` (default 20). Over budget none are made: lines become Inbox cards untriaged, cards keep their
last summary, and asks open cases. A question from a `wrap_up` call costs nothing and still shows.

**Pinned sessions.** Pin a desktop session in the app and it appears on the Sessions tab. Every message the pane sends
into a session is prefixed `From the manager via the Chief pane:` and asked by the guard. A pinned session is shown and
talked to, never driven.

**Session cards.** One card per session, made by one model call when the session moved: state, a two-line summary, what
moved since the last card, chips. Who refreshes: any session while its pane is shown, and the leading session with no
pane shown, each session at most once per `chief.refresh_minutes` (a running one every twice that). What the transcript
says in code overrides the model: a `Waiting on:` line sets the state, a running session is `working`, and one that ends
in an API error or limit message, or said `working` and has not moved for 30 minutes to 2 hours, is `stuck`.

**Picture.** A small `<workspace>/.mother/portrait.jpg` is what the desktop app draws: a wide one (1.4:1 or wider) is
the banner, a square one a spot. `portrait.png` (at most 2 MiB) is what a terminal draws. Without a usable JPEG the
desktop app shows the bundled vector.

## The life of an item

1. **A line.** The watcher appends what changed to the inbox file.
2. **Triage.** Each minute the leader logs muted lines, and asks the model about the rest (at most 20). Code records the
   decision: `log`, or `now`, which makes an Inbox card. No verdict is always `now`. A line whose key has a live case is
   routed to that case instead.
3. **A card.** With a kind, urgency and actions, a notification if urgent, and the what-just-changed line.
4. **Your press.** An action opens a case session, or sends to the one that owns the item. Done, Snooze, Follow and
   Ignore settle it without a session. Everything you press is logged for habits.
5. **Work.** The session investigates, does the next step, and when it stops says so: a question, a wait or done.
6. **Follow-ups** are relayed into the session, not made into new cards.
7. **Closing.** `mother hub done <id>` or `mother chief close-case <key>`, usually run by the session. After that, new
   activity on the thread is a new item. `mother chief why <key>` shows every verdict along the way.

## Cases and relay

A case is one item owned by one session until it is closed.

1. Press an action button (or **Open session**) on an Inbox card, or **Start** on an ask. A new desktop session opens in
   the workspace with the brief typed and not sent:

   ```
   Mother case: Dana asks who owns the checkout migration
   Key: C00000001/1700000000.000100
   Source: https://chat.example.com/archives/C00000001/p1700000000000100
   Why it reached the manager: Dana needs an owner named before Thursday's planning.

   Use the mother:cos skill for how to act. Read the thread and PLAT-123, then recommend an owner.
   When this is finished, run: mother hub done U12
   ```

2. You read it and press Enter. The session claims the key, so the card gets a `case` badge and later lines on that
   thread are routed to this session. It can add related keys with `mother chief watch-key`.
3. When its turn ends on a question it shows under **Needs you**, whether or not it is pinned.
4. New activity arrives as one message starting `Mother relay:`, framed as other people's words that approve nothing: at
   most once per 5 minutes and 6 times an hour, at most 10 lines each. A forged line that starts like one of those frames
   is dropped. A session that is closed or asleep gets nothing, and neither does one whose question is waiting on you;
   its card shows `N new`.
5. `/chief relay off` holds deliveries back. A card then shows **Deliver now**, which delivers once.
6. The session closes the case. An archived Done session's case closes after the 10-second Undo.

**A pinned session can hold a case too.** A chat message link, tracker key or PR you type in a pinned or case session's
prompt joins its case (at most 5 per prompt; `mother chief mention`). A key live in another session is refused. Relays,
pane sends and tool output never count, only what you typed.

A case session reads other people's words, so the guard treats it more strictly than a fresh session: the routine-work
shortcut of `policy.autonomy: "functional"` is off there, and each outbound action asks.

## Waiting, chase and done

Questions and waits come from the session, read in code:

- **Questions.** A turn that ends with the desktop app's `wrap_up` call gives its `ask`, its reply actions as options,
  its first action as the recommendation, and its recap. No model call. Only a turn with no such call falls back to one
  model call over the last 3000 characters of the final message.
- **Waits.** A pinned session's own module adds one section to its system prompt asking it to end every turn with
  `Waiting on: the manager — …`, `Waiting on: <who> — …` or `Waiting on: nothing — done`. A case session gets the same
  from the `cos` skill. `lib/floor.py` reads the last such line; the wait's clock runs while the same who and what repeat.
  No instruction for a session under `chief.ignore_folders`, or after `/chief floor off`.

From that line, with no model call:

| After | The pane shows |
|---|---|
| 24 h waiting on someone else | A `check due` badge and **Check now**: a read-only instruction, at most once a day, never into a running session |
| 3 days waiting on someone else | A chase card in Needs you and one notification |
| `nothing`, not running, no question, 10 minutes quiet | A Done card and a `done` badge |

**Archive** uses the desktop app's own session tools: unpin, then archive. If that is refused it stays unpinned or
pinned, and either way hidden from the floor; the toast says which. **Undo**, for 10 seconds, reverses exactly what
happened. Nothing is deleted.

## Habits and quieting

The pane logs what you do with each card to `.mother/chief/actions.jsonl`: the verb, labels and a 60-character title,
never a message body or a why; kept 90 days. From it, code counts per bucket (a channel, project, repo or person) over 30
days how many items you were shown, engaged with, ignored, or closed unread.

- **As a hint.** Triage gets the line's own buckets' counts and the action you usually pick. It may add a `habit_note`,
  shown on the open card. It is told this is never a reason to hide something addressed to you, a decision only you can
  make, or anything urgent. These counts and that label are the only part of the log that leaves the machine.
- **As a rule.** A line is logged instead of shown when all of these hold: habits on; its verdict says `fyi` or `act` and
  not `now`; it isn't manual or an error; it isn't addressed to you and doesn't mention you; no live case holds its key;
  and one of its buckets has at least 5 items shown in 30 days, none engaged, at least 4 ignored or closed unread. One
  engagement ends it.
- **You can see and undo it.** Health's **Quieted by habit N** lists each line with why, **Show** (it becomes a normal
  Inbox item) and **Stop quieting this** (that bucket is never quieted again). `/chief habits off` stops both the hint and
  the rule; the log is still written.

## Notifications

Only the leading session raises them, title `Mother`, one line, at most 3 a pass: a new question, a new urgent Inbox
item, a source going down, a chase card. What was raised is remembered for 14 days, so neither a reload nor a change of
leader repeats it, and a fresh install raises nothing from the backlog. Inside `manager.quiet_hours` only urgent items are
raised; the rest are remembered as seen, so the morning does not replay the night. A Done card raises none.
`/chief notify off` stops them on this machine.

## The fast loop

The pane's minute pass runs `mother chief status --json --save`, which also writes `.mother/chief/status.json`. While the
pane is shown a 5-second loop stats that file, `heartbeat.json` and `.mother/floor/*.json`, and redraws only when one moved.
It starts no model call and no tick; only a moved floor card spawns a process, because `mother floor list` is the one place
cards are validated. It stops when the pane is hidden; the minute pass or `/chief open` restarts it.

## Health and sign-in cards

**Sources**: each source the tick named, from `now.json`: green `ok`, amber `partial`, red otherwise, with the reason and
how long. **Watcher**: last poll, `stale`, a badge per polled source, and an amber `degraded` line while chat is rate
limited (it backs off and reads only the conversations known to matter). **Watcher errors**: red cards with **Done**, and
**Done all** from two. **Undelivered reports**: routine reports that didn't reach your DM; a press shows the text,
**Dismiss** hides it without deleting, **Show again** restores. **Budget and lead**, **Guard**, **Quieted by habit**.

The red dot on the tab means a source that isn't ok, a watcher error, an undelivered report you haven't dismissed, or a
guard that isn't on.

A source that is down, or has an open watcher error, whose error reads as a sign-in or network problem becomes a card at the
top of Needs you. It goes as soon as the watcher reads the source again. A 429 is never one: that is the rate-limit path above.

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
| `mother hub install\|uninstall\|check [<source>]\|smoke\|refresh\|status\|watch` | The watcher and its launchd job; `check <source>` is what Retry runs |
| `mother hub add\|follow\|unfollow\|mute\|unmute\|watching\|dms` | Hand it a link or note; the watch and mute lists; the DM map |
| `mother hub done\|snooze <U-id>` | Close or snooze an inbox item (the pane's Done and Snooze) |
| `mother channels status\|pause\|resume` | The reply loop: paused channels, pending drafts; stop or restart every channel |
| `mother whatsapp setup` | Guided WhatsApp setup, in your own terminal only: installs and links the bridge, checks database access |
| `mother whatsapp chats\|read` | Recent chats and one chat's messages, from the local database |
| `mother whatsapp send --to WHO --text …` | Send one message from your number; the guard asks first, except for `--to me` |
| `mother whatsapp status\|link\|unlink\|install\|stop` | The bridge's state and lifecycle |
| `mother speak`, `mother tts setup` | Render the spoken brief; install the optional Kokoro neural voice (~330 MB in `~/.mother/tts`, English only) |
| `mother eval brief\|verdict\|report` | A/B the brief's gather mode on the same inputs |

### `mother chief`

The pane calls most of these; the ones you would type yourself are first.

| Verb | What |
|---|---|
| `status [--json] [--save]` | Paused, who leads, today's budget, pinned sessions, the inbox. `--save` also writes `.mother/chief/status.json` |
| `why <key> [--json]` | The last triage verdicts of one key, newest first (no bodies; 30 days) |
| `pause`, `resume` | The chief-only switch (`.mother/chief/PAUSED`); never the global one |
| `relay on\|off\|status` | The relay switch (`.mother/chief/RELAY_OFF`) |
| `relay deliver --for <session id>` | That session delivers what waits once, on its next pass, though relay is off |
| `habits [--json]\|on\|off\|status` | The counts per bucket over 30 days; the switch (`.mother/chief/HABITS_OFF`) |
| `habits show <line id>`, `habits except <bucket>` | A quieted line becomes an Inbox item; never quiet that bucket again |
| `close-case <key>` | The live case holding the key is done, with every open item of its keys |
| `watch-key <key> --for <session id>` | Add a related thread, ticket or PR to a live case |
| `report <name>` | Print one undelivered routine report |
| `tick [--json] [--as <session id>]` | One pass: heartbeat, muted lines logged, the lines that need a decision. `--as` leads only if nobody else does |
| `lines [--json]` | The inbox lines waiting for a decision |
| `context --json` | What the triage prompt says about you: names only, at most 1500 characters |
| `triage --stdin --decide` | Record a decision per judge line from `{judge, verdicts}`, in code |
| `pinned --stdin` | Snapshot the pinned sessions from `list_sessions` rows |
| `tail --json` | Each pinned or case session's recent tail and what its transcript says in code |
| `tldr --stdin` | Store session cards |
| `spend --stdin` | Add one pane model call to today's spend |
| `portrait --json` | The pane's picture |
| `claim --stdin` | A session claims an item's key; refused if another session holds it |
| `mention --stdin` | The keys you named to a session join its case |
| `ask --stdin` | The Ask box: text becomes a `manual` line whose key is held for the case the pane starts |
| `dots-detail <id> --json` | What a dots action or manager item says, from the newest dots output |
| `relays --for <session id> [--ack --stdin]` | The routed lines not yet delivered; `--ack` marks them delivered |
| `act-log --stdin \| --json [--days N]` | Append one action you took; print the log |

### `mother floor`

| Verb | What |
|---|---|
| `card --stdin` | Store one session's card; the end of its final message gives the `Waiting on:` line |
| `list --json` | Every card not older than 7 days: did each session stop on a question, and what it waits on |
| `mark --stdin` | `checked` (refused within 24 h), `keep` (no chase for 3 days), `drop` (no chase for this wait), `done-keep` (no Done card until it moves) |

## Troubleshooting

**`nobody leading`.** No open desktop session is ticking. Open one in the workspace; it takes the lead within a minute.
Nothing is triaged until one does, and lines wait. A terminal session reads but never leads.

**A pane that shows `starting…`.** Normal for up to 3 minutes after a start or a reload: nobody leads yet, but the last
heartbeat is recent. After that it turns red `nobody leading`. `mother chief status` prints the heartbeat.

**Budget spent.** A red line says so and no model call is made: lines arrive untriaged, cards go stale, asks open cases.
Questions from `wrap_up` still show. Raise `chief.daily_budget_usd`, or wait for tomorrow in your timezone. `spend file
unreadable` is the same stop, because `.mother/chief/spend.json` can't be parsed; `mother chief spend` never overwrites it,
so fix or remove the file.

**`guard off`.** You or a session ran `mother guard off`: no guard rule runs in any session. `mother guard on` restores
them. `paused` is `.mother/PAUSED`, which blocks every outbound call until you delete it.

**A source shows `partial` or red.** `mother hub status` gives the reason, `mother hub check <source>` tests one connection
and `mother hub refresh` makes the watcher poll now. `partial` while rate limited is expected: chat reads only known
conversations until a clean pass. A sign-in card means the connection itself needs you.

**No `Waiting on:` line from pinned sessions.** `/chief floor status` in that session says whether the section is in its
prompt. It is left out for a session that isn't pinned yet, a case, an ignored folder, or `/chief floor off`. **Known
limitation:** the section is added on the `prompt.compose` event, and depends on no earlier plugin consuming it. An
organization-managed plugin that runs first and handles that event without passing it on means Mother's section is skipped,
though `/chief floor status` still says it is in the prompt. Those sessions then never get the instruction. Their waiting state
still comes from the session-card refresh, so it updates on the refresh interval rather than each turn. `mother floor list
--json` shows whether a card has a `waiting`.

**Typing lags in a pane field.** What you type is kept in a state value no draw reads, so typing itself doesn't redraw the
pane. If a field still lags, it is the app redrawing a large pane: pick one tab rather than `All`, and `/chief collapse`.
Not measured beyond that.

**Pill labels are hard to read.** The app's text colour decides a pill's label. Use `/chief skin dark` for an app in light
mode, `/chief skin dark-app` for dark mode, or `/chief skin plain`.

**Nothing new on the Inbox.** `mother hub status` for a stale watcher, Health for quieted lines, `mother chief why <key>` for
what triage decided about a thing. A line triage logged isn't lost: it is in the verdict log for 30 days.

## Pausing, stopping, uninstalling

- **Kill switch:** `touch "$(mother path)/.mother/PAUSED"` blocks every outbound call until you delete the
  file. Routines still read and still write to the workspace.
- **Stop the pane's model calls:** `mother chief pause`.
- **Stop one routine:** remove it from `config.json` → `routines`, then ask `/mother:setup` to update the
  scheduled tasks (it disables the task rather than deleting it).
- **Stop the watcher:** `mother hub uninstall`.
- **Uninstall:** disable the `Mother:` tasks in the scheduled tasks list, then
  `/plugin uninstall mother@mother` and `rm ~/.local/bin/mother`. Your workspace folder is untouched.
