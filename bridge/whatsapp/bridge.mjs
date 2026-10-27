#!/usr/bin/env node
// MOTHER's send-only WhatsApp bridge: a linked device (Baileys) that sends one text to one allowlisted chat
// after the manager's proven yes, and nothing else. docs/CONTRACT.md §5d.
//
//   node bridge.mjs            serve on <state>/whatsapp/bridge.sock (0600, no TCP)
//   node bridge.mjs --link     print the QR in this terminal, exit once linked
//   node bridge.mjs --unlink   log the device out and delete <state>/whatsapp/session/
//
// <state> = $MOTHER_STATE_DIR or ~/.mother. The workspace is $MOTHER_WORKSPACE, else the <state>/workspace
// pointer. Incoming messages are never stored, logged or forwarded; logs carry ids and error codes only.
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  checkSend, checkPrompt, parseSent, sentLine, jitteredGap, typingMs, sha256, isoZ, utcDay, minGapSeconds,
} from "./checks.mjs";

export const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const ID_RE = /^w[a-z2-9]{3}$/;
const MAX_REQUEST = 64 * 1024;
const REQUEST_TIMEOUT_MS = 10_000;

const expand = (p) => (p === "~" || p.startsWith("~/") ? path.join(os.homedir(), p.slice(1)) : p);

/** Where the bridge keeps its machine-local state. */
export function locations(env = process.env) {
  const state = expand((env.MOTHER_STATE_DIR || "").trim() || path.join(os.homedir(), ".mother"));
  const dir = path.join(state, "whatsapp");
  return { state, dir, sock: path.join(dir, "bridge.sock"), session: path.join(dir, "session"),
           sent: path.join(dir, "sent.jsonl") };
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

/** `mother whatsapp verify <id>` (no shell): true only on exit 0, i.e. the yes still stands for this text. */
export function verifyWithCli(id, { env = process.env, python = "python3" } = {}) {
  return new Promise((resolve) => {
    execFile(python, [path.join(PLUGIN_ROOT, "bin", "mother"), "whatsapp", "verify", id],
      { env, timeout: 30_000, maxBuffer: 64 * 1024 }, (err) => resolve(!err));
  });
}

function defaultLog(rec) {
  process.stderr.write(JSON.stringify({ at: isoZ(Date.now()), ...rec }) + "\n");
}

const errCode = (e) => String((e && (e.code || e.output?.statusCode || e.name)) || "error");

/**
 * The request handler and socket server, independent of WhatsApp: `client` is anything with
 * linked(), me(), presence(jid, "composing"|"paused"), sendText(jid, text) -> stanza id.
 */
export function createBridge({ env = process.env, client, verify = (id) => verifyWithCli(id, { env }),
                               now = Date.now, rand = Math.random,
                               sleep = (ms) => new Promise((r) => setTimeout(r, ms)), log = defaultLog }) {
  const loc = locations(env);
  let chain = Promise.resolve();
  const serial = (fn) => {  // one message at a time, so the cap and gap checks hold across requests
    const p = chain.then(fn, fn);
    chain = p.catch(() => {});
    return p;
  };
  const fail = (error) => ({ ok: false, error });

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
    if (fs.existsSync(path.join(ws, ".mother", "PAUSED"))) return { error: "paused" };
    return { ws, cfg };
  }

  function settle(approvalPath, suffix, id) {
    try {
      fs.renameSync(approvalPath, approvalPath.replace(/\.json$/, suffix));
    } catch (e) {
      log({ op: "send", draft: id, error: "approval_rename_failed", code: errCode(e) });
    }
  }

  async function send(id) {
    if (typeof id !== "string" || !ID_RE.test(id)) return fail("bad_request");
    const c = context();
    if (c.error) return fail(c.error);
    const base = path.join(c.ws, ".mother", "whatsapp");
    const draft = readJson(path.join(base, "drafts", id + ".json"));
    const approvalPath = path.join(base, "approved", id + ".json");
    const approval = readJson(approvalPath);
    const gapSeconds = jitteredGap(minGapSeconds(c.cfg), rand());
    const r = checkSend({ cfg: c.cfg, draft, approval, sentLines: readSent(), now: now(), gapSeconds });
    if (!r.ok) {
      log({ op: "send", draft: id, error: r.error });
      return r;
    }
    if (!client || !client.linked()) return fail("unlinked");
    if (!(await verify(id))) {  // the second lock re-proves the manager's yes on its own
      log({ op: "send", draft: id, error: "no_approval" });
      return fail("no_approval");
    }
    let stanza;
    try {
      await client.presence(draft.chat, "composing");
      await sleep(typingMs(draft.text, rand()));
      await client.presence(draft.chat, "paused");
      stanza = await client.sendText(draft.chat, draft.text);
    } catch (e) {
      settle(approvalPath, ".failed", id);
      log({ op: "send", draft: id, error: "send_failed", code: errCode(e) });
      return fail("send_failed");
    }
    stanza = typeof stanza === "string" ? stanza : "";
    try {
      appendSent({ at: isoZ(now()), kind: "send", chat: draft.chat, draft: id, sha256: draft.sha256, stanza });
    } catch (e) {
      log({ op: "send", draft: id, stanza, error: "sent_log_failed", code: errCode(e) });
    }
    settle(approvalPath, ".used", id);
    log({ op: "send", draft: id, stanza });
    return { ok: true, stanza };
  }

  async function prompt(text) {
    const c = context();
    if (c.error) return fail(c.error);
    const gapSeconds = jitteredGap(minGapSeconds(c.cfg), rand());
    const r = checkPrompt({ cfg: c.cfg, text, sentLines: readSent(), now: now(), gapSeconds });
    if (!r.ok) {
      log({ op: "prompt", error: r.error });
      return r;
    }
    if (!client || !client.linked()) return fail("unlinked");
    let stanza;
    try {  // the manager's own chat: no "typing…", no cold-contact rule
      stanza = await client.sendText(r.chat, text);
    } catch (e) {
      log({ op: "prompt", error: "send_failed", code: errCode(e) });
      return fail("send_failed");
    }
    stanza = typeof stanza === "string" ? stanza : "";
    try {
      appendSent({ at: isoZ(now()), kind: "prompt", chat: r.chat, sha256: sha256(text), stanza });
    } catch (e) {
      log({ op: "prompt", stanza, error: "sent_log_failed", code: errCode(e) });
    }
    log({ op: "prompt", stanza });
    return { ok: true, stanza };
  }

  function status() {
    const lines = readSent();
    const day = utcDay(now());
    const n = (kind) => (lines ? lines.filter((x) => x.kind === kind && String(x.at || "").slice(0, 10) === day).length
      : null);
    const linked = !!(client && client.linked());
    return { ok: true, linked, me: linked ? client.me() || null : null, sent_today: n("send"),
             prompts_today: n("prompt") };
  }

  async function handle(req) {
    if (!req || typeof req !== "object" || Array.isArray(req)) return fail("bad_request");
    if (req.op === "status") return status();
    if (req.op === "send") return serial(() => send(req.draft));
    if (req.op === "prompt") return serial(() => prompt(req.text));
    return fail("bad_request");
  }

  function onConnection(conn) {
    let buf = "";
    let done = false;
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

  return { handle, listen, send, prompt, status };
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

function tighten(dir) {  // session keys: folder 0700, files 0600
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.chmodSync(dir, 0o700);
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f);
    if (fs.lstatSync(p).isFile()) fs.chmodSync(p, 0o600);
  }
}

/**
 * A linked-device client. `mode` "serve" never shows a QR (an unlinked bridge stays unlinked); "link" passes
 * each QR to onQR. No handler is registered for messages or any other incoming event: the library does what
 * the protocol needs and the bridge keeps nothing.
 */
export async function connectWhatsApp({ sessionDir, mode = "serve", onQR = () => {}, log = defaultLog }) {
  const baileys = await import("baileys");
  const makeWASocket = baileys.makeWASocket || baileys.default;
  const { useMultiFileAuthState, DisconnectReason, jidNormalizedUser, Browsers } = baileys;
  tighten(sessionDir);
  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  let sock = null;
  let linked = false;
  let stopped = false;
  let loggedOut = false;
  let retry = 0;
  const waiters = [];
  const settle = (v) => { while (waiters.length) waiters.shift()(v); };

  const start = () => {
    sock = makeWASocket({
      auth: state, logger: SILENT, browser: Browsers.macOS("Desktop"), markOnlineOnConnect: false,
      syncFullHistory: false, shouldSyncHistoryMessage: () => false, getMessage: async () => undefined,
      emitOwnEvents: false, generateHighQualityLinkPreview: false,
    });
    const s = sock;
    s.ev.on("creds.update", saveCreds);
    s.ev.on("connection.update", (u) => {
      if (s !== sock) return;
      if (u.qr) {
        if (mode === "link") onQR(u.qr);
        else { stopped = true; s.end(undefined); }  // serve mode never pairs
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
        if (code === DisconnectReason.loggedOut) {
          loggedOut = true;
          stopped = true;
          settle(false);
          return;
        }
        if (stopped) { settle(false); return; }
        const wait = code === DisconnectReason.restartRequired ? 0 : Math.min(60_000, 2_000 * 2 ** retry++);
        setTimeout(() => { if (!stopped) start(); }, wait);
      }
    });
  };

  const paired = !!state.creds?.me?.id;
  if (paired || mode === "link") start();
  return {
    paired,
    linked: () => linked,
    loggedOut: () => loggedOut,
    me: () => (linked && sock?.user?.id ? jidNormalizedUser(sock.user.id) : null),
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

async function runLink(loc) {
  if (fs.existsSync(loc.sock) && await alive(loc.sock)) {
    console.error("The bridge is running; stop it before linking again.");
    return 1;
  }
  const { default: qrcode } = await import("qrcode-terminal");
  const client = await connectWhatsApp({
    sessionDir: loc.session, mode: "link",
    onQR: (qr) => {
      console.log("\nWhatsApp > Settings > Linked devices > Link a device, then scan:\n");
      qrcode.generate(qr, { small: true });
    },
  });
  const ok = await client.waitOpen(180_000);
  await new Promise((r) => setTimeout(r, 2_000));  // let the last creds.update land on disk
  client.close();
  tighten(loc.session);
  console.log(ok ? "linked" : "not linked");
  return ok ? 0 : 1;
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

async function runServe(loc) {
  const client = await connectWhatsApp({ sessionDir: loc.session, mode: "serve" });
  if (!client.paired) defaultLog({ op: "connection", state: "unlinked" });
  const server = await createBridge({ client }).listen(loc.sock);
  const stop = () => {
    client.close();
    server.close();
    try { fs.unlinkSync(loc.sock); } catch { /* gone */ }
    process.exit(0);
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  return new Promise(() => {});
}

export async function main(argv = process.argv.slice(2)) {
  process.umask(0o077);  // everything the bridge creates is the manager's alone
  const loc = locations();
  fs.mkdirSync(loc.dir, { recursive: true, mode: 0o700 });
  const arg = argv[0];
  if (arg === "--link") return runLink(loc);
  if (arg === "--unlink") return runUnlink(loc);
  if (arg === undefined) return runServe(loc);
  console.error("usage: bridge.mjs [--link | --unlink]");
  return 2;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().then((code) => { if (typeof code === "number") process.exit(code); },
    (e) => { console.error("bridge: " + errCode(e)); process.exit(1); });
}
