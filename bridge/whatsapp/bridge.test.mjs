// The bridge over a real Unix socket with a stub WhatsApp client: nothing here connects to WhatsApp.
// Run: node --test bridge/whatsapp/*.test.mjs
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { createBridge, locations, workspace, verifyWithCli, tighten } from "./bridge.mjs";

const SELF = "972500000099@s.whatsapp.net";
const DM = "972500000001@s.whatsapp.net";
const OTHER = "972500000002@s.whatsapp.net";
const TEXT = "see you at 3";
const sha = (t) => createHash("sha256").update(t, "utf8").digest("hex");
const isoZ = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");

let tmp, env, ws, wa, client, logs, verified, server, sock, clock;

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

function draft(id = "w3kx", text = TEXT, chat = DM) {
  const d = { id, chat, chat_name: "Dana", text, sha256: sha(text), created: isoZ(Date.now() - 7200_000),
              run: null, status: "open" };
  writeJson(path.join(wa, "drafts", id + ".json"), d);
  return d;
}

function approve(d, extra = {}) {
  writeJson(path.join(wa, "approved", d.id + ".json"),
    { id: d.id, chat: d.chat, sha256: d.sha256, path: "session", evidence: "session:u1", at: isoZ(Date.now()),
      ...extra });
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

const readCfg = () => JSON.parse(fs.readFileSync(path.join(ws, "config.json"), "utf8"));
const writeCfg = (c) => writeJson(path.join(ws, "config.json"), c);
const sentPath = () => path.join(tmp, "state", "whatsapp", "sent.jsonl");
function writeSent(lines) {
  fs.mkdirSync(path.dirname(sentPath()), { recursive: true });
  fs.writeFileSync(sentPath(), lines.map((l) => JSON.stringify(l) + "\n").join(""));
}

const sentLines = () => {
  const p = path.join(tmp, "state", "whatsapp", "sent.jsonl");
  return fs.existsSync(p) ? fs.readFileSync(p, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};

const settled = (id) => ["json", "used", "failed"].filter((x) => fs.existsSync(path.join(wa, "approved", id + "." + x)));

async function start(opts = {}) {
  // a fake clock: sleeping advances it, so waiting out a gap costs no real time
  const b = createBridge({ env, client, verify: async (id, sha) => { verified.push([id, sha]); return opts.verify ?? true; },
                           now: () => clock, sleep: async (ms) => { clock += ms; }, rand: () => 0,
                           log: (r) => logs.push(r), ...opts.bridge });
  server = await b.listen(sock);
  return b;
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mwa-"));
  ws = path.join(tmp, "ws");
  wa = path.join(ws, ".mother", "whatsapp");
  env = { MOTHER_STATE_DIR: path.join(tmp, "state"), MOTHER_WORKSPACE: ws, PATH: process.env.PATH,
          HOME: process.env.HOME };
  writeJson(path.join(ws, "config.json"), {
    workspace: { home_machine: "laptop-a" },
    whatsapp: { enabled: true, self_jid: SELF, allow: [{ jid: DM, name: "Dana" }], min_gap_seconds: 5 },
  });
  client = stubClient();
  logs = [];
  verified = [];
  clock = Date.now();
  sock = locations(env).sock;
});

afterEach(async () => {
  if (server) await new Promise((r) => server.close(r));
  server = null;
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("socket is 0600 in a 0700 folder", async () => {
  await start();
  assert.equal(fs.statSync(sock).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.dirname(sock)).mode & 0o777, 0o700);
  assert.ok(fs.statSync(sock).isSocket());
});

test("status", async () => {
  await start();
  assert.deepEqual(await request({ op: "status" }),
                   { ok: true, linked: true, me: SELF, sent_today: 0, prompts_today: 0 });
  client.isLinked = false;
  assert.deepEqual(await request({ op: "status" }),
                   { ok: true, linked: false, me: null, sent_today: 0, prompts_today: 0 });
});

test("an approved draft goes out once: typing, send, sent.jsonl, approval .used", async () => {
  const d = draft();
  approve(d);
  await start();
  const r = await request({ op: "send", draft: "w3kx" });
  assert.deepEqual(r, { ok: true, stanza: "STANZA1" });
  assert.deepEqual(verified, [["w3kx", d.sha256]]);  // verify is bound to the hash of the bytes sent (M1)
  assert.deepEqual(client.calls, [["presence", DM, "composing"], ["presence", DM, "paused"], ["send", DM, TEXT]]);
  const lines = sentLines();
  assert.equal(lines.length, 1);
  assert.deepEqual(Object.keys(lines[0]), ["at", "kind", "chat", "draft", "sha256", "stanza"]);
  assert.deepEqual({ ...lines[0], at: "x" },
                   { at: "x", kind: "send", chat: DM, draft: "w3kx", sha256: d.sha256, stanza: "STANZA1" });
  assert.match(lines[0].at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.equal(fs.statSync(path.join(tmp, "state", "whatsapp", "sent.jsonl")).mode & 0o777, 0o600);
  assert.ok(fs.existsSync(path.join(wa, "approved", "w3kx.used")));
  assert.ok(!fs.existsSync(path.join(wa, "approved", "w3kx.json")));
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "no_approval" });
  assert.equal(client.calls.filter((c) => c[0] === "send").length, 1);
  assert.deepEqual(settled("w3kx"), ["used"]);
  assert.equal((await request({ op: "status" })).sent_today, 1);
});

test("a slow send still answers a client that half-closed after its request", async () => {
  approve(draft());
  await start({ bridge: { sleep: (ms) => new Promise((r) => setTimeout(r, 200)), now: Date.now } });
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: true, stanza: "STANZA1" });
});

test("no approval record: no_approval, nothing sent", async () => {
  draft();
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "no_approval" });
  assert.deepEqual(client.calls, []);
  assert.deepEqual(verified, []);
});

test("the re-proof fails: no_approval, nothing sent, the approval settles .failed (T6-b)", async () => {
  approve(draft());
  await start({ verify: false });
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "no_approval" });
  assert.deepEqual(client.calls, []);
  assert.deepEqual(settled("w3kx"), ["failed"]);
  assert.deepEqual(sentLines(), []);
});

test("every refusal after the approval was read settles it .failed; already_sent settles .used (T6-b)", async () => {
  const cases = [
    ["off", () => { const c = readCfg(); c.whatsapp.send = false; writeCfg(c); }],
    ["paused", () => fs.writeFileSync(path.join(ws, ".mother", "PAUSED"), "")],
    ["cap", () => writeSent(Array.from({ length: 20 }, (_, i) => ({ at: isoZ(clock - 3600_000), kind: "send",
                                                                    chat: DM, draft: "wq" + i, sha256: "x", stanza: "Q" + i })))],
    ["unlinked", () => { client.isLinked = false; }],
    ["wrong_account", () => { client.meJid = "972500000098@s.whatsapp.net"; }],
    ["not_allowlisted", () => { const c = readCfg(); c.whatsapp.allow = []; writeCfg(c); }],
    ["hash_mismatch", () => draft("w3kx", "see you at 4")],
    ["no_draft", () => fs.rmSync(path.join(wa, "drafts", "w3kx.json"))],
    ["send_failed", () => { client.failSend = true; }],
  ];
  await start();
  const base = readCfg();
  for (const [error, arrange] of cases) {
    writeCfg(base);
    fs.rmSync(path.join(wa, "approved"), { recursive: true, force: true });
    fs.rmSync(path.join(ws, ".mother", "PAUSED"), { force: true });
    fs.rmSync(path.join(tmp, "state", "whatsapp", "sent.jsonl"), { force: true });
    Object.assign(client, { isLinked: true, failSend: false, meJid: SELF });
    approve(draft());
    arrange();
    assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error }, error);
    assert.deepEqual(settled("w3kx"), ["failed"], error);
  }
  writeCfg(base);
  fs.rmSync(path.join(wa, "approved"), { recursive: true, force: true });
  const d = draft();
  approve(d);
  writeSent([{ at: isoZ(clock - 86400_000 * 2), kind: "send", chat: DM, draft: "w3kx", sha256: d.sha256, stanza: "OLD" }]);
  client.calls.length = 0;
  Object.assign(client, { failSend: false });
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "already_sent" });
  assert.deepEqual(settled("w3kx"), ["used"]);
  assert.equal(client.calls.filter((c) => c[0] === "send").length, 0);
});

test("prompt from the wrong linked account: wrong_account (M2)", async () => {
  client.meJid = "972500000098:12@s.whatsapp.net";
  await start();
  assert.deepEqual(await request({ op: "prompt", text: "hello" }), { ok: false, error: "wrong_account" });
  client.meJid = "972500000099:12@s.whatsapp.net";  // the manager's own number on another device: fine
  assert.equal((await request({ op: "prompt", text: "hello" })).ok, true);
  assert.deepEqual(client.calls, [["send", SELF, "hello"]]);
});

test("draft text edited after approval: hash_mismatch (Review Focus 3)", async () => {
  const d = draft();
  approve(d);
  draft("w3kx", "see you at 4");
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "hash_mismatch" });
  assert.deepEqual(client.calls, []);
});

test("a chat off the allowlist: not_allowlisted", async () => {
  approve(draft("w3kx", TEXT, OTHER));
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "not_allowlisted" });
  assert.deepEqual(client.calls, []);
});

test("unlinked: unlinked", async () => {
  approve(draft());
  client.isLinked = false;
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "unlinked" });
  assert.deepEqual(client.calls, []);
});

test("a send that throws: send_failed, approval .failed, no sent.jsonl line", async () => {
  approve(draft());
  client.failSend = true;
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "send_failed" });
  assert.ok(fs.existsSync(path.join(wa, "approved", "w3kx.failed")));
  assert.ok(!fs.existsSync(path.join(wa, "approved", "w3kx.json")));
  assert.deepEqual(sentLines(), []);
});

test("two sends back to back: the second waits out the gap inside the request (T6-b)", async () => {
  approve(draft("w3kx"));
  approve(draft("w9pa", "and bring the deck"));
  await start();
  const [a, b] = await Promise.all([request({ op: "send", draft: "w3kx" }), request({ op: "send", draft: "w9pa" })]);
  assert.deepEqual([a.ok, b.ok], [true, true]);
  const [l1, l2] = sentLines();
  assert.ok(Date.parse(l2.at) - Date.parse(l1.at) >= 5000);
  assert.deepEqual([settled("w3kx"), settled("w9pa")], [["used"], ["used"]]);
});

test("a gap longer than 30 s is not waited: gap, and the approval settles .failed", async () => {
  const c = readCfg();
  c.whatsapp.min_gap_seconds = 40;
  writeCfg(c);
  approve(draft("w3kx"));
  approve(draft("w9pa", "and bring the deck"));
  await start();
  assert.equal((await request({ op: "send", draft: "w3kx" })).ok, true);
  assert.deepEqual(await request({ op: "send", draft: "w9pa" }), { ok: false, error: "gap" });
  assert.deepEqual(settled("w9pa"), ["failed"]);
});

test("WhatsApp returned no message id: the line says stanza null (M4)", async () => {
  approve(draft());
  client.sendText = async () => "";
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: true, stanza: null });
  assert.equal(sentLines()[0].stanza, null);
});

test("sent.jsonl can't be written: a send still settles .used; a prompt says log_failed (M3)", async () => {
  approve(draft());
  writeSent([]);
  fs.chmodSync(sentPath(), 0o400);
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: true, stanza: "STANZA1" });
  assert.deepEqual(settled("w3kx"), ["used"]);
  assert.ok(logs.some((l) => l.error === "sent_log_failed"));
  clock += 60_000;
  const r = await request({ op: "prompt", text: "hello" });
  assert.equal(r.ok, false);
  assert.equal(r.error, "log_failed");
  assert.equal(r.stanza, "STANZA2");
  assert.match(r.detail, /sent but not logged/);
});

test("the approval can't be renamed: the send stands, the failure is logged", async () => {
  approve(draft());
  fs.chmodSync(path.join(wa, "approved"), 0o500);
  try {
    await start();
    assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: true, stanza: "STANZA1" });
    assert.ok(logs.some((l) => l.error === "approval_rename_failed"));
    assert.equal(sentLines().length, 1);
  } finally {
    fs.chmodSync(path.join(wa, "approved"), 0o700);
  }
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

test("prompt: to the manager's own chat only, no typing, logged as a prompt", async () => {
  await start();
  const text = 'WhatsApp -> Dana: "see you at 3" - reply ok w3kx';
  assert.deepEqual(await request({ op: "prompt", text }), { ok: true, stanza: "STANZA1" });
  assert.deepEqual(client.calls, [["send", SELF, text]]);
  const [line] = sentLines();
  assert.deepEqual({ ...line, at: "x" }, { at: "x", kind: "prompt", chat: SELF, sha256: sha(text), stanza: "STANZA1" });
  assert.deepEqual(await request({ op: "prompt", text: "" }), { ok: false, error: "bad_request" });
  const st = await request({ op: "status" });
  assert.deepEqual([st.sent_today, st.prompts_today], [0, 1]);
});

test("PAUSED stops every send and prompt", async () => {
  approve(draft());
  fs.writeFileSync(path.join(ws, ".mother", "PAUSED"), "");
  await start();
  assert.deepEqual(await request({ op: "send", draft: "w3kx" }), { ok: false, error: "paused" });
  assert.deepEqual(await request({ op: "prompt", text: "hi" }), { ok: false, error: "paused" });
  assert.deepEqual(client.calls, []);
});

test("bad requests", async () => {
  await start();
  assert.deepEqual(await request(null, "not json\n"), { ok: false, error: "bad_request" });
  assert.deepEqual(await request({ op: "read" }), { ok: false, error: "bad_request" });
  assert.deepEqual(await request({ op: "send", draft: "../x" }), { ok: false, error: "bad_request" });
  assert.deepEqual(await request({ op: "send", draft: ["w3kx"] }), { ok: false, error: "bad_request" });
  assert.deepEqual(await request([1, 2]), { ok: false, error: "bad_request" });
});

test("logs carry ids and error codes, never text or a phone number", async () => {
  approve(draft());
  await start();
  await request({ op: "send", draft: "w3kx" });
  await request({ op: "prompt", text: "secret prompt body" });
  client.failSend = true;
  approve(draft("w9pa", "another secret"));
  clock += 60_000;
  await request({ op: "send", draft: "w9pa" });
  const all = JSON.stringify(logs);
  assert.ok(logs.length >= 3);
  for (const bad of [TEXT, "secret prompt body", "another secret", DM, SELF, "972500000"]) {
    assert.ok(!all.includes(bad), bad);
  }
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

test("verifyWithCli runs `mother whatsapp verify`: yes for an approved draft, no once its text changes", async () => {
  const cfg = JSON.parse(fs.readFileSync(path.join(ws, "config.json"), "utf8"));
  cfg.whatsapp.approve_via = ["session"];  // no database needed
  writeJson(path.join(ws, "config.json"), cfg);
  const d = draft();
  const transcript = path.join(tmp, "t.jsonl");
  fs.writeFileSync(transcript, JSON.stringify({
    type: "user", uuid: "u1", timestamp: new Date(Date.now() - 600_000).toISOString(), origin: { kind: "human" },
    message: { role: "user", content: "ok w3kx" },
  }) + "\n");
  approve(d, { transcript });
  const venv = { ...env, MOTHER_HOSTNAME: "laptop-a", MOTHER_WHATSAPP_DB: path.join(tmp, "none.sqlite") };
  assert.equal(await verifyWithCli("w3kx", { env: venv, sha256: d.sha256 }), true);
  assert.equal(await verifyWithCli("w3kx", { env: venv, sha256: sha("see you at 4") }), false);  // M1
  draft("w3kx", "see you at 4");
  assert.equal(await verifyWithCli("w3kx", { env: venv, sha256: d.sha256 }), false);
  assert.equal(await verifyWithCli("w9pa", { env: venv, sha256: d.sha256 }), false);
});
