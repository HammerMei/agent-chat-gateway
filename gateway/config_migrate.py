"""One-time migration: fold a `.env`-backed secret directly into config.yaml
as a literal value, then remove `.env`.

Context (docs/design/config-tool.md decision 6 revisited): `.env` was
originally the config tool's recommended place for connector secrets, with
`${VAR}`/`$VAR` references in config.yaml resolved against it at load time
(`gateway/config.py`'s `load_dotenv` + `_expand_env_vars`). On reflection,
splitting one deployment's state across two files bought less than it cost:
`config.yaml` gets the same `chmod 0600` treatment `.env` always had, so the
only real remaining benefit — a config.yaml that's safe to share/back up
without also handing over secrets — is better served by a dedicated export/
redaction path than by permanently maintaining two files as the norm.

Decision: enforce a real migration rather than supporting both forms
indefinitely (a nag people can ignore isn't enforcement). This module holds
the actual migration LOGIC as one function, callable from two TRIGGERS:
`gateway/daemon.py`'s `start_daemon()` (automatic, on every server start —
becomes a permanent no-op the moment `.env` is gone) and a standalone CLI
command (`agent-chat-gateway config migrate-env`) for a manual/dry-run/
Docker-entrypoint invocation. Same function either way — no logic
duplicated between the two call sites.

Known limitation (Docker bind-mount deployments only): `EditableConfig.
save()` writes via `os.replace()`, which replaces a destination that is
itself a symlink rather than writing through it — so in the Docker
entrypoint's Mode 1 (`docker/entrypoint.acg.sh` symlinks the runtime
`config.yaml`/`.env` to a bind-mounted host directory), the FIRST migration
inside the container turns the container's `config.yaml` into a real file
decoupled from the host mount, and the moved-aside `.env` may still be a
symlink pointing at a host file that itself never gets removed. A later
container restart can therefore re-run this migration (harmless — it's
idempotent in effect, just not a true no-op in that one deployment shape).
Pre-existing characteristic of `EditableConfig.save()`'s symlink handling,
not something this module special-cases.

Safety model: backup -> migrate -> validate -> fail-closed. Reuses
`EditableConfig.load()`/`.save()` for the raw load and the validate-before-
write/backup/atomic-replace machinery (never reimplemented) — a migration
that would fail `validate_config()` never touches the real config.yaml, and
`.env` is only ever removed AFTER the migrated config.yaml has been saved
successfully. `gateway/config.py`'s own `load_dotenv`/`_expand_env_vars` are
reused for resolution too, so the literal values this migration writes are
exactly what the daemon would already have resolved at real startup — not a
reimplementation that could quietly diverge.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .config import _expand_env_vars
from .configtool.model import EditableConfig

_ENV_REF_RE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")


@dataclass
class MigrationResult:
    """What `migrate_env_to_config()` actually did, for the caller (daemon
    startup / CLI command) to report to the user."""

    migrated: bool
    ref_count: int = 0
    env_backup_path: Path | None = None


def _count_env_refs(obj: object) -> int:
    """Total `$VAR`/`${VAR}` occurrences anywhere in the raw document's
    string values, recursively — used only to report "N secret(s) migrated"
    to the user; not load-bearing for the migration itself."""
    if isinstance(obj, str):
        return len(_ENV_REF_RE.findall(obj))
    if isinstance(obj, dict):
        return sum(_count_env_refs(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_env_refs(item) for item in obj)
    return 0


def migrate_env_to_config(config_path: str | Path) -> MigrationResult:
    """If a `.env` file sits next to `config_path`, resolve every `$VAR`/
    `${VAR}` reference in the raw config document to its literal value,
    save the result (validated, backed up, atomically — see module
    docstring), then move `.env` into `.config-backups/` (created by the
    save above) so nothing is silently deleted.

    No-op (`MigrationResult(migrated=False)`) if `.env` doesn't exist —
    this is what makes the migration permanently self-limiting: once it has
    run once for a given config directory, every later call (e.g. every
    subsequent daemon start) is a cheap no-op forever.

    Raises `ValueError` if any reference can't be resolved (missing from
    both `.env` and the process environment — same check
    `_expand_env_vars` already performs for the real daemon load path) —
    the caller must treat this as fatal: `.env` and config.yaml are left
    completely untouched, and starting the gateway with a half-migrated or
    still-referencing config would be worse than refusing to start.
    """
    cfg = EditableConfig.load(config_path)
    env_path = cfg.path.parent / ".env"
    if not env_path.exists():
        return MigrationResult(migrated=False)

    ref_count = _count_env_refs(cfg.document)

    # Merge .env's own values into THIS process's environment — the exact
    # same call gateway/config.py's GatewayConfig.from_file makes (doesn't
    # override a var already set in the process environment, so if the
    # daemon would actually have resolved a reference from the ambient
    # environment rather than .env, this migration produces the same
    # literal value the daemon has always been using, not a divergent one).
    load_dotenv(env_path)

    try:
        expanded = _expand_env_vars(cfg.document)
    except ValueError as exc:
        raise ValueError(f"Cannot migrate .env into config.yaml: {exc}") from exc

    cfg.document = expanded
    cfg.mark_dirty()
    cfg.save()  # validate + timestamped backup + atomic replace + chmod

    # Only remove .env once the migrated config.yaml is durably saved —
    # moved (not deleted outright) into a .config-backups/ directory, so
    # there's one designated place for "anything historical," not a stray
    # .env.migrated left in the live config directory. Created here rather
    # than assumed from cfg.save()'s own backup step, so this function
    # doesn't depend on that convention already existing.
    backup_dir = cfg.path.parent / ".config-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    backup_path = backup_dir / f".env.pre-migration.{int(time.time())}"
    env_path.rename(backup_path)
    backup_path.chmod(0o600)

    return MigrationResult(migrated=True, ref_count=ref_count, env_backup_path=backup_path)
