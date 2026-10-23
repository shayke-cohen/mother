---
id: meeting-prep
needs: [tracker.read, chat.read, chat.post_dm]
team_scoped: true
---

# Routine: meeting prep

Runs at `teams[].meetings.weekly.prep_at` for one team (the pointer names it). Early enough for the manager to
review — the default is the morning of the meeting.

1. Follow the `mother:meeting` skill, section **1. Prep**, for this team.
2. If a source isn't `ok` in `capabilities.json`, still build the deck; list the gap in `meta.warnings` and in
   the DM. Never present a partial deck as complete.
3. DM the manager (their own DM only): deck path, the 5 most important lines, and "say `share` to publish".
4. Append the runs line (the dispatcher does this; include deck path and any gap in the note).

Sends nothing to anyone but the manager. Writes only under `teams/<slug>/meetings/`.
