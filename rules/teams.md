# Teams — protocol

Teams the manager runs. **Machine config** (tracker project and board, repos, channels and their modes,
meetings, sprint, people's ids) lives in `config.json` → `teams[]` (CONTRACT §3) and nowhere else. The
`teams/` tree holds the **human context** that config can't: mission, who owns what, rituals, roadmap,
sprint and meeting records, the intake queue.

```
teams/
  TEAMS.md                       the index
  <slug>/                        slug = config.teams[].slug
    TEAM.md                      mission, people and areas, rituals, standing permissions, notes
    roadmap.md                   Now / Next / Later + a dated ## Changes log
    intake.md                    the decision queue: proposed team work awaiting the manager's call
    sprints/YYYY-MM-DD.md        one per sprint, named by its start date
    meetings/YYYY-MM-DD-<kind>.md   agenda before, decisions after
    metrics.md                   one row per closed sprint; appended, never edited
    runs.md                      one line per routine run that touched this team
    repos/<owner>__<repo>.md     optional: short note per repo (monthly-refresh)
people/<slug>.md                 one per person across teams: areas and work items, never assessments
one-on-ones/<slug>/              1:1 prep and notes (mother:one-on-one): topics and commitments only
```

Templates: `${CLAUDE_PLUGIN_ROOT}/skills/team/templates/`.

## Two sources would disagree — so there is one

If `TEAM.md` and `config.json` both list a tracker key, they will differ within a month. `TEAM.md` may
*mention* config values for readability, but config wins, and a skill always reads config. A missing
config value is reported as `missing config: teams[<slug>].<key>`; **never guess** a tracker project from
a team name.

## A team is not a project

A project ends; a team persists. The team's projects live in `projects/`. Project and team slugs share
one namespace and must not collide, because `+<slug>` resolves against both.

## One task store, and one intake queue

Nothing under `teams/` holds a task list; a `## Tasks` heading anywhere in the tree is invalid.

- **The manager's or the agent's own actions** → `todos/open.md`, tagged `` `+<team-slug>` ``.
- **Team work that needs an owner or a ticket** (a bug seen in chat, an ask from another team, a review
  finding) → `teams/<slug>/intake.md` as a `proposed` item. Not a todo.
- **Team members' tracked work** is not a todo either; the tracker is its store.

## Intake lifecycle — the decision queue

`intake.md` stages issues as draft tickets until the manager decides. Template:
`${CLAUDE_PLUGIN_ROOT}/skills/team/templates/intake.md`.

Item line:

```
- #<id> [<status>] <one-line issue> — source: <link> — epic: <KEY|none> — proposed owner: <name> (<why: repo/area>) — rec: <ticket|drop|mine> — priority: <P1-P4> — added <YYYY-MM-DD>
```

`<id>` is a team-local integer, never reused. No customer names, ids or emails — the source link is
enough. Statuses: `proposed`, `ticket <KEY-n>`, `dropped`, `mine`, `blocked: <why>`.

1. A session, `digest` or `dots` adds an item as `proposed`, **with a recommendation** and a proposed
   owner whose reason is a work fact (they own the repo path, they own the area in `people/`, they touched
   the code last). Dedupe against open items and against existing tickets first.
2. `mother:digest` opens with **🗳 Decide**: up to `routines.digest.decision_queue` (default 5)
   `proposed` items, oldest and highest-risk first; the rest wait.
3. The manager answers in that digest's DM thread — `12 ticket`, `13 drop`, `14 mine`, or `ok` (take every
   recommendation **for the items shown in that message**) — or in any session.
4. The next digest run applies it, **only for replies authored by the manager's own chat user id**:
   - `ticket` → create the item in the team's tracker project: label `triage`, **unassigned**, the
     proposed owner and the source link in the description, the agent's footer signature. Status becomes
     `ticket <KEY-n>`. The reply is the yes for that one create, nothing more.
   - `drop` → `dropped`.
   - `mine` → a `~manager` todo tagged `+<slug>`; status `mine`.
   - Ambiguous → ask in the thread; apply nothing.
5. Every applied reply is logged under `## Applied` with the reply's timestamp, so it is never applied
   twice.
6. **Owners are assigned by people**, at the team's planning meeting or by the manager — never by an
   unattended run. The agent proposes; it doesn't assign, transition or comment.

Unattended runs reach the tracker only through a grant that cites the manager's reply
(`rules/outbound.md`, CONTRACT §5).

## Sprint and meeting files

- `sprints/YYYY-MM-DD.md` — `## Goals`, `## Committed` (snapshot from the tracker at the start: key —
  summary — assignee — status), `## Carryover`, `## Review` (filled after the sprint), `## Decisions`
  (the manager's). Snapshot from the tracker; never hand-type ticket lists.
- `meetings/YYYY-MM-DD-<kind>.md` — `## Agenda` before; `## Decisions` and `## Follow-ups` after. Each
  follow-up is also a todo or an intake item. A meeting summary from a connector is distilled to
  decisions and owners, never pasted as a transcript.

## Roadmap

`roadmap.md` is Now / Next / Later plus `## Changes`: every move gets a dated line with its reason. When
the tracker is known, link each item to its epic. **Report drift; don't rewrite the plan** — moving items
is the manager's call.

## Privacy — stricter here, because this tree is about people

- Names, roles, what each person owns, how they prefer to work: fine.
- Performance assessments, ratings, comparisons, mood or engagement tracking, compensation, hiring or
  firing, health, personal circumstances: **never**, not even in a meeting note. A 1:1 note records topics
  and commitments, not evaluations.
- A digest reports **work items**. "No tracker or code movement for Sam in 2 working days" is a fact;
  "Sam did nothing" is a judgement. Leave the reading to the manager.
- `people/<slug>.md` lists areas owned and work items done or in progress over a dated window. Nothing
  else.

## Outbound

Everything a team skill produces goes to the **manager's own DM** or is a **draft** for the manager's yes:
posting in a team channel, creating or transitioning tracker items, inviting people. All of it goes
through the guard (`rules/outbound.md`). A standing exception for one specific recurring post (e.g. the
digest into the standup channel) needs the manager's explicit yes, recorded with its date in
`TEAM.md` → `## Standing permissions`, and still needs a grant for unattended runs.

## Who writes

The agent writes `teams/` conversationally and from team skills and routines. `mother:reflect` only
flags: a sprint file past its end with an empty `## Review`, a meeting file with an agenda and no
decisions a day later, `proposed` intake items older than 14 days, a team in config with no `TEAM.md`.

## Validation (by hand, after every write)

- Every `config.teams[].slug` has `teams/<slug>/TEAM.md` and a row in `TEAMS.md`, and vice versa.
- No `## Tasks` heading anywhere under `teams/`.
- Intake lines match the grammar; ids are unique within the file.
- `metrics.md` rows are only ever appended.
- No team slug equals a project slug.
