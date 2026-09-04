"""What changed between two resolved configurations, and how to name one (#144).

`config reload` compares the configuration the daemon is running against the
one on disk. The comparison is over **parsed dataclasses**, never YAML text:
comments, key order, quoting and a `description:` field (which every entity
parser drops) register as nothing, and a template edit registers as a change
to every entry inheriting it, because inheritance is flattened at parse time.

Three things live here, all pure:

* **the action table** — every field of every config entity is classified once
  (`RELOAD_ACTIONS`), so a new field cannot arrive without saying what a reload
  does about it; `tests/unit/test_config_diff.py` enumerates the dataclasses
  against it;
* **the diff** — `diff_configs(active, candidate)`: entities by `name`, a
  rename read as a removal plus an addition, and the two top-level values
  that are swapped in place;
* **the digest** — `config_digest(config)`: SHA-256 over a canonical
  serialization of the resolved config, so two files that mean the same thing
  hash the same, and `flatten_config` for `config show`, with secrets redacted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from .config import AgentConfig, ConnectorConfig, GatewayConfig, SchedulerConfig
from .core.room_pattern import RoomPattern
from .core.watcher_rule import WatcherRule

# What a reload does when a field of that entity changes.
#   restart-connector — the connector and its session manager are rebuilt
#   restart-agent     — the backend (and an OpenCode sidecar) restarts, its
#                       broker is rebuilt, its resident processors restart
#   reconcile         — every record is re-matched against the current rules
#   value             — the value is replaced in the running service
#   identity          — the field IS the entity's name; a change is a removal
#                       plus an addition, not a change to one entity
#   section           — a top-level block whose entries are classified on
#                       their own dataclass
ReloadAction = Literal[
    "restart-connector", "restart-agent", "reconcile", "value", "identity", "section",
]

RELOAD_ACTIONS: dict[type, dict[str, ReloadAction]] = {
    ConnectorConfig: {
        "name": "identity",
        "type": "restart-connector",
        "raw": "restart-connector",
        "context_inject_files": "restart-connector",
    },
    AgentConfig: {
        "name": "identity",
        "type": "restart-agent",
        "command": "restart-agent",
        "new_session_args": "restart-agent",
        "working_directory": "restart-agent",
        "session_prefix": "restart-agent",
        "lazy_instruction_loading": "restart-agent",
        "context_inject_files": "restart-agent",
        "owner_allowed_tools": "restart-agent",
        "guest_allowed_tools": "restart-agent",
        "timeout": "restart-agent",
        "permissions": "restart-agent",
    },
    WatcherRule: {
        # A rule's name is its identity too, but ownership is recomputed by
        # re-matching, so a renamed rule is harmless: the record it created
        # re-materializes to the new name (#143's classification test).
        "name": "reconcile",
        "connector": "reconcile",
        "agent": "reconcile",
        "rooms": "reconcile",
        "session_idle_days": "reconcile",
        "session_expire_days": "reconcile",
        "context_inject_files": "reconcile",
        "history_handoff": "reconcile",
    },
    GatewayConfig: {
        "connectors": "section",
        "agents": "section",
        "watcher_rules": "section",
        "max_queue_depth": "value",
        "scheduler": "section",
    },
    SchedulerConfig: {
        "completed_job_ttl_days": "value",
    },
}


@dataclass
class EntityChanges:
    """Names added, changed and removed for one kind of entity."""

    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    def to_dict(self) -> dict:
        return {"added": list(self.added), "changed": list(self.changed),
                "removed": list(self.removed)}


@dataclass(frozen=True)
class ValueChange:
    """One top-level value swapped in place."""

    path: str
    old: Any
    new: Any


@dataclass
class ConfigDiff:
    connectors: EntityChanges = field(default_factory=EntityChanges)
    agents: EntityChanges = field(default_factory=EntityChanges)
    rules: EntityChanges = field(default_factory=EntityChanges)
    # A rule list whose entries are all unchanged but stand in a different
    # order routes differently (first match wins), so it is a change to the
    # rules block even though no entity is added, changed or removed.
    rules_reordered: bool = False
    values: list[ValueChange] = field(default_factory=list)

    @property
    def rules_changed(self) -> bool:
        return bool(self.rules) or self.rules_reordered

    def __bool__(self) -> bool:
        return bool(self.connectors or self.agents or self.rules_changed or self.values)

    @property
    def restarted_connectors(self) -> list[str]:
        return list(self.connectors.changed)

    @property
    def restarted_agents(self) -> list[str]:
        return list(self.agents.changed)


def _entity_changes(old: dict[str, Any], new: dict[str, Any]) -> EntityChanges:
    return EntityChanges(
        added=[n for n in new if n not in old],
        changed=[n for n in new if n in old and old[n] != new[n]],
        removed=[n for n in old if n not in new],
    )


def diff_configs(active: GatewayConfig, candidate: GatewayConfig) -> ConfigDiff:
    """Compare two resolved configurations entity by entity.

    Identity is `name` for connectors, agents and rules: a renamed entity is a
    removal plus an addition. For a connector that means every record under
    the old name expires and its state file goes; the plan renderer says so.
    Equality is the dataclasses' own — `ConnectorConfig.raw` is compared as
    the dict it is, a rotated token is a change, a re-indented one is not.
    """
    diff = ConfigDiff()
    diff.connectors = _entity_changes(
        {c.name: c for c in active.connectors}, {c.name: c for c in candidate.connectors})
    diff.agents = _entity_changes(dict(active.agents), dict(candidate.agents))
    diff.rules = _entity_changes(
        {r.name: r for r in active.watcher_rules}, {r.name: r for r in candidate.watcher_rules})
    if not diff.rules:
        diff.rules_reordered = (
            [r.name for r in active.watcher_rules] != [r.name for r in candidate.watcher_rules])
    if active.max_queue_depth != candidate.max_queue_depth:
        diff.values.append(ValueChange(
            "max_queue_depth", active.max_queue_depth, candidate.max_queue_depth))
    for f in fields(SchedulerConfig):
        old, new = getattr(active.scheduler, f.name), getattr(candidate.scheduler, f.name)
        if old != new:
            diff.values.append(ValueChange(f"scheduler.{f.name}", old, new))
    return diff


# ── Digest and dump ─────────────────────────────────────────────────────────────


def canonical(value: Any) -> Any:
    """A resolved config as JSON-safe data, walking dataclass fields.

    `RoomPattern` becomes the string it was compiled from (the only form an
    operator can compare with their file); an enum its value; a path its
    string. Dicts keep their keys — `json.dumps(sort_keys=True)` orders them.
    """
    if isinstance(value, RoomPattern):
        return value.raw
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: canonical(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [canonical(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    return value


def config_digest(config: GatewayConfig) -> str:
    """SHA-256 (hex) of the canonical serialization of the RESOLVED config.

    Templates and inheritance are already expanded in a `GatewayConfig`, so a
    file rewritten with the same meaning — reordered keys, added comments, a
    value moved into a template — keeps its digest, and a rotated secret
    changes it (the digest is over the unredacted values; it is a fingerprint,
    not a dump).
    """
    data = canonical(config)
    # Connectors are an identity-keyed set, and the diff treats them as one;
    # the digest must agree, or reordering two connectors would be "no
    # changes" to reload and "differs" to `config show`, forever. Rule order
    # stays significant — first match wins — and is not touched.
    data["connectors"] = {c["name"]: c for c in data["connectors"]}
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# Case-insensitive substrings of a key that mark its value as a secret.
SECRET_KEY_MARKERS = ("password", "token", "secret")
REDACTED = "***"


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def flatten_config(config: GatewayConfig, *, redact: bool = True) -> list[tuple[str, Any]]:
    """`(dotted.path, value)` pairs over the canonical form, for `config show`.

    Lists index as `[n]`; connectors are keyed by name rather than position so
    two machines' dumps line up. With `redact`, a value under a key that
    names a password, token or secret is replaced by `***` — whatever depth
    it sits at, including inside a connector's type-specific `raw` block.
    """
    data = canonical(config)
    data["connectors"] = {c["name"]: c for c in data["connectors"]}
    out: list[tuple[str, Any]] = []

    def walk(prefix: str, value: Any, key: str) -> None:
        if redact and key and is_secret_key(key) and not isinstance(value, (dict, list)):
            out.append((prefix, REDACTED))
            return
        if isinstance(value, dict):
            if not value:
                out.append((prefix, {}))
            for k in sorted(value):
                walk(f"{prefix}.{k}" if prefix else k, value[k], k)
            return
        if isinstance(value, list):
            if not value:
                out.append((prefix, []))
            for i, item in enumerate(value):
                walk(f"{prefix}[{i}]", item, key)
            return
        out.append((prefix, value))

    walk("", data, "")
    return out


def redacted_config(config: GatewayConfig) -> dict:
    """The canonical form with secrets redacted, for `--json` output."""
    def scrub(value: Any, key: str) -> Any:
        if key and is_secret_key(key) and not isinstance(value, (dict, list)):
            return REDACTED
        if isinstance(value, dict):
            return {k: scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, key) for v in value]
        return value
    return scrub(canonical(config), "")
