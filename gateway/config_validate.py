"""Standalone config.yaml validation — no daemon required.

``GatewayConfig.from_file`` (gateway/config.py) already checks structure and
cross-references (unknown connector/agent, duplicate names, etc.). This module
adds two things `from_file` alone cannot catch, plus an optional lint pass:

1. Per-connector-type validation. Connector dataclasses (RocketChatConfig,
   MattermostConfig, VoiceConfig) are normally only built lazily when the
   daemon actually starts a connector — a bad or empty ``server:`` block goes
   unnoticed until then. Building them here surfaces those errors immediately.
2. A state.json orphan check: warns when a connector's persisted watcher
   state references a watcher name no longer present in the config — that
   session/pause state is silently dropped on the next gateway start
   (see gateway/core/watcher_lifecycle.py's state-pruning behavior).
3. ``--lint``: flags config values that just restate a built-in default, or
   duplicate a value already provided by the matching ``*_defaults`` block —
   noise that can be deleted without changing behavior.

Used by the ``acg config validate`` CLI command; written as a plain function
(not a CLI-only code path) so a future config-editing tool can reuse the same
save-time check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import yaml

from .config import GatewayConfig, _extract_defaults_block
from .connectors.mattermost.config import MattermostConfig
from .connectors.rocketchat.config import RocketChatConfig
from .connectors.voice.config import VoiceConfig
from .core.state import load_state

# Connector types validated via their own dataclass parser. 'script' is
# intentionally omitted — ScriptConnector never reads ConnectorConfig.raw
# (see gateway/connectors/__init__.py), so there's nothing to validate.
_CONNECTOR_VALIDATORS = {
    "rocketchat": RocketChatConfig.from_connector_config,
    "mattermost": MattermostConfig.from_connector_config,
    "voice": VoiceConfig.from_connector_config,
}

# (key, default_value) pairs checked by --lint against each raw entry. Kept
# to top-level scalar/list fields — deep nested paths (e.g. permissions.timeout)
# are intentionally out of scope to keep this a cheap, low-noise pass.
_AGENT_LINT_DEFAULTS: list[tuple[str, object]] = [
    ("session_prefix", "agent-chat"),
    ("lazy_instruction_loading", True),
    ("new_session_args", []),
    ("context_inject_files", []),
    ("timeout", 360),
]
_WATCHER_LINT_DEFAULTS: list[tuple[str, object]] = [
    ("context_inject_files", []),
    ("online_notification", None),
    ("offline_notification", None),
    ("session_id", None),
]
_CONNECTOR_LINT_DEFAULTS: list[tuple[str, object]] = [
    ("reply_in_thread", False),
    ("permission_reply_in_thread", True),
]


@dataclass(frozen=True)
class Finding:
    """A single validation/lint result, attributed to an entity where the
    check that produced it actually knows which entity is at fault.

    Additive alongside ValidationResult's flat string lists (errors/
    warnings/lint_findings), which remain the source of truth for
    `acg config validate`'s CLI output — this exists so the config TUI can
    attach a finding to the right row/screen without re-parsing message
    text. Not every finding can be attributed this precisely: a
    GatewayConfig.from_file load failure (bad structure, unknown reference,
    etc.) covers most cross-field checks and is inherently global — it gets
    entity_kind="global", entity_name=None. See docs/design/config-tool.md's
    validation-attribution section for why threading entity context through
    every from_file raise site is out of scope.
    """

    severity: Literal["error", "warning", "lint"]
    entity_kind: Literal["connector", "agent", "watcher", "global"]
    entity_name: str | None
    field: str | None
    message: str


@dataclass
class ValidationResult:
    config_path: str
    entry_count: int = 0
    watcher_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lint_findings: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_config(config_path: str, lint: bool = False) -> ValidationResult:
    """Validate config.yaml without starting the daemon. See module docstring."""
    result = ValidationResult(config_path=config_path)

    try:
        config = GatewayConfig.from_file(config_path)
    except (ValueError, FileNotFoundError) as exc:
        result.errors.append(str(exc))
        result.findings.append(
            Finding(
                severity="error",
                entity_kind="global",
                entity_name=None,
                field=None,
                message=str(exc),
            )
        )
        return result

    result.watcher_count = len(config.watchers)

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    except OSError as exc:
        msg = f"Could not re-read {config_path}: {exc}"
        result.errors.append(msg)
        result.findings.append(
            Finding(severity="error", entity_kind="global", entity_name=None, field=None, message=msg)
        )
        return result

    result.entry_count = len(raw.get("watchers") or [])

    _check_connectors(config, result)
    _check_state_orphans(config, result)
    if lint:
        _lint_config(raw, result)

    return result


def _check_connectors(config: GatewayConfig, result: ValidationResult) -> None:
    """Instantiate each connector's own config dataclass and flag empty
    credentials — fields from_connector_config defaults to "" rather than
    validating."""
    for connector in config.connectors:
        validator = _CONNECTOR_VALIDATORS.get(connector.type)
        if validator is None:
            continue
        try:
            cfg = validator(connector)
        except ValueError as exc:
            msg = f"Connector '{connector.name}' ({connector.type}): {exc}"
            result.errors.append(msg)
            result.findings.append(
                Finding("error", "connector", connector.name, None, msg)
            )
            continue

        def _empty_field(field_path: str) -> None:
            msg = f"Connector '{connector.name}': {field_path} is empty"
            result.errors.append(msg)
            result.findings.append(
                Finding("error", "connector", connector.name, field_path, msg)
            )

        if connector.type == "rocketchat":
            if not cfg.server_url:
                _empty_field("server.url")
            if not cfg.username:
                _empty_field("server.username")
            if not cfg.password:
                _empty_field("server.password")
        elif connector.type == "mattermost":
            if not cfg.server_url:
                _empty_field("server.url")
            if not cfg.team:
                _empty_field("server.team")


def _check_state_orphans(config: GatewayConfig, result: ValidationResult) -> None:
    """Warn when a connector's persisted state.<connector>.json references a
    watcher name no longer present in the (expanded) config."""
    configured_by_connector: dict[str, set[str]] = {}
    for w in config.watchers:
        configured_by_connector.setdefault(w.connector, set()).add(w.name)

    for connector in config.connectors:
        try:
            states = load_state(connector.name)
        except Exception:
            continue
        configured = configured_by_connector.get(connector.name, set())
        for st in states:
            if st.watcher_name not in configured:
                msg = (
                    f"Connector '{connector.name}': state.json has watcher "
                    f"'{st.watcher_name}' with no matching entry in this config — "
                    "its session/pause state will be dropped on next start. "
                    "Restore the old watcher name (e.g. an explicit 'name:') "
                    "if you want to keep it."
                )
                result.warnings.append(msg)
                result.findings.append(
                    Finding("warning", "connector", connector.name, None, msg)
                )


def _lint_config(raw: dict, result: ValidationResult) -> None:
    agent_defaults = _extract_defaults_block(raw, "agent_defaults", frozenset())
    watcher_defaults = _extract_defaults_block(
        raw, "watcher_defaults", frozenset({"name", "room", "rooms", "session_id"})
    )
    connector_defaults = _extract_defaults_block(raw, "connector_defaults", frozenset({"name"}))

    for agent_name, agent_raw in (raw.get("agents") or {}).items():
        if isinstance(agent_raw, dict):
            _lint_entry(
                "agent", agent_name, agent_raw, "agent_defaults", agent_defaults,
                _AGENT_LINT_DEFAULTS, result,
            )

    for i, wc in enumerate(raw.get("watchers") or []):
        if isinstance(wc, dict):
            label = wc.get("name") or f"watchers[{i}]"
            _lint_entry(
                "watcher", label, wc, "watcher_defaults", watcher_defaults,
                _WATCHER_LINT_DEFAULTS, result,
            )

    for cc in raw.get("connectors") or []:
        if not isinstance(cc, dict):
            continue
        name = cc.get("name") or "?"
        _lint_entry(
            "connector", name, cc, "connector_defaults", connector_defaults,
            _CONNECTOR_LINT_DEFAULTS, result,
        )
        attach = cc.get("attachments")
        if isinstance(attach, dict):
            if attach.get("max_file_size_mb") == 10:
                msg = (
                    f"connectors.{name}.attachments.max_file_size_mb: restates the "
                    "built-in default (10) — can be omitted."
                )
                result.lint_findings.append(msg)
                result.findings.append(
                    Finding("lint", "connector", name, "attachments.max_file_size_mb", msg)
                )
            if attach.get("download_timeout") == 30:
                msg = (
                    f"connectors.{name}.attachments.download_timeout: restates the "
                    "built-in default (30) — can be omitted."
                )
                result.lint_findings.append(msg)
                result.findings.append(
                    Finding("lint", "connector", name, "attachments.download_timeout", msg)
                )


def _lint_entry(
    entity_kind: Literal["connector", "agent", "watcher"],
    entity_name: str,
    entry: dict,
    defaults_block_name: str,
    defaults_block: dict,
    default_table: list[tuple[str, object]],
    result: ValidationResult,
) -> None:
    label = f"{entity_kind}s.{entity_name}"
    for key, default_value in default_table:
        if key not in entry:
            continue
        if entry[key] == default_value:
            msg = (
                f"{label}.{key}: restates the built-in default ({default_value!r}) — "
                "can be omitted."
            )
            result.lint_findings.append(msg)
            result.findings.append(Finding("lint", entity_kind, entity_name, key, msg))
        elif key in defaults_block and entry[key] == defaults_block[key]:
            msg = (
                f"{label}.{key}: matches the inherited {defaults_block_name}.{key} "
                f"value ({entry[key]!r}) — can be omitted from this entry."
            )
            result.lint_findings.append(msg)
            result.findings.append(Finding("lint", entity_kind, entity_name, key, msg))
