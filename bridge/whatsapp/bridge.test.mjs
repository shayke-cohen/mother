// The bridge over a real Unix socket with a stub WhatsApp client: nothing here connects to WhatsApp.
// Run: node --test bridge/whatsapp/*.test.mjs
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import {
  createBridge, locations, workspace, tighten, ticketName, recipientMatches, TICKET_MAX_AGE_MS, serve, idleMs, silenceConsole, runLink, bareJid, closeAction, waVersion, isPaired, clearUnpaired,
} from "./bridge.mjs";

const SELF = "972500000099@s.whatsapp.net";
const DM = "972500000001@s.whatsapp.net";
const GROUP = "120363000000000001@g.us";
const TEXT = "see you at 3";
const sha = (t) => createHash("sha256").update(t, "utf8").digest("hex");
const isoZ = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

let tmp, env, ws, client, logs, server, sock, clock;

function stubClient() {
  const c = { calls: [], isLinked: true, failSend: false, n: 0 };
  c.linked = () => c.isLinked;
  c.meJid = SELF + "";
  c.me = () => c.meJid;
  c.presence = async (jid, s) => { c.calls.push(["presence", jid, s]); };
  c.sendText = async (jid, text) => {
    c.calls.push(["send", jid, text]);
    if (c.failSend) throw Object.assign(new Error("boom"), { code: "ECONNRESET" });
    return "STANZA" + ++c.n;
  };
  return c;
}

function writeJson(p, v) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(v));
}

function request(obj, raw) {
  return new Promise((resolve, reject) => {
    const c = net.connect(sock);
    let buf = "";
    c.setEncoding("utf8");
    c.on("data", (d) => { buf += d; });
    c.on("end", () => { try { resolve(JSON.parse(buf)); } catch (e) { reject(e); } });
    c.on("error", reject);
    c.end(raw !== undefined ? raw : JSON.stringify(obj) + "\n");
  });
}

/** The ticket the guard writes when it decides a send (age in ms; null = none). */
function ticket(to, text, ageMs = 0) {
  const dir = locations(env).tickets;
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const p = path.join(dir, ticketName(to, text));
  fs.writeFileSync(p, JSON.stringify({ at: Math.floor((clock - ageMs) / 1000) }), { mode: 0o600 });
  return p;
}

/** A send the guard decided: its ticket first, then the request. */
function send(jid = DM, text = TEXT, to = jid, extra = {}) {
  ticket(to, text);
  return request({ op: "send", to, jid, text, ...extra });
}
const readCfg = () => JSON.parse(fs.readFileSync(path.join(ws, "config.json"), "utf8"));
const writeCfg = (c) => writeJson(path.join(ws, "config.json"), c);
const sentPath = () => path.join(tmp, "state", "whatsapp", "sent.jsonl");
function writeSent(lines) {
  fs.mkdirSync(path.dirname(sentPath()), { recursive: true });
  fs.writeFileSync(sentPath(), lines.map((l) => JSON.stringify(l) + "\n").join(""));
}

const sentLines = () => {
  const p = sentPath();
  return fs.existsSync(p) ? fs.readFileSync(p, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};

async function start(opts = {}) {
  // a fake clock: sleeping advances it, so waiting out a gap costs no real time
  const b = createBridge({ env, client, now: () => clock, sleep: async (ms) => { clock += ms; }, rand: () => 0,
                           log: (r) => logs.push(r), ...opts.bridge });
  server = await b.listen(sock);
  return b;
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mwa-"));
  ws = path.join(tmp, "ws");
  env = { MOTHER_STATE_DIR: path.join(tmp, "state"), MOTHER_WORKSPACE: ws, PATH: process.env.PATH,
          HOME: process.env.HOME };
  writeJson(path.join(ws, "config.json"), {
    workspace: { home_machine: "laptop-a" },
    whatsapp: { enabled: true, self_jid: SELF, min_gap_seconds: 5 },
  });
  client = stubClient();
  logs = [];
  clock = Date.now();
  sock = locations(env).sock;
});

afterEach(async () => {
  if (server) await new Promise((r) => server.close(r));
  server = null;
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("status", async () => {
  await start();
  assert.deepEqual(await request({ op: "status" }), { ok: true, linked: true, me: SELF, sent_today: 0 });
  client.isLinked = false;
  assert.deepEqual(await request({ op: "status" }), { ok: true, linked: false, me: null, sent_today: 0 });
});

test("a send to someone else: typing, the text, one sent.jsonl line", async () => {
  await start();
  assert.deepEqual(await send(), { ok: true, stanza: "STANZA1" });
  assert.deepEqual(client.calls, [["presence", DM, "composing"], ["presence", DM, "paused"], ["send", DM, TEXT]]);
  const lines = sentLines();
  assert.equal(lines.length, 1);
  assert.deepEqual(Object.keys(lines[0]), ["at", "kind", "chat", "sha256", "stanza"]);
  assert.deepEqual({ ...lines[0], at: "x" }, { at: "x", kind: "send", chat: DM, sha256: sha(TEXT), stanza: "STANZA1" });
  assert.match(lines[0].at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.equal(fs.statSync(sentPath()).mode & 0o777, 0o600);
  assert.equal((await request({ op: "status" })).sent_today, 1);
});

test("a send to a group, and to the manager's own chat without typing", async () => {
  await start();
  assert.equal((await send(GROUP, "ship it")).ok, true);
  clock += 60_000;
  client.calls.length = 0;
  assert.deepEqual(await send(SELF, "note to self", "me"), { ok: true, stanza: "STANZA2" });
  assert.deepEqual(client.calls, [["send", SELF, "note to self"]]);
});

test("the manager's own number in another form: self_mismatch; the wrong linked account: wrong_account", async () => {
  await start();
  assert.deepEqual(await send("972500000099@g.us", "hi"), { ok: false, error: "self_mismatch" });
  client.meJid = "972500000099:12@s.whatsapp.net";  // the manager's own number on another device: fine
  assert.equal((await send()).ok, true);
  client.meJid = "972500000098@s.whatsapp.net";
  clock += 60_000;
  assert.deepEqual(await send(), { ok: false, error: "wrong_account" });
  const c = readCfg();
  delete c.whatsapp.self_jid;
  writeCfg(c);
  client.meJid = SELF;
  assert.deepEqual(await send(), { ok: false, error: "wrong_account" });
  assert.equal(client.calls.filter((x) => x[0] === "send").length, 1);
});

test("refusals send nothing", async () => {
  const cases = [
    ["off", () => { const c = readCfg(); c.whatsapp.send = false; writeCfg(c); }],
    ["off", () => { const c = readCfg(); c.whatsapp.enabled = false; writeCfg(c); }],
    ["paused", () => fs.writeFileSync(path.join(ws, ".mother", "PAUSED"), "")],
    ["cap", () => writeSent(Array.from({ length: 20 }, (_, i) => ({ at: isoZ(clock - 3600_000), kind: "send",
                                                                    chat: DM, sha256: "x", stanza: "Q" + i })))],
    ["cap", () => fs.writeFileSync(sentPath(), "not json\n")],
    ["unlinked", () => { client.isLinked = false; }],
    ["send_failed", () => { client.failSend = true; }],
  ];
  await start();
  const base = readCfg();
  fs.mkdirSync(path.join(ws, ".mother"), { recursive: true });
  fs.mkdirSync(path.dirname(sentPath()), { recursive: true });
  for (const [error, arrange] of cases) {
    writeCfg(base);
    fs.rmSync(path.join(ws, ".mother", "PAUSED"), { force: true });
    fs.rmSync(sentPath(), { force: true });
    Object.assign(client, { isLinked: true, failSend: false, meJid: SELF });
    arrange();
    assert.deepEqual(await send(), { ok: false, error }, error);
  }
  assert.equal(client.calls.filter((x) => x[0] === "send").length, 1);  // only the send_failed attempt
  assert.deepEqual(sentLines(), []);
});

test("bad requests", async () => {
  await start();
  for (const req of [{ op: "send", to: "x", jid: "123@lid", text: "hi" }, { op: "send", to: "x", jid: "status@broadcast", text: "hi" },
                     { op: "send", to: DM, jid: DM, text: "" }, { op: "send", to: DM, jid: DM, text: "   " },
                     { op: "send", to: DM, jid: DM, text: "x".repeat(4097) }, { op: "send", to: DM, jid: DM },
                     { op: "send", to: DM, jid: DM, text: "x".repeat(4090), signature: "*Agnes (AI)*" },
                     { op: "send", jid: DM, text: "hi" }, { op: "send", to: DM, jid: DM, text: "hi", signature: 5 },
                     { op: "send", to: DM, jid: [DM], text: "hi" }, { op: "send", draft: "w3kx" }, { op: "prompt", text: "hi" },
                     { op: "read" }, [1, 2]]) {
    assert.deepEqual(await request(req), { ok: false, error: "bad_request" }, JSON.stringify(req));
  }
  assert.deepEqual(await request(null, "not json\n"), { ok: false, error: "bad_request" });
  assert.deepEqual(client.calls, []);
});

test("two sends back to back: the second waits out the gap inside the request", async () => {
  await start();
  const [a, b] = await Promise.all([send(DM, "one"), send(GROUP, "two")]);
  assert.deepEqual([a.ok, b.ok], [true, true]);
  const [l1, l2] = sentLines();
  assert.ok(Date.parse(l2.at) - Date.parse(l1.at) >= 5000);
});

test("a gap longer than 30 s is not waited: gap", async () => {
  const c = readCfg();
  c.whatsapp.min_gap_seconds = 40;
  writeCfg(c);
  await start();
  assert.equal((await send()).ok, true);
  assert.deepEqual(await send(GROUP, "two"), { ok: false, error: "gap" });
});

test("a slow send still answers a client that half-closed after its request", async () => {
  await start({ bridge: { sleep: () => new Promise((r) => setTimeout(r, 200)), now: Date.now } });
  assert.deepEqual(await send(), { ok: true, stanza: "STANZA1" });
});

test("WhatsApp returned no message id: the line says stanza null", async () => {
  client.sendText = async () => "";
  await start();
  assert.deepEqual(await send(), { ok: true, stanza: null });
  assert.equal(sentLines()[0].stanza, null);
});

test("sent.jsonl can't be written: the send stands, the failure is logged", async () => {
  writeSent([]);
  fs.chmodSync(sentPath(), 0o400);
  await start();
  assert.deepEqual(await send(), { ok: true, stanza: "STANZA1" });
  assert.ok(logs.some((l) => l.error === "sent_log_failed"));
});

test("no workspace: no_workspace", async () => {
  env.MOTHER_WORKSPACE = path.join(tmp, "nowhere");
  await start();
  assert.deepEqual(await send(), { ok: false, error: "no_workspace" });
});

test("logs carry ids and error codes, never text or a phone number", async () => {
  await start();
  await send(DM, "secret body");
  client.failSend = true;
  clock += 60_000;
  await send(GROUP, "another secret");
  await send("123@lid", "third secret");
  await request({ op: "send", to: "Dana", jid: DM, text: "fourth secret" });  // no ticket
  const all = JSON.stringify(logs);
  assert.ok(logs.length >= 3);
  for (const bad of ["secret", DM, SELF, GROUP, "972500000", "120363"]) {
    assert.ok(!all.includes(bad), bad);
  }
});

test("a paired client that is still connecting: status says so, a send waits for the open", async () => {
  connectingClient(150);
  await start({ bridge: { connectWaitMs: 2000 } });
  const st = await request({ op: "status" });
  assert.equal(st.linked, false);
  assert.equal(st.connecting, true);
  assert.deepEqual(await send(), { ok: true, stanza: "STANZA1" });
  assert.equal((await request({ op: "status" })).connecting, undefined);
});

test("a paired client whose connection never opens: unlinked after the window", async () => {
  connectingClient(null);
  await start({ bridge: { connectWaitMs: 100 } });
  assert.deepEqual(await send(), { ok: false, error: "unlinked" });
});

test("an unpaired client is unlinked at once, without waiting", async () => {
  client.isLinked = false;
  client.paired = false;
  client.waitOpen = () => { throw new Error("must not wait"); };
  await start({ bridge: { connectWaitMs: 60_000 } });
  assert.equal((await request({ op: "status" })).connecting, undefined);
  assert.deepEqual(await send(), { ok: false, error: "unlinked" });
});

test("socket is 0600 in a 0700 folder", async () => {
  await start();
  assert.equal(fs.statSync(sock).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.dirname(sock)).mode & 0o777, 0o700);
  assert.ok(fs.statSync(sock).isSocket());
});

test("session keys: folder 0700, files 0600", () => {
  const dir = path.join(tmp, "state", "whatsapp", "session");
  fs.mkdirSync(dir, { recursive: true, mode: 0o755 });
  fs.chmodSync(dir, 0o755);
  fs.writeFileSync(path.join(dir, "creds.json"), "{}", { mode: 0o644 });
  tighten(dir);
  assert.equal(fs.statSync(dir).mode & 0o777, 0o700);
  assert.equal(fs.statSync(path.join(dir, "creds.json")).mode & 0o777, 0o600);
  const fresh = path.join(tmp, "state", "whatsapp", "session2");
  tighten(fresh);
  assert.equal(fs.statSync(fresh).mode & 0o777, 0o700);
});

test("a stale socket is replaced; a live one is not taken over", async () => {
  fs.mkdirSync(path.dirname(sock), { recursive: true });
  fs.writeFileSync(sock, "");
  await start();
  const second = createBridge({ env, client });
  await assert.rejects(second.listen(sock), /already running/);
});

test("workspace: $MOTHER_WORKSPACE, else the state pointer", () => {
  assert.equal(workspace(env), ws);
  const st = path.join(tmp, "state");
  fs.mkdirSync(st, { recursive: true });
  fs.writeFileSync(path.join(st, "workspace"), ws + "\n");
  assert.equal(workspace({ MOTHER_STATE_DIR: st }), ws);
  assert.equal(workspace({ MOTHER_STATE_DIR: path.join(tmp, "nowhere") }), null);
});

test("idleMs reads MOTHER_BRIDGE_IDLE_MS, default 30 minutes", () => {
  assert.equal(idleMs({}), 30 * 60_000);
  assert.equal(idleMs({ MOTHER_BRIDGE_IDLE_MS: "250" }), 250);
  assert.equal(idleMs({ MOTHER_BRIDGE_IDLE_MS: "nope" }), 30 * 60_000);
  assert.equal(idleMs({ MOTHER_BRIDGE_IDLE_MS: "-5" }), 30 * 60_000);
});

test("serve exits cleanly after the idle time with no request, removing its socket", async () => {
  const closed = [];
  const stub = { ...client, linked: client.linked, me: client.me, close: () => closed.push("client") };
  const codes = [];
  const loc = locations(env);
  const { stop } = await serve({ client: stub, loc, ms: 300, exit: (c) => codes.push(c), signals: false, env });
  try {
    assert.ok(fs.existsSync(loc.sock));
    // a request resets the clock: three requests 150 ms apart outlast the 300 ms idle time
    for (let i = 0; i < 3; i++) {
      await new Promise((r) => setTimeout(r, 150));
      assert.equal((await request({ op: "status" })).ok, true);
      assert.deepEqual(codes, []);
    }
    assert.ok(fs.existsSync(loc.sock));
    await new Promise((r) => setTimeout(r, 700));
    assert.deepEqual(codes, [0]);
    assert.deepEqual(closed, ["client"]);
    assert.equal(fs.existsSync(loc.sock), false);
  } finally {
    stop();
  }
});

// A paired client whose connection opens after `delayMs` (or never, when delayMs is null).
function connectingClient(delayMs) {
  client.isLinked = false;
  client.paired = true;
  client.waitOpen = (ms) => new Promise((resolve) => {
    if (delayMs !== null && delayMs <= ms) setTimeout(() => { client.isLinked = true; resolve(true); }, delayMs);
    else setTimeout(() => resolve(false), ms);
  });
}

test("stop: replies ok, then serve exits cleanly and removes its own socket", async () => {
  const codes = [];
  const loc = locations(env);
  const { stop } = await serve({ client: { ...client, close() {} }, loc, ms: 60_000, exit: (c) => codes.push(c),
                                 signals: false, env });
  assert.deepEqual(await request({ op: "stop" }), { ok: true });
  await new Promise((r) => setTimeout(r, 300));
  assert.deepEqual(codes, [0]);
  assert.equal(fs.existsSync(loc.sock), false);
  stop();
});

test("silenceConsole drops console.* output (libsignal prints session objects there)", () => {
  const writes = [];
  const so = process.stdout.write, se = process.stderr.write;
  process.stdout.write = (...a) => { writes.push(String(a[0])); return true; };
  process.stderr.write = (...a) => { writes.push(String(a[0])); return true; };
  const restore = silenceConsole();
  try {
    console.log("a", { key: "SECRET" });
    console.info("Closing session:", { privKey: "SECRET" });
    console.warn("SECRET");
    console.error("SECRET");
    console.debug("SECRET");
  } finally {
    restore();
    process.stdout.write = so;
    process.stderr.write = se;
  }
  assert.deepEqual(writes, []);
});

test("bareJid drops the device suffix", () => {
  assert.equal(bareJid("972500000099:12@s.whatsapp.net"), SELF);
  assert.equal(bareJid(SELF), SELF);
  assert.equal(bareJid(null), null);
  assert.equal(bareJid(""), null);
});

function fakeLinkClient({ open = true, me = "972500000099:7@s.whatsapp.net" } = {}) {
  const c = { closed: false };
  c.me = () => (open && !c.closed ? me : null);
  c.waitOpen = async () => open;
  c.close = () => { c.closed = true; };
  return c;
}

test("--link: the QR goes to the terminal, then one JSON line names the linked account (suffix stripped)", async () => {
  const out = [];
  const loc = locations(env);
  const code = await runLink(loc, {
    connect: async ({ onQR }) => { onQR("QRDATA"); return fakeLinkClient(); },
    qr: { generate: (q) => out.push("QR:" + q) }, out: (l) => out.push(l), err: () => {}, settleMs: 0,
  });
  assert.equal(code, 0);
  assert.ok(out.includes("QR:QRDATA"));
  assert.deepEqual(JSON.parse(out[out.length - 1]), { linked: true, me: SELF });
});

test("--link that never opens: {linked: false, reason}, exit 1, a plain sentence, no raw log on the terminal", async () => {
  for (const [reason, words] of [["qr_expired", "expired"], ["logged_out", "logged"], ["replaced", "another device"],
                                 ["network", "connection"], [null, "3 minutes"]]) {
    const out = [];
    const c = fakeLinkClient({ open: false });
    c.closeReason = () => reason;
    let logged = null;
    const code = await runLink(locations(env), {
      connect: async ({ log }) => { logged = log; log({ op: "connection", state: "close", code: "408" }); return c; },
      qr: { generate() {} }, out: (l) => out.push(l), err: (l) => out.push(l), settleMs: 0,
    });
    assert.equal(code, 1);
    assert.deepEqual(JSON.parse(out[out.length - 1]), { linked: false, reason: reason || "timeout" });
    assert.ok(out.slice(0, -1).some((l) => l.toLowerCase().includes(words)), reason + ": " + out.join("|"));
    assert.ok(!out.slice(0, -1).some((l) => l.startsWith("{")), "no JSON on the terminal: " + out.join("|"));
    const log = fs.readFileSync(path.join(locations(env).dir, "bridge.log"), "utf8");
    assert.match(log, /"op":"connection"/);  // the structured line went to bridge.log
    assert.equal(typeof logged, "function");
  }
});

test("--link --phone: a pairing code instead of a QR, told where to type it", async () => {
  const out = [];
  let opts = null;
  const code = await runLink(locations(env), {
    connect: async (o) => { opts = o; o.onPairCode("ABCD1234"); return fakeLinkClient(); },
    qr: { generate: () => out.push("QR") }, out: (l) => out.push(l), err: () => {}, settleMs: 0, phone: "972500000099",
  });
  assert.equal(code, 0);
  assert.equal(opts.pairPhone, "972500000099");
  assert.ok(!out.includes("QR"));
  assert.ok(out.some((l) => l.includes("Link with phone number instead, then type this code: ABCD-1234")), out.join("|"));
  assert.deepEqual(JSON.parse(out[out.length - 1]), { linked: true, me: SELF });
});

test("--link: later codes say WhatsApp replaced them, not that they timed out", async () => {
  const out = [];
  await runLink(locations(env), {
    connect: async ({ onQR }) => { onQR("A"); onQR("B"); return fakeLinkClient(); },
    qr: { generate() {} }, out: (l) => out.push(l), err: () => {}, settleMs: 0,
  });
  assert.ok(out.some((l) => l.includes("New code (WhatsApp replaces it every 20 seconds or so)")));
  assert.ok(!out.some((l) => l.includes("timed out")));
});

test("--link says where to scan before the first code", async () => {
  const out = [];
  await runLink(locations(env), {
    connect: async ({ onQR }) => { onQR("A"); onQR("B"); return fakeLinkClient(); },
    qr: { generate: (q) => out.push("QR:" + q) }, out: (l) => out.push(l), err: () => {}, settleMs: 0,
  });
  const first = out.findIndex((l) => l.startsWith("QR:"));
  assert.ok(out.slice(0, first).join(" ").includes("Linked devices → Link a device first; each code lasts about 20 seconds"));
  assert.ok(out.includes("QR:B"));
});

test("closeAction: in link mode a close ends the attempt with a reason (no reconnect), 515 restarts", () => {
  assert.deepEqual(closeAction("link", 408, 0), { end: true, reason: "qr_expired" });
  assert.deepEqual(closeAction("link", 401, 0), { end: true, reason: "logged_out", loggedOut: true });
  assert.deepEqual(closeAction("link", 440, 0), { end: true, reason: "replaced" });
  assert.deepEqual(closeAction("link", 428, 0), { end: true, reason: "network" });
  assert.deepEqual(closeAction("link", 515, 0), { retryMs: 0 });
  assert.deepEqual(closeAction("serve", 408, 2), { retryMs: 8000 });
  assert.deepEqual(closeAction("serve", 515, 3), { retryMs: 0 });
  assert.deepEqual(closeAction("serve", 401, 0), { end: true, reason: "logged_out", loggedOut: true });
});

test("waVersion: the live WhatsApp Web version, else the library default (and a log line)", async () => {
  assert.deepEqual(await waVersion(async () => ({ version: [2, 3000, 9], isLatest: true })), [2, 3000, 9]);
  const logged = [];
  assert.equal(await waVersion(async () => ({ version: [2, 3000, 1], isLatest: false }), (r) => logged.push(r)), undefined);
  assert.equal(await waVersion(async () => { throw Object.assign(new Error("x"), { name: "TimeoutError" }); },
                               (r) => logged.push(r)), undefined);
  assert.deepEqual(logged, [{ op: "wa_version", error: "not_found" }, { op: "wa_version", error: "TimeoutError" }]);
});

test("isPaired needs a confirmed account; clearUnpaired drops only a half-done pairing", () => {
  assert.equal(isPaired({ me: { id: "972500000099@s.whatsapp.net" } }), false);
  assert.equal(isPaired({ me: { id: "972500000099:3@s.whatsapp.net" }, account: { details: "x" } }), true);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wa-sess-"));
  fs.writeFileSync(path.join(dir, "creds.json"), JSON.stringify({ me: { id: "1@s.whatsapp.net" }, account: {} }));
  assert.equal(clearUnpaired(dir), false);
  assert.ok(fs.existsSync(path.join(dir, "creds.json")));
  fs.writeFileSync(path.join(dir, "creds.json"), JSON.stringify({ me: { id: "1@s.whatsapp.net" } }));
  assert.equal(clearUnpaired(dir), true);
  assert.ok(!fs.existsSync(dir));
  assert.equal(clearUnpaired(dir), false);  // nothing there
});

test("the guard's ticket name: sha256 of the recipient as typed, NUL, the text (same as hooks/guard.py)", () => {
  assert.equal(ticketName("Dana", "hi"), "ce3bec50a9346edfd0630fc02404c10a2d38f912dd900f8b903b00f339a43ca0");
});

test("no ticket: no_ticket, nothing sent (a send that never got a guard decision)", async () => {
  await start();
  assert.deepEqual(await request({ op: "send", to: DM, jid: DM, text: TEXT }), { ok: false, error: "no_ticket" });
  ticket(DM, "other text");  // a ticket for something else doesn't count
  assert.deepEqual(await request({ op: "send", to: DM, jid: DM, text: TEXT }), { ok: false, error: "no_ticket" });
  ticket("Dana", TEXT);  // nor one for another recipient as typed
  assert.deepEqual(await request({ op: "send", to: DM, jid: DM, text: TEXT }), { ok: false, error: "no_ticket" });
  assert.deepEqual(client.calls, []);
});

test("a ticket is used once and deleted before the send", async () => {
  let seen = null;
  client.sendText = async () => { seen = fs.existsSync(path.join(locations(env).tickets, ticketName("Dana", TEXT))); return "S1"; };
  await start();
  assert.equal((await send(DM, TEXT, "Dana")).ok, true);
  assert.equal(seen, false);
  clock += 60_000;
  assert.deepEqual(await request({ op: "send", to: "Dana", jid: DM, text: TEXT }), { ok: false, error: "no_ticket" });
});

test("a stale ticket: ticket_expired, and it is gone; a refusal later on still spends the ticket", async () => {
  await start();
  const p = ticket(DM, TEXT, TICKET_MAX_AGE_MS + 1000);
  const old = ticket("Sam Roe", "left by a declined prompt", TICKET_MAX_AGE_MS + 1000);
  assert.deepEqual(await request({ op: "send", to: DM, jid: DM, text: TEXT }), { ok: false, error: "ticket_expired" });
  assert.equal(fs.existsSync(p), false);
  assert.equal(fs.existsSync(old), false);  // every send sweeps the expired ones
  const q = ticket(DM, TEXT, TICKET_MAX_AGE_MS - 5000);
  client.isLinked = false;
  assert.deepEqual(await request({ op: "send", to: DM, jid: DM, text: TEXT }), { ok: false, error: "unlinked" });
  assert.equal(fs.existsSync(q), false);
  assert.deepEqual(client.calls, []);
});

test("a ticket that isn't a regular file, or holds no time, doesn't count", async () => {
  await start();
  const dir = locations(env).tickets;
  fs.mkdirSync(dir, { recursive: true });
  const target = path.join(tmp, "elsewhere");
  fs.writeFileSync(target, JSON.stringify({ at: Math.floor(clock / 1000) }));
  fs.symlinkSync(target, path.join(dir, ticketName(DM, TEXT)));
  assert.deepEqual(await request({ op: "send", to: DM, jid: DM, text: TEXT }), { ok: false, error: "no_ticket" });
  assert.ok(fs.existsSync(target));
  fs.writeFileSync(path.join(dir, ticketName(DM, "x")), "{}");
  assert.equal((await request({ op: "send", to: DM, jid: DM, text: "x" })).ok, false);
  assert.deepEqual(client.calls, []);
});

test("the recipient as typed must name the JID: me, a number, a JID; a name can't be checked", async () => {
  await start();
  assert.deepEqual(await send(DM, "a", "me"), { ok: false, error: "wrong_recipient" });
  assert.deepEqual(await send(GROUP, "b", "+972 50 000 0001"), { ok: false, error: "wrong_recipient" });
  assert.deepEqual(await send(GROUP, "c", DM), { ok: false, error: "wrong_recipient" });
  assert.equal((await send(DM, "d", "+972-50-000-0001")).ok, true);
  assert.equal(recipientMatches("Dana", GROUP, SELF), true);
  assert.equal(recipientMatches(" ME ", SELF, SELF), true);
  assert.equal(recipientMatches("me", SELF, undefined), false);
});

test("the signature goes on its own line after a blank one", async () => {
  await start();
  assert.equal((await send(DM, "see you", DM, { signature: "*Agnes (AI)* · on behalf of Dana" })).ok, true);
  assert.deepEqual(client.calls.at(-1), ["send", DM, "see you\n\n*Agnes (AI)* · on behalf of Dana"]);
  assert.equal(sentLines()[0].sha256, createHash("sha256").update("see you\n\n*Agnes (AI)* · on behalf of Dana").digest("hex"));
});
