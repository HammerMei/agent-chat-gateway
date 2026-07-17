"""AgentDetailScreen — view, edit, and create a single agent.

Unlike connectors, the agent schema is complete (additionalProperties:
false in gateway/schema/config.schema.json's $defs/agent), so this shows a
fixed field list with a provenance marker per field (explicit / inherited
from agent_defaults / explicit-null-suppressing) instead of a generic dump.
`_FORM_FIELDS`/`_PERMISSIONS_FORM_FIELDS` below are a manually-maintained
mirror of that schema (matching Phase 1's `_KNOWN_FIELDS`, not a runtime
JSON-schema interpreter) — safe because the schema is closed
(`additionalProperties: false`), so there's no drift risk from a field this
form doesn't know about sneaking in.

Tool lists (`owner_allowed_tools`/`guest_allowed_tools`) still render as
view-only PRE-resolve representation here — editing them is
docs/design/config-tool.md's separate tool-list-editor work, not this
screen.

Edit/create + Save/dirty/navigation machinery lives in `.form_common`
(`FormScreen`) — shared with `ConnectorDetailScreen`. This module supplies
the agent-specific pieces: which fields exist, their dataclass defaults, and
`action_save()`'s entity-shaped insertion (`document["agents"]` is a dict
keyed by name, unlike connectors' list).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Static

from ..formatting import format_value, provenance_label
from ..modals import MessageModal
from ..model import EditableConfig
from .form_common import FieldSpec, FormScreen, apply_update, find_referencing_watcher_labels

if TYPE_CHECKING:
    from ..app import ConfigToolApp

# Top-level agent fields worth a dedicated provenance-annotated line, in the
# same order as AgentConfig's own fields (gateway/core/config.py). View mode
# only — the form below has its own, edit-oriented field list.
_KNOWN_FIELDS = [
    "type", "command", "working_directory", "session_prefix",
    "lazy_instruction_loading", "new_session_args", "context_inject_files",
    "timeout", "permissions",
]

# AgentConfig/PermissionConfig's own dataclass defaults (gateway/core/
# config.py) — used ONLY to prefill the form with the true effective value
# when a field is set by neither the entry nor agent_defaults. Unlike view
# mode (which simply omits a line for an absent field), a form editing that
# field needs to show what it would actually evaluate to right now.
_AGENT_DATACLASS_DEFAULTS: dict[str, object] = {
    "type": "claude",
    "command": "claude",
    "working_directory": "",
    "session_prefix": "agent-chat",
    "lazy_instruction_loading": True,
    "new_session_args": [],
    "context_inject_files": [],
    "timeout": 360,
    "permissions.enabled": False,
    "permissions.timeout": 300,
    "permissions.skip_owner_approval": False,
}

_FORM_FIELDS: list[FieldSpec] = [
    FieldSpec("type", "enum", "Type", options=("claude", "opencode")),
    FieldSpec("command", "str", "Command"),
    FieldSpec("working_directory", "str", "Working directory"),
    FieldSpec("session_prefix", "str", "Session prefix"),
    FieldSpec("lazy_instruction_loading", "bool", "Lazy instruction loading"),
    FieldSpec("new_session_args", "list", "New session args (comma-separated)"),
    FieldSpec("context_inject_files", "list", "Context inject files (comma-separated)"),
    FieldSpec("timeout", "int", "Timeout (seconds)"),
]
_PERMISSIONS_FORM_FIELDS: list[FieldSpec] = [
    FieldSpec("permissions.enabled", "bool", "Permissions enabled"),
    FieldSpec("permissions.timeout", "int", "Permissions timeout (seconds)"),
    FieldSpec("permissions.skip_owner_approval", "bool", "Skip owner approval"),
]
_ALL_FORM_FIELDS = (*_FORM_FIELDS, *_PERMISSIONS_FORM_FIELDS)


def _resolve_working_directory(config_path: Path, raw_value: str) -> Path:
    """Mirror gateway/config.py's own working_directory resolution EXACTLY
    (expanduser, then resolve relative to the config file's directory if
    still not absolute) — used only to compute the inline warning below, so
    it must resolve the same path the real loader would, or the warning
    fires on paths that are actually fine (e.g. `~/...` or a relative path)."""
    expanded = Path(raw_value).expanduser()
    if expanded.is_absolute():
        return expanded
    return (config_path.resolve().parent / expanded).resolve()


def _working_directory_warning(config_path: Path, raw_value: str) -> str:
    """Early, non-blocking heads-up only — NOT a substitute for save()'s own
    validate_config() call, which still hard-fails if the directory is
    missing at save time (GatewayConfig.from_file requires it to exist;
    that enforcement is intentionally left alone here, see
    docs/design/config-tool.md's Phase 2 status notes)."""
    text = raw_value.strip()
    if not text:
        return ""
    resolved = _resolve_working_directory(config_path, text)
    if not resolved.is_dir():
        return f"[yellow]⚠ does not exist yet: {resolved}[/yellow]"
    return ""


class AgentDetailScreen(FormScreen):
    BODY_ID = "agent-detail-body"

    def __init__(
        self,
        cfg: EditableConfig,
        name: str,
        entry: dict,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        self.agent_name = name
        self.entry = entry
        self.mode = mode
        if self.mode != "view":
            self._compute_initial_values(self.entry)
            self._populating = True

    def _entity_noun(self) -> str:
        return "agent"

    def _entity_label(self) -> str:
        return self.agent_name

    def _remove_entry_from_document(self) -> None:
        del self.cfg.document["agents"][self.agent_name]

    def _reinsert_entry_into_document(self) -> None:
        self.cfg.document.setdefault("agents", {})[self.agent_name] = self.entry

    def _referencing_watcher_labels(self) -> list[str]:
        return find_referencing_watcher_labels(self.cfg, agent_name=self.agent_name)

    def _on_enter_edit_mode(self) -> None:
        self._compute_initial_values(self.entry)

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        return _ALL_FORM_FIELDS

    def _defaults_kind(self) -> str:
        return "agent_defaults"

    def _dataclass_defaults(self) -> dict[str, object]:
        return _AGENT_DATACLASS_DEFAULTS

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        description = self.entry.get("description")
        lines = [f"[bold]{self.agent_name}[/bold]"]
        if description:
            lines.append(f"[dim]{description}[/dim]")
        lines.append("")

        try:
            merged = self.cfg.merged_entry("agent_defaults", self.entry)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(f"[red]Could not compute effective values: {exc}[/red]")
            return "\n".join(lines)

        for key in _KNOWN_FIELDS:
            if key not in merged:
                continue
            provenance = self.cfg.field_provenance("agent_defaults", self.entry, key)
            lines.append(
                f"{key}: {format_value(merged[key])}  "
                f"[dim]({provenance_label(provenance)})[/dim]"
            )

        for label, field_key in (
            ("owner_allowed_tools", "owner_allowed_tools"),
            ("guest_allowed_tools", "guest_allowed_tools"),
        ):
            if field_key not in merged:
                continue
            provenance = self.cfg.field_provenance("agent_defaults", self.entry, field_key)
            lines.append("")
            lines.append(f"{label}:  [dim]({provenance_label(provenance)})[/dim]")
            for item in merged.get(field_key) or []:
                if isinstance(item, str):
                    lines.append(f"  → preset: {item}")
                elif isinstance(item, dict):
                    tool = item.get("tool", "?")
                    params = item.get("params")
                    lines.append(f"  {tool} / {params or '(any)'}")

        return "\n".join(lines)

    # ── edit/create form ─────────────────────────────────────────────────────

    def _compose_form(self) -> ComposeResult:
        # can_focus=False: otherwise this container is itself the first
        # focusable widget (user-reported: needed Tab TWICE to reach the
        # first real field — once to focus this container, once to move
        # past it). The container isn't meant to be focused on its own;
        # scrolling still works via the mouse wheel/PageUp/PageDown.
        with VerticalScroll(classes="entity-form", can_focus=False):
            if self.mode == "create":
                yield Static("[bold]New agent[/bold]")
                with Horizontal(classes="field-row"):
                    yield Static("Name", classes="field-label")
                    yield Input(id="field-name", placeholder="agent name")
            else:
                yield Static(f"[bold]{self.agent_name}[/bold]  (editing)")

            with Horizontal(classes="field-row"):
                yield Static("Description", classes="field-label")
                yield Input(
                    id="field-description",
                    value=self._initial_values.get("description") or "",
                )

            for spec in _FORM_FIELDS:
                yield from self._compose_field_row(spec, self.entry)
                if spec.key == "working_directory":
                    yield Static(
                        _working_directory_warning(
                            self.cfg.path, str(self._initial_values.get(spec.key) or "")
                        ),
                        id="wd-warning",
                    )

            yield Static("[bold]Permissions[/bold]")
            for spec in _PERMISSIONS_FORM_FIELDS:
                yield from self._compose_field_row(spec, self.entry)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "field-working_directory":
            self.query_one("#wd-warning", Static).update(
                _working_directory_warning(self.cfg.path, event.input.value)
            )
        super().on_input_changed(event)

    # ── save ─────────────────────────────────────────────────────────────────

    @work
    async def action_save(self) -> None:
        if self.mode == "view":
            return

        updates = self._collect_field_updates()
        if updates is None:
            await self.app.push_screen_wait(
                MessageModal(self._last_field_error or "Invalid field.", title="Could not save")
            )
            return

        name = self.agent_name
        if self.mode == "create":
            name = self.query_one("#field-name", Input).value.strip()
            if not name:
                await self.app.push_screen_wait(
                    MessageModal("Name is required.", title="Could not save")
                )
                return
            if name in self.cfg.agents_raw:
                await self.app.push_screen_wait(
                    MessageModal(f"An agent named '{name}' already exists.", title="Could not save")
                )
                return

        target_entry = self.entry if self.mode == "edit" else dict(self.entry)
        for key, value in updates.items():
            apply_update(target_entry, key, value)

        if self.mode == "create":
            self.cfg.document.setdefault("agents", {})[name] = target_entry
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create":
                # Nothing existed under this name before this screen ever
                # ran — remove it so a failed save doesn't leave a phantom
                # half-created agent sitting in memory.
                del self.cfg.document["agents"][name]
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return

        self.app.pop_screen()
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.notify(f"Saved agent '{name}'.", severity="information")
        app.reload_config()
