# Migrating to watcher rules (dynamic watchers)

The static watcher shape — a `room:` key, or `rooms:` as a list — has been
removed. `watchers:` entries are now **rules**: they declare which rooms an
agent serves, and the gateway creates each room's watcher on demand — on the
room's first message (Rocket.Chat, Mattermost), or eagerly at startup for
connectors with no inbound stream (voice, script). A config still carrying the
old shape fails at load with a pointer at this document, and `acg config
validate` lists every entry that needs rewriting in one pass.

**This is not a 1:1 rename.** Read *What changes underneath* before editing —
some of the old shape's guarantees are deliberately not carried over.

## The rewrite

Old:

```yaml
watchers:
  - name: support
    connector: rc-home
    room: general
  - connector: rc-home
    rooms: [dev, ops]
  - connector: rc-home
    room: "@alice"
```

New:

```yaml
watchers:
  - name: my-rooms            # required — rules are not auto-named
    connector: rc-home
    rooms:
      include: [general, dev, ops]
      direct: true            # replaces the "@alice" entry — see below
```

Field notes:

- **`name:` is required** and names the *rule*, not a watcher. Each created
  watcher gets a derived name like `rc-home:general`, which is what
  `list`/`pause`/`resume`/`reset`/`expire` act on.
- **`rooms:` is a mapping**: `include:` takes glob patterns over room names
  (`eng-*`, `general`, `*`), `except_for:` subtracts from this rule's own
  include, and `direct:` / `group_direct:` opt into the two DM kinds.
- **A DM entry cannot be named.** `room: "@alice"` worked because the static
  path resolved the name at startup; a rule matches rooms as they arrive, and
  a DM has no room name for a pattern to match. `direct: true` claims 1:1
  DMs (the connector's `owners`/`guests` lists still gate who can talk);
  `group_direct: true` claims multi-party DMs, where mentions are required.
- Rules match top-down; the first rule that claims a room wins. `acg config
  validate` warns about rules an earlier rule shadows completely.
- `session_id:` was removed separately and stays removed. To carry context
  into a new session, have the agent summarise the session to a file and list
  it in `context_inject_files` — a handoff survives the backend expiring a
  session, which pinning never did (docs/user-guide.md, Use Case 3).
- Connectors with no inbound stream (**voice, script**) require literal
  `rooms.include` entries — a pattern has nothing to match against there —
  and their rooms start eagerly at boot, as the static shape's did.

## What changes underneath — the accepted losses

1. **Every room gets its own watcher and its own session.** A multi-room
   static entry shared nothing; a rule creates one watcher per room. Existing
   static-era sessions are **not** carried over: the first boot after the
   upgrade prunes every static-era state record (logged per record), and each
   room's first message starts a fresh session. If a session's content
   matters, have the agent summarise it to a file *before* upgrading and
   inject the file into the new session.
2. **Watermarks go with the records.** Messages that arrived while the
   gateway was down across the upgrade boundary are not replayed.
3. **Scheduled jobs name watchers.** Jobs targeting old static watcher names
   point at nothing after the prune — recreate them against the new derived
   names (`acg list` shows them once the rooms have spoken).
4. **A paused room becomes active** unless re-expressed. Pause acts on a
   record, and the static record is pruned. To keep the bot out of a room
   durably, put the room in the rule's `rooms.except_for:` — declarative, and
   effective before the first message. (Pausing the new watcher after it is
   created also works, but only once it exists.)
5. **Watchers now idle and expire.** A room quiet for `session_idle_days`
   (default 15) releases its runtime (session kept; the next message resumes
   it); a further `session_expire_days` (default 15) reclaims the session and
   record entirely, and the room's next message starts fresh. Pause a watcher
   to exempt it from both timers.

## Checklist

1. Rewrite each `watchers:` entry as a rule (above).
2. `acg config validate` — fix every error; read the shadowing warnings.
3. If any old session's content matters, export it via a summary file first.
4. Restart the gateway. Expect one `Pruning static-era watcher record`
   log line per old record — that is the clean break, not a fault.
5. Delete every scheduled job that targeted an old static watcher name
   (`schedule list`, then `schedule delete <id>` for each) — the prune
   removes the records, NOT the jobs, and an orphaned job re-fires forever
   against a watcher that no longer exists. Then recreate the jobs against
   the new watcher names.

## Changing a connector's server (or type)

A persisted watcher record binds to a **platform room id**, and room ids are
meaningful only on the server that minted them. If you point an existing
connector name at a different server (a new `server.url`, or a different
connector `type`), its old records will try to resume against rooms that do
not exist there — each watcher reads `failed`, loudly, until it expires.

When you migrate servers, do one of:

- **rename the connector** — the old `state.<name>.json` is then reported by
  `acg config validate` as belonging to no configured connector, and you can
  delete it deliberately; or
- **delete the state file** for that connector before the first start against
  the new server.

Session content does not survive a server move either way; export anything
that matters first (checklist step 3).

## Mattermost DM watchers created before the `@` strip

Mattermost sends a DM's counterpart `@`-prefixed on the websocket event, and the
connector used to carry that through. Since `@` is not label-safe it was
percent-encoded, so the same person had two handles depending on the platform:

```
rc-e2e:dm:test_user        ← Rocket.Chat
mm-e2e:dm:%40test_user     ← Mattermost, before the strip
```

The connector now drops the prefix, and **there is nothing to migrate.**
Records resolve by room id, not by name, so an existing DM watcher keeps
serving its room under the old name with its session and watermark intact — no
duplicate watcher, no orphan, nothing in `failed`.

What does break is anything that **types** the new handle. `schedule create
mm-e2e:dm:test_user` is rejected with "Watcher … not found in any connector"
while the record still holds the encoded name, because every operator verb is
name-addressed with no room-id fallback.

If you want the clean name, expire the old record and let the room's next
message recreate it:

```bash
agent-chat-gateway expire 'mm-e2e:dm:%40test_user'   # quote it — % and : are shell-relevant
```

Accepted losses, the same ones the rest of this document takes: the session is
fresh (history handoff refetches recent messages), the watermark resets once,
and the old watcher's durable-instructions prompt file is left behind — prompt
files are keyed partly on the watcher name, so a rename orphans one.

Leaving the encoded name in place is also a legitimate choice. It is ugly in
`list` and awkward to type, and that is the whole of the cost.

## `online_notification` / `offline_notification` are removed

The fields are gone from rules and templates — a config still carrying them
fails at load with an unknown-key error. The platform's own presence
indicators are the replacement (they were always the better signal), and the
idle/expire lifecycle means a watcher now starts and stops many times over
its life; announcing each transition was noise, not status.
