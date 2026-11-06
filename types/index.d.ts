// The shape of <workspace>/.mother/now.json (docs/CONTRACT.md §1, lib/now.py merge()).
// No message bodies, no per-person counts: ids, titles and links only.

export type NowAction = {
  id: string       // S<n>
  label: string
  prompt: string   // always `mother now do <id>`: what the panel pastes
  task: string     // the routine's original instruction
}

export type NowItem = {
  id: string
  section: 'on_you' | 'neglected' | 'suggestion'
  signal: string
  title: string
  why: string
  link: string
  also: string[]
  links?: { label: string; url: string }[]   // labelled source links from the tick (CONTRACT §1)
  from?: string | null                       // the sender (chat user id or email), for Ignore by sender
  similar?: { words: string[]; channel?: string } | null   // the tick's "similar messages" rule for Ignore
  team: string | null
  since: string
  first_seen: string
  last_seen: string
  state: 'new' | 'open'
  snooze_until: string | null
  unverified_since?: string
  actions: NowAction[]
}

export type NowCoverage = {
  status: 'ok' | 'partial' | 'couldnt_check'
  reason?: string
  since?: string
}

export type NowTop = {
  rank: number
  title: string
  why: string
  link: string | null
  item_id: string | null
  set_by: 'agent' | 'manager'
  state: 'open' | 'done'
  set_at?: string | null   // ISO time the list was set: the tick that proposed it, or the manager's edit
}

// What the tick's step 6 writes (routines/tick.md): a suggestion to swap; item_id only when it names a list item.
export type NowSwapHint = { title: string; why: string; link: string | null; item_id?: string | null }

export type NowRoutine = { id: string; state: string; expected: string }

export type NowDoc = {
  version: 1
  generated_at: string
  timezone: string | null
  quiet: boolean
  next_at: string
  stale_after: string
  status: 'ok' | 'partial' | 'failed'
  coverage: Record<string, NowCoverage>
  routines: NowRoutine[]
  counts: { on_you: number; neglected: number; suggestion: number }
  next_action: number
  dropped: number
  top: NowTop[]
  top_date: string
  swap_hint: NowSwapHint | null
  items: NowItem[]
}

// The hub's files in <workspace>/.mother/hub/ (docs/CONTRACT.md §5e, lib/hub.py). Readers tolerate any field missing.
export type WatcherSource = {
  status: 'ok' | 'partial' | 'couldnt_check'; reason?: string; since?: string
  every_seconds?: number   // this source's own interval for the current period (0: not polled now)
  at?: string              // this source's last poll
}

export type WatcherDoc = {
  at: string
  every_seconds?: number   // the watcher's current effective interval
  active?: boolean         // inside active hours now
  last_append_at?: string  // the last poll that appended a line the hub must decide
  sources?: Record<string, WatcherSource>
  errors?: string[]
}

export type HubRouted = { line_id: string; key: string; decision: string; session_id?: string; at: string; note?: string }

export type HubPending = {
  key: string; title: string; link: string | null; read?: string; next_step?: string
  name?: string; proposed_at: string; raised_at: string | null   // raised_at null until a card is raised
  task_id?: string | null; last_update_at?: string; also?: string[]
}

export type HubUpdate = {
  id: string   // U<n>
  key: string; title: string; why: string; link: string | null; at: string
  state: 'open' | 'done' | 'snoozed'
  snooze_until?: string
}

export type HubDoc = {
  version: 1
  last_wake?: string | null
  inbox_cursor?: string | null
  next_update?: number
  routed?: HubRouted[]
  pending?: HubPending[]
  updates?: HubUpdate[]
}

export type HubSession = {
  session_id: string; name: string; title: string
  link: string | null     // a claude:// URL, or null
  also?: string[]
  opened_at?: string; last_routed_at?: string | null
  unseen?: number
  state: 'live' | 'dropped'
  kind?: 'cli'            // a row left over from before 0.18.0 (a headless worker); the hub panel leaves it alone
}

export type HubSessions = { version: 1; sessions: Record<string, HubSession> }

// watch.json (`mother hub follow|mute`); the panel reads only `mute`. `until`: muted value -> ISO end of a `--for` mute.
export type HubWatch = { version: 1; mute?: { keys?: string[]; people?: string[]; channels?: string[]; topics?: string[]; until?: Record<string, string> } }

// One page of the Inspector pane: what it shows (`id`: an item id, a U-id, a session or pending key, a team; `filter`: the
// decision queue's team). The pane draws the last ref of the stack in `inspect`; Back pops it.
export type InspectRef = {
  kind: 'item' | 'update' | 'session' | 'sessions' | 'proposal' | 'decisions' | 'team' | 'system'
  id?: string
  filter?: string
}

// What the panel holds of the hub files; null = the file is missing (or never read).
export type HubBundle = { hub: HubDoc | null; sessions: HubSessions | null; watcher: WatcherDoc | null; watch: HubWatch | null }

// One recommended action on a Chief pane card. Only these kinds exist; the pane drops anything else.
export type ChiefChip =
  | { kind: 'reply'; text: string; label?: string }   // `text` is sent; `label` (<= 24) is the button's words, else the text's first words
  | { kind: 'open' }

// A card's TL;DR (at most 2 short lines) and up to 3 chips, from one model call over the session's recent tail.
// A session's card. `tldr` and `chips` alone are the older TL;DR; a refreshed card (CONTRACT §5g "Session refresh")
// also carries its state, what changed since the card before, and the question it stopped on. `at`, `running`, `stuck`
// and `moved` are what the last `mother chief tail --json` read said of the session, joined on by the mod.
export type ChiefCardState = 'working' | 'waiting_manager' | 'waiting_other' | 'done' | 'stuck' | 'idle'
export type ChiefTldr = {
  tldr: string; chips: ChiefChip[]
  state?: ChiefCardState
  progress?: string                  // one line: what changed since the previous card; '' when nothing did
  asks?: boolean; question?: string; options?: FloorOption[]
  waiting_on?: string | null
  prev_state?: ChiefCardState | null // the state of the card this one replaced
  at?: string | null                 // when the card was stored
  running?: boolean                  // its transcript moved in the last 2 minutes, or its turn is open
  stuck?: boolean                    // the code rule (lib/chief.py _is_stuck)
  moved?: boolean                    // the session moved since this card was made; a new one is due
}

// What a dots action or manager item says, from `mother chief dots-detail <id> --json`; null = nothing on disk has it.
export type ChiefDetail = { id: string; text: string; date: string; source_file: string; label?: string | null }

// A pinned desktop session (lib/chief.py desktop_status), from the snapshot a desktop session writes.
export type ChiefDesktop = {
  session_id: string                 // local_…
  cli_session_id: string             // what $.session.send reaches it by
  title: string
  cwd: string
  repo: string                       // the cwd's basename
  last_activity_at: string | null
  last_report: string                // the first 200 characters of its last turn report
  open_link: string | null
}

// One session's card (lib/floor.py): did its last turn stop on a question for the manager. Written by that session's
// own mod at turn end, from its wrap_up call or one model call over its final message; all of it is untrusted text.
export type FloorOption = { label: string; message: string }     // the button, and what it sends as the manager
export type FloorCard = {
  session_id: string                 // the session's CLI id (a pinned row's cli_session_id)
  asks: boolean
  question: string
  options: FloorOption[]             // 0-4
  waiting_on: string | null
  at: string                         // when the question was first asked
  ask_id?: string                    // names this question at this `at`: a press drawn for another one is dropped
  recommendation?: string            // the exact instruction one press would send; absent when nothing was recommended
  why_manager?: string               // why this needs the manager
  recap?: string                     // one line on where the session stands
  source?: 'wrap_up'                 // from the session's own wrap_up call; absent when a model read its final message
  repo?: string; branch?: string     // of the session's checkout, when known
  repo_url?: string                  // when known and on a host where `#123` can be made a link
}
// `held`: session id -> the ask id the manager put on hold; the card stays hidden until its ask id changes.
export type FloorState = { cards: FloorCard[]; self: string | null; answered: string[]; held?: Record<string, string> }

// A session that owns one inbox item's key until it is closed (hooks/cos.ts, skills/cos): a `kind: "case"` row of the
// hub's sessions.json. `unseen` is how many routed lines wait to be delivered into it.
export type ChiefCase = {
  key: string; cli_session_id: string; title: string; link: string | null
  links?: { label: string; url: string }[]   // the case's sources, its own first (lib/chief.py item_links)
  also?: string[]                    // the other keys the case owns (claim `also`, `mother chief watch-key`)
  label?: string                     // what a card shows in place of the raw key
  unseen: number
  last_routed_at: string | null; opened_at: string | null
  deliver_now?: boolean              // `mother chief relay deliver` asked it to deliver once though relay is off
}

// `mother chief status --json` (lib/chief.py status_doc, version 1).
export type ChiefStatus = {
  version: 1
  at: string
  paused: boolean
  chief: { alive: boolean; heartbeat_at: string | null }   // the heartbeat a leading session's `tick --as` writes
  // spent reads 0.0 when spend.json is unreadable; unreadable and over (spent >= limit, or unreadable) mean no model
  // call is made
  budget: { spent: number; limit: number; unreadable?: boolean; over?: boolean }
  desktop?: ChiefDesktop[]           // pinned desktop sessions
  led_by?: string | null             // the session id that wrote the heartbeat while it is fresh (under 120 s), else null
  inbox?: ChiefInboxItem[]           // the hub's open updates (`now` decisions), newest first, at most 10
  inbox_count?: number               // all of them
  cases?: ChiefCase[]                // live case sessions (lib/chief.py cases), oldest first
  relay?: { on: boolean }            // false while .mother/chief/RELAY_OFF exists: routed lines wait, nothing is submitted
  guard?: 'on' | 'off' | 'paused'    // the guard: `paused` while .mother/PAUSED exists (it wins), `off` while .mother/GUARD_OFF does
  health?: ChiefHealth               // the Health tab's reads; an Inbox item keyed `watcher:` is here, not in `inbox`
  habits?: { on: boolean }           // false while .mother/chief/HABITS_OFF exists: nothing is quieted, no habit reaches a triage prompt
  // lines the quiet rule logged instead of showing (lib/habits.py): how many in 7 days, and the last 10, newest first
  quieted?: { count_7d: number; recent: { id: string; key: string; title: string; label: string; bucket: string; at: string; why: string }[] }
  quiet?: boolean                    // inside manager.quiet_hours (manager.timezone): the pane notifies only urgent things
}

// `mother chief status --json` -> health (lib/chief.py health_doc): what the Chief pane's Health tab draws.
export type ChiefHealth = {
  guard?: 'on' | 'off' | 'paused'    // .mother/PAUSED (wins), .mother/GUARD_OFF, else on
  // .mother/hub/watcher.json; null = the watcher never ran. `stale`: in active hours and older than twice its interval
  watcher?: { at: string; every_seconds?: number; active?: boolean; stale?: boolean
              sources?: Record<string, { status: string; reason?: string; since?: string | null; at?: string | null
                                         degraded?: string }> } | null   // degraded: reading less than usual (rate limited)
  watcher_errors?: ChiefInboxItem[]  // the hub's open updates keyed `watcher:<source>`, newest first, at most 20
  watcher_error_count?: number
  // .mother/<routine>-unsent-<YYYY-MM-DD>.md: reports that did not reach the manager's DM, newest first, at most 20
  undelivered?: { name: string; routine: string; date: string; bytes: number }[]
  // sources only the manager can fix, matched against lib/chief.py ACCESS_PATTERNS: a sign-in or token (`signin`), or
  // the VPN, network or a timeout (`network`); one row per source, gone once the watcher reads it again
  access?: ChiefAccess[]
}

export type ChiefAccess = { source: string; kind: 'signin' | 'network'; reason: string; help: string; since?: string | null }

// The pane's "what just changed" line (hooks/events.ts): the one current event, shown under the mood line.
export type ChiefEvent = { kind: string; subject: string; text: string; color: string; at: number }

// The Health tab's own state: report names the manager dismissed (also kept in the plugin store as `chiefDismissed`),
// and each expanded report's text as `mother chief report <name>` printed it (null = it could not be read).
export type ChiefHealthState = { dismissed: string[]; reports: Record<string, string | null> }

export type ChiefInboxItem = {
  id: string   // U<n>
  key: string; title: string; why: string; link: string; at: string | null
  links?: { label: string; url: string }[]   // every labelled source, the item's own first; [] when none is known
  source?: string                    // the key's kind: chat, mail, tracker, code, calendar, manual, error, other
  who?: string                       // the person its newest inbox line names; '' when none
  label?: string                     // what a card shows in place of the raw key: a channel's name, a person, `mail thread`…
  // the triage verdict, from .mother/chief/triage.json; null / [] for an item decided with none (hooks/pane_ui.tsx verdictExtra)
  kind?: 'decide' | 'reply' | 'act' | 'fyi' | null
  on?: 'manager' | 'team' | 'other' | null
  urgency?: 'now' | 'today' | 'later' | null
  actions?: { label: string; prompt: string }[]   // at most 3, the first the recommended one
  reason?: string                    // why it reached the manager, in the verdict's words; at most 100 characters
  habit_note?: string | null         // set by triage when the manager's habits changed its verdict; at most 80 characters
  session: { key: string; title: string; kind?: 'case'; cli_session_id?: string } | null   // the live hub session for the key; a case names its session
}

// Which Chief pane cards are drawn in full. `cards` holds the manager's toggles by card id (`need:<cli id>`,
// `inbox:<U-id>`, `sess:<cli id>`); `all` is `/chief expand` (true) or `collapse` (false), beneath a later toggle and
// above the defaults (Needs you open; Inbox and sessions closed, the selected card open).
export type ChiefOpen = { all: boolean | null; cards: Record<string, boolean> }

// The Chief pane's tab: `picked` is the manager's choice (`needs`, `inbox`, `sessions`, `today`, `all`; null = the
// first tab that holds something); `seen` is each tab's count when it was last opened or left, for its "new" mark.
// `states` is each session's card state when the Sessions tab was last opened or left: a change since marks the tab.
export type ChiefTabs = { picked: string | null; seen: Record<string, number>; states?: Record<string, string> }

// The Chief pane's skin: the manager's `/chief skin dark|dark-app|plain`, kept in the plugin's store; null = the
// default (`dark`: a dark panel on the desktop app, its pills filled for the dark labels of an app in light mode).
export type ChiefSkin = { mode: 'dark' | 'dark-app' | 'plain' | null }

declare module 'claude-code' {
  interface PluginState {
    mother: {
      doc: NowDoc | null
      error: string | null
      workspace: string | null
      selected: string | null   // the Top 3 entry holding the d/x/a hotkeys: top:<rank>
      snoozing: string | null   // the Inspector picker open: fp-snooze / fp-ignore (an item), hub:<U-id> / ignore:hub:<U-id>, ignore:sess:<key>; hub:refresh while a refresh runs
      expanded: string[] | null // Home groups drawn in full (`g-reply`, `g-review`, `g-other`, `g-suggestion`); `ignored` opens Ignored, `insp-more` the item page's extra moves, `insp-dropped` archived sessions
      hub: HubBundle | null     // watcher.json, hub.json, sessions.json and watch.json as last read
      inspect: InspectRef[] | null // the Inspector's back stack; the last ref is the page shown
      chief: ChiefStatus | null    // `mother chief status --json` as last read; null until it answers
      chiefError: string | null    // the last chief CLI failure the Chief pane shows
      chiefSent: Record<string, string> | null   // a pinned card's last send, by cli_session_id: "delivered" or why not
      chiefTldr: Record<string, ChiefTldr> | null   // each card's TL;DR and chips, by a pinned session's cli_session_id
      chiefDraft: Record<string, string> | null     // text held in a card's box, by Input key: a reply chip's (`say:<cli id>`); `inbox-snooze:<id>` is set while an Inbox item's snooze picker is open
      chiefFloor: FloorState | null                 // `mother floor list --json` as last read, this session's id, and the cards answered from the pane since
      chiefOpen: ChiefOpen | null                   // which Chief pane cards are expanded; null = the defaults
      chiefTab: ChiefTabs | null                    // the Chief pane's tab and the counts last seen on each
      chiefHealth: ChiefHealthState | null          // the Health tab: dismissed undelivered reports, loaded report texts
      chiefDetail: Record<string, ChiefDetail | null> | null   // what each dots item does, by Now item id, once its card was pressed (null = nothing on disk)
      chiefSkin: ChiefSkin | null                   // the pane's skin, read from the store at session start
      chiefPeople: Record<string, string> | null    // chat user ids to names, from `mother chief context --json`, read at session start
      chiefTyped: Record<string, string> | null     // what a field holds as it is typed, by Input key; read on a press, never by a draw
      chiefSelected: string | null                  // the card id (`sess:<cli id>`, `inbox:<U-id>`) a button was last pressed on; drawn with a bold border
      chiefEvent: ChiefEvent | null                 // what just changed, by priority (hooks/events.ts); null = nothing to say
    }
  }
}
