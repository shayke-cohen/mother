# Projects — protocol

The container for work that spans sessions.

```
projects/
  PROJECTS.md              the index
  <slug>/
    PROJECT.md             goal, people, files index, milestones, notes
    files/                 working artifacts; files/tmp/ is never kept
```

## What a project is, and isn't

A project holds the **container**: goal, who is involved, dates, files, running context. It does **not**
hold a task list. Action items live in `todos/open.md` tagged `` `+<slug>` `` — the only link between
the two stores (`rules/todos.md`).

**Why:** two task lists disagree within a week, and a half-maintained authoritative list is worse than
none. One store means "what's open on X" is a filter over `todos/open.md`, not a second thing to keep
current. A `## Tasks` heading in `PROJECT.md` is invalid.

A project ends; a team persists. A team's projects live here and name the team under `## People` or
`## Notes`. Project and team slugs share one namespace and must not collide.

## Why this isn't memory

Memory is flat and closes `type` at four values. Projects have mutable state and attachments. They are
complements: `memory/project-<x>.md` records the durable *why*; `projects/<x>/PROJECT.md` records the
live *what*. Cross-link them. **Anything durable in `## Notes` graduates to memory** (via
`mother:remember`) and is linked from here.

## PROJECT.md

Frontmatter is **flat scalars** — deliberately not memory's nested `metadata:` block, so nothing mistakes
a project for a memory. Template: `${CLAUDE_PLUGIN_ROOT}/skills/project/templates/PROJECT.md`.

| Key | Rule |
|---|---|
| `name` | equals the directory name; kebab-case |
| `description` | one specific line — the index shows it |
| `category` | free text; conventions `work`, `personal`, `side` |
| `status` | `active` \| `paused` \| `done` \| `dropped` — nothing else |
| `started` | absolute date, always |
| `due` | optional, absolute. **Omit rather than guess** |
| `review` | optional `weekly` \| `monthly`; informational in v0 (nothing fires from it) |

Required sections: `## Goal`, `## People`, `## Files`, `## Milestones`, `## Notes`.

- **Goal** — one paragraph, concrete enough to tell whether you're there. If it isn't clear yet, write
  what is known and `_Needs sharpening by <manager_short>._` — a visible placeholder beats a made-up goal.
- **People** — name and what they own, one line each. Work facts only (`rules/privacy.md`).
- **Files** — an *annotated* index of local files and external pointers together, because `ls` can't say
  why a file matters. Mark anything outside the workspace `(external)`.
- **Milestones** — `- [ ] YYYY-MM-DD — <what>`; hitting one flips it to `- [x]` and it stays as the record.
- **Notes** — running context, decisions and their reasons.

## The index is not optional

One line per project in `projects/PROJECTS.md`, status inline so finished ones can be skipped unopened:

```
- [Billing migration](billing-migration/PROJECT.md) — `active` `work` — move invoicing to the new ledger
```

A project missing from the index is lost.

## files/ — the size rule

With a git remote, everything here syncs, and history is append-only.
- **Under ~1 MB** — keep it.
- **Over ~1 MB** — don't copy it in. Leave it where it is and add an `(external)` pointer.
- `files/tmp/` is scratch; never keep it.

## Privacy in files/

Relaxed: a full draft, a long note, a meeting write-up — that's what a working file is for.
Still absolute: no credentials, no customer or employee PII, no personal contact details, health,
compensation or performance content. `mother scan` skips binaries, so look at an image or PDF yourself
before filing it.

## Renaming

Three steps, all required: rename the directory and `name:`; update the index row; rewrite every
`` `+<old>` `` in `todos/open.md` **and** `todos/archive.md`.

## Closing

Set `status: done` (or `dropped`) and update the index row. **Leave its todos alone** — never close on
inference. Report open `+<slug>` items so the manager decides. Nothing is deleted; a finished project is
the record.

## Who writes

The agent writes projects **conversationally**, with the manager present. `mother:reflect` **only
flags** — an `active` project untouched 30+ days, a passed `due:` or milestone date, a `done` project with
open `+<slug>` items, a `+<slug>` naming nothing — and never edits `projects/` unattended.

**Why:** a wrong nightly inference that silently rewrites a milestone or status is worse than a flag read
in the morning.

## Validation (by hand, after every write)

- `name` equals the directory; `status` is one of the four; `started` is a date; `due` absent or a date.
- All five sections present; no `## Tasks`.
- The index row exists and its status matches the frontmatter.
- No slug collides with a team slug.
