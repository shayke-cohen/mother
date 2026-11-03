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
