# Doctor fix mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MOTHER's connectors heal themselves. Track health per server and per capability, fall back
between servers, check before each routine, alert once per issue, and walk the manager through a browser
sign-in when only a human can fix it.

**Architecture:** One new stdlib module, `lib/health.py`, exposed as `mother health …`, owns every write to
`capabilities.json` and `health.md`. It classifies errors, records results, picks a server, merges the
doctor's output, makes the alert decision and summarises the log. The skills (doctor, routine dispatcher)
are Markdown, and they call the CLI instead of editing JSON. `lib/channels.py` reads the new list-valued
`servers`, and `hooks/guard.py` enforces the stamp rule.

**Tech Stack:** Python 3.9 stdlib only (`zoneinfo`, `fcntl`), `unittest`, Claude Code plugin Markdown skills.

**Spec:** `docs/superpowers/specs/2026-10-25-doctor-fix-design.md`

## Global Constraints

- Python 3.9 stdlib only. Tests: `python3 -m unittest discover tests` must pass.
- **Work in a fresh worktree off `main`** (superpowers:using-git-worktrees). The main checkout has
  uncommitted work from another session in `bin/mother`, `hooks/guard.py` and `rules/outbound.md`. Never
  touch it, and rebase onto `main` before the final review.
- New code follows the house style of `lib/items.py`: a module docstring listing the commands, JSON out,
  and an exit code that carries meaning.
- `capabilities.json` status values stay the CONTRACT §4 set: `ok`, `untested`, `denied`,
  `missing_scope:<scope>`, `expired`, `error:<short>`.
- Failure classes are exactly: `unreachable`, `dropped`, `invalidated`, `missing_scope`, `denied`, `other`.
- Fixed-by values are exactly: `retry`, `reconnect`, `fallback`, `you`, `—`.
- Pre-flight trust window: **6 hours**. Pre-flight budget: **~60 s**. At most **one** alert per
  (server, capability) per manager-local day.
- No message content, ids of people, or sign-in URLs in `health.md`, `runs.md` or `capabilities.json`.
- Writes to others never go through a server listed in `stamps`. A stamping server is allowed only for
  `manager.chat_dm` (decided by the manager on 2026-10-25).

## Review Focus

1. **A legacy `capabilities.json`** (string `servers`, top-level `"chat.read"` map, no `health`) must keep
   working for `pick`, `servers_by_kind` and the guard, and must not crash: today's real workspace file has
   exactly this shape. *Test in Task 1:* `test_pick_legacy_file`.
2. **A corrupt or missing `capabilities.json`** during `record` or `merge` must not be overwritten with a
   half-file. The command exits 2 and leaves the file untouched. *Test in Task 1:*
   `test_record_refuses_corrupt_file`.
3. **Two routines recording at the same moment** must not lose either write. *Test in Task 1:*
   `test_record_concurrent_writes_both_land`.
4. **Quiet hours that cross midnight** (`22:00-07:00`) in the manager's timezone, not UTC. *Test in Task 3:*
   `test_alert_quiet_hours_cross_midnight`.
5. **A stamping server whose prefix is a gateway segment** (`mcp__gw__slack`) must not also block a
   non-stamping sibling (`mcp__gw__jira`), and the reverse. *Test in Task 5:*
   `test_stamp_rule_matches_segment_not_gateway`.

---

### Task 1: `lib/health.py` core — load, normalize, record, pick (+ `mother health record|pick`)

**Files:**
- Create: `lib/health.py`
- Modify: `bin/mother` (add `cmd_health` like `cmd_items`; add `"health"` to `COMMANDS` and to the
  docstring command list)
- Test: `tests/test_health.py`

**Interfaces:**
- Produces:
  - `load(ws: Path) -> dict`: parses `capabilities.json` (missing → `{"connectors": {}}`). Raises
    `HealthError` on bad JSON.
  - `normalize(caps: dict, cfg: dict) -> dict`: returns a copy in the new shape. Bare-string `servers`
    values become one-item lists. Top-level `servers` `"<kind>.<cap>": prefix` entries move into the
    connector `cfg["connectors"][kind]`. A missing `servers[cap]` defaults
    to `[conn["server"]]`. A missing `health` for the preferred server is seeded from the connector's
    top-level capability value, with `checked_at` = `caps["checked_at"]`.
  - `locked(ws: Path)`: a context manager holding `fcntl.flock` on `.mother/health.lock`. `save(ws, caps)`
    writes atomically (tmp + `os.replace`).
  - `record(caps, vendor, cap, server, status, cls=None, gateway=None, now=None) -> str`: sets
    `health[server][cap] = {status, class, checked_at}`. Keeps the existing `alerted_at` and `pending` when
    the class is unchanged, and clears them when status becomes `ok`. Appends `server` to `servers[cap]` if
    it's absent. Recomputes the rollup `conn[cap]`. Returns the previous status for that entry (`None` if
    new).
  - `rollup(conn, cap) -> str`: `ok` if any server in `servers[cap]` is `ok` for `cap`, otherwise the
    first server's status.
  - `pick(caps, vendor, cap, target=None, max_age_h=None, now=None) -> dict`: returns
    `{"server": str|None, "status": str, "fresh": bool, "fallback": bool, "stamped": bool}`. It walks
    `servers[cap]` in order and takes the first entry whose status is `ok`. With `target="other"`, it skips
    servers in `stamps`, and servers whose `token[s]` differs from the preferred server's token (an unknown
    token on either side counts as equal). `fallback` = the chosen server isn't `servers[cap][0]`.
  - `vendor_of(cfg, kind) -> str`: `cfg["connectors"][kind]`.
  - CLI (JSON on stdout):
    - `mother health record --kind K --cap C --server S --status V [--class X] [--gateway H] [--fixed-by F]`
      prints `{"previous": …, "rollup": …}`.
    - `mother health pick --kind K --cap C [--target dm|other] [--max-age-hours N]` exits 0 with a server,
      and 3 when there is none.
    - Exit 2 on a bad file or bad arguments.
  - `MOTHER_NOW` (epoch seconds) pins the clock, as in `lib/channels.py`.

- [ ] **Step 1: Write failing tests** in `tests/test_health.py` (temp workspace with `config.json` from
  `templates/workspace/config.example.json` and `connectors.chat="slack"`; run the CLI as a subprocess like
  `tests/test_cli.py`):
  - `test_pick_prefers_first_ok`: servers `["W","C"]`, W `ok` → `server=="W"`, `fallback is False`.
  - `test_pick_falls_back_when_preferred_down`: W `error:unreachable`, C `ok` → `server=="C"`,
    `fallback is True`.
  - `test_pick_other_target_skips_stamping`: as above, plus `stamps:["C"]`, `--target other` → exit 3,
    `server is None`.
  - `test_pick_dm_target_allows_stamping`: same file, `--target dm` → `server=="C"`, `stamped is True`.
  - `test_pick_other_target_skips_different_token`: C has no stamp but `token: {"W":"user","C":"bot"}`
    → exit 3.
  - `test_pick_fresh_flag`: `checked_at` 7 h before `MOTHER_NOW`, `--max-age-hours 6` → `fresh is False`;
    5 h before → `True`.
  - `test_pick_legacy_file`: the exact shape of today's workspace file (connector-level
    `servers: {"search": "mcp__X"}` as strings, no `health`, top-level `"read":"ok"`) → `pick read` returns
    the connector `server`, and `pick search` returns `mcp__X`.
  - `test_record_updates_rollup_and_keeps_order`: record W `read` `error:unreachable` while C is `ok`
    → rollup `ok`, `servers.read == ["W","C"]` unchanged.
  - `test_record_clears_alert_on_ok`: an entry with `alerted_at` set, recorded `ok` → no `alerted_at`, no
    `pending`.
  - `test_record_refuses_corrupt_file`: `capabilities.json` = `"{not json"` → exit 2, file bytes unchanged.
  - `test_record_concurrent_writes_both_land`: launch 8 parallel `record` subprocesses for 8 different caps
    → all 8 entries are present afterwards.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_health -v`. Expected: FAIL (`unknown command 'health'`).
- [ ] **Step 3: Implement** `lib/health.py` and `cmd_health` with the interfaces above.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_health -v`. Expected: PASS. Then run
  `python3 -m unittest discover tests`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat(health): per-server capability health, record and pick`.

### Task 2: Classify, health log and summary (+ `mother health classify|summary`, `record --fixed-by`)

**Files:**
- Modify: `lib/health.py`, `bin/mother` (docstring)
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `record` from Task 1.
- Produces:
  - `classify(error: str) -> tuple[str, str]` returns `(status, class)`. Rules are checked in order, and
    matching ignores case:

    | Match | Result |
    |---|---|
    | `missing_scope` / `insufficient scope` / `not allowed` **with** a scope token (`[a-z]+:[a-z.:_]+`) | `("missing_scope:<scope>", "missing_scope")` |
    | `invalidated`, `reconnect it from connector settings`, `needs_auth`, `needs authentication`, `sign in`, `token expired`, `re-authenticate`, `invalid_auth`, `\b401\b` | `("expired", "invalidated")` |
    | `\b403\b`, `permission`, `admin must enable`, `not allowed` | `("denied", "denied")` |
    | `econnrefused`, `enotfound`, `getaddrinfo`, `timed out`, `timeout`, `unreachable`, `fetch failed`, `connection refused`, `network` | `("error:unreachable", "unreachable")` |
    | `connection closed`, `not connected`, `disconnected`, `transport closed`, `server failed` | `("error:dropped", "dropped")` |
    | anything else | `("error:" + scrub(text)[:40], "other")`. `scrub` replaces runs of digits and hex ids (≥8 chars) with `#` |

  - `server_short(prefix) -> str`: drop `mcp__`. For an opaque id segment (≥16 hex/dash characters), keep
    the first 8. `mcp__Willow_Slack__slack` → `Willow_Slack__slack`.
  - `health_line(now, kind, cap, server, cls, fixed_by) -> str`, exactly:
    `- <UTC ISO Z> <kind>.<cap> <server_short> <cls> → <fixed_by|open>`. The log goes in
    `<workspace>/health.md`, which is created with the header line
    `One line per connector failure or fix, appended by \`mother health record\`.`
  - `record` writes a line when the new status isn't `ok` (`→ open`), or when it is `ok`, the previous
    status was not, and `--fixed-by` was given (`→ <fixed_by>`). A repeat with the same status and class as
    the previous one writes no line.
  - `summary(ws, days=7, now=None) -> list[str]`: one line per (server_short, class) over the window,
    most frequent first: `"<server_short> <class> ×N (fixed: retry 1, fallback 2; open 1)"`. When servers
    were recorded with the same `--gateway`, their `unreachable` lines fold into
    `"gateway <host> unreachable ×N"`. The gateway is stored as `health[server]["_gateway"]` and also
    written at the end of the line as ` @<host>`.
  - CLI: `mother health classify --error TEXT` prints `{"status","class"}`.
    `mother health summary [--days N]` prints text lines and exits 0.

- [ ] **Step 1: Write failing tests**:
  - `test_classify_table`: a subTest per row, with the real strings
    `"The user's connection to this connector was invalidated. The user needs to reconnect it from connector settings."`
    → `("expired","invalidated")`,
    `"missing_scope: search:read"` → `("missing_scope:search:read","missing_scope")`,
    `"fetch failed: ECONNREFUSED 10.0.0.1:443"` → unreachable,
    `"403 Forbidden"` → denied,
    `"MCP error: Connection closed"` → dropped,
    `"weird 1234567890abcdef thing"` → `("error:weird # thing","other")`.
  - `test_health_line_format`: assert the exact string for a fixed `MOTHER_NOW`.
  - `test_record_logs_open_then_fixed_once`: record unreachable twice, then ok with `--fixed-by fallback`
    → exactly two lines, ending `→ open` and `→ fallback`.
  - `test_summary_folds_gateway`: two servers with `--gateway mcp-s.example` both unreachable → one line
    starting `gateway mcp-s.example unreachable ×2`.
  - `test_health_md_has_no_ids`: after records with `--server mcp__486bd9bf-5fd4-4917-848c-4f2ddfdfb82e`,
    the file contains `486bd9bf` and not the full uuid.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_health -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `classify`, `server_short`, `health_line`, the logging rule in `record`, and
  `summary`.
- [ ] **Step 4: Run** `python3 -m unittest discover tests`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(health): classify errors, health.md log and summary`.

### Task 3: Merge and alerts (+ `mother health merge|alert|pending`)

**Files:**
- Modify: `lib/health.py`, `lib/channels.py` (extract `quiet_now(cfg, q=None, now=None) -> bool` from
  `in_quiet_hours`, and make `in_quiet_hours` call it), `bin/mother` (docstring)
- Test: `tests/test_health.py`, plus the existing `tests/test_channels.py` (must stay green)

**Interfaces:**
- Consumes: `normalize`, `record`, `locked`, `save` from Task 1.
- Produces:
  - `merge(old: dict, new: dict) -> dict`, used by the doctor. `new` wins for every status, `checked_at`,
    `token`, `stamps` and the top-level `checked_at`/`host`. For each `servers[cap]`, the old order is kept
    for servers present in both, new servers are appended, and servers absent from `new` are dropped. Per
    health entry, `alerted_at` and `pending` from `old` are kept when the class is unchanged and the status
    isn't `ok`.
  - `alert_action(caps, cfg, vendor, cap, server, urgent=False, now=None) -> str` returns `"send"`,
    `"pending"` or `"skip"`. It returns `skip` when the entry's status is `ok`, or when `alerted_at` falls
    on the same manager-local date as `now` (`manager.timezone`). It returns `pending` (and sets
    `pending: true`) when `quiet_now(cfg)` is true and not `urgent`. Otherwise it returns `send`.
  - `mark_alerted(caps, vendor, cap, server, now=None)`: sets `alerted_at` and clears `pending`.
  - `due_pending(caps, cfg, now=None) -> list[dict]` returns `[{kind, cap, server, status, class}]` for
    entries with `pending` true, and returns `[]` while it's still quiet hours.
  - CLI:
    - `mother health merge --file F` takes the doctor's fresh JSON, merges it into the workspace file under
      the lock, and prints `{"changed": [<kind.cap>…]}`, listing the rollups that moved away from `ok`.
    - `mother health alert --kind K --cap C --server S [--urgent] [--sent]` prints `{"action": …}`. With
      `--sent` it calls `mark_alerted` and prints `{"action":"sent"}`.
    - `mother health pending` prints the JSON list from `due_pending`.

- [ ] **Step 1: Write failing tests**:
  - `test_merge_keeps_order_and_alert`: old `servers.read ["W","C"]` with C `alerted_at` set and class
    `invalidated`; new `["C","W","N"]`, with C still invalidated → merged order `["W","C","N"]` and C keeps
    `alerted_at`.
  - `test_merge_drops_alert_when_class_changes`: C changes from `invalidated` to `unreachable` →
    `alerted_at` is gone.
  - `test_merge_reports_newly_broken`: a rollup that goes from `ok` to `expired` is listed in `changed`.
  - `test_alert_once_per_day`: `send` → `--sent` → a second call the same local day gives `skip`, and the
    next local day gives `send`.
  - `test_alert_quiet_hours_cross_midnight`: `manager.timezone="Asia/Jerusalem"`,
    `quiet_hours="22:00-07:00"`, `MOTHER_NOW` = 23:30 local → `pending`. With `--urgent` → `send`. At 06:59
    local → `pending`. At 07:01 local, `pending` returns that entry.
  - `test_pending_empty_during_quiet_hours`: a pending entry at 02:00 local → `[]`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_health -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `merge`, `alert_action`, `mark_alerted`, `due_pending` and `quiet_now` (the
  refactor keeps `in_quiet_hours` behaviour identical).
- [ ] **Step 4: Run** `python3 -m unittest discover tests`. Expected: PASS, including `test_channels`.
- [ ] **Step 5: Commit** `feat(health): doctor merge and once-a-day alert decision`.

### Task 4: `servers_by_kind` reads list-valued servers

**Files:**
- Modify: `lib/channels.py` (`servers_by_kind`, around lines 516–533)
- Test: `tests/test_channels.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (it reads the file directly, so channels stays independent of
  health).
- Produces: `servers_by_kind(ctx)` additionally collects every prefix in `connectors.<vendor>.servers.*`,
  whether the value is a string or a list. The top-level `servers` map is still read.

- [ ] **Step 1: Write failing test** `test_list_valued_servers_reach_the_right_kind`: `capabilities.json`
  with `slack.servers.read = ["mcp__chat__","mcp__chat2__"]` and `jira.servers.read = ["mcp__tracker",
  "mcp__tracker2"]`. A teammate session → `env_servers == "mcp__tracker,mcp__tracker2"` (sorted), and both
  chat prefixes are in `--disallowedTools`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_channels -v`. Expected: the new test FAILS.
- [ ] **Step 3: Implement** the list reading in `servers_by_kind`.
- [ ] **Step 4: Run** `python3 -m unittest discover tests`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(channels): read list-valued capability servers`.

### Task 5: Guard enforces the stamp rule

**Files:**
- Modify: `hooks/guard.py`: `load_ctx` reads `capabilities.json` into `ctx.stamps` (a set of prefixes; a
  missing or bad file gives an empty set and never an error). In `run()`, after `classify` and before
  `decide`, apply the stamp check.
- Test: `tests/test_guard.py`

**Interfaces:**
- Produces: `stamp_block(tool: str, a: dict, ctx) -> tuple|None`. When `a["kind"] == "chat"`,
  `a.get("op") in ("post","reply","schedule","edit")`, the tool's server matches a stamp prefix, and
  `a["channel"]` isn't in `dm_ids(ctx.cfg)`, it returns
  `("deny", "%s goes through a connector that adds its own stamp (%s). Send it through the preferred chat server, or ask the manager." % (a["what"], prefix))`.
  The server matches a stamp prefix when `server_prefix(tool)` equals it or starts with `prefix + "__"`,
  comparing whole `__`-separated segments, never as a plain string prefix.

- [ ] **Step 1: Write failing tests** in a new `StampRule(GuardCase)` class; `setUp` writes
  `capabilities.json` with `{"connectors":{"slack":{"server":"mcp__chat__slack","stamps":["mcp__0a1b2c3d"]}}}`:
  - `test_stamping_server_to_dm_allowed`: `mcp__0a1b2c3d__slack_send_message` `{channel_id: DM, text: SIG}`
    → `allow`.
  - `test_stamping_server_to_team_denied`: the same tool to `TEAM` → `deny`, and the reason contains
    `adds its own stamp`.
  - `test_non_stamping_server_unchanged`: `mcp__chat__slack__post_message` to `TEAM` → `ask` (today's
    behaviour).
  - `test_stamp_rule_matches_segment_not_gateway`: with `stamps:["mcp__gw__slack"]`,
    `mcp__gw__jira__create_issue` → not denied by this rule. `mcp__gw__slackbot__post_message` to `TEAM`
    → not denied by this rule.
  - `test_bad_capabilities_file_ignored`: `capabilities.json` = `"nope"` → a DM post still `allow`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_guard -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `ctx.stamps` loading and `stamp_block`, and log the decision with the existing
  `log()`.
- [ ] **Step 4: Run** `python3 -m unittest discover tests`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(guard): block writes to others through a stamping connector`.

### Task 6: `mother status` shows open health issues; daily doctor default

**Files:**
- Modify: `bin/mother` (`cmd_status`), `templates/workspace/config.example.json` (`routines.doctor` →
  `{"at": "07:00", "days": "all"}`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load`, `normalize` from Task 1 (import `lib/health.py` as `cmd_items` does).
- Produces: in `cmd_status`, after the `doctor:` line, either `health:      ok`, or
  `health:      <N> open — <kind>.<cap> <server_short> <class>; …` (at most 3 listed, then `…`). An entry
  is open when its status isn't `ok` **and** the capability's rollup isn't `ok`. If a fallback covers the
  capability, the entry counts as `covered`, not open: `(+M covered by fallback)`. Exit codes are unchanged.

- [ ] **Step 1: Write failing tests**:
  - `test_status_health_open_and_covered`: calendar read is `expired` on C and `ok` on W (covered), and
    mail read is `error:unreachable` on its only server (open) → the output contains
    `health:      1 open — mail.read` and `(+1 covered by fallback)`.
  - `test_status_health_ok`: everything `ok` → `health:      ok`.
  - `test_example_config_doctor_daily`: the example config's `routines.doctor.days == "all"`, and
    `mother register` emits `mother-doctor` with cron `0 7 * * *`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_cli -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the status line and update the example config.
- [ ] **Step 4: Run** `python3 -m unittest discover tests`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(status): show open connector health; doctor runs daily by default`.

### Task 7: Contract and rules

**Files:**
- Modify: `docs/CONTRACT.md` (§1 layout: add `health.md` and `.mother/health.lock`; §4: the new connector
  shape from spec §2, verbatim example; rollup rule; failure classes; "all runtime writes go through
  `mother health`"; legacy shapes still read). Modify `rules/outbound.md` ("One signature, not two": add
  the DM-only fallback decision and the guard enforcement). Modify `docs/DESIGN.md` (the doctor paragraph:
  daily, heals).
- Test: none new. `python3 -m unittest discover tests` stays green.

- [ ] **Step 1: Edit** the three files. Copy field names exactly from Tasks 1–3 (`servers`, `health`,
  `stamps`, `token`, `status`, `class`, `checked_at`, `alerted_at`, `pending`, `_gateway`).
- [ ] **Step 2: Check** `grep -n "replaces .capabilities.json. wholesale" -r skills docs` returns nothing
  left over in CONTRACT/DESIGN (the doctor skill is Task 8).
- [ ] **Step 3: Commit** `docs: capabilities.json v2 shape, health log, stamp fallback rule`.

### Task 8: Doctor skill and routine — classes, ladder, fix mode, assisted sign-in

**Files:**
- Modify: `skills/doctor/SKILL.md`, `routines/doctor.md`

Content requirements (from spec §1, §3, §5, §6, §8). The skill must:
- **Test every server per kind, not only the main one.** Classify each failure with
  `mother health classify`. Write results to a temp file and apply them with `mother health merge`. The
  "replaces wholesale" sentence is removed.
- **Group gateway failures.** Get each server's URL host from `claude mcp list`. Two or more servers that
  share a host and are `unreachable` become one issue, passed with `--gateway <host>`.
- **Write the server order:** a server with no stamp and the manager's identity comes first. Record
  `stamps` (claude.ai Slack: "Sent using @Claude") and `token` per server.
- **Run the ladder** (spec §3) once per failing capability, and record each step with
  `mother health record … --fixed-by`. The report table gains a `Fixed by` column. Reconnecting a
  claude.ai connector uses `reconnect_session_connector`; its effect lands only after the turn ends, so
  record `reconnect requested`, end the turn, and re-test on resume.
- **Assisted sign-in, interactive only** (spec §5): the built-in browser is the default (load the
  `anthropic-skills:built-in-browser` skill first). Open the claude.ai connector settings and click
  Reconnect. In the account picker, pick the manager's own Google account. **Stop** at a password, 2FA or
  consent screen and say "click Allow in the browser pane". Never type credentials. Never click consent
  on the manager's behalf. When the browser path fails, give the manual path. For Willow `unreachable`,
  say "probably VPN — connect and say done" and never touch the VPN. For a Willow `needs_auth`, use the
  server's own `authenticate` tool.
- **End with `mother health summary`** under the table, then the existing `mother runs` rows.
- **`/mother:doctor fix`** (and the default for interactive runs) runs the ladder.
  **`/mother:doctor check`** only reads and classifies.

`routines/doctor.md`: daily; unattended; ladder steps 1–3 only. For each issue that a fallback doesn't
cover, call `mother health alert`. On `send`, notify with PushNotification **if the tool is present in
the run** (check with ToolSearch; skip silently if not), and DM the manager through
`mother health pick --kind chat --cap post --target dm`. Then call `mother health alert … --sent`. A
healthy day sends nothing.

- [ ] **Step 1: Edit** both files to the requirements above, keeping the skill's existing section style.
- [ ] **Step 2: Check** `grep -n "mother health" skills/doctor/SKILL.md routines/doctor.md`: every
  subcommand used exists in `mother health --help` (the docstring).
- [ ] **Step 3: Commit** `feat(doctor): classify, heal, fall back, assisted sign-in, daily alerts`.

### Task 9: Routine dispatcher pre-flight; setup schedules the doctor

**Files:**
- Modify: `skills/routine/SKILL.md`, `skills/setup/SKILL.md`

Content requirements (from spec §4, §6, §7). `skills/routine/SKILL.md`, step 2:
- **Pre-flight.** For each `needs` capability, run
  `mother health pick --kind K --cap C --max-age-hours 6`. If it is `fresh` and `ok`, trust it. Otherwise
  probe the picked server, or the preferred one when none is ok, with the doctor's probe for that kind.
  Then run ladder steps 1–3 and record each result. Budget ~60 s; when it runs out, fall through to
  "couldn't check".
- **Pending alerts first.** Run `mother health pending` and send each due alert, as in the doctor
  routine. When a routine falls inside quiet hours and a capability it needs is broken, it alerts with
  `--urgent`.
- **The §3 change.** On a mid-run failure, `record` it, then `pick` again (writes use
  `--target dm|other` by recipient) and retry **once** on the server returned. A switch is named in the
  output and in the `runs.md` note as `<kind>.<cap> via fallback <server_short>`. "Do not retry"
  otherwise stays.

`skills/setup/SKILL.md`: the doctor routine is set to `days: "all"`, with `at` 30 minutes before the
earliest `routines.*.at`, never before 05:00. Existing installs are asked once at the next setup.

- [ ] **Step 1: Edit** both files.
- [ ] **Step 2: Check** that every `mother health` call has flags matching Tasks 1–3.
- [ ] **Step 3: Commit** `feat(routine): pre-flight health check, one fallback retry, pending alerts`.

### Task 10: Live verification against the real connectors

No new code. This proves the feature on the manager's real setup. Run it **interactively with the
manager**, since it may reach a browser sign-in.

- [ ] **Step 1:** Load the plugin from the worktree (`claude --plugin-dir <worktree>` in a fresh session),
  and run `mother status` with `MOTHER_WORKSPACE=/Users/shayco/mother-workspace`. Expected: a valid config,
  and the `health:` line renders from today's legacy file without error.
- [ ] **Step 2:** Run `/mother:doctor fix`. Expected on 2026-10-25's state:
  - claude.ai Google Calendar `read` is `expired`/`invalidated` and covered by Willow Calendar (`fallback`).
  - The assisted sign-in opens the connector settings and stops for the manager's click.
  - After the click, a re-test shows `ok` with `→ you` in `health.md`.
- [ ] **Step 3:** With the manager's OK, disconnect VPN and run
  `mother health pick --kind calendar --cap read` after a doctor check. Expected: the claude.ai server is
  chosen, and `health.md` has one `gateway <host> unreachable` line.
- [ ] **Step 4:** Ask the manager to update their workspace `routines.doctor` to daily (their file, their
  OK), then re-register the routines via `/mother:setup`.
- [ ] **Step 5:** Run `python3 -m unittest discover tests` on the rebased branch. Expected: all PASS. Then
  open a PR.
