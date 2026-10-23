# MOTHER

**M**anager's **O**perations, **T**asks, **H**andoffs, **E**xecution & **R**eporting.

A chief of staff for people managers, built on [Claude Code](https://claude.com/claude-code).

> Yes, it's named after the ship's computer in *Alien*. Unlike that one, this Mother never treats the crew as
> expendable: it works for the manager, in the open, and anything another person sees carries its signature.

> **Status: pre-alpha, design stage.** Nothing here runs yet. It is being extracted from a personal agent
> that has been running daily for a real team since August 2026. See [docs/DESIGN.md](docs/DESIGN.md) and
> [docs/ROADMAP.md](docs/ROADMAP.md).

## What it is

MOTHER gives a manager of any kind (engineering, product, operations, support) **their own named agent**, for
example "Agnes (AI) · on behalf of Dana". The agent:

- **Knows your world.** A setup interview *discovers* your people, teams, projects, channels, trackers, repos
  and recurring meetings from your connectors, then shows you a draft to confirm. You don't have to type it in.
- **Proves its tools work.** A connector *doctor* makes live read and (opt-in) write calls, then records what
  actually works. Routines check that record before they run, so a broken Slack connection shows up as
  "couldn't check", never as "all quiet".
- **Runs your routines.** A daily brief, a team digest that opens with a short *decision queue* you answer
  in one line, a weekly team-meeting deck with a **meeting mode** (owners, decisions, tickets from the room)
  that writes outcomes back to your tracker, a recap, and a daily *connect-the-dots* pass that suggests
  actions for you and for itself.
- **Remembers.** People, commitments, decisions and open loops live as plain files in a private workspace,
  and a nightly reflection keeps that memory current.

## Principles

1. **Plugin = code, workspace = data.** The plugin is shared and versioned. Each manager's workspace is
   private, local by default, with an optional git remote.
2. **Disclosure, always.** The agent acts through the manager's own accounts, so every outbound message,
   comment or ticket is signed: `✨ Agnes (AI) · on behalf of Dana`.
3. **Approvals live in code, not just in the prompt.** A pre-tool hook decides what may go out without
   asking (your own DM, drafts), what needs your yes (anything another person sees), and what never happens
   (credentials, deletions, payments). It also provides a kill switch.
4. **Evidence or silence.** Answers cite a source. With no source, it asks you instead of guessing.
5. **Earned autonomy.** Channels start at `draft`. The agent can *propose* more autonomy after a track record,
   but only the manager can grant it.
6. **People, not scores.** It reports work items, never assessments, never mood. The team should know the
   agent exists and what it reads.

## Install (once there is something to install)

```
/plugin marketplace add shayke-cohen/mother
/plugin install mother@mother
/mother setup
```

## License

MIT
