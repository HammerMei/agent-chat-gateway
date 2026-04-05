# agent-chat-gateway User Guide

## What is agent-chat-gateway?

Inspired by [OpenClaw](https://github.com/openclaw/openclaw)'s vision of making AI agents accessible from any messaging app, `agent-chat-gateway` bridges your existing AI agent tools — Claude CLI, OpenCode, or any custom backend — to your team's chat platform. When someone messages the bot in a watched room, the message is forwarded to the configured agent and the response is posted back.

Think of it as a persistent bridge: set up once, configure which rooms to watch, and your AI assistant becomes available across your entire Rocket.Chat workspace — with full support for role-based access control, human-in-the-loop permission approvals, and file attachments.

> **How it compares to Claude Code Channels:** Claude Code's native [Channels](https://code.claude.com/docs/en/channels) feature (v2.1.80+) lets a single Claude Code session receive messages from Telegram, Discord, or iMessage — a great fit for personal use. `agent-chat-gateway` was developed independently before Channels shipped and targets a different layer: team deployments with multiple agent backends (not just Claude Code), Owner/Guest roles with per-tool allow-lists, and a shared workspace where multiple people can interact with the same or different agents across multiple rooms simultaneously.

### Key Concepts

| Term | Meaning |
|--|--|
| **Connector** | Adapter for a chat platform (e.g., Rocket.Chat). Handles incoming messages and posts replies back. |
| **Agent backend** | The AI tool being dispatched to (e.g., Claude CLI subprocess, OpenCode subprocess). |
| **Watcher** | A binding between one chat room and one agent backend. One watcher per room. |

---

## Prerequisites

Before installing, ensure you have:

- **Python 3.12 or later** — verify with `python3 --version`
- **Rocket.Chat server access** — your workspace URL and bot account credentials
- **Claude CLI or OpenCode installed** — at least one agent backend available
  - Claude CLI: https://claude.ai/download
  - OpenCode: https://github.com/anthropics/opencode
- **A Rocket.Chat bot account** — with permissions to post messages and read room history
- **At least one owner username** — someone who can approve/deny tool calls in chat

---

## Installation

For detailed installation instructions, see [install-agent.md](install-agent.md).

Quick summary:
```bash
pip install agent-chat-gateway
mkdir -p ~/.agent-chat-gateway
# Create config.yaml and .env (see Configuration section below)
agent-chat-gateway start
```

---

## Quick Start

### Minimal Working Config

Create `~/.agent-chat-gateway/config.yaml`:

```yaml
connectors:
  - name: rc-home
    type: rocketchat
    server:
      url: "https://chat.example.com"
      username: "mybot"
      password: "${RC_PASSWORD}"
    allowed_users:
      owners:
        - alice
      guests: []

agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watchers:
  - name: general
    connector: rc-home
    room: general
    agent: claude
```

### Start the Daemon

```bash
# Set the password via environment
export RC_PASSWORD="your_bot_password"

# Start the gateway
agent-chat-gateway start

# Check status
agent-chat-gateway status
```

### Send a Message

```bash
# Direct message (bypasses the agent)
agent-chat-gateway send general "Hello from the CLI"

# Or read from stdin
echo "Hello from stdin" | agent-chat-gateway send general -
```

---

## Use Cases

### Use Case 1 — Build a Super-Powered Chatbot for Your Team

Connect your existing agent to multiple rooms with different roles and responsibilities. Use context injection to give each room its own system prompt, and enable human-in-the-loop approval so sensitive operations always require owner sign-off.

**Example: Engineering team with Claude for general chat and OpenCode for development**

```yaml
connectors:
  - name: rc-company
    type: rocketchat
    server:
      url: "${RC_URL}"
      username: "${RC_BOT_USER}"
      password: "${RC_BOT_PASS}"
    allowed_users:
      owners:
        - alice
        - bob
      guests:
        - charlie
        - dan

default_agent: claude

agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300
    owner_allowed_tools:
      - tool: "Read"
      - tool: "WebFetch"
        params: "https?://(www\\.)?github\\.com/.*"
      - tool: "Bash"
        params: "git (log|diff|status|show).*"
    guest_allowed_tools:
      - tool: "Read"
      - tool: "Glob"

  opencode:
    type: opencode
    command: opencode
    working_directory: ~/.agent-chat-gateway/opencode-work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watchers:
  - name: general
    connector: rc-company
    room: general
    agent: claude
    context_inject_files:
      - contexts/team-assistant.md    # team-specific system prompt

  - name: dev
    connector: rc-company
    room: dev
    agent: opencode
    context_inject_files:
      - contexts/engineering-context.md

  - name: support
    connector: rc-company
    room: support
    agent: claude
    context_inject_files:
      - contexts/support-runbook.md
```

**Key settings for this use case:**
- Set `permissions.enabled: true` to keep humans in the loop for sensitive tool calls
- Use `context_inject_files` at the watcher level for room-specific personas or knowledge bases
- Use `guest_allowed_tools` to restrict what non-owners can ask the agent to do
- Add a room profiles context file so the agent knows who it's talking to — see the
  [Room Member Profiles](#example-room-member-profiles) section below and the template at
  [`contexts/rc-room-profiles.example.md`](../contexts/rc-room-profiles.example.md)

---

### Use Case 2 — Access Your Agent from a Messaging App

Already have Claude CLI or OpenCode running on your machine? Expose it to your team via Rocket.Chat with minimal configuration. No special RBAC setup needed if it's just you — set yourself as the sole owner and you're good to go.

**Example: Single-user personal agent bridge**

```yaml
connectors:
  - name: rc-personal
    type: rocketchat
    server:
      url: "${RC_URL}"
      username: "${RC_BOT_USER}"
      password: "${RC_BOT_PASS}"
    allowed_users:
      owners:
        - alice          # Only you — no guests

default_agent: claude

agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/my-agent-work
    timeout: 360
    permissions:
      enabled: false     # Skip approval prompts for personal use

watchers:
  - name: my-assistant
    connector: rc-personal
    room: "@alice"       # DM room — only you can see it
    agent: claude
    online_notification: "✅ Agent ready"
```

**Key settings for this use case:**
- Use `room: "@username"` to watch a DM room instead of a channel — keeps it private
- Set `permissions.enabled: false` for personal use where approval friction isn't needed
- Set yourself as the sole owner; omit `guests` entirely

> **Similar to Claude Code Channels:** Claude Code's [Channels](https://code.claude.com/docs/en/channels) feature (v2.1.80+) also connects external platforms (Telegram, Discord, iMessage) to a local Claude Code session via `claude --channels`. The key differences: Channels is Claude Code-specific and single-user focused, while `agent-chat-gateway` supports any agent backend (Claude CLI, OpenCode, custom), multi-user RBAC, and is designed for team-shared Rocket.Chat workspaces. If you only use Claude Code and only need personal access, Channels may be simpler to set up; if you need team access or a different agent backend, `agent-chat-gateway` is the better fit.

---

### Use Case 3 — Continue an Existing Agent Session Remotely

If you have a long-running agent session already in progress (e.g., a Claude session you started locally), pin a watcher to that session ID so your messaging app picks up exactly where you left off. The session context, memory, and history are all preserved.

> **Similar to Claude Code's Remote Control:** Claude Code's [Remote Control](https://code.claude.com/docs/en/remote-control) feature (`claude --remote-control`) lets you drive a local session from `claude.ai/code` or the Claude mobile app. `agent-chat-gateway` takes a complementary approach: instead of a personal remote interface, your session becomes accessible from your team's shared chat room — with RBAC and permission approval so others can interact safely too.

**Example: Resume an existing Claude session by session ID**

```yaml
connectors:
  - name: rc-home
    type: rocketchat
    server:
      url: "${RC_URL}"
      username: "${RC_BOT_USER}"
      password: "${RC_BOT_PASS}"
    allowed_users:
      owners:
        - alice

default_agent: claude

agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/my-project
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watchers:
  - name: my-project
    connector: rc-home
    room: "@alice"
    agent: claude
    session_id: "ses_abc123def456"    # Pin to your existing session
```

**How to find your existing session ID:**

For Claude CLI, session IDs appear in the output when you run `claude -p`:
```
{"type": "result", "session_id": "ses_abc123def456", ...}
```

Or check your Claude session history directly.

**Key settings for this use case:**
- Set `session_id` to your existing session's ID to resume it from your messaging app
- The gateway will never overwrite a sticky `session_id` — it survives daemon restarts and `reset` commands
- If you want to start fresh later, either remove the `session_id` field or run `agent-chat-gateway reset <watcher>` (only affects non-sticky sessions)

**Tip — Resume workflow:**

```bash
# 1. Start a session locally and note the session ID
claude -p "Let's start working on the auth module"
# → {"session_id": "ses_abc123def456", ...}

# 2. Add that session ID to your config.yaml under the watcher
# 3. Start the gateway
agent-chat-gateway start

# 4. Continue the conversation from Rocket.Chat on your phone or another machine
```

---

## Configuration Reference

### Top-Level Configuration

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `default_agent` | string | No | (none) | Default agent for watchers that don't specify one |
| `max_queue_depth` | integer | No | 100 | Per-room message queue size; 0 = unlimited |
| `connectors` | list | Yes | (none) | Chat platform connections |
| `agents` | dict | Yes | (none) | AI agent backend definitions |
| `watchers` | list | Yes | (none) | Room→agent mappings |

### Connectors

Each connector represents a connection to one chat platform (currently only Rocket.Chat is supported).

```yaml
connectors:
  - name: rc-main                    # Unique identifier
    type: rocketchat                 # Only type currently supported
    server:
      url: "https://chat.example.com"
      username: "bot-username"
      password: "${RC_PASSWORD}"      # Use env-var expansion for secrets
    allowed_users:
      owners:
        - alice                       # Full access; approve/deny tool calls
        - bob
      guests:                         # Restricted access; guest tool allow-list only
        - charlie
    attachments:
      max_file_size_mb: 50           # 0 = no limit
      download_timeout: 30            # Seconds
      cache_dir_global: ~/.agent-chat-gateway/attachments  # connector-global cache directory
    reply_in_thread: false            # Start new thread for replies
    permission_reply_in_thread: true  # Post permission requests in thread
    context_inject_files: []          # Files sent to agent on session start
```

**Connector Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique connector identifier (used in CLI commands and watcher references) |
| `type` | string | Yes | Platform type; currently only `rocketchat` |
| `server.url` | string | Yes | Rocket.Chat server URL (e.g., `https://chat.example.com`) |
| `server.username` | string | Yes | Bot account username |
| `server.password` | string | Yes | Bot account password (use `${VAR}` for env expansion) |
| `allowed_users.owners` | list | No | Usernames with full tool access |
| `allowed_users.guests` | list | No | Usernames with restricted tool access |
| `attachments.max_file_size_mb` | integer | No | Maximum file size; 0 = unlimited |
| `attachments.download_timeout` | integer | No | Seconds to wait per file download |
| `attachments.cache_dir_global` | string | No | Download cache directory (default: `~/.agent-chat-gateway/attachments`; only needed to override the default) |
| `reply_in_thread` | boolean | No | Reply in thread for every message |
| `permission_reply_in_thread` | boolean | No | Post permission requests in threads |
| `context_inject_files` | list | No | Context files for all sessions on this connector |

### Agents

Each agent backend represents a CLI tool (Claude, OpenCode, etc.) that the gateway can dispatch to.

```yaml
agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    new_session_args: []
    session_prefix: "agent-chat"
    context_inject_files: []

    owner_allowed_tools:
      - tool: "Read"
      - tool: "Bash"
        params: "git (log|diff|status).*"
      - tool: "WebFetch"
        params: "https?://.*github.*"

    guest_allowed_tools:
      - tool: "Read"
      - tool: "Glob"

    timeout: 360
    permissions:
      enabled: true
      timeout: 300
      skip_owner_approval: false
```

**Agent Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | Backend type: `claude` or `opencode` |
| `command` | string | Yes | CLI command to invoke (e.g., `claude`, `opencode`) |
| `working_directory` | string | Yes | Working directory for the agent subprocess |
| `new_session_args` | list | No | Extra CLI args for new sessions |
| `session_prefix` | string | No | Prefix for session titles |
| `context_inject_files` | list | No | Context files injected on every session |
| `owner_allowed_tools` | list | No | Auto-approved tools for owners (see Tool Allow-Lists below) |
| `guest_allowed_tools` | list | No | Auto-approved tools for guests (see Tool Allow-Lists below) |
| `timeout` | integer | Yes | Seconds to wait for agent response (must be > `permissions.timeout`) |
| `permissions.enabled` | boolean | No | Enable human-in-the-loop tool approval |
| `permissions.timeout` | integer | No | Seconds before auto-denying unanswered requests (must be < agent `timeout`) |
| `permissions.skip_owner_approval` | boolean | No | If `true`, owners bypass approval prompts (guests still enforced); only use in trusted sandbox environments |

### Watchers

Each watcher binds a Rocket.Chat room to an AI agent backend.

```yaml
watchers:
  - name: general-assistant
    connector: rc-main
    room: general
    agent: claude
    session_id: null
    context_inject_files: []
    online_notification: "✅ _Agent online_"
    offline_notification: "❌ _Agent offline_"
```

**Watcher Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique watcher identifier (used in CLI commands) |
| `connector` | string | Yes | Must match a connector name above |
| `room` | string | Yes | Rocket.Chat room name or `@username` for DMs |
| `agent` | string | No | Agent backend to use; falls back to `default_agent` if omitted |
| `session_id` | string | No | Optional sticky session ID (e.g., `ses_abc123`); `null` = auto-create |
| `context_inject_files` | list | No | Watcher-specific context files |
| `online_notification` | string | No | Message posted when this watcher starts; `~` to suppress |
| `offline_notification` | string | No | Message posted when this watcher stops; `~` to suppress |

### Tool Allow-Lists

`owner_allowed_tools` and `guest_allowed_tools` control which tools can be used without requiring permission approval.

Each entry is an object with:
- **`tool`** (required) — regex matched against the tool name
- **`params`** (optional) — regex matched against the tool's primary parameter

**Example:**

```yaml
owner_allowed_tools:
  - tool: "Read"                      # Allow all Read calls
  - tool: "Bash"
    params: "git (log|diff|status).*"  # Allow specific git commands
  - tool: "WebFetch"
    params: "https?://.*github\\.com.*"  # Allow GitHub only
  - tool: ".*"                        # Allow all tools (use with care!)

guest_allowed_tools:
  - tool: "Read"
    params: ".*\\.md$"                # Allow markdown files only
  - tool: "Glob"                      # Allow any glob pattern
```

**Parameter Extraction:**

- **Bash** — `tool_input["command"]` (the shell command)
- **WebFetch** — `tool_input["url"]` (the full URL)
- **Read/Edit/Write** — `tool_input["file_path"]` (the file path)
- **MCP/Unknown tools** — full `tool_input` serialized as JSON

**Security Note:**

Always use explicit domain patterns to prevent SSRF attacks. Avoid `params: ".*"` for WebFetch:

```yaml
# ❌ DANGEROUS — allows localhost, internal networks
- tool: "WebFetch"
  params: ".*"

# ✅ SAFE — explicit whitelist
- tool: "WebFetch"
  params: "https?://(www\\.)?github\\.com/.*"
```

### Environment Variables

The gateway supports `$VAR` and `${VAR}` expansion in string fields. Create a `.env` file next to your `config.yaml`:

```bash
# ~/.agent-chat-gateway/.env
RC_PASSWORD=your_bot_password
RC_USERNAME=mybot
RC_URL=https://chat.example.com
```

Then reference them in your config:

```yaml
server:
  url: "${RC_URL}"
  username: "${RC_USERNAME}"
  password: "${RC_PASSWORD}"
```

---

## CLI Commands

### Daemon Lifecycle

```bash
# Start the daemon
agent-chat-gateway start [--config path/to/config.yaml]

# Stop the daemon
agent-chat-gateway stop

# Restart (picks up config and code changes)
agent-chat-gateway restart [--config path/to/config.yaml]

# Check status
agent-chat-gateway status
```

### Watcher Control

All commands require the daemon to be running.

```bash
# List all active watchers
agent-chat-gateway list [--connector NAME]

# Pause a watcher (stops processing messages)
agent-chat-gateway pause <watcher-name> [--connector NAME]

# Resume a paused watcher
agent-chat-gateway resume <watcher-name> [--connector NAME]

# Reset a watcher (clear state, create new session)
agent-chat-gateway reset <watcher-name> [--connector NAME]
```

### Direct Messaging

Send messages directly to a room (bypasses the agent):

```bash
# Send text directly
agent-chat-gateway send <room> "message text"

# Read from stdin
echo "Hello" | agent-chat-gateway send <room> -

# Send from file
agent-chat-gateway send <room> --file message.txt

# Attach files
agent-chat-gateway send <room> --attach document.pdf --file caption.txt

# Specify connector
agent-chat-gateway send <room> --connector rc-main "message"
```

### Setup

```bash
# Interactive setup wizard (creates config interactively)
agent-chat-gateway onboard [--repo-path PATH]

# Check for and install updates
agent-chat-gateway upgrade
```

---

## Role-Based Access Control

### Three Roles

**Owner** — Configured in `allowed_users.owners`
- Full tool access subject to `owner_allowed_tools` and permission approval
- Can approve or deny pending permission requests in chat
- Can use all agent features

**Guest** — Configured in `allowed_users.guests`
- Restricted to `guest_allowed_tools` only
- Tools not in the allow-list are auto-denied without owner notification
- Cannot approve/deny requests

**Anonymous** — Not in either list
- Messages are rejected entirely
- No access to the agent

### How It Works

Every message sent to the agent is prefixed with a trusted header:

```
[Rocket.Chat #<room> | from: <username> | role: owner|guest]  <message text>
```

The agent uses this header to determine:
1. Who sent the message
2. What tools they can use
3. Whether to require approval for sensitive operations

---

## User-Aware Responses

Every message the gateway forwards to the agent is prefixed with a trusted header:

```
[Rocket.Chat #general | from: alice | role: owner]  Hey, can you review this PR?
```

The `from: <username>` field tells the agent exactly who sent the message. Combined with a
**room profiles context file**, the agent can greet people by name, reply in their preferred
language, match their communication style, and adjust detail level based on their background —
automatically, for every message.

### How It Works

1. **The gateway injects** sender identity and role on every message (trusted, cannot be spoofed)
2. **The agent reads** the `from:` field to look up the sender's profile
3. **The agent personalizes** tone, language, and response style accordingly

The profile file is just a plain text context file you inject at session start — no code
changes needed.

### Example Profile File

Create a file like `contexts/rc-room-profiles.md` (you can copy the template from
[`contexts/rc-room-profiles.example.md`](../contexts/rc-room-profiles.example.md)):

```markdown
## Rocket.Chat Room Profiles

**IMPORTANT — scope:** The profiles below apply **only** when interacting via the
Rocket.Chat gateway (i.e., when the `[Rocket.Chat #<room> | from: <username> | role: ...]`
message prefix is present). Do NOT apply these profiles during CLI/terminal sessions.

Cross-reference the `from: <username>` field in the message prefix with the profiles
below to personalize your tone, language, and response style for each person in the room.

---

### alice
- **Display name:** Alice
- **Title:** Engineering Lead
- **Language:** English
- **Notes:** Prefers concise technical answers. Comfortable with code snippets.
  Appreciates bullet points over paragraphs.

### bob
- **Display name:** Bob
- **Title:** Product Manager
- **Language:** English
- **Notes:** Non-technical — avoid jargon, use plain language and analogies.
  Focuses on business impact, not implementation details.

### charlie
- **Display name:** Charlie
- **Language:** English / Traditional Chinese (reply in whichever language Charlie writes in)
- **Notes:** Guest role. Primarily asks questions about docs and project status.
  Keep responses factual; do not share internal system details.
```

Then add it to your config. `rc-gateway-context.md` belongs at the **connector level** so it
applies to every room automatically. Room profiles are **watcher-level** since each room has
its own set of people:

```yaml
connectors:
  - name: rc-main
    ...
    context_inject_files:
      - contexts/rc-gateway-context.md   # Gateway behavior rules — shared across all rooms

watchers:
  - name: general
    connector: rc-main
    room: general
    agent: claude
    context_inject_files:
      - contexts/rc-room-profiles.md     # Room member profiles — specific to this room
```

Restart or reset the watcher to load the new context:

```bash
agent-chat-gateway reset general
```

> **Tip:** `contexts/rc-gateway-context.md` (included in the repo) sets up baseline gateway
> behavior: message format parsing, injection protection, response length, and guest access
> rules. Placing it at the connector level ensures every room benefits from it without
> repeating it in each watcher.

---

## Permission Approval System

When `permissions.enabled: true` and a user attempts a tool call not in their allow-list, the gateway intercepts it and requires explicit approval from an owner.

### How It Works

1. **User sends message** → Agent attempts a tool call (e.g., `Bash`)
2. **Gateway intercepts** → Pauses the agent, posts approval request to chat:
   ```
   🔐 **Permission required** [a3k9]
   **Tool:** Bash
   **Params:** command='rm ./build'
   Reply: approve a3k9 / deny a3k9
   ```
3. **Owner responds** → Types `approve a3k9` or `deny a3k9` in the chat
4. **Gateway resolves** → Agent either executes or skips the tool call
5. **Message continues** → Agent response is posted back to the room

### Responding to Permission Requests

In the Rocket.Chat chat room, type directly (no slash prefix):

```
approve a3k9    ← Allow the tool call to proceed
deny a3k9       ← Block the tool call
```

**Important:** These are NOT slash commands. If you type `/approve`, Rocket.Chat's client will intercept it before reaching the gateway.

### Error Messages

- **Invalid ID length** — `⚠️ Invalid ID — expected 4 characters`
- **Unknown request ID** — `⚠️ No pending permission request with ID`
- **Request timed out** — `⏱️ Permission a3k9 timed out — auto-denied.`

### Timeout Behavior

If no approval arrives within `permissions.timeout` seconds, the request is automatically denied:

```yaml
permissions:
  timeout: 300  # 5 minutes
```

### Message Queuing

While a permission request is pending, new messages are queued and will not be processed until the pending request is resolved. Only `approve`/`deny` commands bypass the queue.

### Disabling Approval for Owners

In sandbox environments where interactive approval isn't practical, you can skip owner approval:

```yaml
permissions:
  enabled: true
  skip_owner_approval: true  # ⚠️ WARNING: Disables human-in-the-loop for owners only
  timeout: 300
```

With `skip_owner_approval: true`:
- **Owners:** All tool calls are auto-approved (no RC notification)
- **Guests:** Still subject to `guest_allowed_tools` enforcement

Only use this in trusted, sandboxed environments where interactive approval is not feasible.

---

## Context Files

Context files are injected into the agent session to provide domain knowledge, system prompts, or other guidance. Three levels of context are supported:

### Three-Level Injection

1. **Connector-level** (`connectors[].context_inject_files`) — Shared across all watchers on this connector
2. **Agent-level** (`agents[].context_inject_files`) — Applied to all sessions using this agent
3. **Watcher-level** (`watchers[].context_inject_files`) — Specific to this watcher's session

Files are injected in this order, so watcher-level context overrides agent-level, which overrides connector-level.

### Example

```yaml
connectors:
  - name: rc-main
    context_inject_files:
      - docs/connector-context.txt   # Layer 1: shared context

agents:
  claude:
    context_inject_files:
      - docs/system-prompt.txt       # Layer 2: agent instructions

watchers:
  - name: general
    context_inject_files:
      - docs/domain-context.txt      # Layer 3: room-specific context
```

### Context File Format

Context files are plain text or Markdown. Include them as-is in the agent prompt:

```
# contexts/system-prompt.md
You are an assistant for our engineering team.
- Be concise in responses
- Prioritize code review over feature requests
- Reference our GitHub repo when suggesting changes
```

### Example: Room Member Profiles

A common pattern is to add a profiles context file that tells the agent who is in the room —
their display name, title, language preference, and communication style. This lets the agent
personalize its tone and language for each person automatically.

The repo ships with a ready-to-use template at
[`contexts/rc-room-profiles.example.md`](../contexts/rc-room-profiles.example.md).

To use it:

```bash
# Copy and customize the template
cp contexts/rc-room-profiles.example.md contexts/rc-room-profiles.md
# Edit rc-room-profiles.md with your team's actual profiles
```

Then reference it in your config. `rc-gateway-context.md` belongs at the **connector level**
(shared across all rooms); room profiles are **watcher-level** (room-specific):

```yaml
connectors:
  - name: rc-main
    ...
    context_inject_files:
      - contexts/rc-gateway-context.md   # Gateway behavior rules — shared across all rooms

watchers:
  - name: general
    connector: rc-main
    room: general
    agent: claude
    context_inject_files:
      - contexts/rc-room-profiles.md     # Room member profiles — specific to this room
```

`contexts/rc-gateway-context.md` (included in the repo) sets up baseline gateway behavior:
response length, message format parsing, injection protection, and guest access rules.
Placing it at the connector level means you only need to list it once, and every room
on that connector automatically benefits from it.

### Limits

- **Per-file:** 256 KB maximum
- **Total:** 512 KB maximum across all three levels

If context files exceed the limit, the gateway will log a warning and skip the largest files.

---

## Attachment Handling

When users upload files to Rocket.Chat, the gateway automatically downloads them and injects them into the agent prompt.

### Configuration

```yaml
connectors:
  - name: rc-main
    attachments:
      max_file_size_mb: 50          # Skip files larger than this
      download_timeout: 30           # Seconds to wait per download
      cache_dir_global: ~/.agent-chat-gateway/attachments  # preferred: connector-global cache
```

### What Happens

1. **User uploads file** to a watched room
2. **Gateway detects** attachment in incoming message
3. **Download file** from Rocket.Chat (respects size limits and timeout)
4. **Cache locally** (prevents repeated downloads)
5. **Inject path** into agent prompt: `Attachment: /path/to/file`

### Supported File Types

All file types are supported. The gateway injects the file path into the prompt text, and the agent can read it using the `Read` tool if needed.

### Caching

Files are cached globally in the `cache_dir` and symlinked into each watcher's working directory. This prevents re-downloading the same file across multiple watchers.

---

## Sessions and State

### Automatic Sessions

By default, each watcher creates its own persistent session with the agent backend. The session ID is stored in `~/.agent-chat-gateway/state.<connector>.json` and reused across daemon restarts.

```yaml
watchers:
  - name: general
    session_id: null  # Gateway auto-creates and persists
```

### Sticky Sessions

You can explicitly tie a watcher to a specific session (e.g., a long-running agent session):

```yaml
watchers:
  - name: research
    session_id: "ses_abc123def456"  # Always use this session
```

Sticky sessions are never cleared by the `reset` command — the watcher will reconnect to the same session if it's still alive.

### Resetting State

To clear a watcher's state and create a fresh session:

```bash
agent-chat-gateway reset <watcher-name> [--connector NAME]
```

This:
- Clears the stored session ID (if not sticky)
- Creates a new session on the next message
- Preserves watcher configuration

### Viewing Runtime State

Runtime state is stored in `~/.agent-chat-gateway/`:

| File | Contents |
|---|---|
| `gateway.pid` | PID of the running daemon |
| `gateway.log` | Daemon log output |
| `control.sock` | Unix domain socket for CLI commands |
| `state.<connector>.json` | Persisted watcher definitions per connector |

Check the logs:

```bash
tail -f ~/.agent-chat-gateway/gateway.log
```

View persisted state:

```bash
cat ~/.agent-chat-gateway/state.rc-main.json | jq .
```

---

## Troubleshooting

### Gateway won't start

**Symptom:** `agent-chat-gateway start` returns immediately, `status` shows offline.

**Solution:**
1. Check the log: `tail ~/.agent-chat-gateway/gateway.log`
2. Verify Python 3.12+: `python3 --version`
3. Verify agent backends: `claude --version` and/or `opencode --version`
4. Validate config: `python3 -c "import yaml; yaml.safe_load(open('$HOME/.agent-chat-gateway/config.yaml'))" && echo OK`

### Agent not responding

**Symptom:** You message the bot but get no reply.

**Solution:**
1. Check status: `agent-chat-gateway status`
2. Check logs: `tail -f ~/.agent-chat-gateway/gateway.log`
3. Verify the bot is in the watched room in Rocket.Chat
4. Try the CLI: `agent-chat-gateway send <room> "test"` (should post immediately)
5. Verify agent backend: `claude -p` or `opencode run` (should start a session)

### Permission request hangs

**Symptom:** Permission request posted but owner response has no effect.

**Solution:**
1. Verify exact ID format (4 characters, e.g., `a3k9`)
2. Confirm you typed without a leading slash: `approve a3k9` (not `/approve a3k9`)
3. Check logs for errors: `tail ~/.agent-chat-gateway/gateway.log`
4. Wait for timeout if needed; request will auto-deny after `permissions.timeout` seconds

### High token usage

**Symptom:** Unexpectedly high Claude API costs.

**Solution:**
1. Check agent logs for repeated context injection: `grep "context_inject" ~/.agent-chat-gateway/gateway.log`
2. Reduce context file sizes (keep under 256 KB per file, 512 KB total)
3. Consider disabling context for specific watchers: set `context_inject_files: []`
4. Use sticky sessions (`session_id: "ses_..."`) to maintain conversation history

### Connection failures

**Symptom:** `ERROR: RC websocket disconnected` in logs, frequent reconnects.

**Solution:**
1. Verify Rocket.Chat server is reachable: `curl https://chat.example.com/api/version`
2. Verify bot credentials: `username` and `password` in config
3. Check bot account has required permissions in Rocket.Chat admin panel
4. Check network connectivity: `ping chat.example.com`
5. Review Rocket.Chat server logs for auth failures

### Files not downloading

**Symptom:** Attachments mentioned in messages but not passed to agent.

**Solution:**
1. Check max file size: `agent-chat-gateway` skips files larger than `attachments.max_file_size_mb`
2. Check timeout: increase `attachments.download_timeout` if slow network
3. Verify cache directory exists: `mkdir -p ~/.agent-chat-gateway/attachments`
4. Check logs: `grep "attach" ~/.agent-chat-gateway/gateway.log`

### Config validation errors

**Symptom:** `Error loading config.yaml: ...`

**Solution:**
1. Validate YAML syntax: `python3 -c "import yaml; yaml.safe_load(open('$HOME/.agent-chat-gateway/config.yaml'))"`
2. Check for missing required fields (see Configuration Reference above)
3. Verify all connector/agent references match defined names
4. Check environment variable expansion: `env | grep RC_` (if using `$RC_*`)

---

## Advanced Topics

### Multi-Agent Setup

You can define multiple agents and route different watchers to different backends:

```yaml
agents:
  claude:
    type: claude
    command: claude
    working_directory: ~/.agent-chat-gateway/work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

  opencode:
    type: opencode
    command: opencode
    working_directory: ~/.agent-chat-gateway/opencode-work
    timeout: 360
    permissions:
      enabled: true
      timeout: 300

watchers:
  - name: general
    agent: claude          # General discussions
  - name: development
    agent: opencode        # Code development
  - name: research
    agent: claude          # Research tasks
```

### Multi-Connector Setup

For teams using multiple Rocket.Chat servers or workspaces:

```yaml
connectors:
  - name: rc-company
    server:
      url: https://chat.company.com
      username: bot
      password: "${RC_PASSWORD_COMPANY}"
    allowed_users:
      owners:
        - alice
        - bob

  - name: rc-partner
    server:
      url: https://chat.partner.com
      username: bot
      password: "${RC_PASSWORD_PARTNER}"
    allowed_users:
      owners:
        - charlie

watchers:
  - name: company-general
    connector: rc-company
    room: general
    agent: claude

  - name: partner-collab
    connector: rc-partner
    room: general
    agent: claude
```

### Tool Regex Patterns

Tool allow-list patterns use Python regex (fullmatch). Some examples:

```yaml
owner_allowed_tools:
  # Exact match
  - tool: "Read"

  # Pattern match
  - tool: "bash"
    params: "ls.*"

  # Multiple alternatives
  - tool: "Bash"
    params: "git (log|diff|status|show).*"

  # Match all in a namespace (MCP)
  - tool: "mcp__rocketchat__.*"
    params: ".*\"action\":\\s*\"(get|list)\".*"

  # Unsafe: match all tools (use with care!)
  - tool: ".*"
```

### Working Directory Isolation

Each agent's `working_directory` is where the agent subprocess runs. This isolates agent work:

```yaml
agents:
  claude:
    working_directory: ~/.agent-chat-gateway/claude-work
  opencode:
    working_directory: /data/agent-sessions/opencode
```

The gateway ensures the directory exists and uses it as the agent's current working directory (`cwd`).

### Debugging Mode

The gateway logs at INFO level by default. To see detailed output, tail the log file while the daemon is running:

```bash
tail -f ~/.agent-chat-gateway/gateway.log
```

---

## Getting Help

- **Documentation:** See [install-agent.md](install-agent.md) for installation details
- **Logs:** `tail -f ~/.agent-chat-gateway/gateway.log`
- **GitHub:** https://github.com/HammerMei/agent-chat-gateway/issues
- **Community:** Discuss on Anthropic's community forum

---

## Summary

`agent-chat-gateway` provides a flexible, secure bridge from Rocket.Chat to AI agents. Start with a minimal config, use role-based access control to grant appropriate permissions, and leverage the permission approval system to ensure human oversight of sensitive operations.

For production deployments, carefully review your tool allow-lists, set appropriate timeouts, and monitor your logs for errors and token usage.

Happy chatting!
