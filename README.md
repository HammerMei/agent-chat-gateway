# agent-chat-gateway

[![CI](https://github.com/HammerMei/agent-chat-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/HammerMei/agent-chat-gateway/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-%3E%3D3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Inspired by [OpenClaw](https://github.com/openclaw/openclaw)'s vision of making AI agents accessible from any messaging app, `agent-chat-gateway` takes that idea to the team and developer layer: rather than running a new AI agent for you, it bridges your **existing** local agent sessions — Claude CLI, OpenCode, or any custom backend — to your team's chat platform. No code changes to your agent required; configure once, and your whole team can collaborate through it.

> **How it compares to Claude Code Channels:** Claude Code's native [Channels](https://code.claude.com/docs/en/channels) feature (v2.1.80+) connects a single Claude Code session to Telegram, Discord, or iMessage — a great fit for personal use. `agent-chat-gateway` was developed independently before Channels shipped and is designed for a different layer: multi-user team deployments with multiple agent backends, role-based access control, human-in-the-loop permission approval, and sticky sessions shared across an entire workspace.

## What's Supported

| | Supported today | Extensible via |
|--|--|--|
| **Chat platforms** | Rocket.Chat | `Connector` ABC |
| **Agent backends** | Claude CLI, OpenCode | `AgentBackend` ABC |

## Features

- 🔌 **Pluggable connectors** — Rocket.Chat today; add Slack, Discord, and others via the `Connector` ABC
- 🤖 **Multiple agent backends** — Claude CLI and OpenCode out of the box; add your own via `AgentBackend` ABC
- 🔒 **Role-based access control** — Owner and Guest roles with per-tool allow-lists (regex-based, tree-sitter Bash parsing)
- 🛡️ **Human-in-the-loop approval** — Sensitive tool calls pause for owner `approve`/`deny` before proceeding (similar to Claude Code's permission relay)
- 📌 **Sticky sessions** — Each chat room keeps its own persistent agent session across daemon restarts
- 📁 **Attachment support** — Files uploaded in chat are automatically downloaded and injected into the agent prompt
- 🧠 **Context injection** — Load domain knowledge, system prompts, and room profiles into agent sessions at startup
- ⚡ **Multi-connector** — Run multiple chat platform connections simultaneously

---

## Key Concepts

| Term | Meaning |
|--|--|
| **Connector** | Adapter for a chat platform (e.g., Rocket.Chat). Handles incoming messages and posts replies. |
| **Agent backend** | The AI tool the gateway dispatches to (e.g., Claude CLI, OpenCode). |
| **Watcher** | A binding between one chat room and one agent backend. One watcher per room. |

---

## Use Cases

### 1. Build a Super-Powered Chatbot for Your Team
Turn your existing agentic tool (Claude, OpenCode, or any custom backend) into a shared team assistant in your chat workspace. Configure multiple rooms with different agents, define per-role tool access, and keep humans in the loop for sensitive operations.

### 2. Access Your Agent from Any Messaging App
Already have Claude CLI or OpenCode running on your machine? Bridge it to your team's Rocket.Chat workspace so everyone can interact with it directly from chat — no terminal required. Similar in concept to Claude Code's [Channels](https://code.claude.com/docs/en/channels) feature, but works with any agent backend and is built for multi-user team access.

### 3. Continue an Existing Agent Session Remotely
Pin a chat room to a specific agent session ID and pick up exactly where you left off from your messaging app — similar in spirit to Claude Code's [Remote Control](https://code.claude.com/docs/en/remote-control) feature, but accessible from your team's shared chat room rather than a personal device.

See [docs/user-guide.md](docs/user-guide.md) for configuration examples for each use case.

---

## Quick Start

### Recommended: AI-guided install

The easiest way to install is to ask your AI agent to do it for you — it handles missing dependencies, config setup, and troubleshooting automatically:

**Claude Code:**
```
claude "Please install agent-chat-gateway by following the instructions at https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/docs/install-agent.md"
```

**OpenCode:**
```
opencode "Please install agent-chat-gateway by following the instructions at https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/docs/install-agent.md"
```

### Manual install

```bash
# Requires: Python 3.12+, git
curl -fsSL https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/install.sh | bash
```

The installer checks Python 3.12+, installs [uv](https://docs.astral.sh/uv/) if needed, clones the repo, and launches the interactive setup wizard.

```bash
# Start the daemon
agent-chat-gateway start

# Check it's running
agent-chat-gateway status
agent-chat-gateway list
```

See [docs/install-agent.md](docs/install-agent.md) for a full step-by-step installation guide.

---

## Architecture

```
RC User @mention
    → RC Server (DDP WebSocket event)
    → connectors/rocketchat/            (DDP listener, normalization, attachment download)
    → core/message_processor.py         (per-room queue consumer)
        → enqueue()                     (intercepts approve/deny permission commands)
        → agent.send()                  (calls AgentBackend, blocks on response)
    → AgentBackend                      (session lifecycle + message dispatch)
        → PreToolUse HTTP hook          (pauses for owner approval if permissions.enabled)
    → connector.send_text()             (posts response back to room via REST)
```

The daemon exposes a Unix domain socket (`~/.agent-chat-gateway/control.sock`) so the
`agent-chat-gateway` CLI can send commands (list/pause/resume/reset watchers) to the running process.

---

## Module Layout

```
agent-chat-gateway/
├── config.yaml              # User-editable: connectors, agents, permissions
├── pyproject.toml
└── gateway/
    ├── __init__.py
    ├── agents/
    │   ├── __init__.py          # AgentBackend ABC (create_session, send → AgentResponse)
    │   ├── response.py          # AgentResponse + TokenUsage normalized dataclasses
    │   ├── session.py           # AgentSession: thin scripting wrapper around AgentBackend
    │   ├── claude/              # ClaudeBackend: drives the Claude CLI subprocess
    │   └── opencode/            # OpenCodeBackend: drives the opencode CLI subprocess
    │       └── hooks/
    │           └── role-enforcement.ts  # opencode plugin: guest RBAC + owner approval trigger
    ├── cli.py                   # argparse entry point (start/stop/restart/status/list/pause/resume/reset/send)
    ├── config.py                # YAML loader → GatewayConfig / AgentConfig / PermissionConfig
    ├── daemon.py                # Double-fork daemonize, PID file, signal handling
    ├── service.py               # Top-level orchestrator: connectors + brokers + SessionManagers
    ├── control.py               # Unix socket ControlServer for CLI→daemon command routing
    ├── core/                    # Platform-agnostic library
    │   ├── connector.py         # Connector ABC + normalized dataclasses (Room, IncomingMessage, …)
    │   ├── session_manager.py   # Per-connector orchestrator; delegates to collaborators below
    │   ├── dispatch.py          # MessageDispatcher: routes messages to per-room processors
    │   ├── message_processor.py # Per-room async queue consumer
    │   ├── agent_turn_runner.py # Execute one agent turn (typing → send → deliver)
    │   ├── watcher_lifecycle.py # Watcher state machines (start/pause/resume/reset/stop)
    │   ├── permission.py        # PermissionBroker ABC
    │   ├── permission_state.py  # PermissionRegistry (in-memory) + PermissionRequest
    │   ├── tool_match.py        # Tool allow-list matching (regex + tree-sitter bash parsing)
    │   ├── context_injector.py  # Inject context files into agent sessions
    │   ├── state_store.py       # Persist WatcherState to JSON
    │   └── config.py            # Shared config types (ConnectorConfig, AgentConfig, …)
    └── connectors/
        ├── rocketchat/          # Rocket.Chat connector (DDP WebSocket + REST)
        │   ├── connector.py     # RocketChatConnector(Connector)
        │   ├── config.py        # RocketChatConfig dataclass
        │   ├── normalize.py     # RC raw DDP doc → IncomingMessage
        │   ├── outbound.py      # send_text / typing indicator / online notification
        │   ├── rest.py          # RC REST client (login, post_message, upload_file, …)
        │   └── websocket.py     # DDP-over-WebSocket client with reconnect + keepalive
        └── script/
            └── connector.py     # ScriptConnector — in-memory, for scripting & tests
```

### Key components

| Component | Responsibility |
|---|---|
| `core/connector.py` | Platform-agnostic `Connector` ABC and normalized dataclasses (`Room`, `IncomingMessage`, `UserRole`). |
| `core/session_manager.py` | Orchestrates the connector, spawns `MessageProcessor` per room, persists state, handles CLI control socket. |
| `core/message_processor.py` | Dequeues inbound messages, intercepts `approve`/`deny` permission commands, builds agent prompt, calls `AgentBackend.send()`, posts replies. |
| `connectors/rocketchat/` | All Rocket.Chat knowledge: DDP subscription, REST API, message normalization, RBAC role resolution, attachment download. |
| `connectors/script/` | In-memory `ScriptConnector` for scripting, testing, and agent-to-agent piping. Zero network calls. |
| `agents/response.py` | `AgentResponse` and `TokenUsage` normalized dataclasses returned by all backends. |
| `agents/session.py` | `AgentSession` — thin scripting wrapper around `AgentBackend`. No connector, no room, no state file. |
| `core/permission.py` + `permission_state.py` | Human-in-the-loop approval system. Brokers intercept tool calls, post RC notifications, and await owner decisions. |
| `core/tool_match.py` | Tool allow-list matching; bash commands split via tree-sitter AST to prevent compound-command bypasses. |
| `service.py` | Wires config → connectors → permission brokers → `SessionManager` instances. One `SessionManager` per connector. |
| `config.py` | Loads `config.yaml` into typed dataclasses. Supports env-var expansion (`$VAR`). |

---

## Configuration (`config.yaml`)

```yaml
connectors:
  - name: rc-home
    type: rocketchat
    server:
      url: https://your-rocketchat.example.com
      username: bot-username
      password: "$RC_PASSWORD"        # env-var expansion supported
    allowed_users:
      owners:
        - alice                       # full access: tools, tasks, approve/deny permissions
      guests: []                      # conversation-only; restricted tool whitelist
    attachments:
      max_file_size_mb: 10
      download_timeout: 30
      cache_dir: agent-chat.cache

default_agent: assistance

agents:
  assistance:
    type: claude
    command: claude
    working_directory: /tmp/acg      # required: agent working directory
    timeout: 360                     # seconds; must be > permissions.timeout
    new_session_args: []
    session_prefix: agent-chat       # prefix for Claude session names
    permissions:
      enabled: true                  # enable human-in-the-loop tool approval
      timeout: 300                   # seconds before auto-deny (must be < timeout above)
      skip_owner_approval: false     # set true to auto-approve all owner tool calls
    owner_allowed_tools:             # auto-approved for owners (no prompt shown)
      - tool: "bash"
        params: "ls.*|pwd|echo.*"
    guest_allowed_tools:             # auto-approved for guests; others silently denied
      - tool: "read"
```

---

## Role-Based Access Control

Every message sent to the agent subprocess is prefixed with a trusted header injected by
the connector:

```
[Rocket.Chat #<room> | from: <username> | role: owner|guest]  <message text>
```

The agent uses this prefix to determine the sender's role. Two enforcement layers run in
parallel in the agent subprocess environment:

### Layer 1 — Shell hook (Claude Code `PreToolUse`)

`~/.claude/hooks/pretooluse-role-enforcement.sh` runs on every tool call:

- `ACG_ROLE=owner` → exits 0 (allow all tools)
- `ACG_ROLE=guest` + tool in `ACG_ALLOWED_TOOLS` → exits 0
- `ACG_ROLE=guest` + tool NOT in whitelist → exits 2 (block, no further hooks run)

### Layer 2 — opencode plugin (`tool.execute.before`)

`gateway/agents/opencode/hooks/role-enforcement.ts` is symlinked into the working
directory's `.opencode/plugins/` folder. Same logic as the shell hook for guests.
For owners, triggers `output.status = "ask"` on sensitive tools to fire opencode's
`permission.asked` SSE event (used by the permission approval system).

---

## Human-in-the-Loop Permission Approval

When `permissions.enabled: true`, sensitive tool calls (`Bash`, `Write`, `Edit`,
`MultiEdit`, `NotebookEdit`) are paused and require explicit owner approval via RC chat
before the agent proceeds.

### How it works

```
Agent attempts sensitive tool call
    → PreToolUse HTTP hook fires (Claude) / permission.asked SSE event (opencode)
    → PermissionBroker posts approval request to RC chat room:

        🔐 **Permission required** `[a3k9]`
        **Tool:** `Bash`
        **Params:** `command='rm ./build'`
        Reply `approve a3k9` or `deny a3k9`

    → Owner replies in RC chat
    → MessageDispatcher.dispatch() intercepts the command (BEFORE the queue)
    → asyncio.Future resolved → agent subprocess unblocked
    → Tool executes (approve) or is skipped (deny)
```

### Approval commands

Type these directly in the RC chat room (no slash prefix — RC client would intercept `/` commands):

```
approve a3k9    ← allow the tool call
deny a3k9       ← block the tool call
```

**Important:** These commands are intercepted by the gateway before they reach the agent.
The agent never sees them. If you type a wrong ID:
- Wrong length → `⚠️ Invalid ID — expected 4 characters`
- ID not found → `⚠️ No pending permission request with ID`

### Timeout

Requests not resolved within `permissions.timeout` seconds are auto-denied:
```
⏱️ Permission `a3k9` timed out — auto-denied.
```

### Concurrency

While a tool call is pending approval, new messages are queued and will not be processed
until the pending request is resolved. Only `approve` / `deny` commands unblock the queue.

### Known limitation — Bash sandbox

Claude Code's Bash tool sandbox restricts execution to the working directory.
`approve` resolves the HTTP hook, but Claude Code's sandbox enforcement is a separate layer
that cannot be overridden via hooks. Operations outside the working directory (e.g.
`rm ~/foo`) will still be blocked by Claude Code's Bash sandbox after approval.

### Architecture

```
GatewayService
├── PermissionRegistry          (shared in-process store: request_id → asyncio.Future)
├── session_room_map            (session_id → room_id, for broker → RC room routing)
├── ClaudePermissionBroker      (HTTP server on random localhost port)
│   └── generates temp settings.json passed as --settings to every claude -p call
│       {"hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
│                                   "type": "http", "url": "http://127.0.0.1:<port>/hook"}]}}
├── OpenCodePermissionBroker    (SSE listener on opencode's /events endpoint)
│   └── calls POST /permission/{id}/reply on owner decision
└── expiry_task                 (background: auto-deny requests older than timeout)
```

---

## Agent Backends

### `ClaudeBackend` (`agents/claude/`)

Drives the Claude CLI via subprocesses:

- **Session creation** — `claude -p --output-format json [new_session_args] [--settings <path>]`
  Parses `session_id` from the JSON response and stores it in state.

- **Message sending** — `claude -p --resume <session-id> --output-format stream-json --verbose [--settings <path>]`
  Streams one JSON event per line; extracts text from `type="assistant"` content blocks,
  metadata from `type="result"` (session_id, cost, duration, turns, token usage).

- **Permission hook** — when `permissions.enabled`, `--settings <path>` is injected into
  every call pointing to a gateway-generated settings file with the HTTP `PreToolUse` hook.

- **Environment isolation** — strips `CLAUDECODE` from the subprocess environment; merges
  `ACG_ROLE` / `ACG_ALLOWED_TOOLS` per-message for role enforcement.

- **Attachments** — injects file paths into the prompt text (Claude CLI does not support
  native `-f` attachments in `-p` mode).

### `OpenCodeBackend` (`agents/opencode/`)

Drives the opencode CLI:

- **Session creation** — `opencode run --format json [new_session_args]`
- **Message sending** — `opencode run -s <session-id> --format json [-f <file>...]`
- **Permission** — `permission.asked` SSE events from opencode's HTTP server, triggered
  by the `role-enforcement.ts` plugin setting `output.status = "ask"`.
- **Attachments** — native `-f` flag support.

---

## Agent Response (`AgentResponse`)

Every `AgentBackend.send()` call returns an `AgentResponse`:

```python
@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0    # opencode only

@dataclass
class AgentResponse:
    text: str                         # agent's reply (always present)
    session_id: str | None = None
    usage: TokenUsage | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    is_error: bool = False

    def __str__(self) -> str:
        return self.text              # transparent use in f-strings
```

`MessageProcessor` logs token usage automatically when `response.usage` is present:
```
Agent usage [@alice] in=1234 out=256 cache_read=512 cost=$0.0042
```

---

## Runtime files

All runtime state lives in `~/.agent-chat-gateway/`:

| File | Contents |
|---|---|
| `gateway.pid` | PID of the running daemon |
| `gateway.log` | Daemon log output |
| `control.sock` | Unix domain socket for CLI commands |
| `state.<connector>.json` | Persisted watcher definitions per connector |

---

## CLI usage

```bash
# Daemon lifecycle
agent-chat-gateway start [--config path/to/config.yaml]
agent-chat-gateway restart [--config path/to/config.yaml]
agent-chat-gateway status
agent-chat-gateway stop

# Watcher control (requires running daemon)
agent-chat-gateway list [--connector NAME]
agent-chat-gateway pause <watcher-name> [--connector NAME]
agent-chat-gateway resume <watcher-name> [--connector NAME]
agent-chat-gateway reset <watcher-name> [--connector NAME]   # clear state, start fresh session

# Send messages directly to a room (bypasses agent)
agent-chat-gateway send <room> "message text"
agent-chat-gateway send <room> -                             # read from stdin
agent-chat-gateway send <room> --file message.txt
agent-chat-gateway send <room> --attach file.pdf --file caption.txt

# Setup
agent-chat-gateway onboard                                   # interactive setup wizard
agent-chat-gateway upgrade                                   # check and install updates
```

---

## Adding an Agent Integration

### 1. Implement the `AgentBackend` ABC

```python
from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse

class MyBackend(AgentBackend):

    async def create_session(
        self,
        working_directory: str,
        extra_args: list[str] | None = None,
        session_title: str | None = None,
    ) -> str:
        """Start a new session. Return an opaque session_id string."""
        ...

    async def send(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResponse:
        """Send a message to an existing session. Raise asyncio.TimeoutError on timeout."""
        ...
```

### 2. Register in `service.py`

```python
from .agents.mybackend.adapter import MyBackend

# inside _build_agent_backend():
if agent_cfg.type == "mybackend":
    return MyBackend(command=agent_cfg.command, timeout=timeout)
```

### 3. Add to `config.yaml`

```yaml
agents:
  myagent:
    type: mybackend
    command: mybinary
    new_session_args: []
```

### 4. Smoke-test with `ScriptConnector`

```python
import asyncio
from gateway.connectors.script.connector import ScriptConnector
from gateway.core.session_manager import SessionManager
from gateway.core.config import CoreConfig
from gateway.agents.mybackend.adapter import MyBackend

connector = ScriptConnector()
backend   = MyBackend(command="mybinary", timeout=60)
config    = CoreConfig(timeout=60, agents={"default": backend}, default_agent="default")
manager   = SessionManager(connector, backend, "default", config)

async def test():
    await manager.run_once()
    await manager.add_session("test-room", None, "/tmp")
    await connector.inject("Hello")
    reply = await connector.receive_reply()
    print(reply)

asyncio.run(test())
```

---

## Adding a new Connector

Implement the `Connector` ABC from `gateway/core/connector.py`:

```python
from gateway.core.connector import Connector, IncomingMessage, Room
from gateway.agents.response import AgentResponse

class SlackConnector(Connector):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def register_handler(self, handler) -> None: ...
    async def send_text(self, room_id: str, response: AgentResponse) -> None: ...
    async def resolve_room(self, room_name: str) -> Room: ...
    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        return f"[Slack #{msg.room.name} | from: {msg.sender.username} | role: {msg.role.value}]"
```

Then instantiate it in `service.py` via `connector_factory()`.

---

## Direct Scripting with `AgentSession`

For one-off tasks and agent-to-agent pipelines, skip the full gateway stack:

```python
from gateway.agents.session import AgentSession
from gateway.agents.claude.adapter import ClaudeBackend
from gateway.agents.opencode.adapter import OpenCodeBackend

# Single agent
async with AgentSession(ClaudeBackend("claude", [], 120), "/my/project") as session:
    response = await session.send("What files are here?")
    print(response)   # __str__ → response.text

# Agent-to-agent pipeline
async with (
    AgentSession(OpenCodeBackend("opencode", [], 120), cwd) as oc,
    AgentSession(ClaudeBackend("claude", ["--agent", "assistance"], 120), cwd) as cc,
):
    summary = await oc.send("Summarize the codebase")
    review  = await cc.send(f"Review this summary:\n{summary}")
    print(review)
```

---

## Documentation

| Document | Description |
|---|---|
| [docs/install-agent.md](docs/install-agent.md) | Installation & getting started guide |
| [docs/user-guide.md](docs/user-guide.md) | Full configuration reference and operational guide |
| [docs/architecture.md](docs/architecture.md) | System architecture, module breakdown, data flow diagrams |
| [docs/permission-reference.md](docs/permission-reference.md) | RBAC and human-in-the-loop approval deep dive |
| [docs/supported-features.md](docs/supported-features.md) | Supported features, known limitations, and roadmap |
| [docs/requirements.md](docs/requirements.md) | Functional specification (behavioral requirements) |
