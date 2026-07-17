"""Small display-formatting helpers shared across config TUI screens."""

from __future__ import annotations

from .model import Provenance

# Key names whose values are masked when rendered — mirrors the fields the
# onboard wizard already treats as secrets (gateway/onboard.py's _write_env:
# only credentials, never url/host/team/username).
_SECRET_KEY_NAMES = frozenset({"password", "secret", "token"})

_PROVENANCE_LABEL = {
    Provenance.EXPLICIT: "explicit",
    Provenance.INHERITED: "inherited from defaults",
    Provenance.EXPLICIT_SUPPRESSING: "explicit — suppresses default",
}


def provenance_label(provenance: Provenance) -> str:
    return _PROVENANCE_LABEL[provenance]


def mask_if_secret(key: str, value: object) -> str:
    """Render `value`, masking it if `key` looks like a credential field."""
    if key.lower() in _SECRET_KEY_NAMES and value:
        return "•" * 8
    return format_value(value)


def format_value(value: object) -> str:
    """Compact single-line rendering of a scalar/list/dict value for display."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return ", ".join(format_value(v) for v in value) or "(empty)"
    if isinstance(value, dict):
        return ", ".join(f"{k}={mask_if_secret(k, v)}" for k, v in value.items()) or "(empty)"
    return str(value)


def status_badge(status: str) -> str:
    """Rich markup for a status string ('ok' | 'warning' | 'error' | 'lint')."""
    return {
        "ok": "[green]OK[/green]",
        "warning": "[yellow]WARN[/yellow]",
        "error": "[red]ERROR[/red]",
        "lint": "[cyan]lint[/cyan]",
    }.get(status, status)
