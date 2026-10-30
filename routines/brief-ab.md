---
id: brief-ab
team_scoped: false
description: A/B test of the brief's gather mode — runs the brief twice as a dry run (inline vs readers sub-agents) on the same inputs and asks the manager to pick one blind.
config: routines.brief-ab         # at, days; absent = off. Turn it off after ~10 judged pairs.
default: { at: "09:30", days: workdays }
needs: [calendar.read, mail.read, chat.read, chat.post, tracker.read, code.read]
writes: [.mother/eval/ (machine-local)]
sends: manager.chat_dm only
---

# Routine: brief-ab

This routine decides whether the brief's `readers` gather mode (one `mother:reader` sub-agent per source)
becomes the default. It must beat today's `inline` gather on the same inputs. Both briefs are dry runs:
`MOTHER_DRY_RUN` makes the guard deny every outbound call, so nothing reaches anyone. The day's real brief
is still the `brief` routine's.

## Before this file

`mother:routine` has already resolved the workspace, checked home, PAUSED, config, days and capabilities.
It writes the `runs.md` line from the **Runs note** below.

## Steps

1. **Apply yesterday's pick first.** Read the replies in the thread of the last `A/B brief` DM. Count only
   messages authored by `manager.chat_user`. If one starts with `ab x`, `ab y` or `ab same` (the rest of
   the line is a note), run:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/mother" eval verdict <pair> <x|y|same> --note "<the rest of the line>"
   ```
   The pair id is in that DM. A pair nobody answered stays unjudged; don't guess.
2. **Run a pair:**
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/mother" eval brief
   ```
   This runs two headless dry runs back to back, with the order alternating each day. It prints
   `{pair, X, Y, errors, pairs_so_far}`. X and Y are randomized, so neither you nor the manager knows which
   is which.
   - The headless runs need the same permission rules as unattended routines.
   - If both briefs come back as "couldn't check" everywhere, the run lacked permissions. Note it, and
     don't post the pair.
3. **Post one DM** to `manager.chat_dm`, with the signature (rules/outbound.md) on the first line:
   ```
   <signature>
   A/B brief <pairs_so_far>/10 — pair <pair>. Which of these two briefs was more useful today?
   Reply in this thread: `ab x`, `ab y` or `ab same`, plus a few words on why.

   *Brief X*
   <contents of X>

   *Brief Y*
   <contents of Y>
   ```
   Never say which one is `readers`. If one variant errored, post the other alone, write
   "(the other run failed)", and don't ask for a pick.
4. **Every 5 judged pairs**, run `mother eval report`. Add its `decision` and `reasons` under the DM in
   the thread.
   - Once it says `ship` or `hold` with at least 10 judged pairs, say: "Enough data: <decision>. Turn
     brief-ab off; set `routines.brief.gather` to `readers` if you agree."
   - Only the manager changes the config.

**Runs note:** `pair <id>; errors: <variants or none>; verdict applied: <pair x|y|same or none>; judged <n>`.

## Hard rules

- Only the manager's DM. The briefs are never sent anywhere else, and their contents aren't stored
  outside `.mother/eval/`.
- Don't reveal the X/Y mapping, and don't open `pair.json` to read it, until `mother eval report` is run.
- A dry run that posted anything is a bug. Stop the routine and report it.
