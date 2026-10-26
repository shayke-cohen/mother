# Doctor fix mode: self-healing connectors

Status: draft for review · 2026-10-25 · builds on `skills/doctor`, `skills/routine`, CONTRACT §4

## Why

Routines run on MCP connectors that die without warning, and today the doctor can only tell the manager
what broke, once a week. A broken source turns a routine `partial` or `failed`, and the manager finds out
from a thin brief or a 1:1 prep that never came.

What the `runs.md` lines for 2026-10-25 show:

| Run | What broke |
|---|---|
| weekly-snapshot `failed` | Willow Jira unreachable (offline / VPN) |
| commitments `partial` | Willow Gmail offline; DM went out through the claude.ai Slack connector instead |
| one-on-one `failed` | Willow Calendar offline **and** claude.ai Calendar "connection invalidated" |
| brief, dots, meeting-prep `partial` | Slack search and private history need scopes Willow lacks; capabilities.json missing at run time |

Three lessons drive the design:

1. **A "connected" status lies.** On 2026-10-25 claude.ai Calendar showed `connected` in both
   `claude mcp list` and the session's connector status, but a real call returned "connection was
   invalidated". Only a real call counts.
2. **Most failures are shared or temporary, not expired sign-ins.** When Willow is unreachable (off VPN),
   every Willow server fails together. Re-trying or switching servers fixes that. A human sign-in doesn't.
3. **There are already two servers for each important kind.** Willow and the claude.ai connectors overlap
   for chat, mail, calendar and tracker. One of them failing shouldn't fail a routine.

## Goals

- A routine never fails or goes `partial` because one server is down while another works.
- The doctor fixes what it can without a human: re-test, reconnect, switch servers.
- When only a human can fix it, the human does **one step**. The agent prepares the rest, says exactly
  what to do, and checks again afterwards.
- The manager learns about a break when it happens (or before the routine that needs it), not a week later.
- A record of each failure, so the recurring cause becomes visible (VPN vs. expired sign-in vs. scope).

## Non-goals

- Git sync failures (`pull --rebase` with unstaged changes or several branches). These are real and appear
  in the same log, but they are plumbing, not connectors. Separate follow-up.
- Installing new connectors, or changing Willow or connector infrastructure.
- Connecting a VPN or changing any system or security setting.

## Anti-goals (failure even if it "works")

- **Silent substitution.** Switching servers without saying so, when the backup sees less. For example,
  claude.ai Slack can search but has a different view of private channels. Every switch is named in the
  routine's output and in `runs.md`.
- **A second signature on others' messages.** The rule in `rules/outbound.md`, "One signature, not two",
  stays in force for anything sent to someone other than the manager.
- **Credential handling.** The agent never types a password, a 2FA code or a token. It never stores or
  logs a sign-in URL.
- **Nagging.** At most one alert per broken capability per day. A healthy check sends nothing.
- **Retry loops.** Each step of the healing ladder runs once per check.

## Design

### 1. Failure classes

The doctor classifies by **what fixes it**, recorded alongside the existing CONTRACT §4 status value:

| Class | How it shows up | Fix |
|---|---|---|
| `unreachable` | Network or timeout error; **every** server on one gateway fails together (Willow at `mcp-s.wewix.net`) | Switch to the other server; tell the manager "connect VPN" only if nothing else covers the capability |
| `dropped` | Server status `failed`, or "connection closed" on a call | Reconnect (`reconnect_session_connector` for claude.ai connectors), re-test |
| `invalidated` / `expired` | "Connection invalidated", "reconnect from connector settings", 401, `needs_auth` | Human sign-in (assisted, §5); meanwhile switch servers |
| `missing_scope:<s>` / `denied` | As today | No healing. Send that capability to the server that has it (as today) |

The gateway grouping matters. If two or more servers that share a host fail with a network error, the
doctor records **one** `unreachable` issue for the gateway, not five separate ones.

### 2. Fallback order in `capabilities.json`

Extend CONTRACT §4. Today `servers.<capability>` holds one prefix. It becomes an **ordered list**: the
first server is preferred, and the rest are tested backups.

```json
"slack": { "server": "mcp__Willow_Slack__slack",
           "read": "ok", "post": "ok", "search": "ok",
           "servers": { "read":   ["mcp__Willow_Slack__slack", "mcp__<claude.ai slack>"],
                        "search": ["mcp__<claude.ai slack>"],
                        "post":   ["mcp__Willow_Slack__slack", "mcp__<claude.ai slack>"] },
           "health":  { "mcp__Willow_Slack__slack": {
                          "read": {"status": "error:unreachable", "class": "unreachable",
                                   "checked_at": "…", "alerted_at": null},
                          "search": {"status": "missing_scope:search:read", "class": "missing_scope", "checked_at": "…"} },
                        "mcp__<claude.ai slack>": {
                          "read": {"status": "ok", "checked_at": "…"} } },
           "stamps":  ["mcp__<claude.ai slack>"],
           "token":   { "mcp__Willow_Slack__slack": "user", "mcp__<claude.ai slack>": "user" } }
```

Settled after review (my calls):

- **Health is per server, per capability.** `status` holds the CONTRACT §4 value, `class` holds the §1
  failure class, and each entry has its own `checked_at` (used by the 6-hour trust window in §4) and
  `alerted_at` (used by the alert dedupe in §6).
- **The top-level capability values** (`"read": "ok"`) are a rollup: `ok` when **any** server in that
  capability's list is `ok`, otherwise the preferred server's status. Routines that only read the rollup
  keep working unchanged.
- **The order of the list is a preference, not a cache.** Setup and the doctor write it, preferring a
  server with no stamp and the manager's identity. A failure **never reorders** it. The routine chooses
  a server by health: the first server in the list whose entry for that capability is `ok`. When the
  preferred server recovers, it is used again automatically.
- **`servers` moves inside each connector**, as the doctor skill already writes it. A top-level
  `servers` map (CONTRACT §4 today) and a bare string value are still read for back-compat, but no longer
  written.
- **The doctor merges instead of replacing wholesale.** Across runs it keeps the list order and every
  `alerted_at`, and replaces the rest.
- **One write path.** All updates to `capabilities.json` from routines go through a new `mother health`
  CLI command (record a result, mark a switch, mark alerted). The command also appends the
  `health.md` line and takes a file lock, so concurrent routines never edit the JSON by hand.

**Write fallback rule:**

- Reads may fall back freely.
- A write to anyone other than the manager may fall back only to a server that has **no stamp and the same
  token identity** as the preferred one. Otherwise the write is queued and the manager is asked.
- A write to the manager's own DM may fall back to a stamping server. The manager accepts the extra stamp
  there.
- **The guard enforces the stamp part.** `hooks/guard.py` reads `stamps` from `capabilities.json` and
  denies a chat write through a stamping server unless the target is `manager.chat_dm`. The skills follow
  the rule too, but the guard is what makes it hold.

### 3. The healing ladder (`/mother:doctor fix`, and the default for interactive runs)

For each capability that isn't `ok`, once, in order:

1. **Re-test** with the doctor's probe. Brief network errors clear here.
2. **Reconnect** if the server is reconnectable (a claude.ai connector in `failed`). The reconnect only
   takes effect when the agent's turn ends, so the ladder records "reconnect requested" and re-tests on
   the next turn. Interactive runs end the turn and resume; unattended runs re-test at the next check.
3. **Switch servers.** Test the next server in the list for this capability. Record its health and the
   switch with `mother health`. The list order stays unchanged (§2).
4. **Human step.** Interactive: assisted sign-in (§5). Unattended: alert (§6).
5. **Re-test after the human step.** Report fixed or still broken.

The report table gains a "Fixed by" column: `retry`, `reconnect`, `fallback → <server>`,
`you (sign-in)`, `—`.

### 4. Pre-flight in the routine dispatcher

`skills/routine` step 2 gains a pre-flight. For each capability in the routine's `needs` frontmatter:

- If `capabilities.json` has it `ok` and was checked in the last **6 hours** (my call), trust it.
- Otherwise run that capability's probe on its preferred server, then steps 1–3 of the ladder (never
  step 4 unattended). Update just that capability in `capabilities.json`.
- Time budget: about 60 seconds for the whole pre-flight (my call). When the budget runs out, the run
  falls through to today's behaviour: name the source as "couldn't check".

During a run, if a call fails, the routine records the failure (`mother health`) and tries the next
healthy server for that capability **once** before naming the source as "couldn't check". This covers
reads and writes, and writes still follow the write-fallback rule in §2. It replaces "do not retry" in
`skills/routine` §3 for the switch case only. Retrying the same server is still not allowed.

Pre-flight also **sends any pending alerts**: alerts held for quiet hours (§6) whose quiet hours are over
go out at the start of the next routine, before its own work.

### 5. Assisted sign-in (interactive only)

The aim is to bring the manager to a single click, not to sign in for them.

- **claude.ai connectors** (`invalidated`, `needs_auth`): open the connector settings page in the
  built-in browser, which keeps sign-ins across sessions. Find the connector, click Reconnect, and in
  Google's account picker choose the manager's account when the SSO session is alive. **Stop** at any
  password, 2FA or consent screen and say "click Allow in the browser pane". Never click consent on the
  manager's behalf. After that, re-test.
- **Fallback when browser driving fails**, or the page isn't reachable: give the exact path
  (Settings → Connectors → <name> → Reconnect, or `/mcp` in a terminal session) and wait for "done".
- **Willow `unreachable`**: check the gateway host. When it's down, say "Willow is unreachable, probably
  VPN. Connect and say done". Don't touch the VPN.
- **Willow sign-in** (`needs_auth` on a Willow server): use the server's own `authenticate` tool when it
  has one, and show the URL in the session only.

### 6. Unattended alerts

When a scheduled check (pre-flight or the doctor routine) hits a class that needs a human:

- Send **one push notification** (the app's PushNotification tool, when available in scheduled runs), and
  also a DM to the manager when a chat post server is healthy. The message names the connector, what is
  degraded ("1:1 prep will have no calendar"), and the single step to take.
- Dedupe: record `alerted_at` per issue in `capabilities.json`. No repeat alert for the same issue on the
  same day.
- Respect `manager.quiet_hours`. During quiet hours, mark the alert pending in `capabilities.json`. The
  first routine pre-flight after quiet hours sends it (§4); no extra scheduled task is needed. The
  exception is a routine due during quiet hours that needs the capability: its pre-flight sends the
  alert straight away. (My call.)

### 7. Schedule

The doctor routine moves from weekly to **daily**, at a time before the first routine of the day (setup
picks it: ~30 min before `routines.brief.at`). It stays silent when nothing got worse. `mother register`
and `/mother:setup` need the new default. Existing installs keep their schedule until setup re-runs.

### 8. Health log

Each failure and fix is logged as one line in `<workspace>/health.md`, with no content and no IDs:

```
- <UTC ISO> <kind>.<capability> <server-short> <class> → <fixed-by|open>
```

`/mother:doctor` ends with a 7-day summary from it: "Willow unreachable 4× (all off-VPN hours);
claude.ai Calendar invalidated 1×". That tells the manager whether the problem is the gateway, the
connector, or the schedule.

## Changes by file (expected)

| File | Change |
|---|---|
| `skills/doctor/SKILL.md` | Failure classes, gateway grouping, ladder, fix mode, assisted sign-in, health summary |
| `routines/doctor.md` | Daily; unattended ladder steps 1–3; alerts §6 |
| `skills/routine/SKILL.md` | Pre-flight §4; one switch-servers retry mid-run |
| `docs/CONTRACT.md` §4 | List-valued `servers`, `health`, `stamps`, `alerted_at`; `health.md` in §1 layout |
| `rules/outbound.md` | Write-fallback rule for the manager's own DM |
| `lib/channels.py` | `servers_by_kind` reads list-valued and back-compat `servers`; `MOTHER_CHANNEL_SERVERS` is built from all listed servers |
| `hooks/guard.py` | Reads `stamps`; denies a chat write through a stamping server unless the target is the manager's DM |
| `bin/mother` | New `health` subcommand (record, switch, alert, summary; file lock; appends `health.md`); `status` shows open health issues; `register` daily doctor default |
| `tests/` | Back-compat for string and top-level `servers`; rollup; merge keeps order and `alerted_at`; `health` locking and line format; guard stamp rule; dedupe |

## Left to the builder

- The exact cheapest probe per server type (reuse the doctor's).
- Whether `reconnect_session_connector`, PushNotification and the built-in browser are reachable from a
  **scheduled** run. Check each one; any that isn't is skipped in unattended mode.
- How to locate the claude.ai connector settings page and its Reconnect control. Discover it live and
  don't hard-code coordinates.
- Gateway host detection: derive it from `claude mcp list` URLs, not a hard-coded Willow name (doctor
  rule: never assume a server name).

## Decisions and calls to check (items 2–8 made by Claude, not yet confirmed by the manager)

1. **Decided by the manager (2026-10-25):** when Willow Slack is down, a DM to the manager may go through
   claude.ai Slack, stamp included. This applies to the manager's own DM only.
6. A write to others falls back only to a server with no stamp and the same identity; otherwise it is
   queued and the manager is asked. The guard enforces the stamp part.
7. The list order is a preference that failures never change. The doctor merges `capabilities.json`
   instead of replacing it. All runtime updates go through `mother health`.
8. Alerts held during quiet hours go out with the next routine's pre-flight; no extra scheduled task.
2. Pre-flight trusts a result under 6 hours old, with a ~60 s budget.
3. Daily doctor, ~30 min before the brief.
4. Alerts during quiet hours wait, unless a routine due during quiet hours needs the capability.
5. Git sync failures are out of scope here.
