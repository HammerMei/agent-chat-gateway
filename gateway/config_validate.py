"""Standalone config.yaml validation — no daemon required.

``GatewayConfig.from_file`` (gateway/config.py) already checks structure and
cross-references (unknown connector/agent, duplicate names, etc.). This module
adds two things `from_file` alone cannot catch, plus an optional lint pass:

1. Per-connector-type validation. Connector dataclasses (RocketChatConfig,
   MattermostConfig, VoiceConfig) are normally only built lazily when the
   daemon actually starts a connector — a bad or empty ``server:`` block goes
   unnoticed until then. Building them here surfaces those errors immediately.
   This also includes a lenient ``server.url`` format check (scheme + netloc
   only, via ``urllib.parse.urlparse``) that catches obvious typos (e.g. a
   URL field left as plain text like "test") without rejecting unusual but
   well-formed schemes/ports.
2. A state.json orphan check: warns when a connector's persisted watcher
   state references a watcher name no longer present in the config — that
   session/pause state is silently dropped on the next gateway start
   (see gateway/core/watcher_lifecycle.py's state-pruning behavior).
3. ``--lint``: flags config values that just restate a built-in default, or
   duplicate a value already inherited from the entry's own ``inherits:``
   template — noise that can be deleted without changing behavior.

Used by the ``acg config validate`` CLI command; written as a plain function
(not a CLI-only code path) so a future config-editing tool can reuse the same
save-time check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

import yaml

from .config import (
    TEMPLATE_FORBIDDEN_KEYS,
    GatewayConfig,
    _parse_templates_block,
    collect_config,
    find_shadowed_rules,
)
from .connectors.mattermost.config import MattermostConfig
from .connectors.rocketchat.config import RocketChatConfig
from .connectors.voice.config import VoiceConfig
from .core.state import LegacyStateError, load_state

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
    """Validate config.yaml without starting the daemon. See module docstring.

    Uses `gateway/config.py`'s `collect_config()` — the fault-tolerant
    counterpart to `GatewayConfig.from_file()` — instead of catching a
    single exception from `from_file()` directly. This means a config with
    MULTIPLE independent problems (e.g. two connectors each missing a
    required field, or an agent with a bad `working_directory` alongside an
    unrelated connector issue) reports EVERY one, each correctly attributed
    to entity_kind="connector"/"agent"/"watcher" — not just whichever one
    `from_file()` would have hit first, collapsed into one
    entity_kind="global" finding that never marks a specific row in the
    config TUI's Overview. A structural failure (e.g. `connectors:` isn't
    even a list) is still exactly one global finding — collect_config()
    itself only fans out across genuinely independent per-entity problems."""
    result = ValidationResult(config_path=config_path)

    config, issues = collect_config(config_path)
    for issue in issues:
        result.errors.append(issue.message)
        result.findings.append(
            Finding(
                severity="error",
                entity_kind=issue.entity_kind,
                entity_name=issue.entity_name,
                field=None,
                message=issue.message,
            )
        )

    if config is None:
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
    _check_shadowed_rules(config, result)
    if lint:
        _lint_config(raw, result)

    return result


_SHADOW_SCOPE_WORDING = {
    "rule": "can never fire: every way it reaches a room",
    "named": "will never match a named room: that pattern set",
    "direct": "will never see a 1:1 DM: that reach",
    "group_direct": "will never see a group DM: that reach",
}


def _check_shadowed_rules(config: GatewayConfig, result: ValidationResult) -> None:
    """Warn about rule reaches an earlier rule already claims.

    Warnings, not errors: under first-match precedence a shadowed rule is dead
    config — nearly always a mistake in ordering — but the config is coherent and
    the daemon starts fine, so refusing to load would be wrong.

    This lives here rather than in the loader for two reasons. `from_file()` is
    fail-fast and has no warning channel at all, and `collect_config()`'s
    `ConfigIssue` is documented as "a from_file()-would-have-raised problem" and is
    converted to severity="error" unconditionally above — riding it would report
    dead-but-legal config as a load failure. `validate_config()` already emits
    warnings, and is what both `acg config validate` and the config TUI's banner
    read.

    Nothing warns at daemon startup, because until the watcher manager lands
    nothing consumes rules, so there is no behaviour for a shadowed rule to affect.
    """
    for finding in find_shadowed_rules(config.watcher_rules):
        detail = _SHADOW_SCOPE_WORDING.get(finding.scope, f"reach '{finding.scope}'")
        msg = (
            f"Watcher rule '{finding.rule.name}' {detail} is already claimed by "
            f"the earlier rule '{finding.shadowed_by.name}'. Under first-match "
            "precedence a rule only sees rooms no earlier rule stopped — reorder "
            "them, or narrow the earlier rule."
        )
        result.warnings.append(msg)
        result.findings.append(
            Finding("warning", "watcher", finding.rule.name, "rooms", msg)
        )


def _looks_like_url(value: str) -> bool:
    """Lenient URL format check: scheme + netloc only (catches obvious typos
    like "test" or "localhost:3000" without a scheme). Deliberately does not
    second-guess unusual-but-valid schemes, ports, or paths."""
    parsed = urlparse(value)
    return bool(parsed.scheme) and bool(parsed.netloc)


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

        def _bad_url_field(field_path: str, value: str) -> None:
            msg = (
                f"Connector '{connector.name}': {field_path} ({value!r}) "
                "does not look like a URL (expected e.g. 'https://host')"
            )
            result.errors.append(msg)
            result.findings.append(
                Finding("error", "connector", connector.name, field_path, msg)
            )

        if connector.type == "rocketchat":
            if not cfg.server_url:
                _empty_field("server.url")
            elif not _looks_like_url(cfg.server_url):
                _bad_url_field("server.url", cfg.server_url)
            if not cfg.username:
                _empty_field("server.username")
            if not cfg.password:
                _empty_field("server.password")
        elif connector.type == "mattermost":
            if not cfg.server_url:
                _empty_field("server.url")
            elif not _looks_like_url(cfg.server_url):
                _bad_url_field("server.url", cfg.server_url)
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
        except LegacyStateError as exc:
            # This branch used to be `except Exception: continue`, which would have
            # swallowed the refusal entirely — and `acg config validate` is the first
            # thing an upgrading operator runs, so it would have reported a clean
            # config while the daemon refused to boot on the same files. Reported as
            # an error rather than a warning: the gateway will not start until it is
            # dealt with.
            msg = str(exc)
            result.errors.append(msg)
            result.findings.append(
                Finding("error", "connector", connector.name, None, msg)
            )
            continue
        except Exception:
            # Anything else (unreadable file, malformed JSON) is handled inside
            # load_state by starting fresh, so reaching here means something
            # unexpected — skip this connector's orphan check rather than failing the
            # whole validation over it.
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
    # PR review finding: this used to assume re-parsing these blocks "cannot
    # raise" because validate_config() only reached here after a successful
    # GatewayConfig.from_file() call. That's no longer true now that
    # validate_config() uses collect_config() — a `*_templates:` block can
    # itself be malformed while collect_config() still returns a usable
    # partial config (that failure is already recorded as its own Finding
    # via collect_config()'s issues, same as from_file() would have caught
    # it). Falling back to {} here — instead of re-raising the identical
    # ValueError a second time, uncaught — mirrors collect_config()'s own
    # "record the issue once, keep going with a safe default" behavior.
    def _templates_or_empty(key: str, forbidden_keys: frozenset[str]) -> dict[str, dict]:
        try:
            return _parse_templates_block(raw, key, forbidden_keys)
        except ValueError:
            return {}

    agent_templates = _templates_or_empty("agent_templates", TEMPLATE_FORBIDDEN_KEYS["agent"])
    watcher_templates = _templates_or_empty(
        "watcher_templates", TEMPLATE_FORBIDDEN_KEYS["watcher"]
    )
    connector_templates = _templates_or_empty("connector_templates", TEMPLATE_FORBIDDEN_KEYS["connector"])

    # PR review finding: the guards below (isinstance(agents_raw, dict),
    # the isinstance(str) checks on name/label) exist for the exact same
    # reason _templates_or_empty() above does — collect_config() can return
    # a usable partial config even when `agents:` itself isn't a mapping,
    # or when an individual entry's own 'name:'/'inherits:' is malformed
    # (e.g. a YAML list) — cases that were UNREACHABLE here before this
    # branch (from_file()'s fail-fast behavior meant _lint_config() only
    # ever ran on already-fully-valid data). Without these, `.items()` on a
    # non-dict raises AttributeError, and a non-string name/label reaching
    # Finding.entity_name (typed str | None) crashes
    # gateway/configtool/model.py's StatusIndex with
    # TypeError: unhashable type — both confirmed via direct repro.
    agents_raw = raw.get("agents")
    if isinstance(agents_raw, dict):
        for agent_name, agent_raw in agents_raw.items():
            if isinstance(agent_raw, dict):
                _lint_entry(
                    "agent", agent_name, agent_raw, "agent_templates", agent_templates,
                    _AGENT_LINT_DEFAULTS, result,
                )

    for i, wc in enumerate(raw.get("watchers") or []):
        if isinstance(wc, dict):
            name_hint = wc.get("name")
            label = name_hint if isinstance(name_hint, str) and name_hint else f"watchers[{i}]"
            _lint_entry(
                "watcher", label, wc, "watcher_templates", watcher_templates,
                _WATCHER_LINT_DEFAULTS, result,
            )

    for cc in raw.get("connectors") or []:
        if not isinstance(cc, dict):
            continue
        name_hint = cc.get("name")
        name = name_hint if isinstance(name_hint, str) and name_hint else "?"
        _lint_entry(
            "connector", name, cc, "connector_templates", connector_templates,
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
    templates_key: str,
    templates: dict[str, dict],
    default_table: list[tuple[str, object]],
    result: ValidationResult,
) -> None:
    """Per-entry now, not per-kind: an entry only has an inherited-value to
    compare against if it actually sets `inherits:` — with the old single
    global `*_defaults` block gone, there's no implicit shared value every
    entry of a kind was checked against regardless."""
    label = f"{entity_kind}s.{entity_name}"
    template_name = entry.get("inherits")
    # PR review finding: a malformed 'inherits:' (e.g. a YAML list) is a
    # real, already-reported ConfigIssue/Finding elsewhere (collect_config()
    # itself rejects it) — but `dict.get()` requires a hashable key, so
    # using template_name here unchecked raised an uncaught
    # `TypeError: unhashable type` straight out of --lint, aborting the
    # whole pass and discarding every already-collected finding.
    if not isinstance(template_name, str) or not template_name:
        template_name = None
    template = templates.get(template_name, {}) if template_name else {}
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
        elif template_name and key in template and entry[key] == template[key]:
            msg = (
                f"{label}.{key}: matches the value inherited from "
                f"{templates_key}['{template_name}'].{key} ({entry[key]!r}) — "
                "can be omitted from this entry."
            )
            result.lint_findings.append(msg)
            result.findings.append(Finding("lint", entity_kind, entity_name, key, msg))
