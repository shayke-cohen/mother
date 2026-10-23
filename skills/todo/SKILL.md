---
name: todo
description: Use when the manager mentions something they need to do or are waiting on, asks "what's open", "what am I forgetting", "add a todo", "what do you owe me", or a commitment surfaces that would be lost at session end. Adds, closes, adopts, sweeps and lists items in todos/open.md and todos/archive.md.
---

# Todo

The store for open loops — what the manager or the agent owes. Full grammar and lifecycle:
`${CLAUDE_PLUGIN_ROOT}/rules/todos.md`; read it rather than guessing.

```bash
W="${MOTHER_WORKSPACE:-$(cat ~/.mother/workspace)}"; cd "$W"
```

If `todos/open.md` or `todos/archive.md` is missing, create it from
`${CLAUDE_PLUGIN_ROOT}/skills/todo/templates/`.

**After every write, check the file against `rules/todos.md` → Validation.** If it fails, fix it before
replying; a malformed line breaks the brief and the nightly sweep.

## Sweep first, always

Every invocation starts by sweeping, because the manager may have ticked boxes in an editor:

1. Every `- [x]` line in `open.md` → `archive.md` with `` `done:<today>` `` inserted before the origin;
   remove it from `open.md`.
2. A section left empty keeps its heading and gets `_none_`.

Then do what was asked.

## Adding

Add freely — what the manager asks for and commitments you notice. Don't ask permission for the write.

- **Section**: **Yours** if it's the manager's, **Mine** if the agent promised or took it on, **Blocked**
  if it's real but waiting on someone else.
- **Origin**: `~manager@<today>` when the manager asked; `~agent/session@<today>` when you inferred it from
  conversation; `~agent/<chat|mail|calendar|tracker|code>@<today>` when you inferred it from that source.
- **`due:`** only for a real date, absolute `YYYY-MM-DD`. Convert "Tuesday" to the actual date.
- **`+<slug>`** when it belongs to a project or team that exists.
- **Dedupe** first. If it's already there, say so instead of adding a near-duplicate.

Short enough to scan, specific enough to act on: "Reply to Sam re: billing cutover date", not "Sam".

**Team work is not a todo.** A bug that needs an owner, an ask that needs a ticket → the team's intake
queue (`mother:team`, `rules/teams.md`), where the digest puts it in front of the manager.

## Closing

Move the line to `archive.md` with `` `done:<today>` `` before the origin. Never delete it.

**Never close on inference.** If a sent mail or a Done ticket suggests an item is finished, say "this
looks done — close it?" and wait.

## Adopting

To keep an inferred item past its 14-day expiry, change `~agent/<source>@<date>` to `~manager@<date>`,
keeping the original date. Offer this when the manager engages with an `~agent/*` item without closing
it.

## Listing

Report `open.md` grouped by section: overdue (`due:` past) first, then aging `~manager` items (21+ days),
then the rest, with a count. For `~agent/*` items close to expiry, say so: "expires in 3 days unless you
want it kept". If every section is `_none_`, say the list is empty.

## Don't

- Don't put todos in `memory/`.
- Don't delete anything from `archive.md`, ever.
- Don't add ids, priorities or any token the grammar doesn't define.
- Don't commit; `mother sync` handles it.
