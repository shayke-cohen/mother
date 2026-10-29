---
name: team
description: Use when the manager talks about a team as a whole — "who's on the team", "update the roadmap", "start the sprint file", "notes from planning", "add this to intake", "what's in the queue". Reads and writes teams/<slug>/ (TEAM.md, roadmap, intake, sprints, meetings) and people/. Not a single project, not the daily digest, not meeting decks.
---

# Team

Full protocol: `${CLAUDE_PLUGIN_ROOT}/rules/teams.md`. Machine config (tracker, repos, channels,
meetings, sprint, people's ids) lives in `config.json` → `teams[]`; changing it is `mother:setup`'s job
(edit mode). This skill owns the human side under `teams/<slug>/` and `people/`.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mother" path
```

Its output is the workspace, `$W` below: write the path out literally (and `cd` to it if this session isn't there yet). Never put it in a shell variable or use `$(…)`: in the workspace the guard asks before any command with a `$` expansion, and an unattended run has no one to answer.

**After every write, check against `rules/teams.md` → Validation** and fix it before replying.

## Showing a team

```bash
cat teams/<slug>/TEAM.md teams/<slug>/roadmap.md
grep -E '^\- #[0-9]+ \[proposed\]' teams/<slug>/intake.md
ls teams/<slug>/sprints teams/<slug>/meetings | tail -5
grep -n '`+<slug>`' todos/open.md
```

Plus the team's entry in `config.json`. Lead with what's live: the current sprint, the next meeting, the
decision-queue size, open `+<slug>` items, and any **missing config key** — a gap is a reason a routine
can't do its job, so name it and point at `mother:setup`.

## Creating a team's files

When a team exists in `config.teams` but not under `teams/` (setup adds it; reflect flags the gap):

```bash
mkdir -p teams/<slug>/sprints teams/<slug>/meetings
```

Then, from `${CLAUDE_PLUGIN_ROOT}/skills/team/templates/`: `TEAM.md`, `roadmap.md`, `intake.md`,
`metrics.md`, `runs.md`. Add the row to `teams/TEAMS.md` (from `TEAMS.md` if missing). Fill the mission
from what the manager said; otherwise `_Needs sharpening by <manager_short>._`. The slug must equal the
config slug and must not collide with a project.

## People

`TEAM.md` → `## People` is one line per person: name — role — areas owned, linked to `people/<slug>.md`.
`people/<slug>.md` (template `person.md`) holds areas and **work items** over a dated window — tickets,
PRs, threads, meetings — refreshed on request from the team's tracker and code host. **Never** an
assessment, a rating, a comparison, mood, or anything personal (`rules/privacy.md` → People). Ids for
matching (chat, code handle, email) live in config, not here.

## Intake — the decision queue

Add team work that needs an owner or a ticket as a `proposed` item (grammar in `rules/teams.md`):

1. Dedupe against open intake items and existing tickets (search the tracker if `read` is `ok`).
2. Next free `#<id>`; one-line issue; source link; epic from `roadmap.md` or `none`; a **proposed owner
   with a work-fact reason** (owns the repo path, owns the area, last touched the code); a recommendation
   `ticket` / `drop` / `mine`; priority; today's date.
3. No customer names, ids or emails — the link is enough.

The digest shows the queue and applies the manager's answers. In a session, the manager can decide
directly ("make 12 a ticket"): that message is the yes for that one create — show the exact ticket
first (project, title, description, label `triage`, unassigned, signature footer), create it through the
guard, then set the status and log it under `## Applied` with the session date.

## Sprint files — `sprints/YYYY-MM-DD.md` (sprint start date)

From `templates/sprint.md`: `## Goals`, `## Committed`, `## Carryover`, `## Review`, `## Decisions`.
Snapshot `## Committed` from the tracker (key — summary — assignee — status, `↻` for carried items);
never hand-type ticket lists. The weekly-snapshot routine does this automatically when configured.
`## Decisions` is the manager's.

## Meeting files — `meetings/YYYY-MM-DD-<kind>.md`

From `templates/meeting.md`. Before: `## Agenda` (open `+<slug>` todos, the decision queue, risky sprint
items, roadmap changes). After: `## Decisions` and `## Follow-ups` — every follow-up also becomes a todo
or an intake item. Distil a connector's meeting summary to decisions and owners; never paste a
transcript. Decks and meeting mode are `mother:meeting`.

## Roadmap

Now / Next / Later plus `## Changes`, a dated line with a reason per move. Link items to epics when the
tracker is known. **Report drift; don't rewrite the plan** — moves are the manager's call.

## Outbound

Anything for the team — an agenda in a channel, a tracker create or transition, an invite — is a draft
shown to the manager, then executed through the guard on their yes (`rules/outbound.md`). Never assign
owners in the tracker on your own.
