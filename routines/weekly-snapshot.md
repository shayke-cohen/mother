---
id: weekly-snapshot
team_scoped: false                # loops over config.teams itself
description: After each team's sprint rolls over — snapshot the new sprint, review the one that closed, append a metrics row. Silent.
config: routines.weekly-snapshot  # at, days; absent = off
default: { at: "16:00", days: workdays }   # runs daily, acts only on teams whose sprint just rolled over
needs: [tracker.read, tracker.sprint]
writes: [teams/<slug>/sprints/, teams/<slug>/metrics.md, teams/<slug>/runs.md]
sends: nothing
---

# Routine: weekly-snapshot

Keeps what the tracker forgets: what a sprint committed to at its start, what happened to it, and how
throughput moves week to week. A scheduled run starts with **no memory of any conversation**.

## Before this file

`mother:routine` has already resolved the workspace, run `mother home check`, stopped on `.mother/PAUSED`,
validated `config.json`, checked today against `days`, and loaded `capabilities.json`. It writes the
`runs.md` line from the **Runs note** below. If you were invoked directly rather than through the
dispatcher, do those first.

- `tracker.read` not `ok` → runs note `failed — tracker: <value>`; stop.
- `tracker.sprint` not `ok` → use the sprint field on items if queries expose it; otherwise
  `partial — no sprint access`; stop.
- Teams in scope: those in `config.teams` with `tracker.project` **and** `sprint`. Others are skipped with a
  note, not an error.
- A team is acted on only when a sprint has started since its newest `sprints/*.md` (rollover happened),
  so a daily schedule is cheap: most days it finds nothing to do.

## Steps

For each team in scope (read `rules/teams.md`, `teams/<slug>/TEAM.md`, the newest `sprints/*.md`,
`metrics.md`):

1. **Tracker reads.** Explicit page size and field list (summary, status, assignee, type, parent, labels,
   created, resolution date, sprint). Query by project; scope to `tracker.board` when sprint data carries a
   board. A full page that says it isn't the last is truncated — split the query, and **never write counts
   from a truncated read**.
2. **New sprint.** Find the active sprint. If `sprints/<its start date>.md` doesn't exist, write it from
   `${CLAUDE_PLUGIN_ROOT}/skills/team/templates/sprint.md`: `## Goals` (the tracker's sprint goal, or
   "none set"), `## Committed` (key — summary — assignee — status, `↻` for items carried from the previous
   sprint), `## Carryover` (open items in 3+ sprints), empty `## Review` and `## Decisions`. Old sprint
   still active → `partial — no rollover yet`; the next run catches up. Handle every sprint started since
   the newest file.
3. **Close out the previous sprint.** Fill its `## Review` if empty: done during the sprint (keys), slipped
   (still open, carried), added after the start (in the sprint, not in `## Committed`). Leave
   `## Decisions` alone — it's the manager's.
4. **Metrics.** Append one row to `metrics.md` for the sprint that closed: sprint | dates | committed | done
   | carried | bugs done | added mid-sprint | unassigned in sprint. **Never edit past rows.** A cell you
   couldn't compute is `?`, with the reason in the runs note.
5. Append to `teams/<slug>/runs.md`: `- <UTC ISO> weekly-snapshot <ok|partial|failed> — <note>`.

**Runs note:** `<slug>: new sprint <name>|no rollover, row appended|—; …`.

## Hard rules

- **Read-only over the tracker.** Write only under `teams/<slug>/`.
- Send nothing; the brief and meeting prep surface it.
- Never fabricate a number. `?` and a reason beat a guess.
- Counts are about items; no per-person scoring.
