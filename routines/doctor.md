---
id: doctor
needs: []
team_scoped: false
---

# Routine: weekly doctor

Sign-ins expire independently, so a connector that worked last week can be dead today.

1. Follow the `mother:doctor` skill in **unattended mode**: read checks only, never write checks.
2. Update `capabilities.json`.
3. DM the manager **only if something got worse** since the last run (a capability moved away from `ok`),
   with the fix for each. A healthy week sends nothing.
