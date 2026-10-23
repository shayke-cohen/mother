---
name: project
description: Use when the manager starts or discusses work that spans sessions — "start a project for X", "status of the X project", "add a milestone", "who's on this", "file this doc under the project", "close the project". Creates and updates projects/<slug>/PROJECT.md and files/, and reads its tagged todos. Not for a single action item.
---

# Project

The container for work that spans sessions. Full protocol: `${CLAUDE_PLUGIN_ROOT}/rules/projects.md`.

```bash
W="${MOTHER_WORKSPACE:-$(cat ~/.mother/workspace)}"; cd "$W"
```

**After every write, check the store against `rules/projects.md` → Validation** and fix it before
replying.

**A project never holds a task list.** Action items go to `todos/open.md` tagged `` `+<slug>` `` via
`mother:todo`. A project *and* something to do is two writes to two stores.

## Showing a project

```bash
cat projects/<slug>/PROJECT.md
ls -la projects/<slug>/files/
grep -n '`+<slug>`' todos/open.md
```

Lead with what's live: the next milestone, anything overdue, open items. Goal and notes are context, not
the answer. If `status` isn't `active`, say so first — a paused project's old date is not a problem.

## Creating

1. Check for an existing one: `ls projects/ teams/`. Work that belongs to a live project goes *in* it; a
   slug must not collide with a team slug.
2. Slug is kebab-case. `mkdir -p projects/<slug>/files`.
3. Write `PROJECT.md` from `${CLAUDE_PLUGIN_ROOT}/skills/project/templates/PROJECT.md`. Write what the
   manager actually said; an unclear goal gets `_Needs sharpening by <manager_short>._`. **Omit `due:`
   rather than invent one.**
4. Add the row to `projects/PROJECTS.md` (create it from `templates/PROJECTS.md` if missing). A project
   missing from the index is lost.

## Updating

- **Milestone** — `- [ ] YYYY-MM-DD — <what>` under `## Milestones`; absolute dates. Hit → `- [x]`, kept
  as the record.
- **People** — name and what they own, one line; link `[[their-memory]]` if one exists. Work facts only.
- **Notes** — running context and decisions with reasons. When something becomes durably true,
  **graduate it to memory** via `mother:remember` and link it.
- **Status** — `active` | `paused` | `done` | `dropped`; update the index row to match.

## Filing a document

1. **Check size first.** Over ~1 MB: don't copy it in; add an `(external)` pointer instead.
2. Check content: no credentials, no customer or employee PII, no personal contact, health, compensation
   or performance content. `mother scan` skips binaries — look at an image or PDF yourself.
3. Put it in `projects/<slug>/files/` and add an annotated line to `## Files` saying **why it matters**.

## Closing

Set `status: done` (or `dropped`), update the index row, and **leave its todos alone**. Name any open
`+<slug>` items and say plainly you left them open. Nothing is deleted.

## Renaming

All three steps in `rules/projects.md` → Renaming, including rewriting `+<old>` in both todo files.

## Report back

One line: what changed, where, validation result. Filed a document → where it landed and that it's
indexed. Closed with open todos → name them.
