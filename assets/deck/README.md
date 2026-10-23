# Meeting deck

A single self-contained HTML file per meeting: `template.html` (the fixed design) + one week's JSON, joined by
`build.py`. Keyboard: ←/→ to move, `E` for **meeting mode**.

```
python3 build.py <data.json> <out.html>
```

`example.json` is the reference shape. `build.py` fails if a section is missing.

## Data

Required top-level keys: `meta`, `pipeline`, `sprint`, `experts`, `intake`, `carryover`, `next`, `roadmap`,
`decisions`. Optional: `backlog` (the meeting-mode ticket picker).

| `meta` field | Use |
|---|---|
| `title`, `subtitle`, `date`, `banner`, `agenda[]` | title slide |
| `meeting`, `team`, `agent`, `project` | labels, the results header, the `mother approve tracker <project>` line |
| `tracker_url` | browse prefix for ticket links (e.g. `https://example.atlassian.net/browse/`); empty = plain keys |
| `roster[]` | names in every owner dropdown |
| `sprintSetup` | `current`, `currentId`, `next`, `nextId` (null = create), `start`, `end` |
| `generated`, `sources`, `warnings[]` | provenance, shown on the title slide |

Text fields turn `ABC-123` keys into links when `tracker_url` is set. Outcome fields filled after the meeting:
`intake.items[].owner`, `carryover.items[].outcome`, `decisions.items[].outcome`.

## Meeting mode

Owners per intake item (+ "in next sprint"), carry-over outcomes (Commit / Split / Mark done / Drop / Move out /
Keep), decisions, sprint setup (name, dates, goal, close current, start now), and tickets from the room
(existing — assign / mark done / comment — or new). State is kept in the browser's localStorage per team and
date. **Copy results** produces the text the agent applies (`skills/meeting`, section 3).

## Content rules

The deck is shown to the team: no customer identifiers, no person scoring (shipped work by stream), every
number with its source and time. Exception: `backlog` carries tracker titles verbatim — the team can already
read the tracker.
