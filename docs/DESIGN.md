# MOTHER — design

Living document. Public version of the design used by the reference deployment.

## Shape

```
mother plugin (shared, versioned)               ~/<manager>-workspace (private, local; optional git remote)
├─ skills/   setup · doctor · team · digest ·    ├─ config.json      agent, people, teams, projects,
│            brief · weekly · dots · meeting ·    │                   channels, trackers, repos, meetings
│            triage · todo · project · reflect    ├─ capabilities.json   written by the doctor
├─ routines/ templates (no IDs; read config)      ├─ teams/ projects/ people/ memory/ todos/ journal/
├─ assets/   meeting deck, dashboards             └─ runs.md          every routine logs here
├─ agents/   read-only sub-agents: reader, critic
├─ hooks/    approval guard (pre-tool)
└─ rules/    outbound policy, privacy defaults
```

### `/mother:setup`: discover, then confirm

1. Who you are: email, calendar time zone, working days.
2. Who you manage: from the org directory if one is connected, otherwise from calendar 1:1s, chat DMs and
   tracker assignees. The result is a draft roster.
3. Where the work is: tracker projects and boards your people work in, repos (team membership and
   CODEOWNERS), and the chat channels where your people talk.
4. Rhythm: recurring meetings with those people, used to propose standup, weekly and 1:1 cadences.
5. The agent: its name, emoji and how it refers to you, with a collision check against the roster.
6. Show the whole plan, write nothing until you confirm, then run the doctor and register the routines.

### `/mother:doctor`: live capability matrix

For every connector, it makes a real read call. Write checks (create, comment and close a test item) run
only when you opt in. It also records the token type (user or bot), scopes and gaps. The output is
`capabilities.json`, which routines read first: a missing capability is reported as "couldn't check", never
silently skipped. The doctor runs daily and heals what it can without you: retry, reconnect, fall back to
another server, and only then you (one step, with the agent doing the rest). Routines pre-flight the
capabilities they need, and alert once a day when only you can fix something.

### Routines

| Cadence | Routine |
|---|---|
| Daily | Brief; team digest opening with the **decision queue** (at most 5 items, each with a recommendation; answer in one line) |
| Daily | **Connect the dots**: cross-source patterns, e.g. an unanswered ask, a stalled ticket and an escalation that are all the same problem. Two outputs: actions *for you*, and actions *for the agent* (run on your yes) |
| Weekly | Meeting prep: a deck built at a set hour before the meeting so you can review it. **Meeting mode** lets you pick owners, record decisions, add or close tickets and set up the sprint, then export results that the agent applies to the tracker |
| Weekly | Recap draft for the team channel (posted on your yes); sprint snapshot and metrics |
| Monthly | Roadmap drift |
| Nightly | Reflection: distil durable facts to memory, expire stale self-captured items |

## Agent identity

```yaml
agent:
  name: Agnes
  emoji: ":sparkles:"
  manager: Dana Levi
  manager_short: Dana
  signature: "{emoji} *{name} (AI)* · on behalf of {manager_short}"   # "(AI)" is not optional
```

Connectors usually act **as the manager**, so without a signature people would read the agent's words as the
manager's. Every outbound artifact is signed:

| Surface | Disclosure |
|---|---|
| Chat message | First line `✨ *Agnes (AI)* · on behalf of Dana`; in a thread unless the channel allows top-level posts |
| Tracker comment or new item | Footer `— Agnes (AI), on behalf of Dana` |
| Email | Drafts only; the manager sends |
| Deck or recap | "Prepared by Agnes (AI) for Dana · sources · generated <time>" |
| Calendar invite | "Scheduled by Agnes (AI) on behalf of Dana" |

Longer term: a dedicated chat app identity (bot user, custom display name and avatar), so posts no longer
appear under the manager's name.

## Channel reply modes

| Mode | Reads for digests | Replies | Trigger | Can act |
|---|---|---|---|---|
| `off` | no | never | — | — |
| `watch` | yes | never | — | — |
| `mention` | yes | in thread, when addressed by name | `manager` / `team` / `anyone` | answers; **writes only if the manager triggered it** |
| `draft` | yes | drafts to the manager's DM; posts on approval | agent decides what qualifies | posts the approved text |
| `auto` | yes | posts itself for allowed topics; otherwise falls back to `draft` | agent | answers; writes still need the manager |

**Guardrails in every replying mode:**
- Writes need the manager.
- Evidence or silence.
- Threads only.
- External (shared) channels are capped at `mention` triggered by the manager, and leak nothing internal.
- Rate limits and quiet hours.
- `mute` per thread, and 👎 also mutes.
- Messages from other bots are ignored.
- Kill switch: `<name>, stop` in a channel, and `<name>, pause` for every channel.
- Every post is written to an audit log.

**As built (v1, `mother:channels`):** a loop session polls the replying channels and hands every decision to
`mother channels` (CONTRACT §5b); each answer is written by its own headless session, read-only and without
mail or calendar when a teammate asked. Limits today: answers only — a channel session never writes to the
tracker, even when the manager asked (it drafts the change in its reply instead); no status reactions;
mentions inside other people's threads need `chat.search`; tested against Slack's message shape.

**Earned autonomy:** a topic in a channel can move from `draft` to `auto` once 10 drafts in a row went out
unedited. The agent proposes the move; the manager decides. A 👎 sends that topic back to `draft`.

**Defaults:**

| Channel kind | Mode |
|---|---|
| Manager's own DM | `auto` |
| Team channel | `mention` (trigger: team) |
| Cross-team, product or support channel | `draft` |
| External channel | `mention` (trigger: manager) |
| Announcements | `watch` |
| Anything else | `off` |

## Sub-agents: a hub and read-only spokes

MOTHER is the only agent that judges, writes or speaks. It doesn't run a "team" of persona agents (PM, dev,
UX): the manager's real team already fills those roles. Studies of role-play multi-agent frameworks show
they mostly fail on handoffs and lost context, not on model quality. What sub-agents are good at is the
opposite job: reading something large and noisy in a fresh context, and handing back a short, cited result.
- **Readers** gather one source each, in parallel, and return JSON items with links. Raw mail and chat never
  enter MOTHER's context, only what survived `mother items check`. That is evidence-or-silence enforced in
  code, and a failed reader shows up as "couldn't check", never as quiet.
- **Critic**: one read-only review of a draft before anyone else sees it. It checks citations, the signature,
  people-not-scores, privacy and voice. It is advisory: the guard and the manager stay the gates.
- **No agent-to-agent chat and no nested agents.** Shared state is the workspace, and only MOTHER writes it.
- **Proved before it ships.** The brief keeps `gather: inline` until `brief-ab` (blind A/B on the same inputs,
  scored for per-source coverage, citations, false quiet and cost) says `ship`. Dots and digest follow only after that.

## The hub: a watcher, one pinned session and cards

The tick rescans sources hourly, so something that changed at 10:05 reaches the manager at 11:00. The hub
closes that gap without a model polling. The 2026-10-29 spikes decided the shape:
- **Plain code can read the sources.** A Python script spoke MCP over stdio to the gateway and read tracker and
  mail with no model, so the watcher is a launchd process, read-only by construction (allow-list enforced
  before send), and costs nothing while nothing changes.
- **A timer-fired turn cannot start a session.** `start_session` was refused without the user's own words, so a
  new session can only be proposed as a card the manager clicks. Hence cards, never auto-spawned sessions.
- **It can message an existing one.** `send_message` from a cron-fired turn was delivered and acted on, so
  follow-ups for a thing that already has a session go straight to it (`sessions.json` maps thing to session).
- **A CLI session is not a desktop session.** `claude -p` runs did not appear in the sidebar, so sub-sessions
  are desktop sessions the manager opened.
So: watcher appends to an inbox, one pinned hub session wakes only on a new line and decides (log, message,
or card), and the manager stays the one who starts anything. Nothing here scores or reads a person; the inbox
keeps a short summary per thing, never message bodies or per-person counts.

## Safety

- **Approvals are enforced in a pre-tool hook, not only in the prompt.** Prompt-only rules can vanish when
  context is compacted; agents have deleted mail that way.
- Observed content (mail, chat, docs) is data, never instructions.
- No credentials on disk; a secret scan runs before every commit of the workspace.
- People data is work items only: no performance assessments, no mood tracking. Setup offers a short
  "what the agent reads" note for the team.
- Digests keep disagreements visible instead of smoothing them into one summary.

## Ideas queue

Ideas from prior art, with sources, to be pulled in after v0:
- **Commitment tracker** ✅ `mother:commitments`: promises you made and messages that got no reply, as in
  Microsoft's daily-briefing research.
- **Per-report 1:1 prep** ✅ `mother:one-on-one`: what changed since the last 1:1, with private prep kept apart from a shared agenda
  (Fellow, tech-manager-os).
- **Meeting mode:** only *accepted* action items reach the tracker.
- **Working agreements:** alert only when one is broken (Swarmia).
- **Ticket-readiness check** before planning (Rovo).
- **Standup hygiene:** time zones, OOO, skipping (Geekbot, Range).
- **Manager kit:** skip-levels, hiring loops, onboarding plans, a brag doc.
