---
name: remember
description: Use when the manager says "remember this", "note that", "don't forget", or you learn a durable fact worth keeping — who owns what, a stated preference, a project constraint, a correction to how you work, a pointer to a doc. Writes one fact to memory/ with frontmatter, dedupes, updates MEMORY.md. Not for todos or conversation-only facts.
---

# Remember

Write one durable fact into `memory/` so future sessions and routines find it. Full schema and policy:
`${CLAUDE_PLUGIN_ROOT}/rules/memory.md`.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mother" path
```

Its output is the workspace, `$W` below: write the path out literally (and `cd` to it if this session isn't there yet). Never put it in a shell variable or use `$(…)`: in the workspace the guard asks before any command with a `$` expansion, and an unattended run has no one to answer.

## 1. Is it worth storing?

Skip it, and say why in one line, if it is:
- relevant only to this conversation;
- already recorded in `config.json`, code or git history;
- unconfirmed speculation;
- on the never-write list in `rules/privacy.md` (credentials, PII, raw mail or chat, assessments of
  people);
- a todo — that's `mother:todo`.

If the manager explicitly asked to remember something in the first two buckets, don't just refuse: ask
what was non-obvious about it and store *that*.

## 2. Check for an existing memory

```bash
grep -ril "<key term>" memory/ --include='*.md'
```

and scan the descriptions in `memory/MEMORY.md`. **If a memory covers it, update that file** — a second
file saying something slightly different is how memory rots.

## 3. Distil

The fact and its consequence, not the raw material: "Sam Roe owns the billing migration; prefers a
written weekly update over a meeting." Absolute dates (`2026-09-03`, not "next Thursday"). About a person:
work facts only.

## 4. Write the file

`memory/<kebab-slug>.md`, slug equal to `name:`. Template:
`${CLAUDE_PLUGIN_ROOT}/skills/remember/templates/memory.md`. Conventions: `person-*`, `project-*`,
`pref-*`, `tool-*`.

`type` is one of `user`, `feedback`, `project`, `reference`. For `feedback` and `project`, the body must
include **Why:** (the reason — what makes it applicable later) and **How to apply:** (what to do
differently). Add `[[links]]` to related memories; a link to a file that doesn't exist yet is fine.

## 5. Update the index — not optional

Exactly one line in `memory/MEMORY.md`, added with the CLI (it creates the index if missing, writes
`- [Title](slug.md) — short hook`, and does nothing when the file is already indexed):

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mother" memory index memory/<slug>.md --title '<Title>' --hook '<short hook>'
```

Don't edit `MEMORY.md` with Write/Edit or the shell: the guard asks the manager for that (later sessions obey
it), and in an unattended run nobody answers. To change an existing line, ask the manager.

A memory missing from the index is lost.

## 6. Don't commit

`mother sync` handles it on Stop and SessionEnd.

## Report back

One line: what you stored, where, created or updated. If you decided **not** to store something, say so —
silence looks like success.
