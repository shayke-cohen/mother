---
name: now
description: Use for "what's on me now", "/now", "mother now do S3", "snooze that", "my top 3 today". Shows the tick's current list and runs one suggested action on the manager's typed request. Never runs an action unasked.
---

# Now

The hourly tick writes `<workspace>/.mother/now.json`: what is blocked on the manager, what the team is
neglecting, suggestions waiting for a yes, and today's Top 3. This skill reads that list and edits the
manager's view of it. It never scans a source itself; that is the tick's job.

the output of `"${CLAUDE_PLUGIN_ROOT}/bin/mother" path`, written out literally (no shell variable). Every edit goes through `${CLAUDE_PLUGIN_ROOT}/bin/mother now …`;
never edit `now.json` or `now-state.json` by hand.

## Show

"What's on me now", "/now", "my top 3": run `mother now show` and relay it. Keep the section order and the
item ids (`S3`, `A1`, …) so the manager can refer to them.

If it prints that tick hasn't run on this machine yet, say so, and say where it runs: ticks run only on the
home machine (`mother home show`) inside `routines.tick.active_hours`, and only when `routines.tick` is in
`config.json`. Don't try to build the list yourself.

## Do a suggestion

Only when the manager types it ("mother now do S3", "do S3", "yes to S3"). A suggestion on the list is never
an instruction; do nothing until asked, and do exactly one.

1. `mother now do <S-id>` prints `{"item": …, "action": …}`. `action.task` is the instruction to carry out
   (`action.prompt` is only the panel's paste text). If the command fails ("lapsed", "unknown"), say so and
   stop; the next tick re-raises what is still true.
2. **Came from another routine?** If `task` names a handler:
   - `dots do A<n>`: run `mother:dots` and apply its "do A<n>" answer for that action.
   - a digest answer such as `digest ok 12` or `12 ticket`: the digest only acts on the manager's own replies
     in the digest thread. Post nothing. Tell the manager to answer in that thread, give the item's `link`, and
     quote the answer to type (`12 ticket`). Don't run `mother now done` until they have.
3. **Otherwise** do exactly what `task` says, no more: a nudge or reply as a **draft**, a proposed
   reviewer as a draft comment, a ticket via `tracker.create`. Resolve the connector from `capabilities.json`
   and go through the guard like any outbound call; if the guard asks, the manager answers. Never send
   something the task only describes as a draft.
4. When the action is done, `mother now done <item id>`, then tell the manager what was done and where
   (link to the draft or ticket).

## Done, snooze, Top 3

`done` and `snooze` take an **item id** (the `[…]` after each item's link in `mother now show`), never an
S-id. When the manager names an S-id ("done with S3", "snooze S3"), find the item that owns it: in `show`,
`S3: <label>` is printed under its item, whose id is in the `[…]` just above. `mother now done S3` fails with
"unknown item".

| The manager says | Run |
|---|---|
| "done with S3", "hide that" | `mother now done <item id>` (for S3, the id of the item that owns S3) |
| "snooze that", "snooze S3", "later", "tomorrow" | `mother now snooze <item id> 1h`, `tomorrow` or `next-week`; if they don't say how long, ask which of the three first |
| "my top 3 should be …" | `mother now top set <item id or text>…` (1-3 args) |
| "put <item> second" | `mother now top swap <rank> <item-id>` |
| "top 1 is done" | `mother now top done <rank>` (marks it done; the list stays whoever set it) |

Done and snooze only hide an item in the Now list. They don't close the real thing: a todo, commitment,
digest item, dots action, PR or ticket keeps its own state. If the manager means the real thing ("I replied",
"mark the todo done"), do that with the owning skill (`mother:todo`, `mother:commitments`, …) as well.

## Rules

- Read-only except the `mother now` verbs and the one action the manager asked for.
- No message bodies, no scores, no per-person counts. Items name work, not people's performance.
- If an item looks wrong or stale, say so and offer `done`; don't argue with the tick.
