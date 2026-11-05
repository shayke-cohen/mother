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
  | { kind: 'reply'; text: string }
  | { kind: 'open' }

// A card's TL;DR (at most 2 short lines) and up to 3 chips, from one model call over the session's recent tail.
export type ChiefTldr = { tldr: string; chips: ChiefChip[] }

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
// own mod at turn end from one model call over its final message; all of it is untrusted text.
export type FloorOption = { label: string; message: string }     // the button, and what it sends as the manager
export type FloorCard = {
  session_id: string                 // the session's CLI id (a pinned row's cli_session_id)
  asks: boolean
  question: string
  options: FloorOption[]             // 0-4
  waiting_on: string | null
  at: string                         // when the card was written
}
export type FloorState = { cards: FloorCard[]; self: string | null; answered: string[] }

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
}

export type ChiefInboxItem = {
  id: string   // U<n>
  key: string; title: string; why: string; link: string; at: string | null
  session: { key: string; title: string } | null   // the live hub session for the key
}

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
    }
  }
}
