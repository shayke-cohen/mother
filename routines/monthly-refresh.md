---
id: monthly-refresh
team_scoped: false                # loops over config.teams itself
description: Monthly — refresh stale per-repo notes and flag roadmap drift against the tracker; a short summary to the manager's DM.
config: routines.monthly-refresh  # at, days; absent = off
default: { at: "10:00", days: "monthly:1" }   # see note below
needs: [code.read, tracker.read, chat.post]
writes: [teams/<slug>/repos/, teams/<slug>/roadmap.md (## Changes only), teams/<slug>/runs.md]
sends: manager.chat_dm only
---

# Routine: monthly-refresh

A scheduled run starts with **no memory of any conversation**; everything it needs is here, in
`config.json`, and in the skills it names.

## Before this file

`mother:routine` has already resolved the workspace, run `mother home check`, stopped on `.mother/PAUSED`,
validated `config.json`, checked today against `days`, and loaded `capabilities.json`. It writes the
`runs.md` line from the **Runs note** below. If you were invoked directly rather than through the
dispatcher, do those first.

Schedule: once a month. If `days` can't express "day 1 of the month" in this version, schedule it on
workdays and stop here, `ok — not the first run this month`, when `runs.md` already has a monthly-refresh
line dated this month.

Without `code.read` skip step 1; without `tracker.read` (or a team without `tracker.project`) skip step 2;
name what was skipped.

## Steps

For each team in `config.teams` (read `rules/teams.md`, `teams/<slug>/TEAM.md`, `roadmap.md`):

1. **Repo notes.** For each repo in `repos` whose `teams/<slug>/repos/<owner>__<repo>.md` is missing or has
   a `**Checked:**` date 28+ days old:
   - Use the code host's API and code search only (`gh`, or a code-search connector). **Don't clone**;
     clones stall unattended runs.
   - Dispatch one read-only subagent per repo, at most 5 at a time. Give each the current note (revise,
     don't rewrite) and the team's `people[].code` handles. In a shared repo only the team's paths count
     (`CODEOWNERS`, or paths the team's PRs touch).
   - Each returns the note in the shape of `${CLAUDE_PLUGIN_ROOT}/skills/team/templates/repo.md`. Write it,
     keeping any line the manager added by hand, and set `**Checked:**` to today. A failed agent leaves the
     old date so the next run retries.
2. **Roadmap drift.** Check every epic named in `roadmap.md`, plus epics created or resolved in the last
   31 days. Append dated lines to `## Changes` for: epics that finished; new epics not on the roadmap;
   *Now* epics with no child resolved in 31 days; roadmap items with no epic. **Report drift; never move
   items between Now/Next/Later.**
3. Append to `teams/<slug>/runs.md`: `- <UTC ISO> monthly-refresh <ok|partial|failed> — <note>`.

Then DM the manager (`manager.chat_dm`, first line `agent.signature`), at most 10 lines: notes refreshed,
the drift lines, anything skipped.

**Runs note:** `<n> notes refreshed, <n> drift lines; skipped: <…>; DM sent|not sent`.

## Hard rules

- Read-only over the code host and the tracker. Write only under `teams/<slug>/`.
- Send only to `manager.chat_dm`.
- Never mark a note refreshed if its agent failed.
- "Active in the last 30 days" in a repo note is a list of handles, never a ranking.
