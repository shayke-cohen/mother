# Memory protocol

Memory is flat markdown in `<workspace>/memory/`, one fact per file, indexed by `memory/MEMORY.md`.
It holds what stays true. Work not yet done goes to `todos/` (`rules/todos.md`); containers of work go to
`projects/` and `teams/`. Mixing the stores is how each one rots.

If Claude Code's built-in auto-memory is enabled for the workspace, it must resolve to this same
directory — never keep two memories that can drift.

## File format

```markdown
---
name: sam-roe-billing
description: Sam Roe owns the billing migration; prefers written weekly updates over a sync
metadata:
  type: reference
  domain: work
  tags: [billing, people]
---

Sam Roe owns the billing migration. Prefers a written weekly update over a meeting. Escalation path
for anything touching payment flows.

Related: [[project-billing-migration]]
```

`name` equals the filename without `.md`. `description` is what recall matches, so make it **specific**:
"Sam owns billing migration, wants written updates" beats "notes about Sam".

### Types

Exactly four. Inventing a fifth breaks tools that read the store.

| Type | For | Extra requirement |
|---|---|---|
| `user` | Who the manager is — role, working style, stable preferences | — |
| `feedback` | Guidance the manager gave on **how the agent should work** — corrections and confirmed approaches | **Why:** and **How to apply:** lines |
| `project` | Ongoing work, goals and constraints that code and history don't record | **Why:** and **How to apply:**; absolute dates |
| `reference` | Pointers — people and what they own, dashboards, docs, systems | — |

`domain: work | personal` and `tags: []` are optional slicing fields. Naming conventions: `person-*`,
`project-*`, `pref-*`, `tool-*`. Files stay flat; `memory/patterns/` is the one exception and holds
machine ledgers, not memories.

## When to write

Something **durable** was learned:
- Who someone is and what they own (work facts only — `rules/privacy.md` → People)
- A preference the manager states, especially about how the agent should work
- A project's goal, constraint or decision, and the reason
- A correction the manager made — the highest-value memory and the easiest to lose, because it feels
  like conversation
- A pointer worth keeping: a dashboard, doc, repo, ticket
- A connector quirk verified by a real call ("the tracker's list call returns 1 result unless a page
  size is passed") — so the next run doesn't rediscover it

Convert relative dates to absolute: "next Thursday" is useless in three weeks; write `2026-09-03`.

## When not to write

- What config, code or git history already records
- What matters only inside the current conversation
- What an existing memory covers — **update that file instead**
- Speculation, or anything unconfirmed
- Anything on the never-write list in `rules/privacy.md`
- A todo. "Reply to Sam" is a todo, not a memory.

If the manager asks you to remember something from the first two buckets, ask what was non-obvious about
it and store *that*.

## The index is not optional

Every memory gets exactly one line in `memory/MEMORY.md`:

```
- [Sam Roe — billing](sam-roe-billing.md) — owns billing migration, wants written weekly updates
```

Sessions read the index first and load selectively, so **a memory missing from the index is lost.**
`MEMORY.md` holds one line per memory and no content of its own.

## Linking

`[[name]]` links related memories. A link to a file that doesn't exist yet is fine — it marks something
worth writing later.

## Before trusting a memory

A memory records what was true **when written**. Before acting on one that names a file, a role, a
deadline, a channel or a meeting, check it still holds. People change teams; dates pass.

## Contradictions and decay

A new fact that contradicts an old memory **updates the old file**; never add a second file saying the
opposite. If you can't tell which is right, ask the manager rather than silently choosing.

`mother:reflect` flags contradictions, staleness and index drift nightly. It may fix index drift itself;
it reports contradictions rather than choosing a winner.

## Validation (by hand, after every write)

- Frontmatter has `name`, `description`, `metadata.type`; `name` equals the filename; `type` is one of
  the four.
- `feedback` and `project` bodies have **Why:** and **How to apply:**.
- The index has exactly one line for the file, and no line points at a missing file.

## Sync

Don't commit by hand. The plugin's Stop and SessionEnd hooks run `mother sync`, which scans for secrets
and commits (and pushes, if a remote is set). A secret anywhere in the diff aborts the whole commit.
