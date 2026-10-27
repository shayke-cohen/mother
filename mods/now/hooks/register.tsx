import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { NowDoc } from '../types'

// State the Now panel (Task 9) draws from. Keys are declared in types/index.d.ts.
export const docAtom = atom({ plugin: 'mother-now', key: 'doc' } as const, null)
export const errorAtom = atom({ plugin: 'mother-now', key: 'error' } as const, null)
export const workspaceAtom = atom({ plugin: 'mother-now', key: 'workspace' } as const, null)

export const POLL_MS = 60_000
export const TOASTED_CAP = 500
const DEFAULT_BIN = '~/.claude/plugins/marketplaces/mother/bin/mother'

// What `load` remembers between polls: the mtime it last read, so an unchanged file is not re-read.
export type Loader = { mtimeMs: number | null }
export const newLoader = (): Loader => ({ mtimeMs: null })

export function statusText(doc: NowDoc | null, nowMs: number): string {
  if (!doc) return '◆ no tick here'
  if (nowMs > Date.parse(doc.stale_after)) return '◆ stale'
  let text = `◆ ${doc.counts?.on_you ?? 0} on you · ${doc.counts?.neglected ?? 0} neglected`
  for (const [source, row] of Object.entries(doc.coverage ?? {})) {
    if (row && row.status !== 'ok') text += ` · ${source} ✗`
  }
  return text
}

async function motherBin($: any, configured: string): Promise<string> {
  if (configured !== '~' && !configured.startsWith('~/')) return configured
  const home = (await $.env.get('HOME')) ?? ''
  return home + configured.slice(1)
}

async function workspace($: any, bin: string): Promise<string | null> {
  const known = await read($, workspaceAtom)
  if (known) return known
  try {
    const ran = await $.process.run(['python3', bin, 'path'])
    const path = String(ran.stdout ?? '').trim()
    if (ran.exitCode !== 0 || !path) throw new Error(String(ran.stderr ?? '').trim() || 'no workspace')
    await update($, workspaceAtom, () => path)
    return path
  } catch (err) {
    await update($, errorAtom, () => `mother path failed: ${(err as Error).message}`)
    return null
  }
}

async function toastNew($: any, doc: NowDoc): Promise<void> {
  if (doc.quiet) return
  const seen: string[] = ((await $.store.get('toasted')) as string[] | undefined) ?? []
  const fresh = (doc.items ?? []).filter(i => i.section === 'on_you' && i.state === 'new' && !seen.includes(i.id))
  if (fresh.length === 0) return
  for (const i of fresh) $.ui.toast(`◆ New on you: ${i.title}`)
  await $.store.set('toasted', [...seen, ...fresh.map(i => i.id)].slice(-TOASTED_CAP))
}

// One pass: resolve the workspace, re-read now.json when its mtime moved, toast what is new, draw the
// status line. A file that fails to parse (or has another version) sets `error` and keeps the old doc.
export async function load($: any, bin: string, loader: Loader): Promise<void> {
  const ws = await workspace($, bin)
  if (!ws) {
    await update($, docAtom, () => null)
  } else {
    await readDoc($, `${ws}/.mother/now.json`, loader)
  }
  $.ui.status(statusText(await read($, docAtom), await $.clock.now()))
}

async function readDoc($: any, path: string, loader: Loader): Promise<void> {
  let mtimeMs: number
  try {
    mtimeMs = (await $.fs.stat(path)).mtimeMs
  } catch {
    loader.mtimeMs = null
    await update($, docAtom, () => null)
    await update($, errorAtom, () => null)
    return
  }
  if (mtimeMs === loader.mtimeMs) return
  loader.mtimeMs = mtimeMs
  try {
    const parsed = JSON.parse(await $.fs.read(path))
    if (!parsed || typeof parsed !== 'object' || parsed.version !== 1) throw new Error('unsupported version')
    await update($, docAtom, () => parsed as NowDoc)
    await update($, errorAtom, () => null)
    await toastNew($, parsed as NowDoc)
  } catch (err) {
    await update($, errorAtom, () => `now.json unreadable: ${(err as Error).message}`)
  }
}

export const register: Register = (on, options) => {
  const configured = String(options.mother_bin ?? DEFAULT_BIN)
  const loader = newLoader()

  on('session.start', async ($, e, next) => {
    const bin = await motherBin($, configured)
    const poll = () => load($, bin, loader).catch(() => undefined)
    await poll()
    $.clock.every(POLL_MS, poll)
    return next(e)
  })
}
