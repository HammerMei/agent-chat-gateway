"""Core configuration types — platform-agnostic dataclasses.

These types are the canonical definitions used throughout the core layer.
``gateway.config`` re-exports them for backward compatibility and adds
gateway-level concerns (``GatewayConfig``, YAML parsing, env-var expansion).

All platform-specific settings (server URL, credentials, user allow-lists)
belong in the connector's own config (e.g. RocketChatConfig).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .connector import UserRole

# Built-in context files shipped inside the gateway package.
# Resolved relative to this file: gateway/core/config.py → gateway/core/ → gateway/ → gateway/contexts/
_BUILTIN_CONTEXTS_DIR = Path(__file__).parent.parent / "contexts"

# Built-in tool rules automatically prepended to every agent's owner_allowed_tools.
# These are gateway-specific Bash commands that the agent should always be able to
# call without triggering a 🔐 human-approval prompt — they are the gateway's own
# management interface, not arbitrary shell commands.
#
# Rule: `agent-chat-gateway send ...` — send messages / attach files to RC rooms.
# Rule: `agent-chat-gateway schedule ...` — create/list/delete/pause/resume jobs.
_BUILTIN_OWNER_TOOL_RULES: "list[ToolRule]" = []  # populated after ToolRule is defined

# ── Shared config types ──────────────────────────────────────────────────────

@dataclass
class PermissionConfig:
    """Per-agent permission approval configuration."""
    enabled: bool = False
    timeout: int = 300                              # seconds before auto-deny
    skip_owner_approval: bool = False               # when True, all owner tool calls are auto-approved without RC notification


@dataclass
class ToolRule:
    """A single entry in an owner or guest tool allow list.

    tool:   Regex matched against the tool name (case-insensitive, fullmatch).
            Supports wildcards, e.g. "mcp__rocketchat__get_.*".
    params: Optional regex matched against the tool's primary parameter
            (case-insensitive, fullmatch).  If omitted, only the tool name
            is checked.  The field used for matching depends on the tool:
              Bash / bash   → command string
              WebFetch      → url string
              Read/Edit/
              Write         → file_path string
              unknown / MCP → full tool_input serialized as JSON
    """
    tool: str
    params: str | None = None

    @staticmethod
    def from_config(raw) -> "ToolRule":
        """Parse a config entry — must be a dict with a 'tool' key.

        Validates and pre-compiles both regex patterns at config-load time so
        that misconfigured rules raise a clear ValueError immediately rather
        than producing a cryptic re.error during a live tool-call match.
        """
        import re as _re
        if isinstance(raw, dict):
            tool_pattern = raw.get("tool", "")
            params_pattern = raw.get("params") or None
            # Validate tool regex at config-load time
            try:
                _re.compile(tool_pattern, _re.IGNORECASE)
            except _re.error as e:
                raise ValueError(
                    f"Invalid regex for 'tool' field {tool_pattern!r}: {e}"
                ) from e
            # Validate params regex at config-load time (if provided)
            if params_pattern is not None:
                try:
                    _re.compile(params_pattern, _re.IGNORECASE | _re.DOTALL)
                except _re.error as e:
                    raise ValueError(
                        f"Invalid regex for 'params' field {params_pattern!r}: {e}"
                    ) from e
            return ToolRule(
                tool=tool_pattern,
                params=params_pattern,
            )
        raise ValueError(
            f"Invalid tool rule: {raw!r}. Expected a dict with a 'tool' key, "
            "e.g. {{tool: Bash, params: 'git .*'}}"
        )


# Populate the built-in owner tool rules now that ToolRule is defined.
# These allow agents to call gateway management commands (send / schedule)
# without triggering a 🔐 human-approval prompt in Rocket.Chat.
_BUILTIN_OWNER_TOOL_RULES = [
    ToolRule(tool="Bash", params="agent-chat-gateway\\s+send\\s+.*"),
    ToolRule(tool="Bash", params="agent-chat-gateway\\s+schedule\\s+.*"),
    # date is a read-only command used by agents to compute timestamps.
    # It is safe to auto-approve for owners so that compound bash commands
    # containing $(date ...) sub-expressions do not trigger approval prompts.
    ToolRule(tool="Bash", params="date(\\s+.*)?"),
]


@dataclass
class AgentConfig:
    name: str = "default"
    type: str = "claude"
    command: str = "claude"
    new_session_args: list[str] = field(default_factory=list)
    working_directory: str = ""  # cwd for sidecar process (opencode agents); "" = inherit gateway cwd
    session_prefix: str = "agent-chat"  # prefix for session names/titles
    context_inject_files: list[str] = field(default_factory=list)  # paths injected once per session
    owner_allowed_tools: list[ToolRule] = field(default_factory=list)  # auto-approved for owners
    guest_allowed_tools: list[ToolRule] = field(default_factory=list)  # auto-approved for guests
    timeout: int = 360           # seconds to wait for the agent to respond; must be > permissions.timeout
    permissions: PermissionConfig = field(default_factory=PermissionConfig)

    def effective_owner_allowed_tools(self) -> "list[ToolRule]":
        """Return owner_allowed_tools with built-in gateway rules prepended.

        The built-in rules (``agent-chat-gateway send`` and
        ``agent-chat-gateway schedule``) are always included so that agents can
        call gateway management commands without triggering a 🔐 human-approval
        prompt — no user config required.  User-defined rules follow the
        built-in ones so that custom patterns can further extend the allow list.
        """
        return list(_BUILTIN_OWNER_TOOL_RULES) + list(self.owner_allowed_tools)


@dataclass
class ConnectorConfig:
    """Configuration for a single connector instance.

    'name' is the unique identifier used for state file namespacing and CLI routing.
    'type' determines which Connector implementation is instantiated.
    'raw' holds the connector-specific config dict, passed directly to the connector factory.
    'context_inject_files' holds connector-level context paths injected into every session
    on this connector (layer 1 of 3; agent and watcher layers are added on top).
    """

    name: str
    type: str       # "rocketchat" | "script"
    raw: dict       # type-specific config, passed to connector factory
    context_inject_files: list[str] = field(default_factory=list)


@dataclass
class WatcherConfig:
    """Static definition of a watcher (connector + room + agent binding).

    Defined in config.yaml under 'watchers:'. The gateway starts all configured
    watchers on startup — no runtime add-watcher commands are needed.

    session_id:
      - Set to a session ID string to pin this watcher to an existing session (sticky).
        The session ID is never cleared, even by 'reset'.
      - Set to None (or omit) to let the gateway auto-create a session on first start.
        The generated session ID is stored in state.json and cleared by 'reset'.
    """

    name: str                                        # unique watcher name (used in CLI commands)
    connector: str                                   # must match a ConnectorConfig.name
    room: str                                        # room name or @username for DM
    agent: str                                       # must match an AgentConfig.name
    session_id: str | None = None                    # sticky session ID; None = auto-create
    context_inject_files: list[str] = field(default_factory=list)  # watcher-level context (layer 3)
    online_notification: str | None = "✅ _Agent online_"   # message text on startup; None = suppress
    offline_notification: str | None = "❌ _Agent offline_" # message text on shutdown; None = suppress


# ── CoreConfig ───────────────────────────────────────────────────────────────

@dataclass
class CoreConfig:
    """Platform-agnostic gateway configuration consumed by SessionManager and MessageProcessor."""

    agents: dict[str, AgentConfig] = field(default_factory=dict)
    default_agent: str = ""
    connector_configs: dict[str, ConnectorConfig] = field(default_factory=dict)
    max_queue_depth: int = 100  # max pending messages per room; 0 = unbounded (not recommended)

    def agent_config(self, name: str) -> AgentConfig:
        """Return the AgentConfig for the given agent name, falling back to default."""
        if name and name in self.agents:
            return self.agents[name]
        if self.default_agent and self.default_agent in self.agents:
            return self.agents[self.default_agent]
        # Last resort: return first available config
        if self.agents:
            return next(iter(self.agents.values()))
        return AgentConfig()

    def env_for_role(self, role: UserRole, agent_name: str = "") -> dict[str, str]:
        """Return the subprocess environment variables for the given user role.

        Passed as ``env`` to AgentBackend.send() so the permission broker can
        identify which role is making a request.

        ACG_ROLE is hardcoded ("owner" / "guest") and never user-configurable.
        Tool allow-list enforcement is handled entirely by the permission broker
        using the structured ToolRule lists from config.
        """
        if role == UserRole.OWNER:
            return {"ACG_ROLE": "owner"}
        if role == UserRole.ANONYMOUS:
            raise ValueError(
                "ANONYMOUS users are not permitted to interact with agent sessions"
            )
        return {"ACG_ROLE": "guest"}

    def context_inject_files_for(
        self,
        connector_name: str,
        agent_name: str,
        watcher_ctx: list[str],
    ) -> list[str]:
        """Return the ordered list of context files to inject for a watcher session.

        Concatenates four layers in order:
          0. Built-in system files (auto-injected; no user config needed):
               - rc-gateway-context.md  — injected for every Rocket.Chat connector
               - scheduling-context.md  — injected for every configured connector
          1. Connector-level files (from ConnectorConfig.context_inject_files)
          2. Agent-level files    (from AgentConfig.context_inject_files)
          3. Watcher-level files  (passed in directly as watcher_ctx)

        Built-in injection only fires when ConnectorConfig exists for the given
        connector name.  This keeps unit-test CoreConfigs (which typically have no
        connector entries) unaffected by auto-injection.

        Empty lists at any level are silently skipped.
        """
        result: list[str] = []
        connector_cfg = self.connector_configs.get(connector_name)

        # Layer 0: built-in system context files (shipped inside the package)
        if connector_cfg is not None:
            if connector_cfg.type == "rocketchat":
                result.append(str(_BUILTIN_CONTEXTS_DIR / "rc-gateway-context.md"))
            result.append(str(_BUILTIN_CONTEXTS_DIR / "scheduling-context.md"))
            # Layer 1: connector-level user files
            result.extend(connector_cfg.context_inject_files)

        # Layer 2: agent-level user files
        agent_cfg = self.agent_config(agent_name)
        result.extend(agent_cfg.context_inject_files)

        # Layer 3: watcher-level user files
        result.extend(watcher_ctx)
        return result

    def timeout_for(self, agent_name: str) -> int:
        """Return the configured response timeout (seconds) for the given agent."""
        return self.agent_config(agent_name).timeout

    @classmethod
    def from_gateway_config(cls, cfg) -> "CoreConfig":
        """Derive a CoreConfig from a GatewayConfig (transition helper)."""
        return cls(
            agents=cfg.agents,
            default_agent=cfg.default_agent,
            connector_configs={c.name: c for c in cfg.connectors},
            max_queue_depth=cfg.max_queue_depth,
        )
