---
id: meeting-recap
needs: [tracker.read, chat.post_dm]
team_scoped: true
---

# Routine: meeting recap (draft)

Runs about an hour after the team's weekly meeting.

1. Follow the `mother:meeting` skill, section **4. Recap**.
2. If the meeting results were already applied in a session, log `ok — already applied` and stop.
3. Otherwise DM the manager the recap draft with "reply `post` to send, or reply with edits".
4. **Never post to the team channel from this routine.** The next `digest` run posts it after the manager's
   `post` reply, through a guard grant citing that reply.
