"""Core configuration types — platform-agnostic dataclasses.

These types are the canonical definitions used throughout the core layer.
``gateway.config`` re-exports them for backward compatibility and adds
gateway-level concerns (``GatewayConfig``, YAML parsing, env-var expansion).

All platform-specific settings (server URL, credentials, user allow-lists)
belong in the connector's own config (e.g. RocketChatConfig).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .connector import UserRole

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

        Concatenates three layers in order:
          1. Connector-level files (from ConnectorConfig.context_inject_files)
          2. Agent-level files    (from AgentConfig.context_inject_files)
          3. Watcher-level files  (passed in directly as watcher_ctx)

        Empty lists at any level are silently skipped.
        """
        result: list[str] = []
        connector_cfg = self.connector_configs.get(connector_name)
        if connector_cfg:
            result.extend(connector_cfg.context_inject_files)
        agent_cfg = self.agent_config(agent_name)
        result.extend(agent_cfg.context_inject_files)
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
