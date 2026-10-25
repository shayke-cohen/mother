---
id: one-on-one
team_scoped: false
description: Morning 1:1 prep for each of today's 1:1s (agenda kept apart from private prep), plus cadence-drift alerts for reports whose 1:1s lapsed.
config: routines.one-on-one       # at, days; per person: teams[].people[].one_on_one {cadence_days, title}; absent = off
default: { at: "07:30", days: workdays }
needs: [calendar.read, tracker.read, code.read, chat.read, chat.post]
writes: [one-on-ones/<slug>/<date>.md, one-on-ones/<slug>/next.md, people/<slug>.md]
sends: manager.chat_dm only
---

# Routine: one-on-one

A scheduled run starts with **no memory of any conversation**; everything it needs is here, in
`config.json`, and in the skills it names.

## Before this file

`mother:routine` has already resolved the workspace, run `mother home check`, stopped on `.mother/PAUSED`,
validated `config.json`, checked today against `days`, and loaded `capabilities.json`. It writes the
`runs.md` line from the **Runs note** below. If you were invoked directly rather than through the
dispatcher, do those first.

Calendar read must be `ok`: without it there's no way to know who has a 1:1 today. If it isn't, stop with
`failed — calendar read <value>`.

## Steps

1. Run the **`mother:one-on-one`** skill → Find the 1:1s, for today in `manager.timezone`.
2. For each, write the prep file (skill → Prep). Don't overwrite a prep file that already has `## Notes`.
3. Run the skill's **Cadence drift** check for every configured person.
4. If there's any prep or drift, post one message to `manager.chat_dm`, first line `agent.signature`:

   ```
   *1:1s today*
   • 11:00 Sam — 4 agenda items, 2 open commitments — one-on-ones/sam/<date>.md
   *Drifting*
   • Alex — last 1:1 30 Sep (25 days, cadence 14) — free: Tue 14:00, Wed 10:30
   ```

   Never put Private prep in the message. Nothing to say → send nothing.

Never share an agenda, create an invite or message a report from this routine: those need the manager's
live yes.

**Runs note:** `<n> prepped, <n> drifting; DM sent|quiet; couldn't check: <…>`.
