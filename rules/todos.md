# Open loops — protocol

Work committed to and not yet done. Two files in the workspace:

| File | What |
|---|---|
| `todos/open.md` | the live list |
| `todos/archive.md` | done and expired items; append-only, never pruned |

Team work that needs an owner and a ticket is **not** a todo — it goes to the team's intake queue
(`teams/<slug>/intake.md`, see `rules/teams.md`). `todos/open.md` holds what the **manager** or the
**agent** owes.

## Why this isn't memory

Memory holds durable facts matched against future recall. A todo is transient by definition: "reply to
Sam" in `memory/` pollutes recall and is stale within a week. Different lifetime, different store.

## Sections

Exactly three, always present, in this order. An empty section keeps its heading and holds `_none_`.

- **Yours** — the manager's to do.
- **Mine** — the agent's to do: things it promised or took on. Only these are the agent's to close.
- **Blocked** — real, not actionable now, still wanted in view.

## Line grammar

```
- [ ] <text> [`+<slug>`] [`due:YYYY-MM-DD`] `~<origin>@YYYY-MM-DD`
```

- `- [ ]` open; `- [x]` closed but not yet swept.
- `<text>` — free text, **no backticks**. Short enough to scan, specific enough to act on: "Reply to Sam
  re: billing cutover date", not "Sam".
- `` `+<slug>` `` — optional, first among the tokens. Binds the item to `projects/<slug>/` or
  `teams/<slug>/`. Most items belong to neither; that is normal.
- `` `due:YYYY-MM-DD` `` — optional; a real date only. Never invent one.
- `` `~<origin>@YYYY-MM-DD` `` — **required, always the last token.** The date is when the item was added.

Archived lines add an outcome token **before** the origin, so "origin is last" holds in both files:

```
- [x] Reply to Sam re: cutover `done:2026-08-28` `~agent/chat@2026-08-27`
- [ ] Read the Q2 retro deck `expired:2026-08-24` `~agent/mail@2026-08-10`
```

No ids, no priority field, no other tokens. The grammar is deliberately small.

### Origins

| Origin | Meaning | Expires? |
|---|---|---|
| `manager` | the manager asked for it, or adopted it | **never** |
| `agent/session` | inferred from conversation, including the agent's own promises | 14 days |
| `agent/chat` | inferred from chat | 14 days |
| `agent/mail` | inferred from mail | 14 days |
| `agent/calendar` | inferred from a calendar event | 14 days |
| `agent/tracker` | inferred from the tracker | 14 days |
| `agent/code` | inferred from the code host | 14 days |
| `agent/digest` | from the team digest (`mother:digest`) | 14 days |
| `agent/dots` | from a connect-the-dots pass (`mother:dots`) | 14 days |
| `agent/meeting` | from a meeting's results (`mother:meeting`) | 14 days |

## Capture — free, with provenance

The agent adds items **without asking**: what the manager asks for (`~manager`) and commitments it
notices (`~agent/<source>`). Provenance is what makes free capture safe: unadopted `~agent/*` items expire
on their own, so the list self-cleans and nobody has to garden it.

Dedupe before writing. A near-duplicate is worse than a miss.

**`mother:brief` never writes here.** Its read-only contract is what makes it safe to run unattended. It
reads the list and surfaces overdue and aging items; capture happens in sessions, `digest`, `dots` and
`reflect`.

## Closing

- **The agent closes it** — move the line to `archive.md` with `` `done:<today>` `` before the origin.
- **The manager ticks the box in an editor** — the line becomes `- [x]` and stays until swept. Any
  invocation of `mother:todo`, and `mother:reflect` nightly, sweeps `- [x]` lines into the archive.

**Never close on inference.** A sent mail is not proof the item is done; a ticket marked Done is not
proof the manager's follow-up happened. Report the suspicion ("this looks done — close it?"); don't
apply it.

## Expiry and adoption

`mother:reflect` moves any line still `- [ ]`, with an `~agent/*` origin whose date is **14 or more days**
old, to `archive.md` with `` `expired:<today>` ``. `~manager` items never expire, at any age.

To keep an inferred item, **adopt** it: change `~agent/chat@2026-08-27` to `~manager@2026-08-27`,
keeping the original date. That is the whole operation. A `+<slug>` tag does **not** exempt an item from
expiry, and closing a project or team does **not** close its items.

## Validation (by hand, after every write)

- The three section headings exist, in order; empty sections hold `_none_`.
- Every item line ends with `` `~<origin>@YYYY-MM-DD` ``, origin from the table above.
- No backticks inside `<text>`; tokens appear in the order `+slug`, `due:`, (outcome), origin.
- Every `+<slug>` names an existing `projects/<slug>/PROJECT.md` or `teams/<slug>/TEAM.md`.
- `archive.md` lines carry exactly one outcome token (`done:` or `expired:`).

## Sync

Don't commit by hand; `mother sync` runs on Stop and SessionEnd.
