# Roadmap

| Stage | What | Done when |
|---|---|---|
| **0 · Extract** | Turn the reference deployment's scheduled jobs into config-driven routine templates, and its tool notes into a connector map | The reference deployment runs entirely from `config.json` plus templates |
| **1 · v0** ✅ built 2026-10-23, untested live | `/mother setup` (discovery interview), `/mother doctor` (live capability matrix), approval hook, routines: brief, team digest + decision queue, weekly meeting + deck + meeting mode, recap, connect-the-dots | One manager runs on it daily |
| **2 · Second stack** | A manager on a different stack (e.g. Monday instead of Jira, a non-engineering team) | Setup takes under 30 minutes, and the manager is still using it two weeks later |
| **3 · Channels** | Per-channel reply modes (`off` / `watch` / `mention` / `draft` / `auto`), mute, kill switch, earned autonomy | Draft-to-auto promotions happen, and nobody complains |
| **Later** | Commitment tracker, per-report 1:1 prep, working-agreement alerts, ticket-readiness check, a dedicated chat-app identity instead of the manager's token | — |

The adapter layer stays vendor-neutral: Slack/Teams, Jira/Linear/Monday, Gmail/Outlook, Google/Microsoft
calendars and GitHub/GitLab are all optional connectors, chosen at setup.
