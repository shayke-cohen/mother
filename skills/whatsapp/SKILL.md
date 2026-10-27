---
name: whatsapp
description: Use when the manager asks to read or send WhatsApp messages — "what did Sam say on WhatsApp", "catch me up on the PLAT group", "WhatsApp Dana that I'm late", "send me a note on WhatsApp". Reads chats from the local WhatsApp database and sends through `mother whatsapp send`, which asks the manager first.
---

# WhatsApp

Read the manager's WhatsApp chats and send messages from their own number. Rules: CONTRACT §5d,
`${CLAUDE_PLUGIN_ROOT}/rules/outbound.md` (WhatsApp) and `${CLAUDE_PLUGIN_ROOT}/rules/privacy.md` (WhatsApp).

Run every command as one plain Bash command of its own, written out (not from a variable):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/mother" whatsapp <command> …
```

## Off or not linked

If a command says "whatsapp reading is off", "sending is off", `unlinked`, `bridge_not_installed` or
`wrong_account`, stop and tell the manager to run this in their own terminal (not in this session: it needs
their keyboard and their phone), with `${CLAUDE_PLUGIN_ROOT}` expanded to the absolute path:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/mother" whatsapp setup
```

Never run `setup`, `link` or `unlink` yourself, and never edit the `whatsapp` block of `config.json`: the guard
denies all of it.

## Find a chat

`whatsapp chats --query Dana` lists matching chats: name, `dm` or `group`, last date (`pinned` for a pinned
chat) and JID. Without `--query` it lists the 30 most recent. Name a chat in later commands by its **exact**
name (case doesn't matter), its number, or its JID when two chats share a name. A partial name is refused with
up to 5 candidates.

## Read

| Want | Command |
|---|---|
| One chat | `whatsapp read --chat "Dana Roe"` (its last 100 messages; add `--since 2026-10-26T08:00:00Z` or `--limit 20`) |
| Everything recent | `whatsapp read` (all chats, last 24 hours; `--since` for another window) |
| The manager's own notes | `whatsapp read --chat me` |

A name that isn't exact, or that two chats share, exits 2 with candidates: ask the manager which, or use the JID.
`couldnt_check` means the database couldn't be read (the `reason` says why: `denied` is Full Disk Access);
say so, never "nothing new". Summarise what was said or decided, cite `whatsapp://…` links, and don't copy
message bodies, phone numbers or JIDs into memory, journal, todos or people notes.

## Send

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/mother" whatsapp send --to "Dana Roe" --text 'Running 10 minutes late, start without me'
```

- Put the text in **single quotes** (an apostrophe is `'\''`; a new line is `$'line one\nline two'`). The
  guard denies a text with `$`, `$( )` or backticks inside double quotes, or with control or text-direction
  characters, because it can't show exactly what would be sent.
- Send it as the whole command: nothing chained, piped or wrapped.
- To anyone but the manager, the guard **asks the manager** in the permission prompt, showing who and the
  exact text. That prompt is the approval: don't ask again in chat first unless the wording needs their input.
  MOTHER's signature line is added automatically; don't add one.
- `--to me` sends to the manager's own chat without a prompt.
- It sends only to a chat that already has messages with the manager. A new contact, a chat WhatsApp shows
  without a number, or a name that isn't exact exits 2 with a plain sentence: tell the manager; don't look for
  another way.
- The guard's decision leaves a one-use ticket that the bridge needs within 10 minutes of the prompt, so nothing else can
  send: `no_ticket` or `ticket_expired` means run the same `send` command again (it will ask again).
- **If a send is refused, denied or fails, never retry it by another path** (a script, the bridge socket,
  another tool). Report what it said. `cap` means today's limit (`whatsapp.max_per_day`) is reached; `gap`
  means wait a few seconds and try once more.

On success it prints `{"ok": true, "to", "name", "id"}`; tell the manager it went to `name`.
