# Tick and the Now panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An hourly `tick` routine that keeps `.mother/now.json` (Top 3 today, On you, Neglected, Suggestions,
Couldn't check), a `mother now` CLI that owns that file, and a `mother-now` mod that shows it beside the chat.

**Architecture:** The model gathers and judges (routine `tick` → `mother:reader` per source → `mother items
check`), then pipes candidate items to `mother now merge`. Everything deterministic lives in `lib/now.py`:
working-hours ages, thresholds, stable ids, state carried across ticks, resolution, Top 3 rules and the
manager's done/snooze/top edits. The mod only reads `now.json` and calls the CLI.

**Tech Stack:** Python 3.9 stdlib (`zoneinfo`, `fcntl`), markdown routine/skill files, a TypeScript function-hooks
mod (`claude-code` API, `claude plugin validate|test`).

**Spec:** `docs/superpowers/specs/2026-10-26-tick-and-now-panel-design.md`

## Global Constraints

- Python 3.9 stdlib only; tests are stdlib `unittest` (`python3 -m unittest discover tests`). CONTRACT §8.
- No company names, ids or hostnames in code, tests or examples: `example.com`, `PLAT`, `C00000001`, `example-org`.
- Tick never runs between 22:00 and 06:00: `routines.tick.active_hours` default `"06:00-22:00"` in `manager.timezone`.
- Default `every` 60 minutes; slots at minute :05 (06:05 … 21:05). Tick never pulls or syncs (`sync: false`).
- v1 sources: chat, tracker, mail, calendar, code, workspace. No CI/ops/approval-flow/alerts-channel signals.
- `now.json` and `now-state.json` live in `<workspace>/.mother/` (gitignored), written atomically.
- No message bodies in `now.json`; no per-person counts; `who` is not a field.
- Done/Snooze hide in the panel only; they never edit todos, commitments, digest or dots state.
- The mod makes no MCP or network calls. "Do it" uses `$.prompt.fill`, never `$.prompt.submit`.
- Toasts: once per item id, `on_you` items only, never while `now.json.quiet` is true.
- Suggestions lapse 3 working days after `first_seen`.

## Review Focus

1. A slow signal (scanned every 3rd tick) is not re-seen on a fast tick → it must stay open, not close. (Task 2)
2. A source that couldn't be checked → its items stay, marked `unverified_since`; nothing says "Nothing on you". (Tasks 2, 8)
3. Ages across a weekend or overnight (Fri 17:00 → Mon 09:00) → only active working hours count. (Task 1)
4. An item marked done is requested again (its `since` is newer) → it comes back as new; same `since` → stays hidden. (Task 3)
5. `now.json` missing, half-written or from an older `version` → the panel says so and keeps the last good list; merge never leaves a partial file. (Tasks 2, 8)

---

### Task 1: Working hours and thresholds

**Files:**
- Create: `lib/now.py`
- Test: `tests/test_now.py`

**Interfaces:**
- Produces:
  - `SIGNALS: dict[str, tuple[str, str, str]]` — signal → `(section, source, default_threshold)`. Exactly:
    on_you: `review_requested` (code, `4h`), `unanswered_mention` (chat, `2h`), `decision_open` (workspace, `1d`),
    `ticket_waiting_on_you` (tracker, `0`), `mail_needs_reply` (mail, `4h`), `invite_unanswered` (calendar, `0`),
    `promise_due` (workspace, `0`), `suggestion_waiting` (workspace, `0`);
    neglected: `pr_no_reviewer` (code, `1d`), `pr_approved_unmerged` (code, `2d`), `pr_changes_silent` (code, `3d`),
    `default_branch_red` (code, `2h`), `ticket_stalled` (tracker, `3d`), `bug_unowned` (tracker, `4h`),
    `blocker_cleared` (tracker, `1d`), `sprint_at_risk` (tracker, `0`), `outside_question` (chat, `4h`),
    `action_item_no_ticket` (workspace, `1d`), `one_on_one_drift` (workspace, `0`).
  - `SLOW_SIGNALS: set[str]` = every neglected signal except `default_branch_red`.
  - `Schedule` (dataclass): `tz: ZoneInfo, workdays: list[str], start: time, end: time, every: int` and
    `Schedule.from_config(cfg: dict) -> Schedule` (reads `manager.timezone`, `manager.workdays`,
    `routines.tick.active_hours`, `routines.tick.every`; defaults `06:00-22:00`, 60).
  - `working_hours(a: datetime, b: datetime, s: Schedule) -> float` — hours of `[a, b)` inside workdays ∩ active hours, local to `s.tz`; 0 if `b <= a`.
  - `day_hours(s: Schedule) -> float` — length of the active window (16.0 for defaults). "1d" = this many working hours.
  - `threshold_hours(signal: str, cfg: dict, s: Schedule) -> float | None` — from `routines.tick.thresholds[signal]` else the default; `"Nh"`, `"Nd"`, or `0`; `None` when `routines.tick.signals[signal] is False` or the signal's source connector is `"none"`/absent (`workspace` is always on).

- [ ] **Step 1: Write the failing tests** in `tests/test_now.py` (import `now` from `lib/` the way `tests/test_items.py` imports `items`; config fixture: timezone `Europe/London`, workdays Mon–Fri):

```python
def test_weekend_and_night_do_not_count(self):
    s = now.Schedule.from_config(CFG)
    fri = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)   # 18:00 London (BST)
    mon = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)    # 09:00 London (GMT after DST)
    self.assertAlmostEqual(now.working_hours(fri, mon, s), 4.0 + 3.0)  # Fri 18-22, Mon 06-09

def test_same_day_inside_window(self):
    a = datetime(2026, 10, 27, 9, 0, tzinfo=UTC); b = a + timedelta(hours=6)
    self.assertAlmostEqual(now.working_hours(a, b, S), 6.0)

def test_reversed_range_is_zero(self):
    self.assertEqual(now.working_hours(B, A, S), 0)

def test_threshold_parsing_and_overrides(self):
    self.assertEqual(now.threshold_hours("review_requested", CFG, S), 4.0)
    self.assertEqual(now.threshold_hours("ticket_stalled", CFG, S), 48.0)          # 3d × 16h
    cfg = with_tick(CFG, thresholds={"ticket_stalled": "2d"}, signals={"bug_unowned": False})
    self.assertEqual(now.threshold_hours("ticket_stalled", cfg, S), 32.0)
    self.assertIsNone(now.threshold_hours("bug_unowned", cfg, S))

def test_signal_off_when_connector_absent(self):
    cfg = with_connectors(CFG, tracker="none")
    self.assertIsNone(now.threshold_hours("ticket_stalled", cfg, S))
    self.assertEqual(now.threshold_hours("decision_open", cfg, S), 16.0)       # workspace always on
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_now -v` — Expected: FAIL (`No module named 'now'`).
- [ ] **Step 3: Implement** the interfaces above in `lib/now.py` with a module docstring in the style of `lib/items.py` (purpose, commands, "Python 3.9 stdlib only"). Walk day by day in local time; intersect each workday's `[start, end)` with `[a, b)`.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_now -v` — Expected: PASS.
- [ ] **Step 5: Commit** `git add lib/now.py tests/test_now.py && git commit -m "now: working hours and signal thresholds"`

---

### Task 2: Merge — candidates into `now.json`

**Files:**
- Modify: `lib/now.py`
- Test: `tests/test_now.py`

**Interfaces:**
- Consumes: Task 1 (`SIGNALS`, `Schedule`, `working_hours`, `threshold_hours`).
- Produces:
  - Merge input (the routine pipes this on stdin):
    `{"coverage": {source: {"status": "ok|partial|couldnt_check", "reason": str}}, "checked_signals": [str],
      "routines": [{"id","state","expected"}], "candidates": [{"kind","signal","title","why","link","also":[],
      "team","since": ISO, "actions": [{"label","prompt"}]}], "top": [{"title","why","link"}] (optional),
      "swap_hint": {"title","why","link"} | null}`
  - `item_id(c: dict) -> str` = `f"{c['kind']}:{c['link']}:{c['signal']}"`.
  - `merge(inp: dict, prev: dict | None, state: dict, cfg: dict, now: datetime) -> dict` — returns the `now.json`
    object of spec § "`.mother/now.json`", `version: 1`, plus `quiet: bool` (`manager.quiet_hours` at `now`),
    `next_at` (next :05 slot inside active hours on a scheduled day), `stale_after` (`next_at + every`),
    `counts: {"on_you","neglected","suggestion"}`, `next_action: int`.
  - `write_now(ws: Path, doc: dict) -> None` — writes `.mother/now.json` via temp file + `os.replace`.
  - `load_now(ws: Path) -> dict | None` — `None` when missing, unparsable, or `version != 1`.

Rules `merge` enforces (each one gets a test):
- A candidate whose `threshold_hours` is `None` is dropped; one whose `working_hours(since, now)` is below its threshold is dropped.
- Section comes from `SIGNALS`, except a candidate with non-empty `actions` and `signal == "suggestion_waiting"` → `suggestion`.
- New id → `state: new`, `first_seen = last_seen = now`; seen before → `state: open`, `first_seen` kept.
- Actions get ids `S<n>` from `prev.next_action` (start 1) the first time; an item keeps its action ids across ticks; `prompt` is rewritten to `mother now do S<n>`.
- A prev item not in this tick's candidates is **closed** (dropped) only if its signal is in `checked_signals` and its source's coverage is `ok`; if its source is not `ok` it is kept with `unverified_since` (kept from the first such tick); otherwise kept unchanged.
- Coverage rows whose source connector is absent/`none` are dropped.
- Suggestions with `working_hours(first_seen, now) >= 3 × day_hours` are dropped.

- [ ] **Step 1: Write the failing tests**

```python
def test_below_threshold_is_dropped_above_is_kept(self):
    out = now.merge(inp(cand("review_requested", since=hours_ago(3))), None, {}, CFG, NOW)
    self.assertEqual(out["items"], [])
    out = now.merge(inp(cand("review_requested", since=hours_ago(5))), None, {}, CFG, NOW)
    self.assertEqual(out["items"][0]["state"], "new"); self.assertEqual(out["items"][0]["section"], "on_you")

def test_second_tick_is_open_and_keeps_first_seen(self): ...
    # second merge with prev=first → state "open", first_seen == NOW of first run

def test_action_ids_are_stable(self):
    # first merge assigns S1; second merge of the same candidate keeps S1 and next_action stays 2

def test_slow_signal_not_checked_stays_open(self):      # Review Focus 1
    first = now.merge(inp(cand("ticket_stalled", since=days_ago(5))), None, {}, CFG, NOW)
    second = now.merge(inp(checked=["review_requested"]), first, {}, CFG, NOW + H)
    self.assertEqual(len(second["items"]), 1)

def test_checked_and_gone_is_closed(self):
    second = now.merge(inp(checked=["ticket_stalled"]), first, {}, CFG, NOW + H)
    self.assertEqual(second["items"], [])

def test_couldnt_check_keeps_item_unverified(self):      # Review Focus 2
    second = now.merge(inp(checked=["ticket_stalled"], coverage={"tracker": "couldnt_check"}), first, {}, CFG, NOW + H)
    self.assertEqual(second["items"][0]["unverified_since"], iso(NOW + H))

def test_absent_connector_has_no_coverage_row(self): ...
def test_suggestion_lapses_after_three_working_days(self): ...
def test_quiet_flag_follows_manager_quiet_hours(self): ...   # 19:00-08:00 example config → quiet at 20:00 local
def test_next_at_skips_night_and_weekend(self):              # Fri 21:30 local → Mon 06:05 local
def test_write_is_atomic_and_load_rejects_bad_files(self):   # Review Focus 5
    # write_now leaves no *.tmp; load_now returns None for "", "{", and {"version": 2}
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_now -v` — Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `item_id`, `merge`, `write_now`, `load_now` in `lib/now.py`. Sort items by section (`on_you`, `neglected`, `suggestion`), then oldest `since` first.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_now -v` — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "now: merge candidates into now.json"`

---

### Task 3: Manager state and Top 3 today

**Files:**
- Modify: `lib/now.py`
- Test: `tests/test_now.py`

**Interfaces:**
- Consumes: Task 2 (`merge`, `load_now`, `write_now`).
- Produces:
  - `.mother/now-state.json`: `{"items": {id: {"state": "done|snoozed", "until": ISO|null, "at": ISO}},
    "top": {"date": "YYYY-MM-DD", "set_by": "manager", "entries": [{"title","why","link","item_id"}]} | null}`.
  - `load_state(ws) -> dict`, `save_state(ws, st) -> None` (atomic, under `fcntl.flock` on `.mother/now.lock`).
  - `snooze_until(word: str, now: datetime, s: Schedule) -> datetime` for `1h` (now + 1 h), `tomorrow` (next scheduled day at active start), `next-week` (next Monday-or-first-workday at active start).
  - `apply_edit(doc: dict, st: dict) -> dict` — re-applies state to an existing `now.json` so a panel edit shows without waiting for a tick.
  - `now.json["top"]`: `[{"rank": 1..3, "title","why","link","item_id": str|null, "set_by": "agent|manager", "state": "open|done"}]`, `now.json["top_date"]`, `now.json["swap_hint"]`.

Rules (each gets a test):
- A `done` id is hidden; it comes back as `new` if the candidate's `since` is later than the state's `at`, and the state entry is removed. (Review Focus 4)
- A `snoozed` id is hidden until `until`, then shown as `open` and the entry removed.
- Top 3: if `state.top.date` is today (manager tz) → use it as-is (`set_by: manager`). Else if `prev.top_date` is today → keep prev's top. Else take `inp.top` (max 3, agent-proposed). Never reorder an existing top.
- A top entry with an `item_id` is `done` when that id is closed or marked done; done entries stay listed.
- `swap_hint` is copied from input only when the top is agent-set; a manager-set top shows no hint.

- [ ] **Step 1: Write the failing tests**

```python
def test_done_hides_and_new_request_resurfaces(self):
    st = {"items": {ID: {"state": "done", "at": iso(NOW), "until": None}}}
    self.assertEqual(now.merge(inp(cand(..., since=hours_ago(6))), first, st, CFG, NOW + H)["items"], [])
    out = now.merge(inp(cand(..., since=iso(NOW + 30 * M))), first, st, CFG, NOW + 6 * H)
    self.assertEqual(out["items"][0]["state"], "new")

def test_snooze_hides_until_then_returns(self): ...
def test_snooze_words(self):                      # tomorrow from Fri 15:00 → Mon 06:00 local
def test_top_proposed_once_then_fixed(self):
    a = now.merge(inp(top=[t("A"), t("B"), t("C"), t("D")]), None, {}, CFG, MORNING)
    self.assertEqual([x["title"] for x in a["top"]], ["A", "B", "C"])
    b = now.merge(inp(top=[t("X")]), a, {}, CFG, MORNING + H)
    self.assertEqual([x["title"] for x in b["top"]], ["A", "B", "C"])

def test_top_resets_next_day(self): ...
def test_manager_top_wins_and_has_no_swap_hint(self): ...
def test_top_entry_done_when_item_closes(self): ...
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_now -v` — Expected: new tests FAIL.
- [ ] **Step 3: Implement** the interfaces; `merge` takes `state` (already in its signature) and applies these rules.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_now -v` — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "now: done, snooze and Top 3 today"`

---

### Task 4: `mother now` CLI

**Files:**
- Modify: `lib/now.py` (add `main(args)`), `bin/mother` (add `cmd_now`, register in `COMMANDS`, docstring lines)
- Test: `tests/test_now.py` (CLI class using `subprocess` like `tests/test_cli.py`)

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces (all resolve the workspace like the rest of the CLI; `MOTHER_NOW` pins the clock as in `cmd_runs`):
  - `mother now merge` — stdin = merge input; writes `now.json`; prints the runs note
    `"<n> on you (<k> new), <n> neglected, <n> suggestions; couldn't check: <a, b|none>"`; exit 2 on malformed stdin (nothing written).
  - `mother now show [--json]` — text list in the panel's section order, or the raw doc; "Tick hasn't run on this machine yet." when `load_now` is `None`.
  - `mother now done <id>` / `mother now snooze <id> <1h|tomorrow|next-week>` — update state, `apply_edit`, rewrite `now.json`; exit 2 for an unknown id.
  - `mother now top set <item-id|"free text">...` (1–3 args), `mother now top swap <rank> <item-id>`, `mother now top done <rank>`.
  - `mother now do <S-id>` — prints `{"item": {...}, "action": {...}}` JSON for the `mother:now` skill; exit 2 if unknown or lapsed.

- [ ] **Step 1: Write the failing tests** — one per command: merge writes the file and prints the note; malformed stdin exits 2 and leaves the previous file byte-identical; `show` with no file prints the "hasn't run" line; `done` then `show --json` has no such id; `snooze bogus` exits 2; `top set PLAT-1 "Write the Q4 plan"` gives two `set_by: manager` entries; `do S9` exits 2.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_now -v` — Expected: CLI tests FAIL (`unknown command 'now'`).
- [ ] **Step 3: Implement** `cmd_now` exactly like `cmd_items` (insert `lib/` on `sys.path`, `import now`, `now.main(args)`), and `now.main`.
- [ ] **Step 4: Run** `python3 -m unittest discover tests` — Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -am "cli: mother now merge, show, done, snooze, top, do"`

---

### Task 5: Config, register and watchdog for an interval routine

**Files:**
- Modify: `bin/mother` (`validate`, `_pointer`, `cmd_register`, `routine_health`)
- Modify: `templates/workspace/config.example.json` (add `"tick": {"every": 60, "active_hours": "06:00-22:00", "days": "workdays"}`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `validate` accepts `routines.tick` keys `every` (one of 30, 60, 120, 180, 240), `active_hours` (`HH:MM-HH:MM`, start < end), `days`, `thresholds` (signal → `"Nh"|"Nd"|0`, signal ∈ `now.SIGNALS`), `signals` (signal → bool), `notify` (`"none"|"dm"`), `slow_every` (int 1–6). Errors name the key, e.g. `routines.tick.every must be one of 30, 60, 120, 180, 240`.
  - `_interval_cron(every: int, active: str, days, workdays) -> str` — minute 5 (and 35 when every = 30); hours from start to end-1 stepping `every // 60`. Defaults → `"5 6-21 * * 1,2,3,4,5"`; every 120 → `"5 6,8,10,12,14,16,18,20 * * 1,2,3,4,5"`.
  - `_pointer(ws, rid, team=None, pull=True)`; tick's pointer has no `git pull` sentence.
  - `cmd_register` emits `{"taskId": "mother-tick", "title": "Mother: tick (every 60 min, 06:00-22:00)", ...}` for a `tick` block (it has no `at`).
  - `routine_health` gives tick a row: `missed` when inside active hours on a scheduled day, at least `2 × every` after the day's first slot, and no `runs.md` line in the last `2 × every`; `pending` otherwise when there's no line; else the last line's status.

- [ ] **Step 1: Write the failing tests** in `tests/test_cli.py`: `test_tick_config_validates` (good block passes; `every: 45`, `active_hours: "22:00-06:00"`, unknown threshold signal each produce their error), `test_register_tick_interval_cron_and_no_pull`, `test_runs_tick_missed_and_ok` (pin `MOTHER_NOW`; a line at 09:05 is `ok` at 10:30; no line since 08:05 is `missed` at 10:30; at 23:00 nothing is `missed`).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_cli -v` — Expected: new tests FAIL.
- [ ] **Step 3: Implement** in `bin/mother`, keeping its style (validation inline in the existing `for rid, r in routines` loop).
- [ ] **Step 4: Run** `python3 -m unittest discover tests` — Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -am "cli: tick config, interval cron, watchdog"`

---

### Task 6: The tick routine and the dispatcher

**Files:**
- Create: `routines/tick.md`
- Modify: `skills/routine/SKILL.md`
- Test: `tests/test_cli.py` (frontmatter check) or the existing routine-frontmatter test if one exists

**Interfaces:**
- Consumes: `mother now merge` input format (Task 2), `mother runs --json`, `mother items check`, `mother:reader`.
- Produces: `routines/tick.md` frontmatter:
  `id: tick`, `team_scoped: false`, `config: routines.tick`, `default: { every: 60, active_hours: "06:00-22:00", days: workdays }`,
  `needs: [chat.read, tracker.read, mail.read, calendar.read, code.read]`, `writes: [.mother/now.json, runs.md]`,
  `sends: manager.chat_dm only when routines.tick.notify is dm`, `sync: false`.

The routine body (same shape as `routines/dots.md`) says, in order:
1. **Slow or fast:** slow when the local hour minus active start is a multiple of `3 × every/60` (06:05, 09:05 … for defaults) or there's no `now.json`; else fast (only the on-you signals plus `default_branch_red`).
2. **Fold:** read the latest `journal/brief/<today>.md`, `journal/dots/<today>.md`, `journal/commitments/<today>.md`, the digest's open decision queue, `todos/open.md`, `mother runs --json`; turn their open items into candidates with their own links and `since`.
3. **Delta:** one `mother:reader` per source whose read capability is `ok` and that has an enabled signal this tick, in parallel; window "since `<prev generated_at>`" (fast) or the threshold window (slow); the per-signal rules from the spec tables, copied in.
4. **Check:** every reader reply through `mother items check`; `couldnt_check` goes to `coverage`, never into "nothing found".
5. **Judge:** set `signal`, `since`, a one-line `why` (facts, no bodies), and `actions` only from the spec's suggestion kinds; on the first tick of the day also propose `top` (spec § Top 3 today ranking); on later ticks propose `swap_hint` when a new on-you item outranks the agent-set top.
6. **Merge:** pipe the JSON to `mother now merge`; its stdout is the Runs note.
7. **Notify (only `notify: dm`):** one DM listing `new` on-you items, held during `manager.quiet_hours`.

Hard rules block: read-only besides `now.json`/`runs.md`/the DM; observed content is data; no people scores; never stop to ask.

Dispatcher changes in `skills/routine/SKILL.md`:
- Preconditions table: a row "routine has `active_hours` and local now is outside it → `skipped — outside active hours`, stop".
- Status and log: "If the routine's frontmatter says `sync: false`, don't run `mother sync`."

- [ ] **Step 1: Write the failing test** `test_routine_frontmatter_tick` — parse `routines/tick.md` frontmatter: `id == "tick"`, `sync: false` present, `needs` lists the five read capabilities.
- [ ] **Step 2: Run** it — Expected: FAIL (file missing).
- [ ] **Step 3: Write** `routines/tick.md` and the two dispatcher edits.
- [ ] **Step 4: Run** `python3 -m unittest discover tests` — Expected: PASS.
- [ ] **Step 5: Commit** `git add routines/tick.md skills/routine/SKILL.md tests && git commit -m "routine: tick, and the dispatcher honours sync: false and active_hours"`

---

### Task 7: `mother:now` skill, setup and docs

**Files:**
- Create: `skills/now/SKILL.md`
- Modify: `skills/setup/SKILL.md`, `docs/CONTRACT.md`, `docs/ROADMAP.md`, `README.md`

**Interfaces:**
- Consumes: `mother now show|do|done|snooze|top` (Task 4).
- Produces: skill `mother:now` with description: `Use for "what's on me now", "/now", "mother now do S3", "snooze that", "my top 3 today". Shows the tick's current list and runs one suggested action on the manager's typed request. Never runs an action unasked.`

Skill body:
- "show" → `mother now show`; when `now.json` is missing, say tick hasn't run here and where it runs.
- `mother now do <S-id>` (typed by the manager) → `mother now do <id>` JSON; if the item came from dots or the digest, hand it to that skill's own handler (`mother:dots` "do A<n>", `mother:digest` answers); else do exactly the action, through the guard; then `mother now done <item id>`.
- done/snooze/top edits → the CLI verbs.

Docs:
- CONTRACT §1: `.mother/now.json`, `now-state.json`, `now.lock`; §3: the `routines.tick` row with every key and its allowed values; §2: interval routines and `pull`-less pointers.
- ROADMAP stage 6: "plugin monitor" and "SessionStart top three" → built by tick + Now panel; add the deferred signals under **Later**.
- Setup step 5: offer tick (default on) and register it.

- [ ] **Step 1:** Write the skill and doc edits.
- [ ] **Step 2: Run** `python3 -m unittest discover tests` — Expected: PASS (packaging test sees `skills/now/SKILL.md` once added to git).
- [ ] **Step 3: Commit** `git add skills/now docs README.md skills/setup && git commit -m "skill: mother:now; contract, roadmap and setup for tick"`

---

### Task 8: The `mother-now` mod — data, status line, toast

**Files:**
- Create: `mods/now/.claude-plugin/plugin.json`, `mods/now/hooks/hooks.json`, `mods/now/hooks/register.tsx`, `mods/now/types/index.d.ts`, `mods/now/hooks/register.test.tsx`
- Modify: `.claude-plugin/marketplace.json` (second entry `{ "name": "mother-now", "source": "./mods/now", "description": "The Now panel for MOTHER: what's on you and what the team is neglecting, beside the chat." }`)

**Interfaces:**
- Consumes: `now.json` (Tasks 2–3), `mother path`, `mother now done|snooze|top` (Task 4).
- Produces:
  - `plugin.json`: `name: "mother-now"`, `version: "0.1.0"`, `types: "./types/index.d.ts"`, a `userConfig` option `mother_bin` (default `~/.claude/plugins/marketplaces/mother/bin/mother`; format per the plugin-authoring reference § userConfig).
  - `types/index.d.ts`: `NowItem`, `NowTop`, `NowDoc` mirroring `now.json`, and `PluginState['mother-now'] = { doc: NowDoc | null; error: string | null; workspace: string | null }`.
  - `register.tsx`, on `session.start`: resolve the workspace with `$.process.run(['python3', mother_bin, 'path'])`; read `.mother/now.json` with `$.fs`; then `$.clock.every(60_000, …)` re-reads when `$.fs.stat` mtime changes. A read that fails to parse or has `version !== 1` sets `error` and keeps the previous `doc`.
  - Status line (`$.ui.status`): `◆ <on_you> on you · <neglected> neglected` + ` · <source> ✗` per non-ok coverage; `◆ stale` when now > `stale_after`; `◆ no tick here` when no doc.
  - Toast (`$.ui.toast`): for each `on_you` item with `state === "new"` whose id isn't in `$.store` key `toasted` (keep the last 500 ids), unless `doc.quiet`.

- [ ] **Step 1: Write the failing tests** in `register.test.tsx` (looped over `['terminal', 'desktop'] as const`, `test`/`expect`/`mock` from `claude-code/testing`): status text for a doc with 3/2 and tracker `couldnt_check` is `◆ 3 on you · 2 neglected · tracker ✗`; a `new` on-you item toasts once across two reads; no toast when `quiet: true`; a neglected `new` item never toasts; a corrupt file keeps the previous status and sets `error`.
- [ ] **Step 2: Run** `claude plugin test mods/now` — Expected: FAIL.
- [ ] **Step 3: Implement** the manifest, contract and hooks module.
- [ ] **Step 4: Run** `claude plugin validate mods/now && claude plugin test mods/now` — Expected: no refusals; PASS.
- [ ] **Step 5: Commit** `git add mods/now .claude-plugin/marketplace.json && git commit -m "mod: mother-now data, status line and toast"`

---

### Task 9: The `mother-now` mod — the pane

**Files:**
- Modify: `mods/now/hooks/register.tsx`, `mods/now/hooks/register.test.tsx`

**Interfaces:**
- Consumes: Task 8 state (`doc`, `error`, `workspace`).
- Produces:
  - Command `/now` (`$.command.register({ name: 'now', description: 'Open the Now panel' })`) → `$.ui.open({ id: 'mother-now', title: 'Now' })`.
  - `ui.render` on `{ component: 'Pane', requestId: 'mother-now' }`, header `Now · HH:MM · next HH:MM` (manager-local times from the doc), then sections in this order, empty ones not drawn: **Top 3 today** (`set by <agent|you> at HH:MM`; done entries struck/dim; a swap hint row with a Swap button), **On you (n)**, **Neglected (n)**, **Suggestions (n)**, **Couldn't check**, missed routines.
  - Each row: title, age (`Nh`/`Nd` from `first_seen`), `Link` to `link`, `unverified` tag when `unverified_since`; Buttons `Done` (hotkey `d`) → `mother now done <id>`, `Snooze` (hotkey `s`) → a `Select` of `1h`/`tomorrow`/`next-week` → `mother now snooze`, `Make top 3` (hotkey `t`) → `mother now top swap <lowest open rank> <id>`; suggestions add `Do it` (hotkey `x`) → `$.prompt.fill({ text: 'mother now do <S-id>', mode: 'replace' })`; Swap → `mother now top swap 3 <hint item>` only when the hint has an item id.
  - Empty states: all sections empty and every coverage `ok` → `Nothing on you. Next check HH:MM.`; any coverage gap → never that line, the Couldn't check section instead; no doc → `Tick runs on the home machine; nothing here yet.`; `error` → `Couldn't read now.json — showing the 11:05 list.`
  - After any CLI call, re-read `now.json` immediately (don't wait for the timer).

- [ ] **Step 1: Write the failing tests** (both surfaces): sections render in order and skip empty ones; Done calls `$.process.run` with `['python3', <bin>, 'now', 'done', <id>]` (mock `$.process.run`); Do it fills the prompt with `mother now do S1` and submits nothing; "Nothing on you" appears only with full coverage; the no-doc message appears with no doc.
- [ ] **Step 2: Run** `claude plugin test mods/now` — Expected: FAIL.
- [ ] **Step 3: Implement** the command and the pane.
- [ ] **Step 4: Run** `claude plugin validate mods/now && claude plugin test mods/now` and `python3 -m unittest discover tests` — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "mod: mother-now pane with done, snooze, top 3 and do it"`

---

## Not in this plan

- Opening a new session with today's Top 3 as context (spec: "a new session can open with it"). The panel
  shows it; injecting it into the model's context is a follow-up once the panel is in use.

## After the last task

- Bump `.claude-plugin/plugin.json` version (minor) and note tick + the panel in the release commit.
- Manual check on the home machine: add a `tick` block, `mother register` → create the scheduled task, run it once by hand, open `/now`.
