---
id: doctor
needs: []
team_scoped: false
---

# Routine: daily doctor

Sign-ins expire independently, so a connector that worked yesterday can be dead today. Runs daily, before
the first routine of the day.

1. Follow the `mother:doctor` skill in **unattended mode** (its §9): read checks only, never write checks,
   healing ladder steps 1-3 only (re-test, reconnect, switch server), never a sign-in.
2. Record results with `mother health merge` / `mother health record`; never edit `capabilities.json`.
3. For each issue **not covered by a fallback**, call `mother health alert --kind K --cap C --server S`.
   A gateway group (servers sharing a host, all unreachable) is **one** issue: call it once, for the first
   server and capability in the group, name the gateway in the message, and mark only that entry `--sent`.
   - `send`: PushNotification if that tool is present in this run (check with ToolSearch; skip silently if
     not), and a DM to the manager via the server from `mother health pick --kind chat --cap post
     --target dm`, naming the connector, what is degraded and the single step to take. Then run the same
     `mother health alert … --sent`. If that `pick` exits 3 (chat itself is broken), send the push only,
     skip the DM, still mark `--sent` if the push went out, and note "no chat route for the alert" in the
     run line. If neither push nor DM was possible, do not mark it sent.
   - `pending` (quiet hours): do nothing now; a later routine's pre-flight sends it.
   - `skip`: already alerted today.
4. A healthy day, or one where every failure is covered by a fallback, sends nothing.
