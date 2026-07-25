"""Small display-formatting helpers shared across config TUI screens."""

from __future__ import annotations

from .model import Provenance

# Key names whose values are masked when rendered — mirrors the fields the
# onboard wizard already treats as secrets (gateway/onboard.py's _write_env:
# only credentials, never url/host/team/username).
_SECRET_KEY_NAMES = frozenset({"password", "secret", "token"})

def provenance_label(provenance: Provenance, template_name: str | None = None) -> str:
    """Render a provenance value for display. `template_name` is the
    entry's own `inherits:` value (from `EditableConfig.entry_template_name()`),
    if any — threaded through so INHERITED/EXPLICIT_SUPPRESSING can name which
    template is involved, and so DEFAULT can distinguish "no inherits: at
    all" from "inherits: set, but that template doesn't set this field".

    Deliberately terse (not e.g. "inherited from template '{name}'") — this
    renders inside a fixed-width `.field-provenance` column
    (`test_provenance_markers_are_within_the_visible_terminal_width` pins
    this at a 120-col terminal), and a form with several DEFAULT-with-
    template fields (any template that only sets a couple of keys leaves
    most fields in this state) renders one of these per row — verbose
    wording here multiplies into real, visible overflow, not just a cosmetic
    nit on one row."""
    if provenance == Provenance.EXPLICIT:
        return "explicit"
    if provenance == Provenance.INHERITED:
        return f"from '{template_name}'"
    if provenance == Provenance.EXPLICIT_SUPPRESSING:
        return f"clears '{template_name}'"
    # Provenance.DEFAULT
    if template_name is None:
        return "default"
    return f"default (no '{template_name}')"


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
