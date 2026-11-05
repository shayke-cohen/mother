import type { Hook } from 'claude-code'

// Helpers both panes use. register.tsx re-exports them, so its tests keep their imports; chief.tsx imports them from
// here, which keeps chief.tsx and register.tsx free of an import cycle.

// The engine interface a hook receives; the helpers take it so each pane can pass its own `$`.
export type Dollar = Parameters<Hook<'session.start'>>[0]

// Empty (the default) means this plugin's own bin/mother: the panel ships inside the mother plugin.
export async function motherBin($: Dollar, configured: string): Promise<string> {
  if (!configured) return $.plugin.root + '/bin/mother'
  if (configured !== '~' && !configured.startsWith('~/')) return configured
  const home = (await $.env.get('HOME')) ?? ''
  return home + configured.slice(1)
}

// HH:MM of an ISO instant in the doc's timezone. Falls back to the HH:MM written in the ISO string
// (the instant's own clock, which is UTC for what lib/now.py writes) when Intl or the zone is missing.
export function clock(iso: string | undefined | null, tz: string | null | undefined): string {
  if (!iso) return '--:--'
  const ms = Date.parse(iso)
  if (tz && !Number.isNaN(ms)) {
    try {
      const parts = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(ms)
      const part = (t: string) => parts.find(p => p.type === t)?.value
      const h = part('hour'), m = part('minute')
      if (h && m) return `${h === '24' ? '00' : h}:${m}`
    } catch {
      // fall through to the ISO text
    }
  }
  return /T(\d\d:\d\d)/.exec(iso)?.[1] ?? '--:--'
}

// A coarse age for the hub rows: minutes matter there.
export function ago(iso: string | undefined | null, nowMs: number): string {
  const ms = iso ? Date.parse(iso) : NaN
  if (Number.isNaN(ms)) return ''
  const mins = Math.max(0, Math.floor((nowMs - ms) / 60_000))
  return mins < 1 ? 'now' : mins < 60 ? `${mins}m` : mins < 1440 ? `${Math.floor(mins / 60)}h` : `${Math.floor(mins / 1440)}d`
}

// Control and bidi characters never reach the pane: a tab becomes a space, the rest are dropped.
const UNSAFE_RE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g
export const safe = (text: string): string => text.replace(/\t/g, ' ').replace(UNSAFE_RE, '')

// One line of untrusted text (line-written, model-written) for a toast, a list or a card.
export const oneLine = (text: unknown, cap = 200): string => safe(String(text ?? '')).split(/\s+/).filter(Boolean).join(' ').slice(0, cap)
// ---- hub actions both panes offer (the Now panel's hub updates, the Chief pane's Inbox) ----

export const SNOOZES = ['1h', 'tomorrow', 'next-week'] as const
export const isUrl = (link: string | null | undefined): link is string => !!link && /^https?:\/\//.test(link)

// Run one `mother <group> …` verb. A failure toasts the first line of stderr; `toastOut` toasts the first line of a
// success. True when it exited 0. The caller re-reads what it draws.
export async function motherVerb($: Dollar, bin: string, group: string, args: string[], toastOut = false): Promise<boolean> {
  try {
    const ran = await $.process.run(['python3', bin, group, ...args])
    if (ran.exitCode === 0 && toastOut) {
      const first = String(ran.stdout ?? '').split('\n').find(l => l.trim())
      if (first) $.ui.toast(first.trim())
    }
    if (ran.exitCode !== 0) {
      const line = String(ran.stderr ?? '').split('\n').find(l => l.trim()) ?? `mother ${group} ${args[0]} failed`
      $.ui.toast(line.trim())
    }
    return ran.exitCode === 0
  } catch (err) {
    $.ui.toast(`mother ${group} ${args[0]} failed: ${(err as Error).message ?? String(err)}`)
    return false
  }
}

// The prompt an item's or a Top 3 entry's "Ask" button fills: the item in full, ready to send as is.
export function askPrompt(title: string, why: string | null | undefined, link: string | null | undefined): string {
  return `Help me close this out: "${title}"` + (why ? ` — ${why}` : '') + (link ? ` (${link})` : '')
    + '. What is the fastest next step? Draft anything that reaches another person; don\'t send.'
}

// Open a session: fill the prompt (the way in on a surface that cannot open the link) and run `session seen`.
export function openSession($: Dollar, goHub: (args: string[], rowId?: string | null) => Promise<boolean>, key: string, v: { title: string }) {
  void $.prompt.fill({ text: `Open my "${v.title}" session`, mode: 'replace' })
  void goHub(['session', 'seen', key])
}
