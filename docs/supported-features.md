# Supported Features & Roadmap

This document clearly communicates what agent-chat-gateway supports today, what is known to be limited, and what is planned for future releases.

---

## Currently Supported Features

### Chat Platform Connectors

#### Rocket.Chat
- ✅ **Message routing** via DDP WebSocket protocol
  - Real-time message subscriptions per watched room
  - Automatic reconnect with exponential backoff
  - Per-room message deduplication (watermark-based)
  - Multiple concurrent rooms per connector
  - Multiple Rocket.Chat instances (multi-connector setup)

- ✅ **Message triggering**
  - Direct message (DM) activation — all DMs to bot are forwarded to agent
  - Channel/group activation — requires `@mention` of bot username

- ✅ **Attachments**
  - Inbound attachment download (files, images, documents)
  - File size and timeout limits enforced
  - Attachment metadata injected into agent prompt as text context
  - Multiple attachments per message supported

- ✅ **Typing & Status Indicators**
  - Typing indicator while agent processes message
  - Online/offline notifications per watcher (optional)
  - Configurable notification suppression per watcher

- ✅ **Multi-connector support**
  - Run multiple Rocket.Chat instances simultaneously
  - Each with independent connector config, roles, and watchers

---

### Agent Backends

#### Claude CLI Backend (`claude`)
- ✅ Session creation and persistent conversation history
- ✅ Message sending with `--output-format stream-json`
- ✅ Tool calling via PreToolUse hook for permission approval integration
- ✅ Attachment context injection (as text references in prompt)
- ✅ Timeout enforcement per message
- ✅ Response streaming and completion detection

#### OpenCode CLI Backend (`opencode`)
- ✅ Session creation and persistent conversation history
- ✅ HTTP API message sending
- ✅ Tool calling via SSE `permission.asked` event for approval integration
- ✅ Attachment context injection (as text references in prompt)
- ✅ Per-message environment variable overrides
- ✅ Rate limit detection and reporting
- ✅ Server recovery on reconnect

#### Backend Behavior
- ✅ Normalized response format across backends
- ✅ Explicit session lifecycle (create, send, reset)
- ✅ Non-empty response guarantee (placeholder message if needed)
- ✅ Structured error reporting

---

### Session Management

#### Persistence & Recovery
- ✅ Persistent watcher state across daemon restarts (`state.json`)
- ✅ Auto-created session IDs retained across restarts
- ✅ Fixed (sticky) session IDs preserved across reset operations
- ✅ Graceful recovery from corrupted state files

#### Session Operations
- ✅ Multiple rooms per session (session reuse across different chat rooms)
- ✅ Per-room message queue (serial processing, no race conditions)
- ✅ Queue depth limiting with graceful backpressure rejection
- ✅ Watcher pause/resume (temporarily pause agent invocation)
- ✅ Session reset (clear conversation history, start fresh)

#### Programmatic Access
- ✅ `AgentSession` — lightweight async context manager for scripting
- ✅ `ScriptConnector` — in-memory connector for agent-to-agent pipelines
- ✅ Agent-to-agent piping via `pipe_to()` method
- ✅ Explicit session lifecycle boundaries
- ✅ Attachment support in programmatic sends

---

### Role-Based Access Control (RBAC)

#### Roles
- ✅ **OWNER** — Full tool access (subject to optional approval)
- ✅ **GUEST** — Limited tool access (only tools in guest allow-list)
- ✅ **ANONYMOUS** — No agent access (messages rejected)

#### Configuration
- ✅ Per-connector owners/guests list (user ID-based)
- ✅ Tool allow-lists per role (regex-based matching)
- ✅ Parameter-based tool matching (path normalization, regex patterns)
- ✅ File path normalization (prevents `../` bypass attacks)
- ✅ Case-insensitive tool name matching where applicable

#### Enforcement
- ✅ Role resolved from trusted connector context (not from message text)
- ✅ Bash command parsing via tree-sitter AST (secure, not string split)
- ✅ Automatic guest tool rejection (no owner notification for guest denials)
- ✅ Owner tool matching checked against allow-list

---

### Human-in-the-Loop Permission Approval

#### Approval Workflow
- ✅ Automatic triggering when tool call matches neither owner nor guest allow-lists
- ✅ Permission request visible in chat (Rocket.Chat notification)
- ✅ 4-character approval ID system (`approve a3k9` / `deny a3k9`)
- ✅ Case-insensitive approval ID matching
- ✅ Chat-based approval commands intercepted (not forwarded to agent)

#### Configuration
- ✅ Global permission timeout (auto-deny if owner doesn't respond)
- ✅ Per-request timeout enforcement
- ✅ Auto-approval for tools matching owner allow-lists
- ✅ `skip_owner_approval` option for fully-trusted environments (sandbox mode)
- ✅ Owner-only access to approve/deny commands

#### Queueing & Pause
- ✅ Message queue pauses while approval pending
- ✅ Auto-denial on timeout with visible notification
- ✅ Multiple pending approvals supported (per session)

#### Backend Integration
- ✅ Claude CLI backend via HTTP PreToolUse hook
- ✅ OpenCode backend via SSE `permission.asked` event and reply API

---

### Context Injection

#### File-based Context
- ✅ Three-layer context system
  - Connector-level context (shared across all watchers)
  - Agent-level context (per agent backend)
  - Watcher-level context (per specific room/watcher)

#### Behavior
- ✅ Injected on session start (one-time, not per-message)
- ✅ 256 KB per file limit
- ✅ 512 KB total context limit
- ✅ Multiple context files supported (concatenated)

---

### CLI Operations

#### Daemon Lifecycle
- ✅ `start` — Start daemon in background
- ✅ `stop` — Graceful shutdown
- ✅ `restart` — Restart daemon
- ✅ `status` — Check if daemon is running

#### Watcher Control
- ✅ `list` — List watchers and runtime status (supports multi-connector aggregation)
- ✅ `pause <watcher>` — Pause watcher (stop processing messages)
- ✅ `resume <watcher>` — Resume paused watcher
- ✅ `reset <watcher>` — Clear session state

#### Direct Operations
- ✅ `send <room> <text>` — Send text message to room
- ✅ `send <room> --file <path>` — Send file/attachment to room
- ✅ `send <room> -` — Send stdin content to room (pass `-` as the message argument)
- ✅ Combined text + attachment sends

#### Configuration & Upgrade
- ✅ Interactive onboard wizard (first-run setup)
- ✅ Self-upgrade via CLI command

---

### Configuration

#### Features
- ✅ YAML configuration file
- ✅ Environment variable expansion (`$VAR`, `${VAR}`)
- ✅ `.env` file support for expansion values
- ✅ Multi-connector setup (multiple chat instances)
- ✅ Multi-agent setup (different agents per watcher)
- ✅ Cross-field validation (e.g., agent timeout > permission timeout)
- ✅ Relative path resolution (relative to config file location)

#### Configuration Validation
- ✅ Connector names must be unique
- ✅ Watcher names must be unique
- ✅ Watchers must reference existing connectors and agents
- ✅ Default agent must reference existing agent (if specified)
- ✅ Required paths must exist at validation time
- ✅ Queue depth settings reject invalid values
- ✅ Sticky session IDs validated for uniqueness

---

### Testing & Scripting

#### Unit & Integration Tests
- ✅ Comprehensive test suite covering core functionality
- ✅ Unit tests for connector, permission, session management
- ✅ Integration tests for multi-component workflows

#### Scripting APIs
- ✅ `AgentSession` — Direct session management without connectors
- ✅ `ScriptConnector` — In-memory connector for testing and automation
- ✅ Agent-to-agent piping for multi-stage workflows

---

## Known Limitations & Constraints

### Platform Support

- ❌ **Single connector type**: Only Rocket.Chat supported currently
  - No Slack, Discord, Microsoft Teams, or WhatsApp connectors
  - Webhook-based (push) connectors not yet implemented
  - Rocket.Chat connector is pull-based (DDP subscription polling)

### Agent Backends

- ❌ **Attachment handling**: Files injected as text references only
  - No native binary attachment passing to agent
  - Both Claude CLI and OpenCode backends affected
  - Agent receives attachment as text context, not as file blob

- ❌ **No direct Anthropic API integration**
  - Claude backend requires Claude CLI subprocess
  - No library-level API integration

- ❌ **Response streaming**: Responses posted to chat only after agent completes full turn
  - No streaming message updates
  - User sees final response once, not incremental chunks

---

### Rocket.Chat Specific

- ❌ **Message character limit**: Rocket.Chat 4,000 character limit
  - Long responses automatically chunked
  - Very long messages may split mid-sentence (no intelligent wrapping)

- ✅ **Thread replies** — configurable via `reply_in_thread` (default: false) and
  `permission_reply_in_thread` (default: true for approval notifications)

- ❌ **Slash command conflict**: Permission approve/deny cannot use `/` prefix
  - Rocket.Chat intercepts `/` commands
  - Workaround: use `approve` and `deny` without prefix

---

### Security & Sandbox

- ❌ **End-to-end encryption**: Session state not encrypted at rest
  - Persisted state readable by any process with file access

- ❌ **Sandbox enforcement separation**: Claude Bash tool sandbox is independent
  - Permission approval and Claude Code sandbox are separate systems
  - Approved commands may still be blocked by Claude Code's native sandbox

---

### Configuration & Operations

- ❌ **Hot-reload**: Configuration changes require daemon restart
  - No zero-downtime config updates

- ❌ **Web UI**: No monitoring or configuration dashboard
  - CLI-only operations

- ❌ **Distributed deployment**: Single-process only
  - No multi-node or horizontal scaling
  - Cannot run multiple daemon instances on same config

---

### Observability

- ❌ **Structured logging**: Not implemented (text logs only)
- ❌ **Metrics/observability**: No Prometheus endpoint or metrics collection
- ❌ **Audit logging**: No dedicated audit trail for permission approvals or sensitive operations

---

## Roadmap (Planned & Under Consideration)

### High Priority (Planned Next)

#### Additional Chat Connectors
- 🔄 **Slack connector** — Real-time message routing via Slack API/WebSocket
- 🔄 **Discord connector** — Message routing via Discord API
- 🔄 **Generic webhook connector** — Support push-based events from any platform

#### Operational Features
- 🔄 **Config hot-reload** — Update configuration without restart
- 🔄 **Structured logging** — JSON-formatted logs with machine parsing
- 🔄 **Metrics endpoint** — Prometheus-compatible metrics (message count, latency, errors)

---

### Under Consideration (Future)

#### Advanced Features
- 💡 **Persistent memory across sessions** — Agent remembers past conversations (conversation history)
- 💡 **Heartbeat / proactive agent** — Agent can send unprompted messages to chat
- 💡 **Web UI** — Dashboard for monitoring, configuration, and approval management
- 💡 **Multiple agent sessions per room** — Fan-out to multiple agents simultaneously
- 💡 **Message filtering plugins** — User-defined message preprocessing/filtering

#### Scalability
- 💡 **Multi-node support** — Horizontal scaling (multiple daemons, shared state)
- 💡 **Message broker integration** — Redis/RabbitMQ for distributed queue management

#### Security & Privacy
- 💡 **End-to-end encryption** — Encrypt persisted session state
- 💡 **Audit logging** — Dedicated audit trail for sensitive operations
- 💡 **Role-based API access** — HTTP API with RBAC for external tools

---

## Feature Stability

| Feature | Stability | Notes |
|---------|-----------|-------|
| Rocket.Chat connector | Stable | Production-ready |
| Claude CLI backend | Stable | Production-ready |
| OpenCode backend | Stable | Production-ready |
| Permission approval system | Stable | Production-ready |
| RBAC and tool allow-lists | Stable | Production-ready |
| Context injection | Stable | Production-ready |
| CLI operations | Stable | Production-ready |
| Persistence & recovery | Stable | Production-ready |
| Scripting API | Stable | Stable for scripting |

---

## How to Request Features

If you'd like to see a feature implemented:

1. **Check this document** — Verify it's not already planned
2. **Search issues** — Look for existing feature requests on GitHub
3. **Open a discussion** — Start a GitHub discussion to gauge community interest
4. **Submit an issue** — File a feature request with your use case and motivation

For security-related features or constraints, please contact the maintainers privately via the security reporting process.

