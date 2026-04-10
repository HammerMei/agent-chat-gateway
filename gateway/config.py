"""Configuration loader with environment variable expansion.

Shared config dataclasses (``PermissionConfig``, ``ToolRule``, ``AgentConfig``,
``ConnectorConfig``, ``WatcherConfig``) are defined in ``gateway.core.config``
and re-exported here so existing import paths continue to work.
"""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Re-export core config types — canonical definitions in gateway.core.config
from .core.config import (  # noqa: F401 — re-exports
    AgentConfig,
    ConnectorConfig,
    PermissionConfig,
    ToolRule,
    WatcherConfig,
)


@dataclass
class AttachmentConfig:
    max_file_size_mb: float = 10.0  # files larger than this are skipped (0 = no limit)
    download_timeout: int = 30  # seconds per file download
    cache_dir: str = "agent-chat.cache"  # relative to watcher's working_directory (legacy; unused when cache_dir_global is set)
    cache_dir_global: str = "~/.agent-chat-gateway/attachments"  # connector-global base dir for attachment downloads


@dataclass
class SchedulerConfig:
    """Configuration for the built-in job scheduler.

    default_timezone:
        IANA timezone used when ``--tz`` is not specified on ``acg schedule create``.
        Example: ``"Asia/Taipei"``, ``"America/New_York"``, ``"UTC"``.
        When empty, the ACG server's local timezone is used (and a warning is logged).

    completed_job_ttl_days:
        How long to retain COMPLETED jobs in jobs.json before purging them.
        0 = remove immediately when completed.
        Default: 7 days.
    """
    default_timezone: str = ""          # IANA timezone; empty = server local (with warning)
    completed_job_ttl_days: int = 7     # days to keep completed jobs (0 = delete immediately)


@dataclass
class GatewayConfig:
    connectors: list[ConnectorConfig]
    agents: dict[str, AgentConfig]
    default_agent: str
    watchers: list[WatcherConfig] = field(default_factory=list)
    max_queue_depth: int = 100  # max pending messages per room queue; 0 = unbounded
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)

    @property
    def agent(self) -> AgentConfig:
        """Return the default agent config (convenience accessor).

        Raises KeyError when default_agent is not present in agents — the config
        loader validates this invariant at load time, so this should never trigger
        in production.  Raising here is safer than silently falling back to the
        first agent, which would mask misconfiguration.
        """
        if self.default_agent not in self.agents:
            raise KeyError(
                f"default_agent '{self.default_agent}' not found in agents: "
                f"{list(self.agents)}"
            )
        return self.agents[self.default_agent]

    @staticmethod
    def from_file(path: str | Path) -> "GatewayConfig":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        # Auto-load .env from the same directory as config.yaml (no-op if absent)
        load_dotenv(path.parent / ".env")

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        if not isinstance(raw, dict):
            raise ValueError(
                f"Config file '{path}' must contain a YAML mapping at the top level, "
                f"got {type(raw).__name__}."
            )

        # Expand env vars in all string values recursively
        raw = _expand_env_vars(raw)

        config_dir = Path(path).parent

        # ── Connectors ────────────────────────────────────────────────────────

        connectors_raw = raw.get("connectors", [])
        if not connectors_raw:
            raise ValueError(
                "config.yaml must define at least one connector under 'connectors:'"
            )
        if not isinstance(connectors_raw, list):
            raise ValueError(
                f"config.yaml 'connectors:' must be a list (got {type(connectors_raw).__name__})."
            )

        connectors: list[ConnectorConfig] = []
        seen_connector_names: set[str] = set()
        for i, cc in enumerate(connectors_raw):
            if not isinstance(cc, Mapping):
                raise ValueError(
                    f"Connector entry at index {i} must be a mapping "
                    f"(got {type(cc).__name__})."
                )
            name = cc.get("name", "")
            connector_type = cc.get("type", "")
            if not name:
                raise ValueError("Each connector entry must have a 'name' field")
            if not connector_type:
                raise ValueError(f"Connector '{name}' must have a 'type' field")
            if name in seen_connector_names:
                raise ValueError(
                    f"Duplicate connector name '{name}' found. "
                    "Each connector must use a unique name."
                )
            seen_connector_names.add(name)

            # Resolve connector-level context_inject_files
            raw_ctx = cc.get("context_inject_files", [])
            ctx_files = _resolve_paths(raw_ctx, config_dir)

            # Resolve attachments.cache_dir_global relative to config dir
            # (consistent with working_directory resolution above)
            attach_raw = cc.get("attachments", {})
            if isinstance(attach_raw, dict):
                cache_dir_global = attach_raw.get("cache_dir_global", "")
                if (
                    cache_dir_global
                    and not cache_dir_global.startswith("~")
                    and not Path(cache_dir_global).is_absolute()
                ):
                    attach_raw["cache_dir_global"] = str(
                        (config_dir / cache_dir_global).resolve()
                    )
                # Write the resolved value back into the raw config
                cc["attachments"] = attach_raw

            # Store everything except name/type/context_inject_files as the raw connector config
            connector_raw = {
                k: v
                for k, v in cc.items()
                if k not in ("name", "type", "context_inject_files")
            }
            connectors.append(
                ConnectorConfig(
                    name=name,
                    type=connector_type,
                    raw=connector_raw,
                    context_inject_files=ctx_files,
                )
            )

        # ── Agents ────────────────────────────────────────────────────────────

        agents_raw = raw.get("agents") or {}
        if not isinstance(agents_raw, dict):
            raise ValueError(
                f"config.yaml 'agents:' must be a mapping (got {type(agents_raw).__name__}). "
                f"Expected a dict of agent names to config blocks."
            )
        default_agent = raw.get("default_agent", "")

        agents: dict[str, AgentConfig] = {}
        for agent_name, agent_raw in agents_raw.items():
            if not isinstance(agent_raw, Mapping):
                raise ValueError(
                    f"Agent '{agent_name}' config must be a mapping "
                    f"(got {type(agent_raw).__name__})."
                )
            perm_raw = agent_raw.get("permissions", {})
            if perm_raw and not isinstance(perm_raw, Mapping):
                raise ValueError(
                    f"Agent '{agent_name}': permissions must be a mapping "
                    f"(got {type(perm_raw).__name__})."
                )

            # Resolve context_inject_files (list) relative to the config file's directory
            raw_ctx = agent_raw.get("context_inject_files", [])
            ctx_files = _resolve_paths(raw_ctx, config_dir)

            # Resolve working_directory relative to the config file's directory
            working_directory = agent_raw.get("working_directory", "")
            if working_directory and not Path(working_directory).is_absolute():
                working_directory = str((config_dir / working_directory).resolve())

            # Validate: working_directory is required and must exist
            if not working_directory:
                raise ValueError(
                    f"Agent '{agent_name}': working_directory is required. "
                    f"Set it to the directory where the agent should run."
                )
            if not Path(working_directory).is_dir():
                raise ValueError(
                    f"Agent '{agent_name}': working_directory does not exist "
                    f"or is not a directory: '{working_directory}'"
                )

            agents[agent_name] = AgentConfig(
                name=agent_name,
                type=agent_raw.get("type", "claude"),
                command=agent_raw.get("command", "claude"),
                new_session_args=agent_raw.get("new_session_args", []),
                working_directory=working_directory,
                session_prefix=agent_raw.get("session_prefix", "agent-chat"),
                context_inject_files=ctx_files,
                owner_allowed_tools=_parse_tool_rules(
                    agent_raw.get("owner_allowed_tools", []), agent_name
                ),
                guest_allowed_tools=_parse_tool_rules(
                    agent_raw.get("guest_allowed_tools", []), agent_name
                ),
                timeout=agent_raw.get("timeout", 360),
                permissions=PermissionConfig(
                    enabled=perm_raw.get("enabled", False),
                    timeout=perm_raw.get("timeout", 300),
                    skip_owner_approval=perm_raw.get("skip_owner_approval", False),
                ),
            )

        if not agents:
            raise ValueError(
                "config.yaml must define at least one agent under 'agents:'"
            )

        # Validate that agent.timeout > permissions.timeout for all permission-enabled agents.
        # If agent.timeout <= permissions.timeout, the HTTP call can time out while a permission
        # request is still pending, leaving an orphaned PermissionRequest in the registry.
        for agent_name, agent_cfg in agents.items():
            if (
                agent_cfg.permissions.enabled
                and agent_cfg.timeout <= agent_cfg.permissions.timeout
            ):
                raise ValueError(
                    f"Agent '{agent_name}': timeout ({agent_cfg.timeout}s) must be greater than "
                    f"permissions.timeout ({agent_cfg.permissions.timeout}s). "
                    f"Suggested: set timeout to at least {agent_cfg.permissions.timeout + 60}s."
                )

        if not default_agent:
            default_agent = next(iter(agents))
        elif default_agent not in agents:
            raise ValueError(
                f"default_agent '{default_agent}' not found in agents: {list(agents)}"
            )

        # ── Watchers ──────────────────────────────────────────────────────────

        connector_names = {c.name for c in connectors}
        watchers: list[WatcherConfig] = []
        watchers_raw = raw.get("watchers", [])
        if watchers_raw and not isinstance(watchers_raw, list):
            raise ValueError(
                f"config.yaml 'watchers:' must be a list (got {type(watchers_raw).__name__})."
            )
        seen_watcher_names: set[str] = set()
        for i, wc in enumerate(watchers_raw):
            if not isinstance(wc, Mapping):
                raise ValueError(
                    f"Watcher entry at index {i} must be a mapping "
                    f"(got {type(wc).__name__})."
                )
            watcher_name = wc.get("name", "")
            if not watcher_name:
                raise ValueError("Each watcher entry must have a 'name' field")
            if watcher_name in seen_watcher_names:
                raise ValueError(
                    f"Duplicate watcher name '{watcher_name}' found. "
                    "Each watcher must use a unique name."
                )
            seen_watcher_names.add(watcher_name)

            watcher_room = wc.get("room", "")
            if not watcher_room:
                raise ValueError(
                    f"Watcher '{watcher_name}' must have a non-empty 'room' field"
                )

            watcher_connector = wc.get("connector", "")
            if watcher_connector and watcher_connector not in connector_names:
                raise ValueError(
                    f"Watcher '{watcher_name}' references unknown connector '{watcher_connector}'"
                )

            watcher_agent = wc.get("agent", default_agent)
            if watcher_agent not in agents:
                raise ValueError(
                    f"Watcher '{watcher_name}' references unknown agent '{watcher_agent}'"
                )

            # Resolve watcher-level context_inject_files
            raw_ctx = wc.get("context_inject_files", [])
            ctx_files = _resolve_paths(raw_ctx, config_dir)

            watchers.append(
                WatcherConfig(
                    name=watcher_name,
                    connector=watcher_connector or connectors[0].name,
                    room=watcher_room,
                    agent=watcher_agent,
                    session_id=wc.get("session_id") or None,
                    context_inject_files=ctx_files,
                    online_notification=wc.get(
                        "online_notification", "✅ _Agent online_"
                    ),
                    offline_notification=wc.get(
                        "offline_notification", "❌ _Agent offline_"
                    ),
                )
            )

        # Validate no duplicate sticky session IDs across watchers — duplicate IDs
        # cause silent overwrite of session→room / session→connector routing maps,
        # leading to permission notifications landing in the wrong room.
        seen_session_ids: set[str] = set()
        for wc in watchers:
            if wc.session_id:
                if wc.session_id in seen_session_ids:
                    raise ValueError(
                        f"Duplicate sticky session_id '{wc.session_id}' found across "
                        f"watchers. Each watcher must use a unique session_id."
                    )
                seen_session_ids.add(wc.session_id)

        max_queue_depth = raw.get("max_queue_depth", 100)
        if not isinstance(max_queue_depth, int):
            raise ValueError(
                f"config.yaml 'max_queue_depth' must be an integer (got {type(max_queue_depth).__name__})."
            )
        if max_queue_depth < 0:
            raise ValueError("config.yaml 'max_queue_depth' must be >= 0")

        # ── Scheduler ─────────────────────────────────────────────────────────

        scheduler_raw = raw.get("scheduler", {}) or {}
        if not isinstance(scheduler_raw, Mapping):
            raise ValueError(
                f"config.yaml 'scheduler:' must be a mapping (got {type(scheduler_raw).__name__})."
            )
        scheduler_ttl = scheduler_raw.get("completed_job_ttl_days", 7)
        if not isinstance(scheduler_ttl, int) or scheduler_ttl < 0:
            raise ValueError(
                "config.yaml 'scheduler.completed_job_ttl_days' must be a non-negative integer."
            )
        scheduler_cfg = SchedulerConfig(
            default_timezone=scheduler_raw.get("default_timezone", ""),
            completed_job_ttl_days=scheduler_ttl,
        )

        return GatewayConfig(
            connectors=connectors,
            agents=agents,
            default_agent=default_agent,
            watchers=watchers,
            max_queue_depth=max_queue_depth,
            scheduler=scheduler_cfg,
        )


def _parse_tool_rules(raw_list: list, agent_name: str = "") -> list["ToolRule"]:
    """Parse a list of raw config entries into ToolRule objects."""
    rules = []
    for i, entry in enumerate(raw_list):
        try:
            rules.append(ToolRule.from_config(entry))
        except ValueError as e:
            raise ValueError(
                f"Agent '{agent_name}': invalid tool rule at index {i}: {e}"
            ) from e
    return rules


def _resolve_paths(paths: list, base_dir: Path) -> list[str]:
    """Resolve a list of path strings relative to base_dir."""
    resolved = []
    for p in paths:
        if p and not Path(p).is_absolute():
            resolved.append(str((base_dir / p).resolve()))
        elif p:
            resolved.append(p)
    return resolved


_config_logger = logging.getLogger("agent-chat-gateway.config")


def _expand_env_vars(obj, _path: str = ""):
    """Recursively expand $ENV_VAR and ${ENV_VAR} in string values.

    Raises ValueError when an unresolved placeholder (e.g. ``${MISSING_VAR}``)
    is detected so that startup fails immediately with a clear diagnostic instead
    of silently using the literal placeholder string as a config value.
    """
    if isinstance(obj, str):
        expanded = os.path.expandvars(obj)
        if re.search(r"\$\{?\w+", expanded):
            raise ValueError(
                f"Unresolved environment variable in config key '{_path}': {expanded!r}. "
                f"Set the environment variable or remove the placeholder from config.yaml."
            )
        return expanded
    elif isinstance(obj, dict):
        return {
            k: _expand_env_vars(v, f"{_path}.{k}" if _path else k)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_expand_env_vars(item, f"{_path}[{i}]") for i, item in enumerate(obj)]
    return obj
