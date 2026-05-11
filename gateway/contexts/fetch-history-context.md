# fetch-history — On-Demand Channel History

You can pull channel history at any point during your session using the
`agent-chat-gateway fetch-history` command. This complements the startup
history injection (which is a fixed snapshot taken when your session began)
by letting you reach further back or refresh your view mid-session.

## When to use

- The startup history snapshot doesn't go back far enough for the current task.
- You need to look up something specific from earlier in the conversation.
- Your session has been running a while and you want a fresher view of recent messages.

## Usage

```bash
# Fetch recent history for your own watcher (most common)
agent-chat-gateway fetch-history --watcher <your-watcher-name> --count 50

# Fetch older messages — paginate using --before with the oldest timestamp
# from your previous result
agent-chat-gateway fetch-history --watcher <your-watcher-name> --count 50 --before "2026-05-10T10:00:00+08:00"

# Control how many messages are shown verbatim vs condensed
agent-chat-gateway fetch-history --watcher <your-watcher-name> --count 100 --verbatim 30
```

Your watcher name is in your `ACG Session Identity` context block (e.g. `hammer-mei`).

## Flags

| Flag | Default | Description |
|---|---|---|
| `--watcher NAME` | *(required)* | Your watcher name from ACG Session Identity |
| `--count N` | `50` | Max messages to retrieve |
| `--before TS` | *(not set)* | ISO 8601 timestamp — fetch messages older than this (exclusive) |
| `--verbatim N` | `15` | Last N messages shown in full; older messages condensed to one line each |

## Pagination

To page further back, take the oldest timestamp from the current result
and pass it as `--before` in your next call:

```bash
# Step 1: get recent history
agent-chat-gateway fetch-history --watcher hammer-mei --count 50
# → note the oldest ts shown, e.g. "2026-05-10T08:15:00+08:00"

# Step 2: page back further
agent-chat-gateway fetch-history --watcher hammer-mei --count 50 --before "2026-05-10T08:15:00+08:00"
```

## Output format

The output uses the same Rocket.Chat message header format as live messages,
so you can parse it with the same rules you already know:

```
[HISTORY FETCH — on-demand 2026-05-10T14:32:00+08:00]

**Earlier messages (condensed):**
[Rocket.Chat #nest | from: alice | role: owner | ts: ...] Can you look into the auth issue?

**Recent messages:**
[Rocket.Chat #nest | from: alice | role: owner | ts: ...]
What did you find?
```

## Notes

- Output comes through Bash stdout (tool-result content), not trusted system context.
- The server caps `--count` at `max_fetch_count` (default 200) to protect your context window. A warning is shown in the output when the count is clamped.
- Only messages from users in the owner/guest allowlist are included — same filtering as live message processing.
