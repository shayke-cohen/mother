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
export type WatcherSource = { status: 'ok' | 'partial' | 'couldnt_check'; reason?: string; since?: string }

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
}

export type HubSessions = { version: 1; sessions: Record<string, HubSession> }

// watch.json (`mother hub follow|mute`); the panel reads only `mute`. `until`: muted value -> ISO end of a `--for` mute.
export type HubWatch = { version: 1; mute?: { keys?: string[]; people?: string[]; channels?: string[]; until?: Record<string, string> } }

// What the panel holds of the hub files; null = the file is missing (or never read).
export type HubBundle = { hub: HubDoc | null; sessions: HubSessions | null; watcher: WatcherDoc | null; watch: HubWatch | null }

declare module 'claude-code' {
  interface PluginState {
    mother: {
      doc: NowDoc | null
      error: string | null
      workspace: string | null
      selected: string | null   // row holding the d/s/t/x hotkeys: an item id or top:<rank>
      snoozing: string | null   // the row whose snooze Select is open (an item id, hub:<U-id>), or ignore:<row> for its Ignore picker
      expanded: string[] | null // sections drawn in full (`on_you`, `neglected`, `suggestion`) and `health`; `hub` flips the Hub section's default, `routed` opens Recently routed, `ignored` opens Ignored
      hub: HubBundle | null     // watcher.json, hub.json, sessions.json and watch.json as last read
    }
  }
}
