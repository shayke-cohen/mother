---
name: meeting
description: Use for a team's weekly meeting — build the deck and agenda, share it on the manager's yes, apply pasted "meeting results" (owners, decisions, tickets, sprint rollover) to the tracker, and draft the recap / sprint list. Triggers on "prep the weekly", "build the deck", "share the deck", a paste starting with "mother approve tracker" or "— meeting results", "draft the recap".
---

# Meeting

One weekly meeting per team in `config.json` → `teams[].meetings.weekly`. Four jobs, in order of the week:
**prep → share → apply results → recap.** The deck is the one artifact that carries them all.

Before anything: resolve the workspace (`${CLAUDE_PLUGIN_ROOT}/bin/mother path`), read `config.json` and
`capabilities.json`. A capability that isn't `ok` is named in the output and in the deck's `meta.warnings`,
never silently dropped. Outbound rules: the plugin's `rules/outbound.md`.

## 1. Prep (routine `meeting-prep`, or on request)

Build `teams/<slug>/meetings/<date>-deck.json` (date = the meeting's date) and render it:

```
python3 "${CLAUDE_PLUGIN_ROOT}/assets/deck/build.py" teams/<slug>/meetings/<date>-deck.json teams/<slug>/meetings/<date>-deck.html
```

Copy the shape of `${CLAUDE_PLUGIN_ROOT}/assets/deck/example.json`; the schema is in `assets/deck/README.md`.
Fill it from:

| Section | Source |
|---|---|
| `meta` | config: `agent.name` → `agent`, meeting title → `meeting`, team slug, `tracker.project` → `project`, tracker browse URL → `tracker_url`, roster names → `roster`; sprint ids for `sprintSetup` (current and next sprint on the team's board) |
| `pipeline` | the team's main queue (whatever the team's "work waiting" is: support queue, tickets in a state, PRs); stuck > 14 days split *ours* vs *customer/external*; trend from `teams/<slug>/metrics.md` |
| `sprint` | the current sprint: done / in progress / to do, shipped work **grouped by stream, never by person** |
| `experts` | "voices from the field": support, ops or customer-facing channels in config (`title` names the group) — themes counted as threads, no customer details |
| `intake` | `teams/<slug>/intake.md` items still `proposed`, plus unassigned tracker items labelled for triage; each with a proposed owner and the reason (area, repo ownership) |
| `carryover` | open sprint items that have been in 3+ sprints (count the sprint history field) with a recommendation |
| `next` | proposed goals, one per stream, each with a done-test — a **proposal** for the room |
| `roadmap` | moving / stalled / new-not-on-roadmap, against `teams/<slug>/roadmap.md` |
| `decisions` | what the room must close, each with a recommendation |
| `backlog` | every open, non-epic tracker item (key, summary, status, assignee, priority) for the meeting-mode picker. Trackers cap page sizes: read in slices (e.g. by created date) until each slice is complete |

Content rules (the deck is shown to the team): no customer names or identifiers, no person scoring, every
number names its source and time (`meta.sources`, `meta.generated`). Also write
`teams/<slug>/meetings/<date>-meeting.md` with the agenda in text. Then DM the manager: the deck path, the 5
lines that matter most, and "say `share` to publish it to the team before the meeting".

## 2. Share (on the manager's "share")

Publish the deck where the team can open it — whatever `teams[].meetings.weekly.publish` names
(`file` = just the path, or a hosting connector the manager configured). Post the link to the team channel
**only with the manager's yes for that post**, signed per `rules/outbound.md`. Never share a deck that still
carries `meta.warnings` the manager hasn't seen.

## 3. Apply meeting results (the manager pastes them)

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
- Don't edit `assets/deck/template.html` for one meeting — change the JSON.
