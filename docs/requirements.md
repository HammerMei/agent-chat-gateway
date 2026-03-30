# agent-chat-gateway: Functional Specification

## 1. Purpose and Scope

The `agent-chat-gateway` daemon bridges messaging platforms (Rocket.Chat and others) to persistent AI agent sessions. The system accepts user messages from monitored chat rooms or direct messages, forwards eligible messages to a configured agent backend, and posts the agent's response back to the originating chat destination.

**Scope:** This specification defines externally observable behavior and user-facing requirements. It intentionally avoids internal architecture and implementation details except where necessary to clarify requirements.

**High-level goals:**
- Accept chat messages from one or more messaging platforms
- Maintain persistent agent sessions keyed by chat room or conversation
- Apply role-based access control to enforce tool usage policies
- Provide a CLI interface for operational control
- Persist watcher state across daemon restarts

---

## 2. Core Behavioral Requirements

### 2.1 Message Routing

The gateway SHALL:
1. Watch one or more configured chat rooms or direct message conversations (watchers)
2. When an authorized user sends a qualifying message, forward that message to the configured agent session
3. Post the agent response back to the same room or conversation context
4. Process messages for each watched room sequentially to preserve order
5. Maintain persistent sessions such that later messages in the same watcher continue the same agent conversation unless explicitly reset

### 2.2 Message Eligibility

The gateway SHALL:
1. Apply sender allow-list rules before forwarding a message to the agent
2. In Rocket.Chat channels or groups, require the bot to be mentioned (via `@mention`) before treating a message as intended for the gateway
3. In direct messages, NOT require a mention
4. Reject messages from anonymous users without forwarding
5. Resolve sender role from the trusted connector context, NOT from user-provided message content

### 2.3 Message Content and Attachments

The gateway SHALL:
1. Support inbound messages with text, file attachments, or both
2. Download accepted file attachments before invoking the agent
3. When a message contains only attachments with no text, still provide usable attachment context to the agent
4. When a message contains neither usable text nor usable attachment context, still produce a non-empty placeholder message for the agent
5. Enforce configured attachment download limits (file size, timeout)

### 2.4 Reply Delivery

The gateway SHALL:
1. Send normal agent replies as chat messages
2. When thread reply behavior is enabled by configuration, deliver replies in the configured thread mode
3. When online/offline notifications are enabled by configuration, post those notifications to the monitored room
4. When a watcher-specific notification is set to null, suppress that notification

---

## 3. Watcher Lifecycle

### 3.1 Watcher Definition and Startup

A watcher SHALL:
1. Bind exactly one chat room or conversation (in one connector) to exactly one agent configuration
2. Have a unique name within its connector scope
3. Start automatically when the daemon starts
4. Persist its runtime state across daemon restarts

### 3.2 Session Identity

The gateway SHALL:
1. Support fixed session IDs (user-configured in watchers) and auto-created session IDs
2. For auto-created session IDs, persist that session identity across daemon restarts so the same agent conversation continues
3. For fixed session IDs, preserve them across reset operations
4. When a watcher with an auto-created session ID is reset, create a fresh session on the next message

### 3.3 Watcher State Persistence

The gateway SHALL:
1. Persist watcher runtime state across restarts, including at minimum the active session identity and paused/unpaused status
2. Recover gracefully from missing or corrupted state files rather than crashing

---

## 4. CLI Interface Requirements

### 4.1 Operational Commands

The gateway SHALL support the following commands via the `agent-chat-gateway` CLI:

| Command | Purpose |
|---------|---------|
| `start [--config FILE]` | Start the daemon service |
| `stop` | Stop the daemon service |
| `restart [--config FILE]` | Restart the daemon service |
| `status` | Show daemon status (running/not running, uptime, watcher count) |
| `list [--connector NAME]` | List all watchers and their status |
| `pause WATCHER [--connector NAME]` | Pause a watcher (stops processing messages) |
| `resume WATCHER [--connector NAME]` | Resume a paused watcher |
| `reset WATCHER [--connector NAME]` | Reset a watcher session and runtime state |
| `send ROOM [MESSAGE] [--file FILE] [--attach FILE] [--connector NAME]` | Send a message or file to a room |

### 4.2 Operational Behavior

The gateway SHALL:
1. Report watchers with their status (active/inactive/paused), agent assignment, session ID, and connector
2. When paused, stop a watcher from processing new messages until resumed
3. When resumed, re-enable message processing for a paused watcher
4. When reset, clear runtime session state according to the session identity rules in Section 3
5. When status is requested, indicate whether the daemon is running and display uptime if running
6. When a command requires a running daemon and the daemon is not running, fail clearly with a non-success exit code

### 4.3 Multi-Connector Support

When multiple connectors are configured, the gateway SHALL:
1. Allow the `list` command to show watchers across all connectors
2. Allow selective listing by connector with the `--connector` flag
3. For commands targeting a specific watcher, accept an optional `--connector` flag to disambiguate (default: first configured connector)
4. Return partial results with per-connector errors when some connectors fail during aggregated operations

---

## 5. Direct Message and File Upload

The gateway SHALL support sending messages and files to chat rooms outside the normal watcher flow:

1. Users SHALL be able to send a text message to a room via the `send` command
2. Users SHALL be able to upload a file with an optional caption via the `send` command with `--attach`
3. A send operation MAY include text, a file, or both
4. The system SHALL accept room identifiers in the formats supported by the configured connector (e.g., `#channel`, `@username`, room ID)
5. The system SHALL validate that local files exist before attempting to send or upload
6. The system SHALL reject conflicting input modes (e.g., inline text and `--file` simultaneously)
7. The system SHALL reject a send request that contains neither message content nor a file attachment

---

## 6. Role-Based Access Control (RBAC)

### 6.1 Roles and Assignment

The gateway SHALL support at least two distinct roles for users:

| Role | Assignment | Purpose |
|------|-----------|---------|
| Owner | Assigned by connector from trusted platform identity | Administrator; may use pre-approved tools and manage permissions |
| Guest | Assigned by connector from trusted platform identity | Limited user; restricted to configured allowed tools only |

Additionally, the gateway SHALL:
1. Recognize an anonymous role for unauthenticated users and reject their messages
2. Assign sender role from the trusted connector context, NOT from user-provided message content

### 6.2 Owner Tool Access

When an owner uses a tool:
1. If the tool is in the owner's auto-approved allow-list, the tool executes without further approval
2. If permissions are enabled and the tool is NOT in the auto-approved allow-list, the tool enters the human approval workflow
3. If permissions are disabled, all tools are auto-approved for owners

### 6.3 Guest Tool Access

When a guest uses a tool:
1. The tool MAY execute only if it matches the guest allow-list policy
2. Tools outside the guest allow-list SHALL be denied automatically
3. Denied tool attempts SHALL NOT enter the human approval workflow (fail immediately)

### 6.4 Tool Matching Rules

Tool allow-list policies SHALL support:
1. Matching by tool name (required)
2. Optional parameter-based matching to further restrict tool usage
3. Case-insensitive tool name matching where required by tool ecosystems
4. Full-pattern parameter matching (not loose substring matching)
5. Normalized file paths so equivalent spellings do not bypass policy
6. For tools with multiple extracted parameters, all extracted values MUST satisfy the configured allow policy

---

## 7. Human Approval Workflow

### 7.1 Triggering Approval

When permissions are enabled, the gateway SHALL:
1. Intercept tool actions by owners that fall outside the auto-approved set
2. Post an approval request visible in chat to eligible owners (other owners in the same room)
3. Include a short, human-typable approval ID in the request message

### 7.2 Approval Commands

The gateway SHALL support two approval commands in chat:
- `approve ID` — approve a pending tool request
- `deny ID` — deny a pending tool request

The gateway SHALL:
1. Intercept these commands and NOT forward them to the agent as normal chat input
2. Support case-insensitive approval ID matching
3. Return a validation error if the approval ID format is invalid
4. Return a clear error if the supplied approval ID does not correspond to a pending request
5. Enforce owner-only access to approval commands

### 7.3 Approval Timeout and Queueing

The gateway SHALL:
1. Expire each approval request after the configured permission timeout
2. Auto-deny expired requests and generate a visible timeout notification
3. While an approval is pending for a watcher/session, queue later user messages for that watcher until the approval is resolved

---

## 8. Configuration Requirements

### 8.1 Configuration Structure

The gateway SHALL require:
1. At least one connector
2. At least one agent backend
3. At least one watcher

The gateway SHALL validate that:
1. Connector names are unique
2. Watcher names are unique within their connector scope
3. Each watcher references an existing connector
4. Each watcher references an existing agent
5. If a default agent is specified, it references an existing agent

### 8.2 Path and Environment Handling

The gateway SHALL:
1. Require that path-based configuration values (e.g., working directories) exist at validation time
2. Resolve relative paths relative to the configuration file location
3. Support environment variable expansion in all configuration values (e.g., `$VARIABLE` or `${VARIABLE}`)
4. Optionally support a `.env` file colocated with the configuration file for variable expansion

### 8.3 Configuration Validation

The gateway SHALL:
1. Reject queue depth settings with negative values
2. If permission timeouts are enabled, require that the overall agent timeout is greater than the permission timeout
3. Prevent distinct watchers from reusing the same fixed session ID in a way that would create ambiguous routing

---

## 9. Error Handling and Recovery

### 9.1 Message Queue and Backpressure

The gateway SHALL:
1. Maintain bounded message queues per watcher to prevent unbounded memory growth
2. Reject excess messages when a queue is full, rather than blocking indefinitely
3. Provide a user-visible, rate-limited failure message when a message is rejected due to queue fullness

### 9.2 Agent Timeout

The gateway SHALL:
1. Return a user-visible timeout message if the agent exceeds the configured timeout
2. Terminate the agent invocation and not wait indefinitely

### 9.3 Agent Backend Failures

The gateway SHALL:
1. Return a sanitized user-visible error message if the agent backend fails
2. NOT expose internal error details, stack traces, or backend-specific internals to chat users

### 9.4 Connection Recovery

The gateway SHALL:
1. Be restartable without losing watcher runtime state
2. Resume operations using persisted state across restarts
3. Handle transient connector failures gracefully (e.g., temporary network loss, platform API downtime)

### 9.5 Graceful Shutdown

The gateway SHALL:
1. On shutdown signal (SIGTERM), allow in-flight message processing to complete (configurable timeout)
2. Drain queued messages up to the configured timeout
3. Post offline notifications after messages are fully drained

---

## 10. Attachment Handling

The gateway SHALL:
1. Support inbound messages with file attachments
2. Download files to local disk before forwarding the message to the agent
3. Provide the local file path to the agent for processing
4. Enforce configured limits on file size and download timeout
5. Inject a human-readable description of attachments into the agent prompt when the agent cannot process files natively

---

## 11. Scripting and Programmatic Access

The gateway SHALL provide programmatic session access for scripts:

1. A programmatic session interface SHALL support explicit start and stop lifecycle boundaries
2. Multiple sends on the same programmatic session SHALL reuse the same underlying agent session
3. Sending on an unstarted session SHALL fail explicitly
4. Programmatic sends SHALL return a normalized agent response object regardless of backend
5. Programmatic sends MAY support optional attachments
6. Programmatic sends MAY support optional per-message environment overrides when supported by the selected agent backend

---

## 12. Agent Backend Requirements

### 12.1 Common Requirements

All supported agent backends SHALL:
1. Present a normalized response shape to the rest of the system
2. Support creating a new session and sending subsequent turns to an existing session
3. When returning no textual output, still produce a non-empty placeholder response
4. Surface backend failures as structured errors that the gateway can translate into user-visible messages

### 12.2 Claude Backend

The Claude backend SHALL:
1. Support explicit session creation and resumed message sending
2. Support permission-hook behavior when permissions are enabled
3. Accept attachment context via prompt injection even if native file uploads are unsupported
4. Respect the configured timeout and terminate invocations that exceed it

### 12.3 OpenCode Backend

The OpenCode backend SHALL:
1. Support explicit session creation and subsequent session messaging
2. Map backend rate limiting into a structured rate-limit failure that the gateway can handle
3. NOT silently recreate a missing session when a send targets an unknown session ID
4. Support recovery from temporary backend service unavailability when conditions permit
5. For per-message environment overrides, either support them or clearly indicate unsupported status

---

## 13. Non-Goals

This specification does not define:
- Internal module organization or class hierarchies
- Specific subprocess commands or HTTP endpoints used internally
- Binary file formats for internal state persistence
- Implementation-specific connector or agent backend internals
- Exact performance targets or throughput requirements
