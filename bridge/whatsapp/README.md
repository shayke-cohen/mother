# WhatsApp bridge (send-only)

The one process that sends WhatsApp messages for MOTHER. It links to the manager's WhatsApp as a device
(like WhatsApp Web, via [Baileys](https://github.com/WhiskeySockets/Baileys)) and sends one text to one chat
when `mother whatsapp send` asks; the guard asked the manager before that command ran. It reads nothing: incoming messages are handled only as far
as the protocol needs and are never stored, logged or forwarded. Reading goes through the local database
(`mother whatsapp read`). The rules are in `docs/CONTRACT.md` §5d.

Optional. Nothing else in MOTHER depends on Node or on this folder. Node 20 or later, macOS, home machine only.

## Install

```bash
mother whatsapp install
```

This copies the bridge into `<state>/whatsapp/app/` (outside the plugin, which a plugin update replaces) and runs
`npm ci --ignore-scripts --omit=dev` there. Run it again after a plugin update that changes this folder
(`mother whatsapp status` says `reinstall_needed`). Node 20 or later: found on PATH, in Homebrew, or the newest nvm.

Versions are pinned exactly in `package.json` and `package-lock.json`. `node_modules/` is never committed.

Known risks of the dependency:
- Baileys is pinned to a **release candidate** (`7.0.0-rc14`, npm's `latest` at the time). It is an
  unofficial client of the WhatsApp Web protocol and can break when WhatsApp changes it.
- It has **install scripts** (Baileys' Node-version check, protobufjs' postinstall). They are not needed:
  `npm ci --ignore-scripts` installs a working bridge (checked on macOS arm64, Node 24), so skip them.
- It pulls **`sharp`**, which brings prebuilt native binaries (`@img/sharp-darwin-*`, libvips) for image
  handling the bridge never uses.

## Run

| Command | Does |
|---|---|
| `node bridge.mjs --link` | Prints the QR in the terminal; scan it in WhatsApp > Settings > Linked devices. Exits once linked. |
| `node bridge.mjs --link --phone <digits>` | The same with a pairing code instead of a QR: Link a device > Link with phone number instead, and type it. |
| `node bridge.mjs` | Serves on `<state>/whatsapp/bridge.sock`. An unlinked bridge stays unlinked and refuses every send. |
| `node bridge.mjs --unlink` | Logs the device out and deletes `<state>/whatsapp/session/`: the kill switch. |

`<state>` is `$MOTHER_STATE_DIR` or `~/.mother`. The workspace is `$MOTHER_WORKSPACE`, else the
`<state>/workspace` pointer. Session keys live in `<state>/whatsapp/session/` (folder 0700, files 0600),
outside the workspace, never synced. The agent never runs these directly or talks to the socket: it goes
through `mother whatsapp …`, and the guard denies anything else that touches the socket or the state.

A freshly started bridge answers `send` only once its WhatsApp connection is open (it waits up to 20 s;
`unlinked` only with no session or when the connection fails in that window). It drops every `console.*` call
(libsignal prints session objects there), so `bridge.log` holds ids and codes only. Normally nobody starts it by
hand: `mother whatsapp send` starts `node bridge.mjs` detached (log in `<state>/whatsapp/bridge.log`) when the
socket is missing or refuses, and the bridge exits by itself after 30 minutes with no request
(`MOTHER_BRIDGE_IDLE_MS` overrides it, in ms; the socket is removed). `mother whatsapp link|unlink` run
`--link|--unlink` in your terminal.

## Protocol

A Unix socket, mode 0600, no TCP. One JSON request per connection, one JSON reply, each ending in a newline.

| Request | Reply |
|---|---|
| `{"op": "status"}` | `{"ok": true, "linked", "me", "sent_today"}`, plus `"connecting": true` while a paired session is still opening |
| `{"op": "stop"}` | `{"ok": true}`, then the bridge exits (used by `mother whatsapp stop`, `link`, `unlink`) |
| `{"op": "send", "to", "jid", "text", "signature"?}` | `{"ok": true, "stanza"}` or `{"ok": false, "error"}` |

Before every `send` the bridge checks, in order: `jid` is a user (`<digits>@s.whatsapp.net`) or group
(`<digits>@g.us`) JID, `to` is a string, `text` isn't blank and the signed text is at most 4096 characters (else
`bad_request`); the workspace config is readable (`no_workspace`); **the guard's ticket**
`<state>/whatsapp/tickets/<sha256(to NUL text)>` exists as a regular file and is under 120 s old, and it is
deleted right then, so it is used once (`no_ticket`, `ticket_expired`; older tickets are swept too); `to`
names `jid` when it is `me`, a number or a JID (`wrong_recipient`); sending is on and the workspace isn't
PAUSED; `sent.jsonl` is readable and today's sends are under `max_per_day`; the jittered `min_gap_seconds`
since the last send (waited out inside the request when 30 s or less, else `gap`); linked; the linked account
is `whatsapp.self_jid` (`wrong_account`); and the manager's own number is addressed only as `self_jid`
(`self_mismatch`). Then it shows "typing…" (not in the manager's own chat), sends `text` plus a blank line and
`signature` when one is given, and appends one line to `sent.jsonl` (`{"at", "kind": "send", "chat", "sha256",
"stanza"}`). The ticket is what ties a send to the guard's decision: a script that reaches the socket without
one is refused.

Errors: `bad_request`, `no_workspace`, `no_ticket`, `ticket_expired`, `wrong_recipient`, `off`, `paused`,
`cap`, `gap`, `unlinked`, `wrong_account`, `self_mismatch`, `send_failed`, `internal`.

## Test

```bash
node --test bridge/whatsapp/*.test.mjs      # or: cd bridge/whatsapp && node --test
```

`checks.mjs` holds the pure rules; `bridge.test.mjs` drives the real socket with a stub WhatsApp client. No
test connects to WhatsApp.
