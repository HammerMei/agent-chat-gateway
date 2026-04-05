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
- **Rocket.Chat server** with a dedicated bot account
  - You'll need bot username and password
  - You'll need Rocket.Chat admin or room owner access to add the bot to rooms
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

## Step 2: Create a Bot Account in Rocket.Chat

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

---

## Step 3: Create the Configuration

Create a directory for the gateway's runtime files:
```bash
mkdir -p ~/.agent-chat-gateway
```

### Create `.env` file

Store sensitive credentials:
```bash
cat > ~/.agent-chat-gateway/.env << 'EOF'
RC_URL=https://your-rocket-chat-server.com
RC_USERNAME=agent-bot
RC_PASSWORD=your-bot-password
EOF
chmod 600 ~/.agent-chat-gateway/.env
```

Replace the values with your Rocket.Chat server URL and bot credentials.

### Create `config.yaml` file

```bash
cat > ~/.agent-chat-gateway/config.yaml << 'EOF'
connectors:
  - name: rc-home
    type: rocketchat
    server:
      url: $RC_URL
      username: $RC_USERNAME
      password: $RC_PASSWORD
    allowed_users:
      owners:
        - your-username
      guests: []
    attachments:
      max_file_size_mb: 10
      download_timeout: 30
    reply_in_thread: false
    permission_reply_in_thread: true    # post approval requests inside a thread
    context_inject_files:
      - contexts/rc-gateway-context.md  # injected once per session: RC format, RBAC rules

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
    session_id: null
    context_inject_files: []            # watcher-level extra context (usually empty)
    online_notification: "✅ _Agent online_"
    offline_notification: "❌ _Agent offline_"
EOF
```

**Replace:**
- `your-username` — your Rocket.Chat username (the one who will own the bot)
- `your-rocket-chat-server.com` — your Rocket.Chat server URL
- `~/.agent-chat-gateway/work` — **the project folder where Claude Code or OpenCode will run tasks and create files**. Default to the current project directory (`pwd`); ask the user to confirm or change it before proceeding.
- If using OpenCode instead of Claude, change `type: claude` and `command: claude` to `type: opencode` and `command: opencode`

> **About `context_inject_files`:** The file `contexts/rc-gateway-context.md` is copied to
> `~/.agent-chat-gateway/contexts/` by the installer. It tells the agent about the RC message
> format, RBAC rules, and how to send files. Paths are resolved relative to `~/.agent-chat-gateway/`.

> **About `owner_allowed_tools`:** Any tool call not matched by these rules triggers a
> human-in-the-loop approval notification in RC. Adjust the list to match your security needs —
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

1. Open Rocket.Chat and go to your direct message with the bot (or the configured room)
2. Send a message (e.g., "Hello, what can you do?")
3. The agent should respond in a few seconds
4. If you don't see a response, check the logs: `tail -f ~/.agent-chat-gateway/gateway.log`

---

## Step 7: Add More Watchers (Optional)

To monitor additional rooms:

1. Add the bot to the room in Rocket.Chat
2. Edit `~/.agent-chat-gateway/config.yaml` and add a new watcher entry:
   ```yaml
   - name: dev-room
     connector: rc-home
     room: "#dev"
     agent: my-agent
     session_id: null
   ```
3. Restart the daemon: `agent-chat-gateway restart`

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
