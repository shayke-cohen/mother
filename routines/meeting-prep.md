---
id: meeting-prep
needs: [tracker.read, chat.read, chat.post_dm]
team_scoped: true
---

# Routine: meeting prep

Runs at `teams[].meetings.weekly.prep_at` for one team (the pointer names it). Early enough for the manager to
review — the default is the morning of the meeting.

1. Follow the `mother:meeting` skill, section **1. Prep**, for this team: build the sprint plan, publish it
   where `meetings.weekly.publish` says.
   The deck's Unowned intake also reads `teams/<slug>/meetings/<meeting date>-hygiene.md` when it exists
   (skill §1, "Unowned intake"): its owners stay proposals, deduped by ticket key behind `intake.md`. No file →
   intake as before.
2. If a source isn't `ok` in `capabilities.json`, still build the plan; say what is missing in `meta.banner`
   and in the DM. Never present a partial plan as complete.
3. DM the manager (their own DM only): the plan link, the 5 most important lines, the tracker changes applying
   would make, the team message as it would be posted, and the paste-back block
   (`mother approve tracker …` / `mother approve chat …` / `apply sprint plan <date>`).
4. Append the runs line (the dispatcher does this; include the plan link, any gap, and `hygiene: <n> intake items` or `no hygiene file` in the note).

Sends nothing to anyone but the manager and writes nothing to the tracker: applying is section **2. Apply the
plan**, in a session where the manager pasted the approve lines. Writes only under `teams/<slug>/meetings/`.
