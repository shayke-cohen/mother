// ---- mention search: the leading desktop session finds the manager's mentions and DMs (CONTRACT §5g) ----
// The watcher's chat token may not search (missing scope), so it never sees a mention in a channel it does not poll,
// a group DM, or a DM from someone not in config. A desktop session's own chat connector can search. The leader runs
// two queries on it, on `chief.mention_search_seconds` (and on `/chief refresh`), and hands the raw answers to
// `mother chief mention-search record`, which parses them and writes inbox lines with the watcher's keys and dedup.
// `mother chief mention-search plan` decides when, from what time and whether this session leads; the interval and the
// backoff live there. No model call; the tool only searches; nothing is ever posted. The text is untrusted: the mod
// never reads it, it only passes it through.
import type { Dollar } from './shared'

export const SEARCH_TOOL = 'slack_search_public_and_private'
export const SEARCH_CALLS_MAX = 6
export const SEARCH_TIMEOUT_MS = 20_000
export const SEARCH_LIMIT = 20
const RATE_RE = /\b429\b|rate[ _-]?limit|ratelimited|too many requests/i

type CliRan = { ok: boolean; stdout: string; stderr: string }
export type ChiefCli = (args: string[], stdin?: string) => Promise<CliRan>

// The server serving the search tool in this session, found by the tool's name (`mcp__<server>__<tool>`): the
// connector's server name differs from machine to machine. null when none is connected. Takes the first one found.
export async function searchServer($: Dollar): Promise<string | null> {
  let tools: { name?: unknown; mcp?: unknown }[] = []
  try { tools = await $.tool.list() } catch { return null }
  const tail = '__' + SEARCH_TOOL
  for (const t of Array.isArray(tools) ? tools : []) {
    const name = typeof t?.name === 'string' ? t.name : ''
    if (t?.mcp !== true || !name.startsWith('mcp__') || !name.endsWith(tail)) continue
    const server = name.slice('mcp__'.length, name.length - tail.length)
    if (server) return server
  }
  // The list may leave out tools that load on demand; the context breakdown names every MCP tool with its server.
  try {
    const rows = (await $.session.usage({ breakdown: 'summary' })).context.breakdown?.mcpTools ?? []
    for (const r of rows) if (typeof r?.name === 'string' && r.name.endsWith(tail) && r.serverName) return r.serverName
  } catch { /* no breakdown here: no server */ }
  return null
}

// The next page's cursor from an answer's `pagination_info` ("... use cursor `<c>`"), else ''.
export function nextCursor(info: unknown): string {
  return /cursor\W{0,3}([A-Za-z0-9+/=_-]{4,200})/.exec(String(info ?? ''))?.[1] ?? ''
}

// One answer's text as {results, info}: the tool answers `{"results": "<markdown>", "pagination_info": "..."}`; any
// other text is taken as the markdown itself.
export function pageOf(text: string): { results: string; info: string } {
  try {
    const v = JSON.parse(text)
    if (v && typeof v === 'object' && typeof v.results === 'string') return { results: v.results, info: String(v.pagination_info ?? '') }
  } catch { /* not JSON: the markdown itself */ }
  return { results: text, info: '' }
}

type Called = { ok: true; text: string } | { ok: false; why: string }

// One search call with a timer: every text block of the answer, or why not (a deny, an error result, a timeout).
async function callSearch($: Dollar, server: string, args: Record<string, unknown>): Promise<Called> {
  const stop = new AbortController()
  try {
    const timedOut = $.clock.sleep(SEARCH_TIMEOUT_MS, { signal: stop.signal }).then(() => 'timeout' as const, () => 'cancelled' as const)
    const r: any = await Promise.race([$.mcp.call(server, SEARCH_TOOL, args), timedOut])
    if (r === 'timeout' || r === 'cancelled') return { ok: false, why: 'the search gave no answer' }
    const text = (Array.isArray(r?.content) ? r.content : []).filter((b: any) => b?.type === 'text').map((b: any) => String(b.text ?? '')).join('\n')
    if (r?.isError) return { ok: false, why: text.split('\n').find((l: string) => l.trim())?.trim().slice(0, 160) || 'the search failed' }
    return { ok: true, text }
  } catch (err) {
    return { ok: false, why: String((err as Error)?.message ?? err).split('\n')[0]?.slice(0, 160) || 'the search is not reachable' }
  } finally {
    stop.abort()
  }
}

let running = false
// When to ask `plan` again (ms on $.clock): what it or the last record said, so a pass in between spawns nothing.
// `force` skips it. A plan that could not be read waits PLAN_RETRY_MS.
let askAt = 0
const PLAN_RETRY_MS = 180_000
export function resetMentionSearch(): void { running = false; askAt = 0 }

function untilMs(iso: unknown, nowMs: number, fallback: number): number {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN
  return Number.isFinite(t) && t > nowMs ? Math.min(t, nowMs + 1_800_000) : nowMs + fallback
}

// One run when `plan` says this session is due (`force`: `/chief refresh`, which skips the interval, never the leader
// rule). Returns what happened in a few words, for the refresh command's answer.
export async function mentionPass($: Dollar, cli: ChiefCli, force = false): Promise<string> {
  if (running) return 'already running'
  const nowMs = await $.clock.now()
  if (!force && nowMs < askAt) return 'not due'
  running = true
  try {
    let sid = ''
    try { sid = String((await $.session.id()) ?? '').trim() } catch { sid = '' }
    if (!sid) return 'no session id'
    const planned = await cli(['mention-search', 'plan', '--as', sid, ...(force ? ['--force'] : [])])
    let plan: any = null
    try { plan = planned.ok ? JSON.parse(planned.stdout) : null } catch { plan = null }
    if (!plan || plan.due !== true) {
      askAt = untilMs(plan?.next_at, nowMs, plan?.why === 'another session leads' ? 60_000 : PLAN_RETRY_MS)
      return typeof plan?.why === 'string' ? plan.why : 'not run'
    }
    const queries: { label: string; query: string }[] = (Array.isArray(plan.queries) ? plan.queries : [])
      .filter((q: any) => q && typeof q.label === 'string' && typeof q.query === 'string').slice(0, 2)
    const maxPages = Number.isInteger(plan.max_pages) && plan.max_pages >= 1 && plan.max_pages <= 3 ? plan.max_pages : 3
    const run: Record<string, unknown> = { started_at: plan.started_at, status: 'ok', pages: [] as unknown[], calls: 0 }
    const pages = run.pages as { label: string; results: string; truncated: boolean }[]
    const server = await searchServer($)
    if (!server) {
      run.status = 'unavailable'
    } else {
      let calls = 0
      outer: for (const q of queries) {
        let cursor = ''
        for (let page = 0; page < maxPages; page++) {
          if (calls >= SEARCH_CALLS_MAX) break outer
          calls++
          const args: Record<string, unknown> = { query: q.query, after: String(plan.after ?? ''), sort: 'timestamp', sort_dir: 'asc',
            limit: SEARCH_LIMIT, include_context: false, include_bots: false, response_format: 'detailed' }
          if (cursor) args.cursor = cursor
          const got = await callSearch($, server, args)
          if (!got.ok) {
            run.status = RATE_RE.test(got.why) ? 'rate_limited' : 'error'
            run.reason = got.why
            break outer
          }
          const pg = pageOf(got.text)
          cursor = nextCursor(pg.info)
          pages.push({ label: q.label, results: pg.results, truncated: !!cursor && page === maxPages - 1 })
          if (!cursor) break
        }
      }
      run.calls = calls
    }
    const recorded = await cli(['mention-search', 'record', '--stdin'], JSON.stringify(run))
    let r: any = null
    try { r = recorded.ok ? JSON.parse(recorded.stdout) : null } catch { r = null }
    askAt = untilMs(r?.next_at, await $.clock.now(), PLAN_RETRY_MS)
    if (!r) return 'the run could not be recorded'
    return run.status === 'ok' ? `${Number(r.added) || 0} found` : String(run.status)
  } finally {
    running = false
  }
}

// The Health tab's chat row for the search, from ChiefStatus.health.mention_search.
export function mentionSearchText(m: any, nowMs: number): string {
  const head = 'mentions via desktop search'
  if (!m || typeof m !== 'object' || m.status === 'never' || !m.status) return `${head}: not run yet (it needs a desktop session with the Slack connector)`
  const at = typeof m.last_run_at === 'string' ? Date.parse(m.last_run_at) : NaN
  const mins = Number.isFinite(at) ? Math.max(0, Math.round((nowMs - at) / 60_000)) : null
  const last = mins === null ? '' : mins < 1 ? 'last run just now' : mins < 120 ? `last run ${mins}m ago` : `last run ${Math.round(mins / 60)}h ago`
  const reason = typeof m.reason === 'string' ? m.reason : ''
  if (m.status === 'ok') return `${head}: ok, ${last ? last + ', ' : ''}${Number(m.found) || 0} found`
  if (m.status === 'unavailable') return `${head}: ${reason || 'not available (no Slack search connector in this session)'}`
  const nx = typeof m.next_at === 'string' ? Date.parse(m.next_at) : NaN
  const wait = Number.isFinite(nx) && nx > nowMs ? `, next try in ${Math.max(1, Math.round((nx - nowMs) / 60_000))}m` : ''
  return `${head}: ${m.status === 'rate_limited' ? 'rate limited' : 'failed'}${reason ? ` (${reason})` : ''}, backing off${wait}${last ? `; ${last}` : ''}`
}
