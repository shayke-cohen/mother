# <Team> — intake queue

Team work seen in chat, mail, meetings or reviews that needs an owner or a ticket, staged here until the
manager decides. Protocol: `rules/teams.md` → Intake lifecycle.

**How it moves**
1. A session, the digest or connect-the-dots adds an item as `proposed`, with a recommendation.
2. The daily digest opens with **🗳 Decide**: the oldest and highest-risk `proposed` items, capped by
   `routines.digest.decision_queue`.
3. The manager answers in the digest's DM thread — `12 ticket`, `13 drop`, `14 mine`, or `ok` for every
   recommendation in that message — or in any session. Only the manager's own messages count.
4. The next run applies it: `ticket` → a tracker item labelled `triage`, unassigned, proposed owner and
   source in the description; `drop` → `dropped`; `mine` → a `~manager` todo. Owners are assigned by
   people, never by a run.

Line format:
`- #<id> [<status>] <one-line issue> — source: <link> — epic: <KEY|none> — proposed owner: <name> (<why>) — rec: <ticket|drop|mine> — priority: <P1-P4> — added <YYYY-MM-DD>`
Statuses: `proposed`, `ticket <KEY-n>`, `dropped`, `mine`, `blocked: <why>`. Ids are never reused.
No customer names, ids or emails; the source link is enough.

## Queue

_none_

## Applied
<!-- - YYYY-MM-DD reply <ts>: #12 ticket → KEY-123, #13 drop -->
_none_
