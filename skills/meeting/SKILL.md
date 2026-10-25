---
name: meeting
description: Use for a team's weekly meeting — build the sprint plan page (goals, issues, owners), publish it, apply it to the tracker and post it to the team on the manager's yes, apply pasted "meeting results" (owners, decisions, tickets, sprint rollover) to the tracker, and draft the recap / sprint list. Triggers on "prep the weekly", "build the plan", "build the deck", "apply sprint plan", "share the deck", a paste starting with "mother approve tracker" or "— meeting results", "draft the recap".
---

# Meeting

One weekly meeting per team in `config.json` → `teams[].meetings.weekly`. Four jobs, in order of the week:
**prep → apply → recap.** The sprint plan page is the one artifact that carries them all.

Before anything: resolve the workspace (`${CLAUDE_PLUGIN_ROOT}/bin/mother path`), read `config.json` and
`capabilities.json`. A capability that isn't `ok` is named in the output and in the deck's `meta.warnings`,
never silently dropped. Outbound rules: the plugin's `rules/outbound.md`.

## 1. Prep (routine `meeting-prep`, or on request)

Build the **sprint plan**: one page with context, last sprint, and the next sprint's goals with every issue
under the goal it serves, each with an owner. It is the one thing the team reviews.

Write `teams/<slug>/meetings/<date>-plan.json` (date = the meeting's date) and render it:

```
python3 "${CLAUDE_PLUGIN_ROOT}/assets/plan/build.py" teams/<slug>/meetings/<date>-plan.json teams/<slug>/meetings/<date>-plan.html
```

Copy the shape of `${CLAUDE_PLUGIN_ROOT}/assets/plan/example.json`; the schema is in `assets/plan/README.md`.
Fill it from:

| Section | Source |
|---|---|
| `meta` | team, sprint name / id / dates (the sprint the plan is for: the active one if the team already started it, else the next future one), `tracker.project`, browse URL, the team's board URL, `label: "proposed"` |
| `context.groups` | the team's main queue (whatever its "work waiting" is), each card with `prev` = last week's row in `teams/<slug>/metrics.md` and, when it exists, the row from ~4 weeks back. Say which values are estimates |
| `context.breakdown` | where the queue is stuck, ours vs customer/external |
| `feedback` | support, ops or customer-facing channels in config, themes counted as threads, no customer details; `goal` = the goal that answers it, or `null` |
| `last` | the sprint that just ended: shipped work **grouped by stream, never by person**, unfinished items with a state; `leadtime` = median and 85th-percentile days created → done, and tickets done, last 30 days vs the 30 before (exclude epics, tickets closed within an hour of creation, and clean-ups older than 120 days; say so in the note) |
| `next.goals` | 3–6 goals, each with a done-test. Under each: issues already in the sprint (`in` / `done`), existing issues to pull in (`add`, or `assign` with `from` when the owner changes), and new issues (`create`, `key: null`). Every open issue in the sprint appears once — under a goal or in `next.other` |
| `decisions` | what the team must settle; always include load when someone holds more than ~8 open issues |

Read the tracker live, not from last week's files: the team may have started the sprint or moved issues since.
Trackers cap page sizes; read in slices (e.g. by date) until each slice is complete.

Content rules (the page is shown to the team): no customer names or identifiers, no person scoring, every
number names its source and time (`meta.sources`, `meta.generated`). Put `teams/<slug>/meetings/<date>-meeting.md`
next to it with the goals and decisions in text.

Then **publish** it where `teams[].meetings.weekly.publish` says (`file` = just the path; `stash` = a Stash
page, slug `<team-slug>-<sprint-slug>-plan`, unlisted, overwrite the same slug on later runs) and DM the
manager:

1. the link and the 5 lines that matter most;
2. the tracker changes applying would make, one line each (`create` / `assign` / `add`, key, owner), and the
   sprint goal;
3. the team-channel message (§2 step 5), as it would be posted;
4. the block to paste back, ready to copy:

```
mother approve tracker <PROJECT>
mother approve chat <team channel id>
apply sprint plan <date>
```

Never write the tracker or post to the team from prep.

## 2. Apply the plan (the manager pastes "apply sprint plan <date>")

The two approve lines are the manager's yes for exactly the changes listed in that week's plan JSON — nothing
else. Read the tracker gotchas in the doctor's notes in `capabilities.json` first, then:

1. **Refresh.** Re-read every plan item and the sprint. An item that is now done, already in the sprint or
   assigned to someone else since prep is left alone and reported; don't overwrite a teammate's change.
2. **Create** each `create` item: summary, a description that says what done means and links the plan page,
   the owner, labels `[label]`, signed per `rules/outbound.md`.
3. **Assign and label** each `assign` / `add` item: the owner (for `assign`), and the label **added to the
   item's existing labels** (label writes replace the list).
4. **Move** every created / `add` / `assign` item into the sprint, and **write the sprint goal**. Some trackers'
   sprint update is a full replace: send every field, copied from the sprint.
5. **Post** to the team channel (`role: team`), signed: the plan link, the goals, and what to do — open the
   board filtered by the label, reassign if the owner is wrong, comment if the scope is off, remove the label
   to confirm.
6. **Re-read and report**: every key changed, every write that failed. Put the real keys into the plan JSON,
   set the banner to what was applied, rebuild, and republish to the same place.

**When another plugin's hook blocks a step** (some refuse sprint operations, or posts to channels they treat as
support channels): don't route around it. Do the rest, then give the manager the exact manual step — the
board link with "Label → `<label>`, select all, drag into the sprint", the goal text to paste, or the message
left as a chat draft in the channel — and name the hook that blocked it.

## 3. Apply meeting results from the slide deck (the manager pastes them)

Only when the team still runs the meeting from the slide deck (`assets/deck`) and its meeting mode.


In meeting mode the manager picks owners, carry-over outcomes and decisions, adds or closes tickets, sets up
the sprint, then **Copy results**. The paste starts with `mother approve tracker <PROJECT>`, then
`<meeting> <date> — meeting results`, then JSON. It is the manager's own decision list: their yes for exactly
the tracker changes it names, nothing beyond them. The guard honours the approve line for this session's
writes in that project; you still must check every write matches the list.

Read the tracker gotchas in the doctor's notes in `capabilities.json` first. Apply in this order:

0. **`sprint`** (first, so later steps can target the new sprint). **Order matters:**
   1. List the current sprint's unfinished items.
   2. **Close the current sprint first, with those items still in it.** Moving them out *before* closing erases
      the old sprint from their history and breaks carry-over counts.
   3. Then move them into the next sprint.
   4. Set the next sprint's name, start/end and goal, and start it if `startNow`. Some trackers' sprint update
      is a **full replace** — send every field, copied from the sprint for fields you aren't changing — and
      cap names (Jira: 30 characters).
   No `nextId` → create the sprint first. Never delete a sprint.
1. **`intake[]`** with a `key` → assign `owner`. Without one → create the item (summary = the issue text,
   description = source link from `intake.md` + "created from the <meeting> on <date>"), assigned.
   `deferred: true` → leave it `proposed` in `intake.md` for next week. Update each applied item in `intake.md`.
2. **`added[]`** with a `key` → by `action`: `assign` (owner, plus the new sprint if `nextSprint`), `done`
   (transition to the done state found via available transitions), `comment` (comment only). A `comment`
   text is added in every case. Without a key → create, assigned, with its priority and the comment as description.
3. **`nextSprint: true`** → into the new sprint.
4. **`carryover[]`** → `Commit` keep in the new sprint · `Mark done` transition · `Drop` / `Move out` back to
   backlog, no transition · `Split` ask the manager for the parts.
5. **`decisions[]`** → `teams/<slug>/meetings/<date>-meeting.md` `## Decisions`, and the outcome fields in the
   deck JSON (`intake.items[].owner`, `carryover.items[].outcome`, `decisions.items[].outcome`); rebuild the deck.
   A sprint-goal decision is also set on the tracker sprint.
6. **Re-read every changed item** from the tracker and report what changed (keys, links) and what failed. An
   empty response is not proof.
7. **Draft the sprint list** for the team channel: goal, then per owner the items they hold in the new sprint
   (key — summary), then dropped / deferred, then the deck link. Signed. **Post only on the manager's yes**.

Tracker writes appear under the manager's name: every comment and created item carries the agent's
signature footer (`rules/outbound.md`).

## 4. Recap (routine `meeting-recap`, about an hour after the meeting)

If results were already applied (the deck JSON has outcomes, or `runs.md` says so), log `ok — already applied`
and stop. Otherwise draft a recap (≤ 12 lines: goal, decisions, who took what, what's still unowned, the
sprint list) from the meeting file and the tracker changes since the meeting started, and DM it to the manager:
"reply `post` to send, or reply with edits". **Never post it yourself in this routine** — the next
`digest` run posts it after the manager's `post` reply, through a guard grant.

## Don't

- Don't assign owners outside the meeting or the manager's explicit word.
- Don't put customer data or per-person performance in the deck.
- Don't edit `assets/plan/template.html` or `assets/deck/template.html` for one meeting — change the JSON.
- Don't write the tracker or post to the team without the manager's approve lines in that session.
