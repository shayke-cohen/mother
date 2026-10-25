---
id: commitments
team_scoped: false
description: Daily commitment tracker — promises made by and to the manager, and asks with no reply after N days; captured as open loops, listed in the manager's DM with nudge drafts on their yes.
config: routines.commitments      # at, days, no_reply_days (3), lookback_days (14), draft_nudges (false); absent = off
default: { at: "16:30", days: workdays }
needs: [mail.read, chat.read, chat.post]
writes: [todos/open.md, todos/archive.md, journal/commitments/<date>.md]
sends: manager.chat_dm; mail drafts only (on the manager's yes, or draft_nudges)
---

# Routine: commitments

A scheduled run starts with **no memory of any conversation**; everything it needs is here, in
`config.json`, and in the skills it names.

## Before this file

`mother:routine` has already resolved the workspace, run `mother home check`, stopped on `.mother/PAUSED`,
validated `config.json`, checked today against `days`, and loaded `capabilities.json`. It writes the
`runs.md` line from the **Runs note** below. If you were invoked directly rather than through the
dispatcher, do those first.

## Steps

1. **Apply yesterday's answers.** Read the previous `journal/commitments/*.md`. If the manager replied in
   that DM thread (authored by `manager.chat_user`) with `nudge <n>`, `done <n>`, `keep <n>` or `drop <n>`,
   apply them as in the **`mother:commitments`** skill §4. Nudges become mail drafts or text in the
   manager's DM, never sends. Unanswered numbers lapse after 3 days.
2. Run the **`mother:commitments`** skill §0–3 for the window.
3. If anything is new, overdue (`due:` past) or looks done, post one message to `manager.chat_dm`, first
   line `agent.signature`, under ~15 lines:

   ```
   *Commitments — <Day DD Mon>*
   You promised
   1. <what> → <person>, due <date> — <link>
   Waiting on
   2. <what> ← <person>, due <date> — <link>
   No reply (<n>+ days)
   3. <ask> → <person>, sent <date> — <link>
   Looks done
   4. <todo> — <link>
   Reply "nudge 3", "done 4", "keep 2", "drop 1"
   ⚠️ Couldn't check: <sources>
   ```

   Nothing new and nothing overdue: send nothing, and say so in the runs note. Respect
   `manager.quiet_hours`.

**Runs note:** `<n> made, <n> owed, <n> no-reply, <n> applied; DM sent|quiet; couldn't check: <…>`.
