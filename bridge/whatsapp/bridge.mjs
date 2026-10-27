#!/usr/bin/env node
// MOTHER's send-only WhatsApp bridge: a linked device (Baileys) that sends one text to one chat when
// `mother whatsapp send` asks with a fresh ticket from the guard, and nothing else. docs/CONTRACT.md §5d.
//
//   node bridge.mjs            serve on <state>/whatsapp/bridge.sock (0600, no TCP)
//   node bridge.mjs --link     print the QR in this terminal, exit once linked; the last stdout line is
//                              {"linked": true, "me": "<jid>"} (device suffix stripped) or {"linked": false}
//   node bridge.mjs --link --phone <digits>   the same, with a pairing code to type on the phone instead of a QR
//   node bridge.mjs --unlink   log the device out and delete <state>/whatsapp/session/
//
// <state> = $MOTHER_STATE_DIR or ~/.mother. The workspace is $MOTHER_WORKSPACE, else the <state>/workspace
// pointer. Incoming messages are never stored, logged or forwarded; logs carry ids and error codes only.
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  checkSend, parseSent, sentLine, jitteredGap, typingMs, sha256, isoZ, utcDay, minGapSeconds, sendableJid, MAX_TEXT,
} from "./checks.mjs";

const MAX_REQUEST = 64 * 1024;
const REQUEST_TIMEOUT_MS = 10_000;
const MAX_GAP_WAIT_MS = 30_000;  // a request waits out at most this much of min_gap_seconds, then refuses `gap`
export const TICKET_MAX_AGE_MS = 600_000;  // a guard ticket is good for one send within 10 minutes of the prompt

/** The guard's ticket name for a send: sha256 of the recipient as typed, a NUL, and the text (CONTRACT §5d). */
export const ticketName = (to, text) => sha256(to + "\0" + text);

/** Does `to` (as typed) name `jid`? `me` is self_jid; a JID or number must be that JID; a name can't be checked. */
export function recipientMatches(to, jid, selfJid) {
  const t = to.trim();
  if (t.toLowerCase() === "me") return !!selfJid && jid === selfJid;
  if (t.includes("@")) return jid === t;
  const digits = t.replace(/[\s+()\-]/g, "");
  if (/^\d{6,15}$/.test(digits)) return jid === digits + "@s.whatsapp.net";
  return true;
}

const expand = (p) => (p === "~" || p.startsWith("~/") ? path.join(os.homedir(), p.slice(1)) : p);

/** Where the bridge keeps its machine-local state. */
export function locations(env = process.env) {
  const state = expand((env.MOTHER_STATE_DIR || "").trim() || path.join(os.homedir(), ".mother"));
  const dir = path.join(state, "whatsapp");
  return { state, dir, sock: path.join(dir, "bridge.sock"), session: path.join(dir, "session"),
           sent: path.join(dir, "sent.jsonl"), tickets: path.join(dir, "tickets") };
}

/** The workspace, resolved like bin/mother: $MOTHER_WORKSPACE, else the <state>/workspace pointer. */
export function workspace(env = process.env) {
  const w = (env.MOTHER_WORKSPACE || "").trim();
  if (w) return expand(w);
  try {
    const p = fs.readFileSync(path.join(locations(env).state, "workspace"), "utf8").trim();
    return p ? expand(p) : null;
  } catch {
    return null;
  }
}

function defaultLog(rec) {
  process.stderr.write(JSON.stringify({ at: isoZ(Date.now()), ...rec }) + "\n");
}

const errCode = (e) => String((e && (e.code || e.output?.statusCode || e.name)) || "error");

/**
 * The request handler and socket server, independent of WhatsApp: `client` is anything with
 * linked(), me(), presence(jid, "composing"|"paused"), sendText(jid, text) -> message id.
 */
export const CONNECT_WAIT_MS = 20_000;  // a send waits this long for a paired client's connection to open

export function createBridge({ env = process.env, client, onActivity = () => {}, onStop = () => {},
                               connectWaitMs = CONNECT_WAIT_MS, now = Date.now, rand = Math.random,
                               sleep = (ms) => new Promise((r) => setTimeout(r, ms)), log = defaultLog }) {
  const loc = locations(env);
  let chain = Promise.resolve();
  const serial = (fn) => {  // one message at a time, so the cap and gap checks hold across requests
    const p = chain.then(fn, fn);
    chain = p.catch(() => {});
    return p;
  };
  const fail = (error) => ({ ok: false, error });

  /** Linked now, or paired and connecting: wait for the connection to open. Unpaired or closed: not linked. */
  async function ensureLinked() {
    if (!client) return false;
    if (client.linked()) return true;
    if (client.paired && typeof client.waitOpen === "function") return !!(await client.waitOpen(connectWaitMs));
    return false;
  }

  const readJson = (p) => {
    try {
      const v = JSON.parse(fs.readFileSync(p, "utf8"));
      return v && typeof v === "object" && !Array.isArray(v) ? v : null;
    } catch {
      return null;
    }
  };

  function readSent() {
    try {
      return parseSent(fs.readFileSync(loc.sent, "utf8"));
    } catch (e) {
      return e.code === "ENOENT" ? [] : null;
    }
  }

  function appendSent(rec) {  // one append of one whole line, O_APPEND: a reader never sees a torn line
    fs.mkdirSync(loc.dir, { recursive: true, mode: 0o700 });
    fs.appendFileSync(loc.sent, sentLine(rec), { mode: 0o600, flag: "a" });
  }

  function context() {
    const ws = workspace(env);
    const cfg = ws ? readJson(path.join(ws, "config.json")) : null;
    if (!ws || !cfg) return { error: "no_workspace" };
    return { ws, cfg, paused: fs.existsSync(path.join(ws, ".mother", "PAUSED")) };
  }

  const bare = (jid) => String(jid || "").replace(/:\d+(?=@)/, "");

  /** Delete tickets older than TICKET_MAX_AGE_MS (or unreadable). */
  function sweepTickets() {
    let names = [];
    try { names = fs.readdirSync(loc.tickets); } catch { return; }
    for (const n of names) {
      const p = path.join(loc.tickets, n);
      const at = ticketAt(p);
      if (at === null || now() - at >= TICKET_MAX_AGE_MS) {
        try { fs.unlinkSync(p); } catch { /* gone already */ }
      }
    }
  }

  function ticketAt(p) {  // ms, or null when it isn't a regular file holding {"at": <epoch seconds>}
    try {
      if (!fs.lstatSync(p).isFile()) return null;
      const at = JSON.parse(fs.readFileSync(p, "utf8")).at;
      return Number.isFinite(at) ? at * 1000 : null;
    } catch {
      return null;
    }
  }

  /** Take (delete) the guard's ticket for this send: null when good, else the refusal code. */
  function takeTicket(to, text) {
    const p = path.join(loc.tickets, ticketName(to, text));
    let st;
    try { st = fs.lstatSync(p); } catch { return "no_ticket"; }
    const at = st.isFile() ? ticketAt(p) : null;
    try { fs.unlinkSync(p); } catch { return "no_ticket"; }  // one use: gone before anything is sent
    if (!st.isFile()) return "no_ticket";
    if (at === null || now() - at >= TICKET_MAX_AGE_MS || at - now() > 60_000) return "ticket_expired";
    return null;
  }

  async function send(req) {
    const { to, jid, text, signature } = req;
    const refuse = (error, extra = {}) => {
      log({ op: "send", error, ...extra });
      return fail(error);
    };
    if (!sendableJid(jid) || typeof text !== "string" || typeof to !== "string"
        || (signature !== undefined && typeof signature !== "string")) return refuse("bad_request");
    const full = signature ? text + "\n\n" + signature : text;
    if (!text.trim() || full.length > MAX_TEXT) return refuse("bad_request");
    const c = context();
    if (c.error) return refuse(c.error);
    const bad = takeTicket(to, text);
    sweepTickets();
    if (bad) return refuse(bad);
    if (!recipientMatches(to, jid, c.cfg?.whatsapp?.self_jid)) return refuse("wrong_recipient");
    if (c.paused) return refuse("paused");
    const check = () => checkSend({ cfg: c.cfg, jid, text: full, sentLines: readSent(), now: now(),
                                    gapSeconds: jitteredGap(minGapSeconds(c.cfg), rand()) });
    let r = check();
    if (!r.ok && r.error === "gap" && r.waitMs <= MAX_GAP_WAIT_MS) {  // wait out a short gap inside the request
      await sleep(r.waitMs);
      r = check();
    }
    if (!r.ok) return refuse(r.error);
    if (!(await ensureLinked())) return refuse("unlinked");
    const self = c.cfg?.whatsapp?.self_jid;
    const me = bare(client.me());
    if (!me || me !== self) return refuse("wrong_account");
    if (jid !== self && jid.split("@")[0] === me.split("@")[0]) return refuse("self_mismatch");  // own number, odd form
    let stanza;
    try {
      if (jid !== self) {  // "typing…" for someone else; never in the manager's own chat
        await client.presence(jid, "composing");
        await sleep(typingMs(full, rand()));
        await client.presence(jid, "paused");
      }
      stanza = await client.sendText(jid, full);
    } catch (e) {
      return refuse("send_failed", { code: errCode(e) });
    }
    stanza = typeof stanza === "string" && stanza ? stanza : null;
    try {
      appendSent({ at: isoZ(now()), chat: jid, sha256: sha256(full), stanza });
    } catch (e) {  // sent all the same; the cap undercounts this one
      log({ op: "send", stanza, error: "sent_log_failed", code: errCode(e) });
    }
    log({ op: "send", stanza });
    return { ok: true, stanza };
  }

  function status() {
    const lines = readSent();
    const day = utcDay(now());
    const linked = !!(client && client.linked());
    const out = { ok: true, linked, me: linked ? client.me() || null : null,
                  sent_today: lines ? lines.filter((x) => x.kind === "send"
                    && String(x.at || "").slice(0, 10) === day).length : null };
    if (!linked && client && client.paired && !(client.loggedOut && client.loggedOut())) out.connecting = true;
    return out;
  }

  async function handle(req) {
    if (!req || typeof req !== "object" || Array.isArray(req)) return fail("bad_request");
    if (req.op === "status") return status();
    if (req.op === "stop") {  // only makes the bridge exit; the socket is 0600
      setTimeout(onStop, 50);
      return { ok: true };
    }
    if (req.op === "send") return serial(() => send(req));
    return fail("bad_request");
  }

  function onConnection(conn) {
    let buf = "";
    let done = false;
    onActivity();
    conn.setEncoding("utf8");
    conn.setTimeout(REQUEST_TIMEOUT_MS, () => { if (!done) conn.destroy(); });
    const answer = async () => {
      if (done) return;
      done = true;
      conn.setTimeout(0);
      let req = null;
      try {
        req = JSON.parse(buf.split("\n")[0]);
      } catch {
        req = null;
      }
      let reply;
      try {
        reply = await handle(req);
      } catch (e) {
        log({ op: "request", error: "internal", code: errCode(e) });
        reply = fail("internal");
      }
      if (!conn.destroyed) conn.end(JSON.stringify(reply) + "\n");
      onActivity();
    };
    conn.on("data", (chunk) => {
      buf += chunk;
      if (buf.length > MAX_REQUEST) {
        done = true;
        conn.end(JSON.stringify(fail("bad_request")) + "\n");
      } else if (buf.includes("\n")) {
        answer();
      }
    });
    conn.on("end", answer);
    conn.on("error", () => {});
  }

  /** Listen on <state>/whatsapp/bridge.sock, mode 0600 in a 0700 folder. Refuses if a bridge already answers. */
  async function listen(sockPath = loc.sock) {
    fs.mkdirSync(path.dirname(sockPath), { recursive: true, mode: 0o700 });
    fs.chmodSync(path.dirname(sockPath), 0o700);
    if (fs.existsSync(sockPath)) {
      if (await alive(sockPath)) throw new Error("a bridge is already running on " + sockPath);
      fs.unlinkSync(sockPath);  // stale socket from a crashed run
    }
    // allowHalfOpen: a client that ends its side after the request still gets the reply of a slow send
    const server = net.createServer({ allowHalfOpen: true }, onConnection);
    const old = process.umask(0o177);  // the socket is born 0600: no window where another user can connect
    try {
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(sockPath, resolve);
      });
    } finally {
      process.umask(old);
    }
    fs.chmodSync(sockPath, 0o600);
    return server;
  }

  return { handle, listen, send, status };
}

/** True when something answers on the socket. */
export function alive(sockPath) {
  return new Promise((resolve) => {
    const s = net.connect(sockPath);
    s.once("connect", () => { s.destroy(); resolve(true); });
    s.once("error", () => resolve(false));
  });
}

// ------------------------------------------------------------------ WhatsApp (Baileys), loaded only when run

const SILENT = {
  level: "silent",
  child() { return SILENT; },
  trace() {}, debug() {}, info() {}, warn() {}, error() {}, fatal() {},
};

export function tighten(dir) {  // session keys: folder 0700, files 0600
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.chmodSync(dir, 0o700);
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f);
    if (fs.lstatSync(p).isFile()) fs.chmodSync(p, 0o600);
  }
}

const LINK_REASONS = { 408: "qr_expired", 401: "logged_out", 440: "replaced" };

/**
 * What a closed connection does next. Link mode ends the attempt on any close but 515 (restart after a scan):
 * a 408 there is the QR timing out, and reconnecting would show a code the window then kills. Serve mode
 * reconnects with backoff. 401 (logged out) ends both. Returns {end, reason, loggedOut?} or {retryMs}.
 */
export function closeAction(mode, code, retry) {
  if (code === 401) return { end: true, reason: "logged_out", loggedOut: true };
  if (code === 515) return { retryMs: 0 };
  if (mode === "link") return { end: true, reason: LINK_REASONS[code] || "network" };
  return { retryMs: Math.min(60_000, 2_000 * 2 ** retry) };
}

/** True once a pairing completed: a pairing code sets creds.me before the phone confirms, so me alone isn't it. */
export function isPaired(creds) {
  return !!(creds && creds.me && creds.me.id && creds.account);
}

/** Drop a half-done pairing (keys without a confirmed account) so a new link starts clean. */
export function clearUnpaired(sessionDir) {
  const f = path.join(sessionDir, "creds.json");
  let creds = null;
  try { creds = JSON.parse(fs.readFileSync(f, "utf8")); } catch { return false; }
  if (isPaired(creds)) return false;
  fs.rmSync(sessionDir, { recursive: true, force: true });
  return true;
}

/**
 * The WhatsApp Web version to announce: the live one from web.whatsapp.com (WhatsApp refuses to pair a client
 * that is too old), else the library's built-in one. `fetchLatest` is baileys' fetchLatestWaWebVersion.
 */
export async function waVersion(fetchLatest, log = () => {}) {
  try {
    const r = await fetchLatest({ signal: AbortSignal.timeout(10_000) });
    if (r && r.isLatest && Array.isArray(r.version)) return r.version;
    log({ op: "wa_version", error: "not_found" });
  } catch (e) {
    log({ op: "wa_version", error: errCode(e) });
  }
  return undefined;  // the library's default
}

/**
 * A linked-device client. `mode` "serve" never shows a QR (an unlinked bridge stays unlinked); "link" passes
 * each QR to onQR. No handler is registered for messages or any other incoming event: the library does what
 * the protocol needs and the bridge keeps nothing.
 */
export async function connectWhatsApp({ sessionDir, mode = "serve", onQR = () => {}, log = defaultLog,
                                        pairPhone = null, onPairCode = () => {} }) {
  const baileys = await import("baileys");
  const makeWASocket = baileys.makeWASocket || baileys.default;
  const { useMultiFileAuthState, DisconnectReason, jidNormalizedUser, Browsers, fetchLatestWaWebVersion } = baileys;
  tighten(sessionDir);
  const version = fetchLatestWaWebVersion ? await waVersion(fetchLatestWaWebVersion, log) : undefined;
  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  let sock = null;
  let linked = false;
  let stopped = false;
  let loggedOut = false;
  let reason = null;
  let retry = 0;
  let pairAsked = false;
  const waiters = [];
  const settle = (v) => { while (waiters.length) waiters.shift()(v); };

  const start = () => {
    sock = makeWASocket({
      auth: state, logger: SILENT, browser: Browsers.macOS("Desktop"), markOnlineOnConnect: false,
      ...(version ? { version } : {}),
      syncFullHistory: false, shouldSyncHistoryMessage: () => false, getMessage: async () => undefined,
      emitOwnEvents: false, generateHighQualityLinkPreview: false,
    });
    const s = sock;
    s.ev.on("creds.update", saveCreds);
    s.ev.on("connection.update", (u) => {
      if (s !== sock) return;
      if (u.qr) {
        if (mode !== "link") { stopped = true; s.end(undefined); }  // serve mode never pairs
        else if (!pairPhone) onQR(u.qr);
        else if (!pairAsked) {  // the first QR means the socket is ready to ask for a pairing code instead
          pairAsked = true;
          s.requestPairingCode(pairPhone).then(onPairCode, (e) => {
            log({ op: "pair_code", error: errCode(e) });
            reason = "network";
            stopped = true;
            settle(false);
          });
        }
      }
      if (u.connection === "open") {
        linked = true;
        retry = 0;
        log({ op: "connection", state: "open" });
        settle(true);
      } else if (u.connection === "close") {
        linked = false;
        const code = u.lastDisconnect?.error?.output?.statusCode;
        log({ op: "connection", state: "close", code: String(code ?? "none") });
        const next = closeAction(mode, code, retry);
        if (next.end) {
          reason = next.reason;
          if (next.loggedOut) loggedOut = true;
          stopped = true;
          settle(false);
          return;
        }
        if (stopped) { settle(false); return; }
        if (next.retryMs) retry++;
        setTimeout(() => { if (!stopped) start(); }, next.retryMs);
      }
    });
  };

  const paired = isPaired(state.creds);
  if (paired || mode === "link") start();
  return {
    paired,
    linked: () => linked,
    loggedOut: () => loggedOut,
    me: () => {  // the phone-number JID when WhatsApp gives one (user.id, else creds.me.id), else what there is
      if (!linked) return null;
      const ids = [sock?.user?.id, state.creds?.me?.id].filter(Boolean).map((j) => jidNormalizedUser(j));
      return ids.find((j) => j.endsWith("@s.whatsapp.net")) || ids[0] || null;
    },
    closeReason: () => reason,
    presence: (jid, s) => sock.sendPresenceUpdate(s, jid),
    sendText: async (jid, text) => (await sock.sendMessage(jid, { text }))?.key?.id,
    waitOpen: (ms) => (linked ? Promise.resolve(true) : new Promise((resolve) => {
      waiters.push(resolve);
      setTimeout(() => resolve(linked), ms);
    })),
    logout: () => sock.logout(),
    close: () => { stopped = true; try { sock?.end(undefined); } catch { /* already closed */ } },
  };
}

/** A user JID without its device suffix ("<digits>:<device>@s.whatsapp.net" -> "<digits>@s.whatsapp.net"). */
export function bareJid(jid) {
  return typeof jid === "string" && jid ? jid.replace(/:\d+(?=@)/, "") : null;
}

/**
 * Pair a linked device: the QR goes to this terminal, then one JSON line on stdout says how it went, for
 * `mother whatsapp setup` to learn the manager's own JID: {"linked": true, "me": "<jid>"} or {"linked": false}.
 * `connect`, `qr`, `out`, `err` and `settleMs` are for tests.
 */
export const LINK_WORDS = {
  qr_expired: "The QR code expired before it was scanned.",
  logged_out: "WhatsApp logged this device out.",
  replaced: "Another device replaced this link.",
  network: "The connection to WhatsApp dropped.",
  timeout: "Nothing was scanned within 3 minutes.",
};

export async function runLink(loc, { connect = connectWhatsApp, qr = null, out = (l) => console.log(l),
                                     err = (l) => console.error(l), settleMs = 2_000, phone = null } = {}) {
  if (fs.existsSync(loc.sock) && await alive(loc.sock)) {
    err("The bridge is running; stop it before linking again.");
    return 1;
  }
  const qrcode = phone ? null : qr || (await import("qrcode-terminal")).default;
  clearUnpaired(loc.session);
  const fileLog = (rec) => {  // structured lines go to bridge.log only; the terminal gets the QR and sentences
    try {
      fs.mkdirSync(loc.dir || path.dirname(loc.sock), { recursive: true, mode: 0o700 });
      fs.appendFileSync(path.join(loc.dir || path.dirname(loc.sock), "bridge.log"),
        JSON.stringify({ at: isoZ(Date.now()), ...rec }) + "\n", { mode: 0o600 });
    } catch { /* a log is never worth failing a link */ }
  };
  let shown = 0;
  const client = await connect({
    sessionDir: loc.session, mode: "link", log: fileLog, pairPhone: phone,
    onQR: (code) => {
      out(shown++ === 0
        ? "\nOpen WhatsApp → Settings → Linked devices → Link a device first; each code lasts about 20 seconds.\n"
        : "\nNew code (WhatsApp replaces it every 20 seconds or so):\n");
      qrcode.generate(code, { small: true });
    },
    onPairCode: (code) => {
      const c = String(code).replace(/-/g, "");
      out("\nOn your phone: WhatsApp → Settings → Linked devices → Link a device → Link with phone number "
        + "instead, then type this code: " + (c.length === 8 ? c.slice(0, 4) + "-" + c.slice(4) : c) + "\n");
    },
  });
  const ok = await client.waitOpen(180_000);
  const me = ok ? bareJid(client.me()) : null;  // read while the connection is open
  const reason = ok ? null : (client.closeReason && client.closeReason()) || "timeout";
  await new Promise((r) => setTimeout(r, settleMs));  // let the last creds.update land on disk
  client.close();
  if (fs.existsSync(loc.session)) tighten(loc.session);
  if (!ok) out(LINK_WORDS[reason] || LINK_WORDS.network);
  out(JSON.stringify(ok && me ? { linked: true, me } : { linked: false, reason: ok ? "no_account" : reason }));
  return ok && me ? 0 : 1;
}

async function runUnlink(loc) {
  if (fs.existsSync(path.join(loc.session, "creds.json"))) {
    try {
      const client = await connectWhatsApp({ sessionDir: loc.session, mode: "serve" });
      if (client.paired && await client.waitOpen(20_000)) await client.logout();
      client.close();
    } catch (e) {
      console.error("logout failed (" + errCode(e) + "); deleting the keys anyway");
    }
  }
  fs.rmSync(loc.session, { recursive: true, force: true });
  console.log("unlinked");
  return 0;
}

export const IDLE_MS = 30 * 60_000;  // exit after this long with no request; `mother whatsapp` restarts it on demand

/** Idle timeout in ms: $MOTHER_BRIDGE_IDLE_MS when it is a positive number (tests), else 30 minutes. */
export function idleMs(env = process.env) {
  const n = Number(env.MOTHER_BRIDGE_IDLE_MS);
  return Number.isFinite(n) && n > 0 ? n : IDLE_MS;
}

/**
 * Serve on loc.sock until SIGINT/SIGTERM, a `stop` request, or `ms` without a request, then close the client
 * and the socket and call `exit(0)`. `client` is the WhatsApp client (anything with close()).
 */
export async function serve({ client, loc, ms = idleMs(), exit = (c) => process.exit(c), signals = true,
                              env = process.env, connectWaitMs }) {
  let last = Date.now();
  let timer = null;
  let stopping = false;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    if (timer) clearInterval(timer);
    try { client.close(); } catch { /* already closed */ }
    server.close();
    try { fs.unlinkSync(loc.sock); } catch { /* gone: closing the server removes it too */ }
    exit(0);
  };
  const bridge = createBridge({ env, client, onActivity: () => { last = Date.now(); }, onStop: stop,
                                ...(connectWaitMs ? { connectWaitMs } : {}) });
  const server = await bridge.listen(loc.sock);
  timer = setInterval(() => {
    if (Date.now() - last >= ms) {
      defaultLog({ op: "idle_exit" });
      stop();
    }
  }, Math.max(20, Math.min(30_000, Math.floor(ms / 4))));
  if (signals) {
    process.on("SIGINT", stop);
    process.on("SIGTERM", stop);
  }
  return { server, stop };
}

/** Drop every console.* call: libraries (libsignal) print session objects there, key material included.
 * The bridge's own lines go through defaultLog (stderr, ids and codes only). Returns a restore function. */
export function silenceConsole() {
  const keep = {};
  for (const m of ["log", "info", "warn", "debug", "error", "trace"]) {
    keep[m] = console[m];
    console[m] = () => {};
  }
  return () => { for (const m of Object.keys(keep)) console[m] = keep[m]; };
}

async function runServe(loc) {
  silenceConsole();
  if (fs.existsSync(loc.sock) && await alive(loc.sock)) return 0;  // another bridge answers: don't connect twice
  const client = await connectWhatsApp({ sessionDir: loc.session, mode: "serve" });
  if (!client.paired) defaultLog({ op: "connection", state: "unlinked" });
  await serve({ client, loc });
  return new Promise(() => {});
}

export async function main(argv = process.argv.slice(2)) {
  process.umask(0o077);  // everything the bridge creates is the manager's alone
  const loc = locations();
  fs.mkdirSync(loc.dir, { recursive: true, mode: 0o700 });
  const arg = argv[0];
  if (arg === "--link" && argv.length === 1) return runLink(loc);
  if (arg === "--link" && argv[1] === "--phone" && /^\d{6,15}$/.test(argv[2] || "") && argv.length === 3) {
    return runLink(loc, { phone: argv[2] });
  }
  if (arg === "--unlink") return runUnlink(loc);
  if (arg === undefined) return runServe(loc);
  console.error("usage: bridge.mjs [--link [--phone <digits>] | --unlink]");
  return 2;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  // exit only once stdout is flushed: on macOS a pipe is written asynchronously, and setup reads the last line
  const exit = (code) => process.stdout.write("", () => process.exit(code));
  main().then((code) => { if (typeof code === "number") exit(code); },
    (e) => { process.stderr.write("bridge: " + errCode(e) + "\n"); process.exit(1); });
}
