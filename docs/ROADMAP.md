# Roadmap

| Stage | What | Done when |
|---|---|---|
| **0 · Extract** | Turn the reference deployment's scheduled jobs into config-driven routine templates, and its tool notes into a connector map | The reference deployment runs entirely from `config.json` plus templates |
| **1 · v0** ✅ built 2026-10-23; first pilot live the same day | `/mother:setup` (discovery interview), `/mother:doctor` (live capability matrix), approval hook, routines: brief, team digest + decision queue, weekly meeting + deck + meeting mode, recap, connect-the-dots | One manager runs on it daily |
| **2 · Second stack** | A manager on a different stack (e.g. Monday instead of Jira, a non-engineering team) | Setup takes under 30 minutes, and the manager is still using it two weeks later |
| **3 · Channels** ✅ built 2026-10-23, untested live | Per-channel reply modes (`off` / `watch` / `mention` / `draft` / `auto`), mute, kill switch, earned autonomy | Draft-to-auto promotions happen, and nobody complains |
| **4 · Manager kit** ✅ built 2026-10-25, untested live | Routine watchdog (`mother runs`), commitment tracker (`mother:commitments`), per-report 1:1 prep and cadence drift (`mother:one-on-one`), brief inbox piles and `me`/`prep`/`you` triage with optional reply drafts | The pilot answers triage with "go" most days, and a missed routine shows up in the brief |
| **5 · Memory and judgement** | Decision log per team plus a "same decision keeps changing" alert; memory provenance (source, date, confidence) with contradiction checks in reflect; a goals file and a weekly time-vs-goals check on the manager's own calendar; working-agreement alerts (stale PRs, no reviewer, no movement) at team or PR level; standing "watcher" questions run as routines; a post-send follow-up hook | — |
| **6 · Platform** | Cloud routines (`/schedule`) so the brief runs with the laptop closed; a plugin monitor instead of `/loop /mother:channels`, plus a "1:1 in 10 minutes" nudge; phone channels (Telegram/iMessage) with permission relay for approvals; SessionStart context (today's top three); `userConfig` for name, workspace and quiet hours; a plugin agent for the persona; read-only reader and critic sub-agents (in progress: brief behind a blind A/B, then dots and digest) | — |
| **Later** | Focus-time and scheduling proposals (tentative holds only on a yes), external-attendee meeting briefs from the manager's own history, ticket-readiness check, setup recommendations for transcript (Granola, Fireflies, Zoom), HR (BambooHR: who's out) and on-call (PagerDuty) connectors, a dedicated chat-app identity instead of the manager's token | — |

**Out of scope, on purpose:** performance-review or promotion drafting, skill assessments, sentiment or
speaker scores, mood check-ins, per-person delivery metrics, calendar changes without a yes, and meeting bots
that record without consent. They break "people, not scores" or the approval rule (DESIGN.md → Safety).

The adapter layer stays vendor-neutral: Slack/Teams, Jira/Linear/Monday, Gmail/Outlook, Google/Microsoft
calendars and GitHub/GitLab are all optional connectors, chosen at setup.
