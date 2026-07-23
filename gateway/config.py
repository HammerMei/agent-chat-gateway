"""Configuration loader.

Shared config dataclasses (``PermissionConfig``, ``ToolRule``, ``AgentConfig``,
``ConnectorConfig``, ``WatcherConfig``) are defined in ``gateway.core.config``
and re-exported here so existing import paths continue to work.

``GatewayConfig.from_file()`` no longer resolves ``$VAR``/``${VAR}`` in
config values (docs/design/config-tool.md decision 6, final revision) —
secrets live directly in config.yaml (``chmod 0600``), and any pre-existing
``.env``-backed config is auto-migrated into that form on the first
``agent-chat-gateway start`` (``gateway/config_migrate.py``) or the config
TUI's launch, both enforced, not optional. An audit before removing this
found ambient (non-``.env``) ``$VAR`` resolution had no real caller anywhere
in this project — no systemd unit, no K8s manifest, no doc recommending it,
no committed example using it; only unit tests exercising the mechanism
itself. ``_expand_env_vars()``/``ENV_VAR_REF_RE`` below are KEPT (not dead
code) — ``gateway/config_migrate.py``'s one-time migration still needs them
to resolve a legacy ``.env``-backed value into a literal at migration time;
they're simply no longer called from the normal load path. Once migrated
(or if a value merely happens to look like ``${SOMETHING}``), it is treated
as a plain string like any other — deliberately, so a password that
happens to resemble a placeholder is never silently misinterpreted.
"""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Re-export core config types — canonical definitions in gateway.core.config
from .core.config import (  # noqa: F401 — re-exports
    AgentConfig,
    ConnectorConfig,
    HistoryHandoffConfig,
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

    completed_job_ttl_days:
        How long to retain COMPLETED jobs in jobs.json before purging them.
        0 = remove immediately when completed.
        Default: 7 days.
    """
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

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        if not isinstance(raw, dict):
            raise ValueError(
                f"Config file '{path}' must contain a YAML mapping at the top level, "
                f"got {type(raw).__name__}."
            )

        # No $VAR/${VAR} expansion here — see module docstring. Any such
        # string in a loaded config is treated as a plain literal.

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

        connector_defaults = _extract_defaults_block(
            raw, "connector_defaults", frozenset({"name"})
        )

        connectors: list[ConnectorConfig] = []
        seen_connector_names: set[str] = set()
        for i, cc_raw in enumerate(connectors_raw):
            if not isinstance(cc_raw, Mapping):
                raise ValueError(
                    f"Connector entry at index {i} must be a mapping "
                    f"(got {type(cc_raw).__name__})."
                )
            cc = _deep_merge(connector_defaults, cc_raw)
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

            # Store everything except name/type/context_inject_files/description
            # as the raw connector config. 'description' is an optional,
            # informational-only annotation (shown by the config TUI) — it
            # must never leak into connector `raw` (which is passed verbatim
            # to each connector type's from_connector_config()).
            connector_raw = {
                k: v
                for k, v in cc.items()
                if k not in ("name", "type", "context_inject_files", "description")
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

        agent_defaults = _extract_defaults_block(raw, "agent_defaults", frozenset())
        tool_presets = _parse_tool_presets(raw)

        agents: dict[str, AgentConfig] = {}
        for agent_name, agent_raw_entry in agents_raw.items():
            if not isinstance(agent_raw_entry, Mapping):
                raise ValueError(
                    f"Agent '{agent_name}' config must be a mapping "
                    f"(got {type(agent_raw_entry).__name__})."
                )
            agent_raw = _deep_merge(agent_defaults, agent_raw_entry)
            perm_raw = agent_raw.get("permissions", {})
            if perm_raw and not isinstance(perm_raw, Mapping):
                raise ValueError(
                    f"Agent '{agent_name}': permissions must be a mapping "
                    f"(got {type(perm_raw).__name__})."
                )

            # Resolve context_inject_files (list) relative to the config file's directory
            raw_ctx = agent_raw.get("context_inject_files", [])
            ctx_files = _resolve_paths(raw_ctx, config_dir)

            # Resolve working_directory: expand a leading ~ first (matching
            # the cache_dir_global handling below), then resolve relative to
            # the config file's directory if still not absolute.
            working_directory = agent_raw.get("working_directory", "")
            if working_directory:
                working_directory = str(Path(working_directory).expanduser())
                if not Path(working_directory).is_absolute():
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

            lazy_instruction_loading = agent_raw.get("lazy_instruction_loading", True)
            if not isinstance(lazy_instruction_loading, bool):
                raise ValueError(
                    f"Agent '{agent_name}': lazy_instruction_loading must be a boolean"
                )

            agents[agent_name] = AgentConfig(
                name=agent_name,
                type=agent_raw.get("type", "claude"),
                command=agent_raw.get("command", "claude"),
                new_session_args=agent_raw.get("new_session_args", []),
                working_directory=working_directory,
                session_prefix=agent_raw.get("session_prefix", "agent-chat"),
                lazy_instruction_loading=lazy_instruction_loading,
                context_inject_files=ctx_files,
                owner_allowed_tools=_resolve_tool_entries(
                    agent_raw.get("owner_allowed_tools", []),
                    tool_presets,
                    agent_name,
                    "owner_allowed_tools",
                ),
                guest_allowed_tools=_resolve_tool_entries(
                    agent_raw.get("guest_allowed_tools", []),
                    tool_presets,
                    agent_name,
                    "guest_allowed_tools",
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

        watcher_defaults = _extract_defaults_block(
            raw, "watcher_defaults", frozenset({"name", "room", "rooms", "session_id"})
        )

        seen_watcher_names: set[str] = set()
        for i, wc_raw in enumerate(watchers_raw):
            if not isinstance(wc_raw, Mapping):
                raise ValueError(
                    f"Watcher entry at index {i} must be a mapping "
                    f"(got {type(wc_raw).__name__})."
                )
            wc = _deep_merge(watcher_defaults, wc_raw)

            # ── room / rooms: exactly one form, 'room' is a single-item alias ──
            raw_room = wc.get("room")
            raw_rooms = wc.get("rooms")
            if raw_room and raw_rooms:
                raise ValueError(
                    f"Watcher entry at index {i}: set either 'room' or 'rooms', not both."
                )
            if raw_rooms is not None:
                if not isinstance(raw_rooms, list) or not raw_rooms:
                    raise ValueError(
                        f"Watcher entry at index {i}: 'rooms' must be a non-empty list "
                        "of room names."
                    )
                if not all(isinstance(r, str) and r for r in raw_rooms):
                    raise ValueError(
                        f"Watcher entry at index {i}: 'rooms' entries must be "
                        "non-empty strings."
                    )
                if len(set(raw_rooms)) != len(raw_rooms):
                    dupes = sorted({r for r in raw_rooms if raw_rooms.count(r) > 1})
                    raise ValueError(
                        f"Watcher entry at index {i}: 'rooms' contains duplicate "
                        f"room(s): {dupes}."
                    )
                rooms_list = list(raw_rooms)
            elif raw_room:
                rooms_list = [raw_room]
            else:
                raise ValueError(
                    f"Watcher entry at index {i} must have a non-empty "
                    "'room' or 'rooms' field"
                )

            # 'name' / 'session_id' pin a single sticky identity — only meaningful
            # when the entry expands to exactly one watcher.
            explicit_name = wc.get("name") or None
            explicit_session_id = wc.get("session_id") or None
            if len(rooms_list) > 1:
                if explicit_name:
                    raise ValueError(
                        f"Watcher entry at index {i}: 'name' can only be set when "
                        f"there is exactly one room (found {len(rooms_list)} in "
                        "'rooms') — remove 'name' or split into single-room entries."
                    )
                if explicit_session_id:
                    raise ValueError(
                        f"Watcher entry at index {i}: 'session_id' can only be set "
                        f"when there is exactly one room (found {len(rooms_list)} in "
                        "'rooms') — remove 'session_id' or split into single-room "
                        "entries."
                    )

            watcher_connector = wc.get("connector", "")
            if watcher_connector and watcher_connector not in connector_names:
                raise ValueError(
                    f"Watcher entry at index {i} references unknown connector "
                    f"'{watcher_connector}'"
                )
            resolved_connector = watcher_connector or connectors[0].name

            watcher_agent = wc.get("agent", default_agent)
            if watcher_agent not in agents:
                raise ValueError(
                    f"Watcher entry at index {i} references unknown agent "
                    f"'{watcher_agent}'"
                )

            # Resolve watcher-level context_inject_files (shared across expanded rooms)
            raw_ctx = wc.get("context_inject_files", [])
            ctx_files = _resolve_paths(raw_ctx, config_dir)

            hh_raw = wc.get("history_handoff", {}) or {}
            history_handoff = HistoryHandoffConfig(
                enabled=hh_raw.get("enabled", False),
                fetch_count=hh_raw.get("fetch_count", 50),
                verbatim_tail=hh_raw.get("verbatim_tail", 15),
            )

            for room in rooms_list:
                watcher_name = explicit_name or _auto_watcher_name(
                    resolved_connector, room
                )
                if "/" in watcher_name:
                    raise ValueError(
                        f"Watcher name '{watcher_name}' must not contain '/' — "
                        "watcher names are used as filesystem path components "
                        "(e.g. <working_directory>/.acg-attachments/<name>, "
                        "<RUNTIME_DIR>/system-prompts/<name>.md) "
                        "and a '/' could escape the intended directory."
                    )
                if watcher_name in seen_watcher_names:
                    origin = (
                        "explicit 'name:'"
                        if explicit_name
                        else f"auto-generated from connector '{resolved_connector}' "
                        f"+ room '{room}'"
                    )
                    raise ValueError(
                        f"Duplicate watcher name '{watcher_name}' found ({origin}). "
                        "Each watcher must use a unique name — set an explicit "
                        "'name:' to disambiguate."
                    )
                seen_watcher_names.add(watcher_name)

                watchers.append(
                    WatcherConfig(
                        name=watcher_name,
                        connector=resolved_connector,
                        room=room,
                        agent=watcher_agent,
                        session_id=explicit_session_id,
                        context_inject_files=ctx_files,
                        online_notification=wc.get("online_notification"),
                        offline_notification=wc.get("offline_notification"),
                        history_handoff=history_handoff,
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


def _extract_defaults_block(
    raw: dict, key: str, forbidden_keys: frozenset[str]
) -> dict:
    """Pop and validate a top-level ``<x>_defaults:`` mapping.

    Returns an empty dict if the key is absent. Raises ValueError if the
    block is not a mapping, or if it sets a key that identifies an
    individual entry (e.g. ``name``) rather than something safe to inherit
    across every entry.

    An optional ``description`` on the defaults block itself (annotating the
    shared block, shown by the config TUI) is stripped from the returned
    dict — it must never deep-merge into every entry, or every entry would
    end up displaying the defaults block's description as its own.
    """
    block = raw.get(key, {}) or {}
    if not isinstance(block, Mapping):
        raise ValueError(
            f"config.yaml '{key}:' must be a mapping (got {type(block).__name__})."
        )
    bad = sorted(forbidden_keys & block.keys())
    if bad:
        raise ValueError(
            f"config.yaml '{key}:' must not set {bad} — these fields identify "
            "an individual entry and must be set per-entry, not inherited."
        )
    result = dict(block)
    result.pop("description", None)
    return result


def _deep_copy(value):
    """Recursively copy dicts/lists so merged config entries never alias."""
    if isinstance(value, Mapping):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _deep_merge(base: Mapping, override: Mapping) -> dict:
    """Deep-merge two mappings; ``override`` wins at every level.

    - Both values are dicts -> recursively merged.
    - Otherwise (list, scalar, or ``None``) -> the override value replaces
      the base value verbatim. An explicit ``null`` in ``override``
      intentionally suppresses a base value, rather than being treated as
      "unset".
    - Always returns a brand-new nested structure so the result never shares
      a mutable dict/list with ``base`` or ``override``. This matters
      because per-entry parsing later mutates dicts in place (e.g. resolving
      ``attachments.cache_dir_global`` to an absolute path) — without a deep
      copy, that mutation would leak into a shared ``*_defaults`` block and
      corrupt every other entry merged against it.
    """
    merged = {k: _deep_copy(v) for k, v in base.items()}
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, Mapping):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = _deep_copy(v)
    return merged


def _parse_tool_presets(raw: dict) -> dict[str, list["ToolRule"]]:
    """Parse and validate the top-level ``tool_presets:`` block.

    Each preset is a named list of inline tool-rule dicts (same shape as
    ``owner_allowed_tools``/``guest_allowed_tools`` entries). Presets are
    flat: a preset's rule list may not reference another preset by name.
    All presets are parsed and regex-validated eagerly here, even if unused
    by any agent, so a broken preset fails fast at config load.
    """
    presets_raw = raw.get("tool_presets", {}) or {}
    if not isinstance(presets_raw, Mapping):
        raise ValueError(
            f"config.yaml 'tool_presets:' must be a mapping "
            f"(got {type(presets_raw).__name__})."
        )
    presets: dict[str, list[ToolRule]] = {}
    for preset_name, rules_raw in presets_raw.items():
        if not isinstance(rules_raw, list):
            raise ValueError(
                f"tool_presets['{preset_name}'] must be a list of tool rules "
                f"(got {type(rules_raw).__name__})."
            )
        rules: list[ToolRule] = []
        for i, entry in enumerate(rules_raw):
            if isinstance(entry, str):
                raise ValueError(
                    f"tool_presets['{preset_name}'][{i}]: presets cannot reference "
                    f"another preset ('{entry}') — a preset must be a flat list of "
                    "inline tool rules."
                )
            try:
                rules.append(ToolRule.from_config(entry))
            except ValueError as e:
                raise ValueError(
                    f"tool_presets['{preset_name}']: invalid tool rule at index {i}: {e}"
                ) from e
        presets[preset_name] = rules
    return presets


def _resolve_tool_entries(
    raw_list: list,
    presets: dict[str, list["ToolRule"]],
    agent_name: str,
    field_name: str,
) -> list["ToolRule"]:
    """Resolve one agent's owner/guest_allowed_tools list into ToolRule objects.

    Each entry is either a string (the name of a ``tool_presets:`` entry,
    expanded in place) or a dict (an inline ``{tool, params}`` rule). Both
    forms may be freely mixed; list order is preserved.
    """
    rules: list[ToolRule] = []
    for i, entry in enumerate(raw_list):
        if isinstance(entry, str):
            preset = presets.get(entry)
            if preset is None:
                available = ", ".join(sorted(presets)) or "(none defined)"
                raise ValueError(
                    f"Agent '{agent_name}': unknown tool preset '{entry}' in "
                    f"{field_name}[{i}]. Available presets: {available}"
                )
            rules.extend(preset)
            continue
        try:
            rules.append(ToolRule.from_config(entry))
        except ValueError as e:
            raise ValueError(
                f"Agent '{agent_name}': invalid tool rule at index {i} in "
                f"{field_name}: {e}"
            ) from e
    return rules


_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")
_NAME_COLLAPSE_DASH_RE = re.compile(r"-{2,}")


def _sanitize_room_for_name(room: str) -> str:
    """Turn a room identifier into a filesystem/CLI-safe watcher-name fragment.

    - A leading '@' (DM room, e.g. '@alice') becomes a 'dm-' prefix: '@alice' -> 'dm-alice'.
    - Any character outside [A-Za-z0-9._-] (including '/') becomes '-'.
    - Runs of '-' collapse to one; leading/trailing '-' and '.' are stripped.
    """
    prefix = "dm-" if room.startswith("@") else ""
    body = room[1:] if room.startswith("@") else room
    body = _NAME_SANITIZE_RE.sub("-", body)
    sanitized = _NAME_COLLAPSE_DASH_RE.sub("-", prefix + body).strip("-.")
    if not sanitized:
        raise ValueError(
            f"Could not derive a safe watcher name from room {room!r} — "
            "set an explicit 'name:' for this entry."
        )
    return sanitized


def _auto_watcher_name(connector: str, room: str) -> str:
    """Deterministic watcher name for a (connector, room) pair: '<connector>-<room>'."""
    return f"{connector}-{_sanitize_room_for_name(room)}"


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

# $VAR / ${VAR} reference pattern — the one place this is defined.
# gateway/config_migrate.py's migration imports this directly (code-review
# finding: it used to keep its own independent copy of this exact regex,
# which had already drifted out of sync once).
ENV_VAR_REF_RE = re.compile(r"\$\{?\w+")


def _expand_env_vars(obj, _path: str = ""):
    """Recursively expand $ENV_VAR and ${ENV_VAR} in string values.

    NOT called by `GatewayConfig.from_file()` (see module docstring) — the
    real gateway loader treats `$VAR`/`${VAR}` as a plain literal string,
    same as everything else. This function's only remaining caller is
    `gateway/config_migrate.py`'s one-time migration, which uses it to
    resolve a legacy `.env`-backed value into its literal form.

    Raises ValueError when an unresolved placeholder (e.g. ``${MISSING_VAR}``)
    is detected, so a migration fails loudly rather than silently writing
    the literal placeholder string into config.yaml as if it were the real
    secret value.
    """
    if isinstance(obj, str):
        expanded = os.path.expandvars(obj)
        # Check for unresolved placeholders on the *original* string, not the
        # expanded result.  Scanning the expanded value causes false positives
        # when a resolved secret itself contains a $WORD pattern (e.g. a
        # password like "myPass$HM").  A placeholder is truly unresolved only
        # when it still appears verbatim in the expanded output.
        unresolved = [
            m.group()
            for m in ENV_VAR_REF_RE.finditer(obj)
            if m.group() in expanded
        ]
        if unresolved:
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
