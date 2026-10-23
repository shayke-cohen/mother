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
├─ hooks/    approval guard (pre-tool)
└─ rules/    outbound policy, privacy defaults
```

### `/mother setup`: discover, then confirm

1. Who you are: email, calendar time zone, working days.
2. Who you manage: from the org directory if one is connected, otherwise from calendar 1:1s, chat DMs and
   tracker assignees. The result is a draft roster.
3. Where the work is: tracker projects and boards your people work in, repos (team membership and
   CODEOWNERS), and the chat channels where your people talk.
4. Rhythm: recurring meetings with those people, used to propose standup, weekly and 1:1 cadences.
5. The agent: its name, emoji and how it refers to you, with a collision check against the roster.
6. Show the whole plan, write nothing until you confirm, then run the doctor and register the routines.

### `/mother doctor`: live capability matrix

For every connector, it makes a real read call. Write checks (create, comment and close a test item) run
only when you opt in. It also records the token type (user or bot), scopes and gaps. The output is
`capabilities.json`, which routines read first: a missing capability is reported as "couldn't check", never
silently skipped. The doctor re-runs weekly, because sign-ins expire.

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
- **Commitment tracker:** promises you made and messages that got no reply, as in Microsoft's daily-briefing
  research.
- **Per-report 1:1 prep:** what changed since the last 1:1, with private prep kept apart from a shared agenda
  (Fellow, tech-manager-os).
- **Meeting mode:** only *accepted* action items reach the tracker.
- **Working agreements:** alert only when one is broken (Swarmia).
- **Ticket-readiness check** before planning (Rovo).
- **Standup hygiene:** time zones, OOO, skipping (Geekbot, Range).
- **Manager kit:** skip-levels, hiring loops, onboarding plans, a brag doc.
