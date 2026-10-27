# Tick and the Now panel — design

Date: 2026-10-26 · Status: draft, awaiting review · Roadmap: stage 6 ("a plugin monitor", "SessionStart context")

## Why

MOTHER's routines each look at one slice once a day: the brief at 08:45, dots at 13:00, commitments at 16:30,
the digest at 09:00. Between them, the manager still opens Slack, Jira, GitHub and mail to answer one
question: **"what is waiting on me, and what is the team quietly dropping?"** Nothing in MOTHER answers that
during the day, and nothing shows it beside the chat.

Two parts:

1. **Tick**: a routine that runs every hour during the day and keeps one current list: things blocked on the
   manager, and things the team is neglecting, each with a link and, where the agent can help, a suggested
   action.
2. **Now panel**: a mod (a pane beside the chat, a status-line count, a toast) that shows that list and
   lets the manager act on it, snooze it or mark it done.

The manager's words for what tick must never miss: *"things that are blocked on me or the team is
neglecting."*

## Goals

- One list, current within the hour, of items **on the manager** and items **the team is neglecting**.
- Every item has a link that opens the thing itself (PR, ticket, thread, mail).
- Suggested agent actions the manager can start with one click, run only on the manager's yes.
- A source that couldn't be read is shown as "couldn't check", never as "all clear".
- Quiet: an item shows up once, stays until it's resolved, done or snoozed, and is never re-announced.
- Cheap enough to run hourly: tick reuses what the other routines already found, and only reads what
  changed since the last tick.

## Non-goals

- Replacing the brief, dots, digest or commitments. Tick folds in their open items; it does not redo their
  work or their messages.
- A general activity feed. No "FYI" section: if it isn't on the manager or neglected, it isn't on the list.
- Sending anything to anyone but the manager. Tick posts nowhere by default.
- Running off-hours (see Schedule).

## Anti-goals (would make this a failure even if it works)

- **Nagging**: the same item announced every hour, or a toast for something the manager already saw.
- **False quiet**: an empty panel because a sign-in expired.
- **People scores**: anything that reads as "who is slow". Items are stuck *work*; no per-person counts,
  rankings or trends (DESIGN.md → Safety, "people, not scores").
- **Acting without a yes**: a suggestion that runs because nobody objected.
- **Token burn**: 16 full five-source sweeps a day.
- **A panel that calls the network**: the mod only reads local files and fills the prompt box.

## Schedule (decided by the manager)

- Tick **does not run between 22:00 and 06:00** in `manager.timezone`.
- My call: that is a setting, `routines.tick.active_hours`, default `"06:00-22:00"`, separate from
  `manager.quiet_hours` (which governs notifications and is often narrower, e.g. `19:00-08:00`).
- My call: default interval 60 minutes (`routines.tick.every`, 30–240), on `days` (default `workdays`).
  First run at the start of active hours, last run before its end (06:05 … 21:05 for the defaults).
- My call: tick fires at **:05**, not :00, so it doesn't collide with routines set on the hour (the digest
  at 09:00).
- My call: **tick does not pull or sync.** Hourly pulls and commits would flood the workspace history. Its
  scheduled-task pointer skips `git pull`, and the routine frontmatter carries `sync: false`, which tells the
  dispatcher to skip `mother sync`. Tick still appends its `runs.md` line (the watchdog reads it locally),
  and the next routine that syncs commits those lines with its own. Tick writes nothing else that's tracked
  in git.

## What tick looks for

Thresholds are defaults in config (`routines.tick.thresholds`), counted in **working hours** (inside
`manager.workdays` and active hours), so a Friday-evening PR is not "3 days stale" on Monday.

### On you (blocked on the manager)

| Signal | Source | Default |
|---|---|---|
| Review requested from the manager, not given | code | > 4 working h |
| DM or @-mention with no reply from the manager (a 👀 reaction alone doesn't count) | chat | > 2 working h |
| Digest decision-queue item still unanswered | workspace (`journal/`, digest thread) | > 1 working day |
| Ticket assigned to the manager in a waiting/approval state, or naming them as approver | tracker | any |
| Mail from a report, or triaged `me` by the brief, needing a reply | mail (+ brief triage) | > 4 working h |
| Meeting invite unanswered, meeting within 48 h | calendar | any |
| Promise the manager made, now past due | `journal/commitments/` | past due |
| Agent suggestions waiting for a yes (dots `A…` actions, digest asks) | workspace | open |

### Neglected by the team

| Signal | Source | Default |
|---|---|---|
| PR with no reviewer assigned | code (team repos) | > 1 working day |
| PR approved but not merged | code | > 2 working days |
| PR with changes requested, author silent | code | > 3 working days |
| Default branch checks red, as the code host reports them (e.g. `gh` check runs) | code | > 2 h |
| Ticket in progress with no movement (status, comment, linked PR) | tracker | > 3 working days |
| High-priority bug with no owner | tracker | > 4 working h |
| Ticket whose blocker closed, but it didn't restart | tracker | > 1 working day |
| Sprint ends within 2 working days with untouched work | tracker | — |
| Question from outside the team in a team channel, no reply | chat (configured team channels) | > 4 working h |
| Meeting action item with no ticket | `teams/<slug>/meetings/`, recap | > 1 working day |
| 1:1 past cadence | `one-on-one` drift | as configured |

**v1 uses only the five configured sources plus the workspace (decided by the manager).** These signals are
left for later, because each one needs a connector or config MOTHER doesn't have yet: release/deploy approval
flows, CI outside the code host (e.g. Buildkite), ops alerts (Grafana), an `alerts` channel role with
unacknowledged alerts, mail from the manager's own manager (needs a config key), and tickets assigned to
someone who's out today (needs teammates' calendars).

**Turning signals on and off (my call).** `routines.tick.signals` is a map of signal name → `false` to turn
one off; every signal not listed is on. A signal whose connector isn't configured at all (`connectors.<x>`
is `none` or absent) is silently off, so a fresh install shows no "couldn't check" rows for tools the manager
doesn't use. A signal whose connector is configured but not `ok` in `capabilities.json` is shown under
"couldn't check".

## How tick works

```
scheduled task (hourly, active hours)
  └─ mother:routine id=tick            preconditions, runs.md line (existing dispatcher)
       ├─ fold   read workspace outputs: latest brief triage, dots, commitments, digest queue,
       │         todos/open.md, `mother runs --json` (missed/failed routines)
       ├─ delta  mother:reader per source, window = since last tick (fast signals every tick;
       │         slow neglect scans every 3rd tick — see Cost)
       ├─ check  `mother items check` on every reader reply (evidence or silence; couldn't check ≠ quiet)
       ├─ judge  apply thresholds, write one-line `why`, attach suggested actions
       └─ merge  `mother now merge` (Python): dedupe, carry state, write .mother/now.json
```

- **Fold first.** Most "on you" items already exist in the brief triage, dots, commitments and the digest
  queue. Tick reads those files and re-checks each item's state at its link (still open? answered?), so it
  doesn't redo the morning's work.
- **Delta reads.** Each reader gets the window "since `<last tick>`" plus the per-source signal rules above.
  Readers stay read-only and return cited items (CONTRACT §5c); nothing new in the reader contract.
- **Judgement is the model's; merging is code's.** The routine hands `mother now merge` a list of candidate
  items. The merge is deterministic and tested: stable ids, state carried across ticks, resolved items closed.
- **Resolution.** An item drops off when its source says it's resolved (review given, PR merged, reply
  sent, ticket moved) or the manager marks it done. An item not seen in a tick whose source was `ok` is
  closed; one whose source was `couldnt_check` is kept, marked "unverified since HH:MM".

### `.mother/now.json`

Machine-local (gitignored with the rest of `.mother/`): it changes hourly and would flood sync with commits,
and it lives on the home machine where tick runs. My call.

```json
{
  "version": 1,
  "generated_at": "2026-10-26T11:00:12Z",
  "next_at": "2026-10-26T12:00:00Z",
  "status": "ok | partial | failed",
  "coverage": {
    "chat": {"status": "ok"},
    "tracker": {"status": "couldnt_check", "reason": "expired", "since": "2026-10-26T09:00:00Z"}
  },
  "routines": [{"id": "dots", "state": "missed", "expected": "2026-10-26T13:00Z"}],
  "items": [
    {
      "id": "pr:example-org/platform-api#412:review",
      "section": "on_you | neglected | suggestion",   // see also top-level "top": [...]
      "signal": "review_requested",
      "title": "Review requested on platform-api#412 (rate limiter)",
      "why": "Asked 6 working hours ago; Sam is waiting to merge before the freeze.",
      "link": "https://github.com/example-org/platform-api/pull/412",
      "also": ["PLAT-881"],
      "team": "platform",
      "first_seen": "2026-10-26T07:00:00Z",
      "last_seen": "2026-10-26T11:00:00Z",
      "state": "new | open | snoozed | done | resolved",
      "snooze_until": null,
      "actions": [
        {"id": "S1", "label": "Draft a nudge to the reviewer", "prompt": "mother now do S1"}
      ]
    }
  ]
}
```

- `id` is stable: `<kind>:<link>:<signal>`, so the same PR in two signals is two items, and the same item
  across ticks is one.
- `state: new` lasts for one tick; that is what the panel's toast fires on (once).
- `who` is deliberately absent from the schema beyond what `title` needs to name the work.
- `why` is one line, facts only, no message bodies. Titles are short paraphrases, the same standard as the
  brief's DM.

### Top 3 today (asked for by the manager)

The panel opens with **your three most important tasks for today**, above On you.

- My call: the **first tick of the day** proposes them, after the brief has run if it's scheduled earlier.
  It picks from today's on-you items, todos due today or overdue (`todos/open.md`, Yours), promises due today
  (`journal/commitments/`), prep for today's important meetings (1:1s, the weekly), and the brief's `me`
  triage. It ranks by: someone is blocked on it, it's due today, and it's tied to a meeting today.
  Each pick carries a one-line reason ("Sam can't merge until you review").
- **They stay put for the day.** Later ticks don't reshuffle them. A tick only marks one done when its
  source shows it resolved, and flags a new item that clearly outranks them as "consider swapping" without
  changing the list.
- **The manager owns the list.** `mother now top set "<text or item id>" …` replaces them, and
  `mother now top swap <n> <item id>` swaps one; from the panel, a row's menu has "Make top 3". A list the
  manager set is never changed by tick.
- Done items stay on the list, struck through, so the day shows progress; the next day starts fresh.
- Stored as `top` in `now.json` (`[{rank, title, why, link, item_id|null, set_by: agent|manager,
  state: open|done}]`) and in `now-state.json` when the manager set it.
- The list is also the SessionStart "today's top three" from the roadmap: a new session can open with it.

### Manager state: `.mother/now-state.json`

**Done and Snooze act on the panel only (decided by the manager).** They don't close the todo, commitment,
decision or dots action the item came from; those routines keep their own state. Tick still closes an item on
its own when its source shows it resolved (the manager answered the digest, the PR merged, the todo was
closed).

`{"<item id>": {"state": "done | snoozed", "until": "<ISO>|null", "at": "<ISO>"}}`. Written by
`mother now done|snooze <id>` (the panel calls it), read by `mother now merge`. Done means "hide this id";
if the same id is detected again after its source changes (e.g. a new review request), it comes back as new.
Snooze defaults: "1h", "tomorrow" (next active-hours start), "next week".

### Suggestions (agent actions)

Each suggestion is something the agent can do inside existing rules, e.g.:

- draft a nudge in the manager's DM, or a mail draft (never sent);
- propose a reviewer for an unreviewed PR (as a draft comment, or a request on a grant);
- create a ticket for a meeting action item (tracker create via grant/approval);
- ask the person out today whether their in-progress ticket should be reassigned (draft only).

A suggestion folded in from another routine (a dots `A…` action, a digest ask) keeps that routine's own
handler: `mother now do <id>` hands it to that skill (e.g. the same as answering `do A3` to dots), so
`mother:now` never re-implements another routine's action. Tick's own suggestions are run by `mother:now`.

They run **only** when the manager starts them: the panel's "Do it" **fills the prompt box** with
`mother now do <action id>` and the manager presses Enter. That keeps the action human-typed, so the
existing guard and session-approval rules apply unchanged (CONTRACT §5). The panel never submits a prompt
by itself. Unstarted suggestions lapse after 3 working days, like dots.

A new interactive skill, `mother:now`, handles `mother now do <id>` (read the item, do exactly that action,
through the guard), and also "what's on me now?" in chat (prints the current list as text for sessions
without the panel).

### Notifications

- Default: none outside the panel. The status line and the panel's toast are the notification.
- My call: an optional `routines.tick.notify: "dm"` sends one DM per tick listing only **new** `on_you`
  items, never repeated, held during `manager.quiet_hours`. Off by default.

## The Now panel (mod)

What the manager sees beside the chat:

```
┌ Now · 11:00 · next 12:00 ───────────────────────┐
│ ON YOU (3)                                      │
│ • Review platform-api#412 (rate limiter)  6h  ↗ │
│   [Done] [Snooze ▾]                             │
│ • Decision: drop PLAT-870 from sprint?    1d  ↗ │
│ • Reply to Alex: rollout date             3h  ↗ │
│ NEGLECTED (4)                                   │
│ • platform-web#88 approved, not merged    3d  ↗ │
│ • PLAT-812 in progress, no movement       4d  ↗ │
│ SUGGESTIONS (2)                                 │
│ • Draft a nudge for #412's reviewer  [Do it]    │
│ COULDN'T CHECK                                  │
│ • Tracker: sign-in expired since 09:00          │
│ ROUTINES: dots missed at 13:00                  │
└─────────────────────────────────────────────────┘
status line:  ◆ 3 on you · 4 neglected · tracker ✗
```

- **Pane** (`$.ui.open`, drawn by a `ui.render` hook on `Pane`): sections Top 3 today, On you, Neglected, Suggestions,
  Couldn't check, plus missed routines. Each row: title, age, link (opens in the browser), Done, Snooze.
  Suggestions add Do it. An empty section isn't drawn; an all-empty list with full coverage says
  "Nothing on you. Next check 12:00." An empty list with any coverage gap never says that.
- **Status line** (`$.ui.status`): `◆ 3 on you · 4 neglected` plus `✗` per source that couldn't be checked,
  or `◆ stale` if `generated_at` is older than two intervals during active hours.
- **Toast** (`$.ui.toast`): once per item, when an `on_you` item first appears (`state: new`). Not during
  `manager.quiet_hours`, not for `neglected` items.
- **Data**: the mod reads `<workspace>/.mother/now.json` with `$.fs` and re-reads it on a `$.clock.every`
  timer (1 min) when its mtime changes. The workspace path comes from `$MOTHER_WORKSPACE` or
  `~/.mother/workspace`, like the CLI.
- **Writes**: Done/Snooze run `mother now done|snooze` with `$.process.run` (the CLI owns the file format
  and locking). Do it uses `$.prompt.fill`. No MCP, no network.
- **Command**: `/now` opens or focuses the pane.
- **Surfaces**: desktop and terminal both. Tested on both, as the mod kit recommends.
  - Desktop: a side pane with buttons per row (Done, Snooze ▾, Do it); the toast sits above the prompt.
  - Terminal: a bordered box with a cursor row and keys `↑↓` move, `⏎` open link, `d` done, `s` snooze,
    `x` do it, `esc` close; the toast is one line above the prompt; counts sit in the status line.
  - VS Code and mobile: whatever the surface draws of the pane; where none is drawn, `/now` prints the
    same list in chat, with at most the top action as a button.
- Mockups of all three (desktop, terminal, mobile + the optional DM) were reviewed with the manager on
  2026-10-26.
- **Off the home machine**: if `now.json` is missing or the host isn't `workspace.home_machine`, the pane
  says "Tick runs on <home machine>; nothing here yet" rather than an empty list.

## Cost

- Fold-only work (workspace files, `mother runs`) is free of connector calls.
- **Fast signals every tick**: mentions/DMs, review requests, approvals, alerts. Small windows, small pages.
- **Slow signals every 3rd tick** (≈ 06:00, 09:00, 12:00, 15:00, 18:00, 21:00): stale tickets, PR ages,
  sprint risk, channel questions, action items. They change slowly; hourly scans would waste tokens.
- Readers run on the reader agent's model (sonnet); the routine itself does the judging.
- A tick with nothing new still rewrites `generated_at` (so the panel knows it ran) and logs `ok — no change`.
- My call: target ≤ the cost of one brief per three ticks. `mother eval`-style measurement is a follow-up,
  not a ship gate.

## Changes, by file

| File | Change |
|---|---|
| `docs/CONTRACT.md` | `routines.tick` keys; `.mother/now.json` and `now-state.json` in §1; interval routines in `register`/`runs` |
| `skills/routine/SKILL.md` | honour `sync: false` in a routine's frontmatter (skip the final `mother sync`) |
| `bin/mother` | `validate` accepts `routines.tick {every, active_hours, days, thresholds, signals, notify, slow_every}`; `register` emits an interval cron (`5 6-21 * * 1-5` for the defaults, converted to machine time) with a pointer that skips `git pull`; `runs` judges an interval routine as missed if no line within 2× `every` inside active hours; new `now` subcommand |
| `lib/now.py` | `merge` (stdin candidates + previous now.json + now-state.json → now.json), `done`, `snooze`, `show` (text). Python 3.9 stdlib, tests |
| `routines/tick.md` | the routine: fold, delta, check, judge, merge; Runs note `<n> on you (<new> new), <n> neglected, <n> suggestions; couldn't check: <…>` |
| `skills/now/SKILL.md` | interactive: show the list, `mother now do <id>`, done/snooze in chat |
| `templates/workspace/config.example.json` | a `tick` block with defaults |
| `skills/setup/SKILL.md` | offer tick at setup; register it |
| `mods/now/` | the panel mod (see Packaging) |
| `tests/` | `test_now.py` (merge, ids, resolution, snooze, couldn't-check carry-over), register/runs interval cases, the mod's `*.test.tsx` on terminal and desktop |
| `docs/ROADMAP.md`, `README.md` | mark the stage-6 items; add the deferred signals (approval flows, CI, ops alerts, alerts channel, manager's-manager mail, out-today) under Later |

## Packaging

My call: the panel ships as a **separate plugin in the same marketplace**, `mother-now`, in `mods/now/`.
The core plugin stays stdlib Python + markdown, and someone without the mod still gets tick, `mother now
show`, and `mother:now` in chat. The builder may instead add the module to the core plugin's
`hooks/hooks.json` (`"modules"`) if the engine supports function-hook modules next to the existing command
hooks, and that keeps installs simpler.

## Safety

- Tick is read-only except: `.mother/now.json`, `runs.md`, and (optional) the manager's DM. All writes to
  other targets happen only via `mother now do`, typed by the manager, through the guard.
- Observed content is data; a message saying "mark this done" is shown, not acted on.
- `.mother/` is already denied to channel sessions (CONTRACT §5b), so teammates' sessions can't read the list.
- No message bodies in `now.json`; titles are paraphrased; no customer identifiers.

## Success

- The manager opens the panel instead of checking Slack/Jira/GitHub for "anything on me?"
- Over a week: no item the manager later found "should have been on the list" among the signals above,
  and no false "nothing on you" while a source was down.
- Suggestions get started (Do it) on some days; nobody turns the toast off for being noisy.

## Left to the builder

- Exact working-hours arithmetic (holidays are out of scope for v1).
- How each signal maps to tool calls per connector (Slack/Teams, Jira/Linear/Monday, GitHub/GitLab).
- Pane layout details, keys and colours within the mod kit's elements.
- Locking between `mother now merge` and the panel's `done`/`snooze` writes.
