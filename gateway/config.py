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
from typing import Literal

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

# v0.2's global `*_defaults:` blocks (removed in v0.3 — see docs/migration-0.3.md) merged
# flatly and unconditionally into EVERY entry of a kind, regardless of type: setting
# `command`/`type` there to give claude agents a custom wrapper silently broke any
# opencode agent that didn't override it (gateway/agents/opencode/adapter.py execs
# `agent_cfg.command` directly as the sidecar binary). Named `*_templates:` + a per-entry
# `inherits:` field (below) replace them — a template only ever applies to entries that
# explicitly opt in, so type-specific fields are finally safe to share. A leftover old key
# is a hard, actionable error rather than a silent behavior change.
_REMOVED_DEFAULTS_KEYS: dict[str, str] = {
    "agent_defaults": "agent_templates",
    "connector_defaults": "connector_templates",
    "watcher_defaults": "watcher_templates",
}

# Single source of truth for the identity keys a named `*_templates:` block may
# not set — the keys that identify one specific entry, and so cannot be shared
# by everything inheriting the template.  Passed to _parse_templates_block().
#
# This existed as four byte-identical `frozenset({"name", "room", "rooms",
# "session_id"})` literals (two here, one in config_validate, one inline in the
# config tool) plus a fifth copy in a dict there, with a comment claiming unit
# tests kept them in sync.  No such test existed, and nothing imported anything
# — they were four hand-maintained copies of one rule.  The connector and agent
# sets were duplicated the same way.
#
# Kind strings are plain ("agent"/"connector"/"watcher"), not the
# `<kind>_templates` block names, and not the retired `*_defaults` names above.
TEMPLATE_FORBIDDEN_KEYS: dict[str, frozenset[str]] = {
    "connector": frozenset({"name"}),
    "agent": frozenset(),
    "watcher": frozenset({"name", "room", "rooms", "session_id"}),
}

# Single source of truth for history_handoff's per-field defaults: read from
# HistoryHandoffConfig's OWN dataclass field defaults below, not re-typed as
# separate literals here. These two drifted apart once, for over two months
# (commit 31f966d flipped only the dataclass default to enabled=True — opt-out,
# not opt-in — and missed this loader, which stayed hardcoded at enabled=False).
_HH_DEFAULTS = HistoryHandoffConfig()

# Per-type fallback for `command` when an agent (or its template) sets `type`
# but not `command`. Deliberately NOT a single hardcoded string (e.g. always
# "claude") — that was the other half of the bug _REMOVED_DEFAULTS_KEYS above
# describes: a fixed fallback is wrong for whichever type it doesn't match
# (an opencode agent silently defaulting to command "claude" would still exec
# the wrong binary). `type` itself has no fallback and is required below,
# same as `working_directory` — so this map only ever needs to cover known
# types; an unrecognized `type` value surfaces at runtime instead
# (gateway/service.py's "Unknown agent type" check), unchanged from before.
_AGENT_TYPE_DEFAULT_COMMAND: dict[str, str] = {
    "claude": "claude",
    "opencode": "opencode",
}


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
        """The real, production config loader — deliberately fail-fast: stops
        at the FIRST problem found (single, clear, actionable error), same
        as always. Per-entity parsing (one connector/agent/watcher at a
        time) is delegated to `_parse_one_connector()`/`_parse_one_agent()`/
        `_parse_one_watcher_entry()` (module-level functions below) so this
        method and `collect_config()`'s fault-tolerant counterpart (used by
        `gateway/config_validate.py` for the config TUI's Status column and
        the pre-save "did this edit make anything new go wrong" check) share
        exactly one implementation of every validation rule — never two
        copies that could quietly drift apart."""
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

        for old_key, new_key in _REMOVED_DEFAULTS_KEYS.items():
            if old_key in raw:
                raise ValueError(
                    f"config.yaml '{old_key}:' is no longer supported (removed) — "
                    f"define shared fields under '{new_key}:' instead and add "
                    "'inherits: <template-name>' to each entry that should use "
                    "them. See docs/migration-0.3.md."
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

        connector_templates = _parse_templates_block(
            raw, "connector_templates", TEMPLATE_FORBIDDEN_KEYS["connector"]
        )

        connectors: list[ConnectorConfig] = []
        seen_connector_names: set[str] = set()
        for i, cc_raw in enumerate(connectors_raw):
            connectors.append(
                _parse_one_connector(cc_raw, i, connector_templates, config_dir, seen_connector_names)
            )

        # ── Agents ────────────────────────────────────────────────────────────

        agents_raw = raw.get("agents") or {}
        if not isinstance(agents_raw, dict):
            raise ValueError(
                f"config.yaml 'agents:' must be a mapping (got {type(agents_raw).__name__}). "
                f"Expected a dict of agent names to config blocks."
            )
        default_agent = raw.get("default_agent", "")

        agent_templates = _parse_templates_block(raw, "agent_templates", TEMPLATE_FORBIDDEN_KEYS["agent"])
        tool_presets = _parse_tool_presets(raw)

        agents: dict[str, AgentConfig] = {}
        for agent_name, agent_raw_entry in agents_raw.items():
            agents[agent_name] = _parse_one_agent(
                agent_name, agent_raw_entry, agent_templates, tool_presets, config_dir
            )

        if not agents:
            raise ValueError(
                "config.yaml must define at least one agent under 'agents:'"
            )

        if not default_agent:
            default_agent = next(iter(agents))
        elif not isinstance(default_agent, str):
            # PR review finding: same class of bug as the per-entity 'name'/
            # 'type'/'connector'/'agent'/'session_id' checks elsewhere in
            # this module — a truthy-but-non-string top-level
            # 'default_agent:' (e.g. a YAML list) reached
            # `default_agent not in agents` (a dict) unchecked, crashing
            # with an uncaught TypeError instead of a clean ValueError.
            raise ValueError(
                f"config.yaml 'default_agent' must be a string (got {type(default_agent).__name__})."
            )
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

        watcher_templates = _parse_templates_block(
            raw, "watcher_templates", TEMPLATE_FORBIDDEN_KEYS["watcher"]
        )

        seen_watcher_names: set[str] = set()
        for i, wc_raw in enumerate(watchers_raw):
            watchers.extend(
                _parse_one_watcher_entry(
                    wc_raw, i, watcher_templates, connector_names, connectors, agents,
                    default_agent, config_dir, seen_watcher_names,
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

        max_queue_depth = _parse_max_queue_depth(raw)

        # ── Scheduler ─────────────────────────────────────────────────────────

        scheduler_cfg = _parse_scheduler(raw)

        return GatewayConfig(
            connectors=connectors,
            agents=agents,
            default_agent=default_agent,
            watchers=watchers,
            max_queue_depth=max_queue_depth,
            scheduler=scheduler_cfg,
        )


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
      copy, that mutation would leak into a shared template block
      (referenced by another entry's ``inherits:``) and corrupt it.
    """
    merged = {k: _deep_copy(v) for k, v in base.items()}
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, Mapping):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = _deep_copy(v)
    return merged


def _parse_templates_block(
    raw: dict, key: str, forbidden_keys: frozenset[str]
) -> dict[str, dict]:
    """Parse and validate a top-level ``<x>_templates:`` mapping: named,
    reusable field blocks an entry opts into via its own ``inherits:``
    field (resolved by ``_resolve_inherits`` below), replacing v0.2's single
    global ``*_defaults:`` block per kind (see ``_REMOVED_DEFAULTS_KEYS``
    above for why). Modeled directly on ``_extract_defaults_block``'s own
    validation conventions, applied per named template instead of once.

    Returns ``{}`` if the key is absent. Raises ValueError if the block, or
    any one named template within it, is not a mapping; if a named template
    sets a forbidden key (an identity field belonging to one specific
    entry, e.g. ``name`` — never safe to share, mirrors
    ``_extract_defaults_block``'s own message); or if a named template sets
    ``inherits`` itself — templates cannot nest (mirrors
    ``_parse_tool_presets``'s "no preset-of-presets" rule below).

    Deliberately does NOT check for any field being "required" — a template
    is meant to be a partial field set (e.g. a template need not set
    ``working_directory``, since that's inherently per-agent); "is
    everything required actually present" is checked only once, on the
    fully-resolved (template ∪ entry) dict downstream, exactly where it
    already ran before this mechanism existed.

    An optional ``description`` on each named template (annotating it,
    shown by the config TUI) is stripped before being returned — same rule
    ``_extract_defaults_block`` applies, for the same reason: it must never
    deep-merge into an entry that inherits this template.
    """
    templates_raw = raw.get(key, {}) or {}
    if not isinstance(templates_raw, Mapping):
        raise ValueError(
            f"config.yaml '{key}:' must be a mapping (got {type(templates_raw).__name__})."
        )
    templates: dict[str, dict] = {}
    for name, block in templates_raw.items():
        if not isinstance(block, Mapping):
            raise ValueError(
                f"{key}['{name}'] must be a mapping (got {type(block).__name__})."
            )
        if "inherits" in block:
            raise ValueError(
                f"{key}['{name}'] must not set 'inherits' — templates cannot "
                "inherit from another template (no nested templates)."
            )
        bad = sorted(forbidden_keys & block.keys())
        if bad:
            raise ValueError(
                f"{key}['{name}'] must not set {bad} — these fields identify "
                "an individual entry and must be set per-entry, not inherited."
            )
        result = dict(block)
        result.pop("description", None)
        templates[name] = result
    return templates


def _resolve_inherits(
    entry_raw: Mapping,
    templates: dict[str, dict],
    templates_key: str,
    entity_kind: str,
    entity_label: str,
) -> dict:
    """Resolve one entry's ``inherits:`` field (if set) against the parsed
    ``templates`` dict (from ``_parse_templates_block`` above): deep-merge
    the named template's fields with the entry's own fields, the entry
    winning on conflict (via the same ``_deep_merge`` the old ``*_defaults``
    blocks used — the merge algorithm itself needed no changes for this new
    purpose). No ``inherits:`` set -> the entry's own fields, unchanged.
    ``inherits`` is always popped from the result — it is never itself a
    real field downstream, whether resolved or absent.
    """
    entry = dict(entry_raw)
    template_name = entry.pop("inherits", None)
    if template_name is None:
        return entry
    if not isinstance(template_name, str) or not template_name:
        raise ValueError(
            f"{entity_kind} '{entity_label}': 'inherits' must be a non-empty "
            f"string naming a {templates_key} entry (got {template_name!r})."
        )
    template = templates.get(template_name)
    if template is None:
        available = ", ".join(sorted(templates)) or "(none defined)"
        raise ValueError(
            f"{entity_kind} '{entity_label}': unknown {templates_key} "
            f"'{template_name}'. Available templates: {available}"
        )
    # User-reported: nothing stopped an entry from declaring its own 'type'
    # (e.g. a rocketchat connector, or a claude agent) while `inherits:`
    # pointed at a template written for a DIFFERENT type (e.g. a mattermost
    # connector template, or an opencode agent template) — the entry's own
    # type silently won the merge, but the rest of that template's fields
    # were written for the wrong protocol/backend entirely. Only an actual
    # CONTRADICTION is an error: a template with no 'type' opinion of its
    # own (a genuinely generic, shared field set) is still a legitimate
    # thing to inherit regardless of the entry's type, and an entry with no
    # 'type' of its own is meant to inherit the template's type outright
    # (the config TUI's "switch template to switch type" feature relies on
    # exactly that case).
    entry_type = entry.get("type")
    template_type = template.get("type")
    if entry_type and template_type and entry_type != template_type:
        raise ValueError(
            f"{entity_kind} '{entity_label}': type '{entry_type}' does not "
            f"match {templates_key}['{template_name}']'s own type "
            f"'{template_type}' — an entry cannot inherit a template "
            "written for a different type."
        )
    return _deep_merge(template, entry)


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


# ── Per-entity parsing, extracted out of GatewayConfig.from_file() ──────────
#
# Each function below is EXACTLY the per-connector/per-agent/per-watcher body
# that used to be inlined directly in from_file()'s own for-loops — same
# checks, same exact error messages, same order — just given a name so it
# can be called from two places instead of one: from_file() itself (still
# fail-fast: the first ValueError raised here propagates straight out,
# stopping the whole load, exactly as before) and collect_config() below
# (fault-tolerant: wraps each call in its own try/except and keeps going).
# This is the ONLY place each of these rules is implemented — from_file()'s
# production fail-fast behavior and collect_config()'s multi-error
# collection can never quietly drift apart, because they share the code.


def _parse_one_connector(
    cc_raw: object,
    index: int,
    connector_templates: dict[str, dict],
    config_dir: Path,
    seen_connector_names: set[str],
) -> ConnectorConfig:
    if not isinstance(cc_raw, Mapping):
        raise ValueError(
            f"Connector entry at index {index} must be a mapping "
            f"(got {type(cc_raw).__name__})."
        )
    cc = _resolve_inherits(
        cc_raw, connector_templates, "connector_templates",
        "Connector entry", f"index {index}",
    )
    name = cc.get("name", "")
    connector_type = cc.get("type", "")
    if not name:
        raise ValueError("Each connector entry must have a 'name' field")
    if not isinstance(name, str):
        # PR review finding: a truthy-but-non-string 'name' (e.g. a YAML
        # list) is technically not caught by `not name` above, and used to
        # reach `name in seen_connector_names` below unchecked — an
        # uncaught `TypeError: unhashable type` (for a list/dict) instead
        # of a clean ValueError every caller expects. Pre-existing in
        # from_file() too (this function is extracted verbatim from it),
        # not something this session's collect_config() work introduced —
        # just never exercised until collect_config()'s own per-entity
        # attribution (gateway/config_validate.py's ConfigIssue.entity_name)
        # started reading a NAME off of every connector, whether or not it
        # goes on to parse successfully.
        raise ValueError(
            f"Connector entry at index {index}: 'name' must be a string "
            f"(got {type(name).__name__})."
        )
    if not connector_type:
        raise ValueError(f"Connector '{name}' must have a 'type' field")
    if not isinstance(connector_type, str):
        # PR review finding: same class of bug as the 'name' check above,
        # on the field one line down — a truthy-but-non-string 'type'
        # (e.g. a YAML list) reached ConnectorConfig.type unchecked, later
        # crashing gateway/config_validate.py's
        # `_CONNECTOR_VALIDATORS.get(connector.type)` with
        # `TypeError: unhashable type` on every validate_config() call, not
        # just --lint. _parse_one_agent()'s equivalent 'type' check already
        # does this; this one was simply missed in the same sweep.
        raise ValueError(
            f"Connector '{name}': 'type' must be a string (got {type(connector_type).__name__})."
        )
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
    # (consistent with working_directory resolution below)
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
    return ConnectorConfig(
        name=name,
        type=connector_type,
        raw=connector_raw,
        context_inject_files=ctx_files,
    )


def _parse_one_agent(
    agent_name: str,
    agent_raw_entry: object,
    agent_templates: dict[str, dict],
    tool_presets: dict[str, list["ToolRule"]],
    config_dir: Path,
) -> AgentConfig:
    if not isinstance(agent_raw_entry, Mapping):
        raise ValueError(
            f"Agent '{agent_name}' config must be a mapping "
            f"(got {type(agent_raw_entry).__name__})."
        )
    agent_raw = _resolve_inherits(
        agent_raw_entry, agent_templates, "agent_templates", "Agent", agent_name,
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

    # Resolve working_directory: expand a leading ~ first (matching
    # the cache_dir_global handling above), then resolve relative to
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

    # Validate: type is required (directly or via an inherits: template) —
    # no fallback. A silent default here is exactly the failure mode
    # _AGENT_TYPE_DEFAULT_COMMAND's docstring describes: an agent that
    # forgot `inherits:` (or a template) would otherwise look "valid"
    # while silently running as the wrong type.
    agent_type = agent_raw.get("type", "")
    if not agent_type:
        raise ValueError(
            f"Agent '{agent_name}': type is required (directly or via "
            "an inherits: agent_templates entry). Set it to 'claude' "
            "or 'opencode'."
        )
    if not isinstance(agent_type, str):
        raise ValueError(
            f"Agent '{agent_name}': type must be a string "
            f"(got {type(agent_type).__name__})."
        )
    command = agent_raw.get("command") or _AGENT_TYPE_DEFAULT_COMMAND.get(
        agent_type, ""
    )

    # session_idle_days / session_expire_days: on-the-fly watcher lifecycle
    # (docs/design/dynamic-watcher-design.md). Both optional; None means the
    # idle/expire lifecycle is off. When both are set, idle must be strictly
    # less than expire — otherwise a watcher jumps straight from active to
    # gone, skipping the back-burner state entirely. This only covers the
    # config-level half of that invariant: the effective expiry is actually
    # min(session_expire_days, the agent backend's own
    # typical_session_retention_days()), and that half can't be checked here
    # since this loader only ever sees AgentConfig, never a live AgentBackend
    # instance — the runtime code that consumes these values (not yet built)
    # must re-check against the effective value.
    session_idle_days = agent_raw.get("session_idle_days")
    if session_idle_days is not None:
        if isinstance(session_idle_days, bool) or not isinstance(session_idle_days, int):
            raise ValueError(
                f"Agent '{agent_name}': session_idle_days must be an integer "
                f"(got {type(session_idle_days).__name__})."
            )
        if session_idle_days <= 0:
            raise ValueError(
                f"Agent '{agent_name}': session_idle_days must be a positive "
                f"integer (got {session_idle_days})."
            )
    session_expire_days = agent_raw.get("session_expire_days")
    if session_expire_days is not None:
        if isinstance(session_expire_days, bool) or not isinstance(session_expire_days, int):
            raise ValueError(
                f"Agent '{agent_name}': session_expire_days must be an integer "
                f"(got {type(session_expire_days).__name__})."
            )
        if session_expire_days <= 0:
            raise ValueError(
                f"Agent '{agent_name}': session_expire_days must be a positive "
                f"integer (got {session_expire_days})."
            )
    if (
        session_idle_days is not None
        and session_expire_days is not None
        and session_idle_days >= session_expire_days
    ):
        raise ValueError(
            f"Agent '{agent_name}': session_idle_days ({session_idle_days}) must "
            f"be strictly less than session_expire_days ({session_expire_days}) — "
            "otherwise a watcher would jump straight from active to expired, "
            "skipping the idle back-burner state entirely."
        )

    agent_cfg = AgentConfig(
        name=agent_name,
        type=agent_type,
        command=command,
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
        session_idle_days=session_idle_days,
        session_expire_days=session_expire_days,
    )

    # Validate that agent.timeout > permissions.timeout when permissions are
    # enabled — folded in here (previously a separate pass over the fully-
    # built `agents` dict, AFTER from_file()'s agent loop finished) since it
    # only ever needs this one agent's own already-built AgentConfig;
    # per-entity exactly like every other check above, so collect_config()
    # below can attribute it to the right agent instead of stopping the
    # whole load. If agent.timeout <= permissions.timeout, the HTTP call can
    # time out while a permission request is still pending, leaving an
    # orphaned PermissionRequest in the registry.
    #
    # PR review finding, accepted trade-off: folding this in HERE (instead
    # of leaving it as its own pass after every agent has already been
    # built) changes which agent's error `from_file()` raises FIRST when
    # MULTIPLE agents are simultaneously broken for DIFFERENT reasons — e.g.
    # agent_a with a bad timeout/permissions.timeout relationship, followed
    # in the YAML by agent_b missing working_directory, now raises agent_a's
    # error first (this check runs inline, per-agent) instead of agent_b's
    # (which the old code always hit first, since the separate timeout pass
    # only ran after every agent had ALREADY been individually validated and
    # built). No test in tests/unit/test_config_loading.py pins this
    # specific cross-agent ordering (only ever one problem per fixture), and
    # `from_file()` was never a "report every problem" API to begin with —
    # but it IS a real, verified difference in the exact single error message
    # a multi-broken-agent config's from_file() call raises. Accepted because
    # the alternative (a second, separate implementation of this exact check
    # in collect_config()'s own agent loop, just to preserve from_file()'s
    # incidental ordering) would reintroduce the very rule-duplication risk
    # this whole extraction exists to eliminate.
    if agent_cfg.permissions.enabled and agent_cfg.timeout <= agent_cfg.permissions.timeout:
        raise ValueError(
            f"Agent '{agent_name}': timeout ({agent_cfg.timeout}s) must be greater than "
            f"permissions.timeout ({agent_cfg.permissions.timeout}s). "
            f"Suggested: set timeout to at least {agent_cfg.permissions.timeout + 60}s."
        )

    return agent_cfg


def _parse_one_watcher_entry(
    wc_raw: object,
    index: int,
    watcher_templates: dict[str, dict],
    connector_names: set[str],
    connectors: list[ConnectorConfig],
    agents: dict[str, AgentConfig],
    default_agent: str,
    config_dir: Path,
    seen_watcher_names: set[str],
) -> list[WatcherConfig]:
    """Parses ONE raw `watchers:` entry into its expanded list of
    WatcherConfigs (more than one only when `rooms:` names several rooms).
    Callers (from_file()/collect_config()) must ensure `connectors` is
    non-empty before calling this — `resolved_connector` falls back to
    `connectors[0].name` when an entry doesn't set its own `connector:`,
    exactly as from_file() always guaranteed by construction (an earlier
    raise would have stopped everything before reaching here); collect_config()
    checks this explicitly since it can legitimately end up with zero
    successfully-parsed connectors."""
    if not isinstance(wc_raw, Mapping):
        raise ValueError(
            f"Watcher entry at index {index} must be a mapping "
            f"(got {type(wc_raw).__name__})."
        )
    wc = _resolve_inherits(
        wc_raw, watcher_templates, "watcher_templates",
        "Watcher entry", f"index {index}",
    )

    # ── room / rooms: exactly one form, 'room' is a single-item alias ──
    raw_room = wc.get("room")
    raw_rooms = wc.get("rooms")
    if raw_room and raw_rooms:
        raise ValueError(
            f"Watcher entry at index {index}: set either 'room' or 'rooms', not both."
        )
    if raw_rooms is not None:
        if not isinstance(raw_rooms, list) or not raw_rooms:
            raise ValueError(
                f"Watcher entry at index {index}: 'rooms' must be a non-empty list "
                "of room names."
            )
        if not all(isinstance(r, str) and r for r in raw_rooms):
            raise ValueError(
                f"Watcher entry at index {index}: 'rooms' entries must be "
                "non-empty strings."
            )
        if len(set(raw_rooms)) != len(raw_rooms):
            dupes = sorted({r for r in raw_rooms if raw_rooms.count(r) > 1})
            raise ValueError(
                f"Watcher entry at index {index}: 'rooms' contains duplicate "
                f"room(s): {dupes}."
            )
        rooms_list = list(raw_rooms)
    elif raw_room:
        if not isinstance(raw_room, str):
            # PR review finding: the plural 'rooms:' form validates each
            # element (`isinstance(r, str) and r`, above) but this singular
            # alias didn't — a truthy-but-non-string 'room' (e.g. an int)
            # reached _sanitize_room_for_name()'s `room.startswith("@")`
            # unchecked (via _auto_watcher_name()), crashing with
            # AttributeError instead of a clean ValueError.
            raise ValueError(
                f"Watcher entry at index {index}: 'room' must be a string "
                f"(got {type(raw_room).__name__})."
            )
        rooms_list = [raw_room]
    else:
        raise ValueError(
            f"Watcher entry at index {index} must have a non-empty "
            "'room' or 'rooms' field"
        )

    # ── exclude_room: only meaningful alongside room: "*" ──────────────────
    # (docs/design/dynamic-watcher-design.md). Shape is validated now so this
    # field is test-covered ahead of the actual rule-matching engine, but
    # room: "*" itself is rejected below — accepting it today would let a
    # user configure a watcher the runtime has no way to act on correctly
    # (there's no room literally named "*").
    raw_exclude = wc.get("exclude_room")
    if raw_exclude is not None:
        if not isinstance(raw_exclude, list) or not raw_exclude:
            raise ValueError(
                f"Watcher entry at index {index}: 'exclude_room' must be a "
                "non-empty list of room names."
            )
        if not all(isinstance(r, str) and r for r in raw_exclude):
            raise ValueError(
                f"Watcher entry at index {index}: 'exclude_room' entries "
                "must be non-empty strings."
            )
        if len(set(raw_exclude)) != len(raw_exclude):
            dupes = sorted({r for r in raw_exclude if raw_exclude.count(r) > 1})
            raise ValueError(
                f"Watcher entry at index {index}: 'exclude_room' contains "
                f"duplicate room(s): {dupes}."
            )

    is_wildcard_room = rooms_list == ["*"]
    if raw_exclude is not None and not is_wildcard_room:
        raise ValueError(
            f"Watcher entry at index {index}: 'exclude_room' is only valid "
            "when 'room' is the wildcard \"*\" — it has no meaning against "
            "an explicit room name or a 'rooms:' list."
        )
    if is_wildcard_room:
        raise ValueError(
            f"Watcher entry at index {index}: room: \"*\" (rule-based room "
            "matching / on-the-fly watchers) is not implemented yet — use an "
            "explicit 'room:' or 'rooms:' list for now. See "
            "docs/design/dynamic-watcher-design.md."
        )

    # 'name' / 'session_id' pin a single sticky identity — only meaningful
    # when the entry expands to exactly one watcher.
    explicit_name = wc.get("name") or None
    if explicit_name is not None and not isinstance(explicit_name, str):
        # PR review finding: a truthy-but-non-string 'name' (e.g. a YAML
        # list) used to reach `watcher_name in seen_watcher_names` below
        # unchecked — an uncaught `TypeError: unhashable type` instead of a
        # clean ValueError every caller expects. Same class of bug as
        # _parse_one_connector()'s identical fix, and equally pre-existing
        # in from_file() (this function is extracted verbatim from it).
        raise ValueError(
            f"Watcher entry at index {index}: 'name' must be a string "
            f"(got {type(explicit_name).__name__})."
        )
    explicit_session_id = wc.get("session_id") or None
    if explicit_session_id is not None and not isinstance(explicit_session_id, str):
        # PR review finding: same class of bug as 'name' above — a
        # truthy-but-non-string 'session_id' (e.g. a YAML list) reached
        # `wc.session_id in seen_session_ids` (a set, in both from_file()'s
        # and collect_config()'s post-loop duplicate check) unchecked,
        # crashing with an uncaught TypeError.
        raise ValueError(
            f"Watcher entry at index {index}: 'session_id' must be a string "
            f"(got {type(explicit_session_id).__name__})."
        )
    if len(rooms_list) > 1:
        if explicit_name:
            raise ValueError(
                f"Watcher entry at index {index}: 'name' can only be set when "
                f"there is exactly one room (found {len(rooms_list)} in "
                "'rooms') — remove 'name' or split into single-room entries."
            )
        if explicit_session_id:
            raise ValueError(
                f"Watcher entry at index {index}: 'session_id' can only be set "
                f"when there is exactly one room (found {len(rooms_list)} in "
                "'rooms') — remove 'session_id' or split into single-room "
                "entries."
            )

    watcher_connector = wc.get("connector", "")
    if watcher_connector and not isinstance(watcher_connector, str):
        # PR review finding: same class of bug as 'name'/'session_id' above
        # — a truthy-but-non-string 'connector' (e.g. a YAML list) reached
        # `watcher_connector not in connector_names` (a set) unchecked,
        # crashing with an uncaught TypeError instead of a clean ValueError.
        raise ValueError(
            f"Watcher entry at index {index}: 'connector' must be a string "
            f"(got {type(watcher_connector).__name__})."
        )
    if watcher_connector and watcher_connector not in connector_names:
        raise ValueError(
            f"Watcher entry at index {index} references unknown connector "
            f"'{watcher_connector}'"
        )
    if not watcher_connector and not connectors:
        # from_file() can never reach this function with an empty
        # `connectors` list (an earlier structural check already raised),
        # and collect_config() guards against it too (its own "no connectors
        # parsed successfully" branch returns before ever reaching the
        # watcher loop) — but EditableConfig.expanded_watchers() calls this
        # function directly, per raw watcher entry, against whatever partial
        # `connectors` collect_config() returned, so an all-connectors-failed
        # config CAN legitimately land here. Without this guard,
        # `connectors[0].name` below raises an uncaught IndexError instead
        # of the ValueError every caller's `except ValueError` expects.
        raise ValueError(
            f"Watcher entry at index {index} has no explicit 'connector' set "
            "and no connectors are configured to default to."
        )
    resolved_connector = watcher_connector or connectors[0].name

    watcher_agent = wc.get("agent", default_agent)
    if not isinstance(watcher_agent, str):
        # PR review finding: same class of bug as 'connector' above — a
        # truthy-but-non-string 'agent' (e.g. a YAML list) reached
        # `watcher_agent not in agents` (a dict) unchecked, crashing with
        # an uncaught TypeError instead of a clean ValueError.
        raise ValueError(
            f"Watcher entry at index {index}: 'agent' must be a string "
            f"(got {type(watcher_agent).__name__})."
        )
    if watcher_agent not in agents:
        raise ValueError(
            f"Watcher entry at index {index} references unknown agent "
            f"'{watcher_agent}'"
        )

    # Resolve watcher-level context_inject_files (shared across expanded rooms)
    raw_ctx = wc.get("context_inject_files", [])
    ctx_files = _resolve_paths(raw_ctx, config_dir)

    # Defaults sourced from module-level _HH_DEFAULTS — see its docstring above.
    hh_raw = wc.get("history_handoff", {}) or {}
    history_handoff = HistoryHandoffConfig(
        enabled=hh_raw.get("enabled", _HH_DEFAULTS.enabled),
        fetch_count=hh_raw.get("fetch_count", _HH_DEFAULTS.fetch_count),
        verbatim_tail=hh_raw.get("verbatim_tail", _HH_DEFAULTS.verbatim_tail),
    )

    # Names are staged here, NOT written into `seen_watcher_names` directly,
    # until this whole entry finishes successfully (committed just before
    # returning below). PR review finding: with the old
    # "add-as-you-go-then-maybe-raise" approach, a multi-room `rooms:` entry
    # that registered its first room's name fine and then raised on a LATER
    # room (e.g. that later room's name genuinely collides with something
    # else) left the FIRST room's name permanently in `seen_watcher_names`
    # even though this entry's exception means NONE of its watchers actually
    # exist in the result — harmless in from_file()'s fail-fast mode (any
    # raise there aborts the whole load anyway) but a real bug once
    # collect_config() reuses this same function and keeps going past a
    # failed entry: a later, perfectly valid entry could then be rejected as
    # a "duplicate" of a watcher that was never actually added.
    staged_names: set[str] = set()
    result: list[WatcherConfig] = []
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
        if watcher_name in seen_watcher_names or watcher_name in staged_names:
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
        staged_names.add(watcher_name)

        result.append(
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
    seen_watcher_names.update(staged_names)
    return result


def _parse_max_queue_depth(raw: dict) -> int:
    """Parses and validates the top-level `max_queue_depth:` field. Shared
    by from_file() (lets a ValueError propagate immediately) and
    collect_config() (catches it, records a ConfigIssue, falls back to the
    default 100) — PR review finding: this was pasted twice, byte-identical,
    before this extraction; unlike connectors/agents/watchers, there was no
    single shared implementation for from_file()/collect_config() to drift
    apart from."""
    max_queue_depth = raw.get("max_queue_depth", 100)
    if not isinstance(max_queue_depth, int):
        raise ValueError(
            f"config.yaml 'max_queue_depth' must be an integer (got {type(max_queue_depth).__name__})."
        )
    if max_queue_depth < 0:
        raise ValueError("config.yaml 'max_queue_depth' must be >= 0")
    return max_queue_depth


def _parse_scheduler(raw: dict) -> "SchedulerConfig":
    """Parses and validates the top-level `scheduler:` block. Shared by
    from_file()/collect_config() — see _parse_max_queue_depth()'s docstring
    for why this extraction exists."""
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
    return SchedulerConfig(completed_job_ttl_days=scheduler_ttl)


def _max_queue_depth_and_scheduler_or_defaults(
    raw: dict, issues: list["ConfigIssue"]
) -> tuple[int, "SchedulerConfig"]:
    """Best-effort max_queue_depth/scheduler parse, appending a ConfigIssue
    and falling back per-field (independently) on failure — same behavior
    collect_config()'s own final section already gives these two fields on
    its happy path. PR review finding: collect_config()'s several EARLIER
    structural early-return branches used to hardcode
    `max_queue_depth=100, scheduler=SchedulerConfig()` instead of calling
    this — silently discarding an otherwise-valid max_queue_depth/scheduler:
    value behind a completely unrelated structural problem elsewhere (e.g.
    a malformed `watchers:` block), the exact "don't hide an unrelated,
    already-successful value's own state behind a different issue" bug this
    whole function exists to avoid for connectors/agents/watchers. These
    two fields have no entity dependency at all, so there's no reason they
    couldn't always be parsed this way, everywhere collect_config() returns."""
    try:
        max_queue_depth = _parse_max_queue_depth(raw)
    except ValueError as exc:
        issues.append(ConfigIssue("global", None, str(exc)))
        max_queue_depth = 100

    try:
        scheduler_cfg = _parse_scheduler(raw)
    except ValueError as exc:
        issues.append(ConfigIssue("global", None, str(exc)))
        scheduler_cfg = SchedulerConfig()

    return max_queue_depth, scheduler_cfg


@dataclass(frozen=True)
class ConfigIssue:
    """One structural/per-entity problem discovered by collect_config()'s
    fault-tolerant pass — the non-fail-fast counterpart to from_file()'s
    single ValueError. Deliberately dependency-free (no import of
    gateway/config_validate.py's own, richer `Finding` dataclass) since
    config_validate.py already imports FROM this module — the reverse
    import would be circular. `gateway/config_validate.py`'s
    `validate_config()` converts each `ConfigIssue` into its own `Finding`
    (always severity="error" — everything collect_config() reports is a
    from_file()-would-have-raised problem, same severity from_file() always
    implied by raising at all)."""

    entity_kind: Literal["connector", "agent", "watcher", "global"]
    entity_name: str | None
    message: str


def collect_config(path: str | Path) -> tuple["GatewayConfig | None", list[ConfigIssue]]:
    """Fault-tolerant counterpart to `GatewayConfig.from_file()`: instead of
    stopping at the FIRST bad connector/agent/watcher, collects a
    `ConfigIssue` for EVERY one that fails independently and keeps going —
    reusing `_parse_one_connector()`/`_parse_one_agent()`/
    `_parse_one_watcher_entry()` verbatim (never a second copy of any rule).

    Returns a best-effort `GatewayConfig` built from whatever entities DID
    parse successfully, so callers (`gateway/config_validate.py`'s
    `validate_config()`) can still run further per-entity checks against it
    (e.g. `_check_connectors()`'s empty-credential check) — this is what
    lets the config TUI's Overview attribute a real per-row error to the
    SPECIFIC agent/watcher/connector at fault, instead of one global
    "something is wrong" banner that never marks any row, and lets
    `EditableConfig.save()` compare "problems before this edit" against
    "problems after" on a genuinely per-entity basis.

    Only the three per-entity for-loops (connectors/agents/watchers) get
    this fault-tolerant treatment. Every STRUCTURAL check — is `connectors:`
    even a list, is there at least one agent, does `default_agent` resolve,
    is `watchers:` a list, `tool_presets:`/`*_templates:` blocks themselves
    well-formed, `max_queue_depth`/`scheduler:` shape — stays a hard,
    immediate stop: there's no meaningful "keep going with the other 9
    connectors" fallback when the document's basic shape is broken (e.g.
    which connector would you even skip if `connectors:` isn't a list at
    all?). `max_queue_depth`/`scheduler:` are the one partial exception —
    invalid values there don't gate any single entity's validity, so they're
    collected as issues and the returned config falls back to their safe
    defaults, rather than discarding all the connector/agent/watcher
    progress already made.

    Returns `(None, [issue])` ONLY when a structural check fails before ANY
    entity has even started parsing (missing file, malformed top level,
    `connectors:` itself not a list, etc.) — nothing usable to build yet.
    Returns `(config, [])` when everything succeeds — identical to what
    `from_file()` would return. Every OTHER structural check (agents:
    malformed, zero agents, `default_agent` invalid, watchers: malformed)
    still returns as much of a partial `GatewayConfig` as had already parsed
    successfully BEFORE that check — e.g. a broken `default_agent` still
    returns every connector and agent that parsed fine (with `watchers=[]`,
    since expansion can't proceed safely without a valid default) rather
    than discarding them too. This matters: `_check_connectors()` and
    friends (`gateway/config_validate.py`) run against whatever this
    returns, and an unrelated, already-successful entity's OWN problems
    must never be hidden behind a completely different structural issue
    elsewhere in the file — PR review caught this discarding real,
    unrelated connector-credential problems in exactly that way.

    In every case, `partial_config` (when not None) omits exactly the
    entities that individually failed (an entity referencing one of THOSE —
    e.g. a watcher whose `agent:` failed to parse — independently reports
    its own "unknown agent" issue too; not a duplicate, still useful, it
    tells you *that* watcher is also affected)."""
    path = Path(path)
    if not path.exists():
        return None, [ConfigIssue("global", None, f"Config file not found: {path}")]

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        return None, [
            ConfigIssue(
                "global", None,
                f"Config file '{path}' must contain a YAML mapping at the top level, "
                f"got {type(raw).__name__}.",
            )
        ]

    for old_key, new_key in _REMOVED_DEFAULTS_KEYS.items():
        if old_key in raw:
            return None, [
                ConfigIssue(
                    "global", None,
                    f"config.yaml '{old_key}:' is no longer supported (removed) — "
                    f"define shared fields under '{new_key}:' instead and add "
                    "'inherits: <template-name>' to each entry that should use "
                    "them. See docs/migration-0.3.md.",
                )
            ]

    config_dir = path.parent
    issues: list[ConfigIssue] = []

    # ── Connectors ────────────────────────────────────────────────────────
    connectors_raw = raw.get("connectors", [])
    if not connectors_raw:
        return None, [
            ConfigIssue(
                "global", None,
                "config.yaml must define at least one connector under 'connectors:'",
            )
        ]
    if not isinstance(connectors_raw, list):
        return None, [
            ConfigIssue(
                "global", None,
                f"config.yaml 'connectors:' must be a list (got {type(connectors_raw).__name__}).",
            )
        ]

    try:
        connector_templates = _parse_templates_block(
            raw, "connector_templates", TEMPLATE_FORBIDDEN_KEYS["connector"]
        )
    except ValueError as exc:
        return None, [ConfigIssue("global", None, str(exc))]

    connectors: list[ConnectorConfig] = []
    seen_connector_names: set[str] = set()
    for i, cc_raw in enumerate(connectors_raw):
        # PR review finding: `name:` itself might be malformed (e.g. a list
        # instead of a string) on an entry that ALSO fails for some other
        # reason — ConfigIssue.entity_name/Finding.entity_name are typed
        # `str | None` everywhere downstream, and EditableConfig's save-gate
        # (model.py's _new_errors_introduced_by_this_save()) puts this value
        # straight into a set of tuples, which raises an uncaught
        # `TypeError: unhashable type` for anything non-hashable (e.g. a
        # list). Only ever use name_hint when it's genuinely a usable
        # string; anything else falls back to the same "(index i)" label an
        # absent name already gets.
        name_hint = cc_raw.get("name") if isinstance(cc_raw, Mapping) else None
        if not isinstance(name_hint, str) or not name_hint:
            name_hint = None
        try:
            connectors.append(
                _parse_one_connector(cc_raw, i, connector_templates, config_dir, seen_connector_names)
            )
        except ValueError as exc:
            issues.append(ConfigIssue("connector", name_hint or f"(index {i})", str(exc)))

    # ── Agents ────────────────────────────────────────────────────────────
    # PR review finding: every early-return in this section used to
    # `return None, issues` — discarding the connectors already parsed
    # above, so validate_config()'s _check_connectors()/_check_state_orphans()
    # never ran on them even though they have nothing to do with an agents:-
    # section problem. Returned as a partial config (agents={}, watchers=[])
    # instead, so already-successful connectors keep getting checked.
    agents_raw = raw.get("agents") or {}
    if not isinstance(agents_raw, dict):
        issues.append(
            ConfigIssue(
                "global", None,
                f"config.yaml 'agents:' must be a mapping (got {type(agents_raw).__name__}). "
                f"Expected a dict of agent names to config blocks.",
            )
        )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents={}, default_agent="", watchers=[],
                max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )
    default_agent = raw.get("default_agent", "")

    try:
        agent_templates = _parse_templates_block(raw, "agent_templates", TEMPLATE_FORBIDDEN_KEYS["agent"])
        tool_presets = _parse_tool_presets(raw)
    except ValueError as exc:
        issues.append(ConfigIssue("global", None, str(exc)))
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents={}, default_agent="", watchers=[],
                max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )

    agents: dict[str, AgentConfig] = {}
    for agent_name, agent_raw_entry in agents_raw.items():
        try:
            agents[agent_name] = _parse_one_agent(
                agent_name, agent_raw_entry, agent_templates, tool_presets, config_dir
            )
        except ValueError as exc:
            issues.append(ConfigIssue("agent", agent_name, str(exc)))

    if not agents:
        # Every agent independently failed (or there were none defined) —
        # still return the connectors already parsed above (same reasoning
        # as the branches above/below: an unrelated connector problem must
        # not be hidden behind this).
        issues.append(
            ConfigIssue(
                "global", None,
                "config.yaml must define at least one agent under 'agents:'",
            )
        )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents={}, default_agent="", watchers=[],
                max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )

    if not default_agent:
        default_agent = next(iter(agents))
    elif not isinstance(default_agent, str) or default_agent not in agents:
        # PR review finding: this used to `return None, issues` here,
        # discarding every connector/agent that DID parse successfully —
        # meaning validate_config()'s _check_connectors()/_check_state_orphans()
        # never ran on them, silently hiding a real, unrelated connector
        # credential problem behind this equally-real but UNRELATED
        # default_agent problem (and, via EditableConfig.save()'s before/
        # after comparison, that hidden problem could then reappear later
        # and be misclassified as "a new problem this save introduced").
        # Watcher expansion genuinely can't proceed safely without a valid
        # default_agent to fall back an entry's implicit `agent:` field to,
        # so watchers are skipped (empty) — but every connector/agent that
        # DID parse is still returned, so their own checks keep running.
        #
        # PR review finding (round 6): a truthy-but-non-string
        # default_agent (e.g. a YAML list) reached `default_agent not in
        # agents` (a dict) unchecked, crashing with an uncaught TypeError
        # instead of a clean, collected ConfigIssue — same class of bug as
        # the per-entity 'name'/'type'/'connector'/'agent' checks elsewhere
        # in this module, folded into the same branch here rather than
        # duplicating the partial-config return a third time.
        if not isinstance(default_agent, str):
            issues.append(
                ConfigIssue(
                    "global", None,
                    f"config.yaml 'default_agent' must be a string "
                    f"(got {type(default_agent).__name__}).",
                )
            )
        else:
            issues.append(
                ConfigIssue(
                    "global", None,
                    f"default_agent '{default_agent}' not found in agents: {list(agents)}",
                )
            )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors,
                agents=agents,
                default_agent=next(iter(agents)),
                watchers=[],
                max_queue_depth=mqd,
                scheduler=sched,
            ),
            issues,
        )

    # ── Watchers ──────────────────────────────────────────────────────────
    connector_names = {c.name for c in connectors}
    if not connectors:
        # Every connector independently failed — from_file() could never
        # reach this point (an earlier raise would have stopped it first),
        # but collect_config() can legitimately get here. Nothing to
        # meaningfully resolve an implicit `connector:` against, so watchers
        # are skipped (empty) — but see the default_agent branch above for
        # why every AGENT that DID parse must still be returned rather than
        # discarded wholesale.
        issues.append(
            ConfigIssue(
                "global", None,
                "No connectors parsed successfully — cannot resolve watcher entries.",
            )
        )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=[],
                agents=agents,
                default_agent=default_agent,
                watchers=[],
                max_queue_depth=mqd,
                scheduler=sched,
            ),
            issues,
        )

    watchers: list[WatcherConfig] = []
    watchers_raw = raw.get("watchers", [])
    if watchers_raw and not isinstance(watchers_raw, list):
        # Connectors AND agents have already parsed successfully by this
        # point — same "don't hide an unrelated, already-successful entity's
        # checks behind this" reasoning as every branch above.
        issues.append(
            ConfigIssue(
                "global", None,
                f"config.yaml 'watchers:' must be a list (got {type(watchers_raw).__name__}).",
            )
        )
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents=agents, default_agent=default_agent,
                watchers=[], max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )

    try:
        watcher_templates = _parse_templates_block(
            raw, "watcher_templates", TEMPLATE_FORBIDDEN_KEYS["watcher"]
        )
    except ValueError as exc:
        issues.append(ConfigIssue("global", None, str(exc)))
        mqd, sched = _max_queue_depth_and_scheduler_or_defaults(raw, issues)
        return (
            GatewayConfig(
                connectors=connectors, agents=agents, default_agent=default_agent,
                watchers=[], max_queue_depth=mqd, scheduler=sched,
            ),
            issues,
        )

    seen_watcher_names: set[str] = set()
    for i, wc_raw in enumerate(watchers_raw):
        # See the identical connector-loop comment above: only ever use
        # name_hint when it's genuinely a usable string.
        name_hint = wc_raw.get("name") if isinstance(wc_raw, Mapping) else None
        if not isinstance(name_hint, str) or not name_hint:
            name_hint = None
        try:
            watchers.extend(
                _parse_one_watcher_entry(
                    wc_raw, i, watcher_templates, connector_names, connectors, agents,
                    default_agent, config_dir, seen_watcher_names,
                )
            )
        except ValueError as exc:
            issues.append(ConfigIssue("watcher", name_hint or f"(index {i})", str(exc)))

    # Cross-watcher duplicate session_id check — needs the full list, same
    # as from_file()'s own post-loop pass.
    seen_session_ids: set[str] = set()
    for wc in watchers:
        if wc.session_id:
            if wc.session_id in seen_session_ids:
                issues.append(
                    ConfigIssue(
                        "global", None,
                        f"Duplicate sticky session_id '{wc.session_id}' found across "
                        f"watchers. Each watcher must use a unique session_id.",
                    )
                )
            else:
                seen_session_ids.add(wc.session_id)

    # max_queue_depth/scheduler: don't gate any single entity's validity —
    # collected as issues, falling back to safe defaults for the returned
    # config, rather than discarding all connector/agent/watcher progress.
    # Same helper every earlier structural early-return in this function
    # now also uses (PR review finding) — max_queue_depth/scheduler have no
    # entity dependency at all, so there's no reason an early return
    # elsewhere should ever have hardcoded these to their defaults instead
    # of actually parsing them.
    max_queue_depth, scheduler_cfg = _max_queue_depth_and_scheduler_or_defaults(raw, issues)

    config = GatewayConfig(
        connectors=connectors,
        agents=agents,
        default_agent=default_agent,
        watchers=watchers,
        max_queue_depth=max_queue_depth,
        scheduler=scheduler_cfg,
    )
    return config, issues
