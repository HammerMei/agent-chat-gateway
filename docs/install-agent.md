# Installation & Getting Started Guide

Follow this guide to install `agent-chat-gateway` and set up your first agent watcher.

---

## Prerequisites

Before starting, ensure you have:

- **Python 3.12 or later** — Run `python3 --version` to check
- **git** — Run `git --version` to check
- **Claude CLI** or **OpenCode CLI** installed and authenticated
  - Claude CLI: https://claude.ai/download
  - OpenCode CLI: https://opencode.ai
- **A chat server with a dedicated bot account** — either works, pick one:
  - **Rocket.Chat**: bot username + password, and admin or room-owner access to add the bot to rooms
  - **Mattermost**: a Bot Account access token (or a regular account's username + password), and the bot account must be a member of the team your channels live in
- **Basic familiarity** with YAML and the command line

---

## Step 1: Install the Gateway

Run the one-line installer with `--no-onboard` to skip the interactive setup wizard:

```bash
curl -fsSL https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/install.sh | bash -s -- --no-onboard
```

This will:
1. Check Python 3.12+ and install `uv` if missing
2. Clone the repo to `~/.agent-chat-gateway/repo`
3. Install dependencies with `uv sync`
4. Copy bundled context files to `~/.agent-chat-gateway/contexts/`
5. Create a symlink at `~/.local/bin/agent-chat-gateway`
6. Add `~/.local/bin` to PATH in `~/.bashrc` / `~/.zshrc` if needed

After the installer finishes, it will print:
- The executable location: `~/.local/bin/agent-chat-gateway`
- The `source` command needed to activate it in the current shell (e.g. `source ~/.zshrc`)

**Inform the user:** The executable has been installed at `~/.local/bin/agent-chat-gateway`. They need to run `source ~/.zshrc` (or `source ~/.bashrc`, depending on their shell) — or restart their terminal — before `agent-chat-gateway` will work as a direct command.

Verify the installation:
```bash
~/.local/bin/agent-chat-gateway --help
```

You should see the available commands: `start`, `stop`, `status`, `list`, `pause`, `resume`, `reset`, `send`, `onboard`, `upgrade`.

---

## Step 2: Create a Bot Account

**Ask the user which chat platform they're connecting** (Rocket.Chat or Mattermost) before
proceeding — the rest of this step and Step 3 branch on that choice.

### Option A: Rocket.Chat

1. Log in to your Rocket.Chat server as an administrator
2. Click the avatar menu → **Administration** → **Users**
3. Click **New User**
4. Fill in:
   - **Name:** (e.g., "Agent Bot")
   - **Username:** (e.g., `agent-bot`) — **Keep this handy; you'll need it**
   - **Email:** (any valid email address)
   - **Password:** (e.g., auto-generate or set a strong password) — **Keep this handy**
5. Uncheck **Require password change** (optional)
6. Check **Verified email** and **Active** (optional but recommended)
7. Click **Save User**
8. Add the bot to at least one room or DM (you can do this later)

### Option B: Mattermost

1. Create the bot account — either works:
   - **Bot Account** (recommended): System Console → Integrations → Bot Accounts → **Add Bot Account**, or via CLI: `mmctl --local bot create --username agent-bot --display-name "Agent Bot"`. This issues an access token directly — **keep it handy; you'll need it**, it's shown only once.
   - **Regular account + username/password**: create (or reuse) a normal Mattermost user account instead, if you'd rather authenticate with credentials than a token.
2. **Add the bot to the team** your channels live in — this is required and easy to miss (Mattermost channels are team-scoped, unlike Rocket.Chat rooms): `mmctl --local team add <team-name> <bot-username>`, or via System Console.
3. Add the bot to at least one channel (you can do this later)
4. **Keep handy:** the bot's access token (or username/password), and the **team name** (the URL slug, not the display name) — you'll need both in Step 3.

---

## Step 3: Create the Configuration

Create a directory for the gateway's runtime files:
```bash
mkdir -p ~/.agent-chat-gateway
```

### Create `config.yaml` file

Credentials go directly into `config.yaml` below — no separate `.env` file needed.
`agent-chat-gateway` chmods `config.yaml` to `0600` automatically (on every save via
the config TUI, and on every `agent-chat-gateway start`), so this is safe as long as
you don't commit your filled-in copy to version control.

> Coming from an older setup that used a `.env` file? Nothing to do — the next
> `agent-chat-gateway start` folds it into `config.yaml` automatically and removes
> it (one-time). Run `agent-chat-gateway config migrate-env` first if you'd rather
> do it manually / as a dry run.

**Rocket.Chat:**
```bash
cat > ~/.agent-chat-gateway/config.yaml << 'EOF'
connectors:
  - name: rc-home
    type: rocketchat
    server:
      url: https://your-rocket-chat-server.com
      username: agent-bot
      password: your-bot-password
    allowed_users:
      owners:
        - your-username
      guests: []
    attachments:
      max_file_size_mb: 10
      download_timeout: 30
    reply_in_thread: false
    permission_reply_in_thread: true    # post approval requests inside a thread
    # No context_inject_files needed here — the RC-specific gateway context
    # (message format, RBAC rules, how to send files) is injected automatically.

agents:
  my-agent:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    new_session_args: []                # extra CLI flags passed when creating a new session
    session_prefix: "agent-chat"
    context_inject_files: []            # agent-level extra context (usually empty)

    # ── Tool allow-lists ───────────────────────────────────────────────────
    # Tools matched here are auto-approved for that role — no RC notification.
    # Anything NOT matched triggers the human-in-the-loop approval flow.
    # Each entry: tool (regex on tool name) + optional params (regex on primary param).
    owner_allowed_tools:
      - tool: "Read"
      - tool: "Glob"
      - tool: "Grep"
      - tool: "WebSearch"
      - tool: "WebFetch"
        params: "https?://(www\\.)?github\\.com/.*"
      - tool: "WebFetch"
        params: "https?://[^/]*\\.wikipedia\\.org/.*"
      - tool: "Bash"
        params: "git (log|diff|status|show)( .*)?"   # read-only git
      - tool: "Bash"
        params: "ls( .*)?"
      - tool: "Bash"
        params: "agent-chat-gateway\\s+send\\s+.*"   # agent-initiated file send to RC

    guest_allowed_tools:
      - tool: "Read"
      - tool: "Glob"
      - tool: "Grep"
      - tool: "WebFetch"
        params: "https?://[^/]*\\.wikipedia\\.org/.*"
      - tool: "Bash"
        params: "agent-chat-gateway\\s+send\\s+.*"

    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watchers:
  - name: dm-me
    connector: rc-home
    room: "@your-username"
    agent: my-agent
EOF
```

**Mattermost** (same `agents:`/`watchers:` shape — only the `connectors:` block and the
watcher's `connector:`/`room:` values differ from the Rocket.Chat example above):
```bash
cat > ~/.agent-chat-gateway/config.yaml << 'EOF'
connectors:
  - name: mm-home
    type: mattermost
    server:
      url: https://your-mattermost-server.com
      team: your-team-name
      token: your-bot-access-token
      # username: your-bot-username   # use this + password instead of token, not both
      # password: your-bot-password
    allowed_users:
      owners:
        - your-username
      guests: []
    attachments:
      max_file_size_mb: 10
      download_timeout: 30
    reply_in_thread: false
    permission_reply_in_thread: true    # post approval requests inside a thread
    # No context_inject_files needed here — the Mattermost-specific gateway context
    # (message format, RBAC rules, how to send files) is injected automatically.

agents:
  my-agent:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    new_session_args: []                # extra CLI flags passed when creating a new session
    session_prefix: "agent-chat"
    context_inject_files: []            # agent-level extra context (usually empty)

    # ── Tool allow-lists ───────────────────────────────────────────────────
    # Tools matched here are auto-approved for that role — no chat notification.
    # Anything NOT matched triggers the human-in-the-loop approval flow.
    # Each entry: tool (regex on tool name) + optional params (regex on primary param).
    owner_allowed_tools:
      - tool: "Read"
      - tool: "Glob"
      - tool: "Grep"
      - tool: "WebSearch"
      - tool: "WebFetch"
        params: "https?://(www\\.)?github\\.com/.*"
      - tool: "WebFetch"
        params: "https?://[^/]*\\.wikipedia\\.org/.*"
      - tool: "Bash"
        params: "git (log|diff|status|show)( .*)?"   # read-only git
      - tool: "Bash"
        params: "ls( .*)?"
      - tool: "Bash"
        params: "agent-chat-gateway\\s+send\\s+.*"   # agent-initiated file send to chat

    guest_allowed_tools:
      - tool: "Read"
      - tool: "Glob"
      - tool: "Grep"
      - tool: "WebFetch"
        params: "https?://[^/]*\\.wikipedia\\.org/.*"
      - tool: "Bash"
        params: "agent-chat-gateway\\s+send\\s+.*"

    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watchers:
  - name: dm-me
    connector: mm-home
    room: "@your-username"
    agent: my-agent
EOF
```

> By default a watcher doesn't announce online/offline in chat. Watching more than one
> room with the same connector+agent? Use `rooms: [a, b, ...]` instead of `room:` and
> one entry expands into a watcher per room automatically — see Step 7 below and
> `config.example.yaml` for the full annotated format (including `tool_presets` and
> `*_templates`/`inherits:` for larger multi-bot setups).

**Replace:**
- `your-username` — your username on the chosen chat platform (the one who will own the bot)
- `your-rocket-chat-server.com` / `your-mattermost-server.com` — your server URL
- `your-team-name` (Mattermost only) — the team's URL slug (not its display name) that your channels live in
- `~/.agent-chat-gateway/work` — **the project folder where Claude Code or OpenCode will run tasks and create files**. Default to the current project directory (`pwd`); ask the user to confirm or change it before proceeding.
- If using OpenCode instead of Claude, change `type: claude` and `command: claude` to `type: opencode` and `command: opencode`

> **About context injection:** The built-in gateway context (message format, RBAC rules,
> how to send files — `rc-gateway-context.md` or `mm-gateway-context.md`, matched
> automatically to the connector's `type`) is injected on every session with no config
> needed. `context_inject_files` is only for your own additional context, if any.

> **About `owner_allowed_tools`:** Any tool call not matched by these rules triggers a
> human-in-the-loop approval notification in chat. Adjust the list to match your security needs —
> the example above allows common read-only tools and safe bash commands. See
> `config.example.yaml` for more patterns and documentation.

Create the working directory if it doesn't already exist:
```bash
mkdir -p <working_directory>
```

---

## Step 4: Start the Daemon

```bash
agent-chat-gateway start
```

You should see output indicating the daemon is starting. If there are any errors, check the log:
```bash
tail -f ~/.agent-chat-gateway/gateway.log
```

---

## Step 5: Verify It's Running

Check the status:
```bash
agent-chat-gateway status
```

Expected output:
```
Gateway:  running (pid=12345)
Uptime:   0h 0m 15s
PID file: /Users/yourname/.agent-chat-gateway/gateway.pid
Log file: /Users/yourname/.agent-chat-gateway/gateway.log
Watchers: 1
```

List watchers:
```bash
agent-chat-gateway list
```

Expected output:
```
dm-me: (rc-home) @your-username [my-agent] session=agent-chat-xxxx [active]
```

---

## Step 6: Send a Test Message

1. Open your chat platform and go to your direct message with the bot (or the configured room/channel)
2. Send a message (e.g., "Hello, what can you do?")
3. The agent should respond in a few seconds
4. If you don't see a response, check the logs: `tail -f ~/.agent-chat-gateway/gateway.log`

---

## Step 7: Add More Watchers (Optional)

To monitor additional rooms/channels:

1. Add the bot to the room (Rocket.Chat) or channel (Mattermost — must also already be a
   team member, see Step 2)
2. Edit `~/.agent-chat-gateway/config.yaml` and add the room to your watcher. If you're
   watching several rooms with the same connector+agent, use `rooms:` instead of adding a
   whole new entry per room — it expands into one watcher per room automatically, naming
   each one `<connector>-<room>`:
   ```yaml
   - connector: rc-home   # or mm-home
     agent: my-agent
     rooms: ["general", "dev"]   # bare channel name, no leading "#", on either platform
   ```
   A one-off room still works as a single entry with `room:` instead of `rooms:` — see
   `config.example.yaml` for the full annotated format, including `name:` for pinning a
   specific watcher's identity (needed if you rely on its session surviving a config edit).
3. Validate before restarting: `agent-chat-gateway config validate --config ~/.agent-chat-gateway/config.yaml`
4. Restart the daemon: `agent-chat-gateway restart`

---

## Next Steps

- **User Guide:** See `docs/user-guide.md` for how to use the system, manage permissions, and handle approvals
- **Architecture:** See `docs/architecture.md` for an overview of how the system works internally
- **Requirements:** See `docs/requirements.md` for the detailed functional specification
- **Troubleshooting:** Check `gateway.log` for errors; common issues are usually permission-related or credential mismatches

---

## Common Commands

| Command | Purpose |
|---------|---------|
| `agent-chat-gateway start` | Start the daemon |
| `agent-chat-gateway stop` | Stop the daemon |
| `agent-chat-gateway status` | Show status and uptime |
| `agent-chat-gateway list` | List all watchers |
| `agent-chat-gateway pause WATCHER` | Pause a watcher |
| `agent-chat-gateway resume WATCHER` | Resume a paused watcher |
| `agent-chat-gateway reset WATCHER` | Reset a watcher session |
| `agent-chat-gateway send ROOM MESSAGE` | Send a text message to a room |
| `agent-chat-gateway send ROOM --file FILE` | Send message from a file (or `-` for stdin) |
| `agent-chat-gateway send ROOM --attach FILE` | Upload a file attachment to a room |
| `tail -f ~/.agent-chat-gateway/gateway.log` | View logs |
| `agent-chat-gateway onboard` | Interactive setup wizard (alternative to manual config) |
