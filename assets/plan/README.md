# Sprint plan

One static HTML page per week: `template.html` (the fixed design) + one week's JSON, joined by `build.py`. No
script, so it renders anywhere (a hosted page, a file preview, a phone). It replaces the slide deck as the
thing the team reviews: every issue appears once, under the goal it serves.

```
python3 build.py <plan.json> <out.html>
```

`example.json` is the reference shape. `build.py` fails if a section is missing or an item has an unknown
`change`.

## Data

Required top-level keys: `meta`, `context`, `feedback`, `last`, `next`, `decisions`.

| Key | Use |
|---|---|
| `meta` | `team`, `title`, `sprint`, `sprintId`, `start`, `end`, `project`, `tracker_url` (browse prefix), `board_url`, `label` (default `proposed`), `goal` (else the goal titles joined), `banner`, `generated`, `sources` |
| `context.groups[]` | `title` + `cards[]`: `value`, `label`, `note`, optional `prev[]` (`value`, `label`, `estimate`) and `higherIsBetter` (`true` / `false` / `null` = neutral) |
| `context.breakdown[]` | `title`, `text`, `tone` (`good` / `warn` / `bad` / `accent`) |
| `feedback.themes[]` | `title`, `count`, `tone`, `detail`, `goal` (the goal that answers it, or `null`) |
| `last` | `title`, `sub`, optional `leadtime` (`cards`, `note`), `streams[]` of `items[]` with `key`, `text`, `state` (`done` / `carry` / `open` / `split` / `decide` / `move`) |
| `next.goals[]` | `title`, `done` (the done-test), `items[]`: `key` (`null` for a new issue), `summary`, `owner`, `status`, `change`, optional `from` (previous owner) |
| `next.other[]` | issues already in the sprint that serve no goal: `key`, `summary`, `owner`, `status` |
| `next.inSprint` | the sprint's live issue count (else counted from the items) |
| `decisions[]` | open questions, each with a recommendation |

`change` says what applying the plan does to an item: `in` already in the sprint, untouched · `done` ·
`add` move into the sprint · `assign` move in and set the owner · `create` new issue. The apply step writes
`add`, `assign` and `create` items and nothing else; all of them get the `meta.label` label.

Cards show `+12%` red when a number got worse, green when better, blue when flat or neutral. Lower is better
unless `higherIsBetter` is `true`.

## Content rules

Shown to the team: no customer names or identifiers, no person scoring, every number with its source and time.
