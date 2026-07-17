"""AgentDetailScreen — view, edit, and create a single agent.

Unlike connectors, the agent schema is complete (additionalProperties:
false in gateway/schema/config.schema.json's $defs/agent), so this shows a
fixed field list with a provenance marker per field (explicit / inherited
from agent_defaults / explicit-null-suppressing) instead of a generic dump.
"_FORM_FIELDS"/"_PERMISSIONS_FORM_FIELDS" below are a manually-maintained
mirror of that schema (matching Phase 1's `_KNOWN_FIELDS`, not a runtime
JSON-schema interpreter) — safe because the schema is closed
(`additionalProperties: false`), so there's no drift risk from a field this
form doesn't know about sneaking in.

Tool lists (`owner_allowed_tools`/`guest_allowed_tools`) still render as
view-only PRE-resolve representation here — editing them is
docs/design/config-tool.md's separate tool-list-editor work, not this
screen.

Edit/create semantics (docs/design/config-tool.md decision 2 — "editing an
inherited field always writes an explicit per-entry override"): nothing is
written to `EditableConfig.document` until Save. On Save, every field is
diffed against the value captured when the form opened; only fields that
actually changed are written to the raw entry (clearing a field back to
blank reverts it to inherited, rather than writing an explicit null — see
`_apply_update`). This keeps every untouched field's provenance exactly as
it was, and keeps a not-yet-saved edit from ever touching disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Checkbox, Footer, Header, Input, Select, Static

from ..formatting import format_value, provenance_label
from ..modals import ConfirmModal
from ..model import EditableConfig, Provenance
from .base import DetailScreen

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


@dataclass(frozen=True)
class _FieldSpec:
    key: str  # dotted for a permissions sub-field, e.g. "permissions.timeout"
    kind: Literal["str", "int", "bool", "list", "enum"]
    label: str
    options: tuple[str, ...] | None = None


_FORM_FIELDS: list[_FieldSpec] = [
    _FieldSpec("type", "enum", "Type", options=("claude", "opencode")),
    _FieldSpec("command", "str", "Command"),
    _FieldSpec("working_directory", "str", "Working directory"),
    _FieldSpec("session_prefix", "str", "Session prefix"),
    _FieldSpec("lazy_instruction_loading", "bool", "Lazy instruction loading"),
    _FieldSpec("new_session_args", "list", "New session args (comma-separated)"),
    _FieldSpec("context_inject_files", "list", "Context inject files (comma-separated)"),
    _FieldSpec("timeout", "int", "Timeout (seconds)"),
]
_PERMISSIONS_FORM_FIELDS: list[_FieldSpec] = [
    _FieldSpec("permissions.enabled", "bool", "Permissions enabled"),
    _FieldSpec("permissions.timeout", "int", "Permissions timeout (seconds)"),
    _FieldSpec("permissions.skip_owner_approval", "bool", "Skip owner approval"),
]
_ALL_FORM_FIELDS = (*_FORM_FIELDS, *_PERMISSIONS_FORM_FIELDS)


class _AgentForm(VerticalScroll):
    """The form's scroll container — Up/Down move between fields instead of
    scrolling (VerticalScroll's own inherited action_scroll_up/down, bound
    to up/down by default). A form is naturally row-oriented, so this reads
    more like a real form than a scrollable document; Home/End/PageUp/
    PageDown and the mouse wheel still scroll if the form doesn't fit the
    terminal. Only overriding the two ACTIONS (not adding new BINDINGS) —
    Select's own dropdown still handles up/down itself while open, since
    that's resolved before it ever reaches this container."""

    def action_scroll_up(self) -> None:
        self.screen.focus_previous()

    def action_scroll_down(self) -> None:
        self.screen.focus_next()


def _widget_id(key: str) -> str:
    return "field-" + key.replace(".", "-")


def _get_nested(d: dict, dotted_key: str) -> object:
    if "." not in dotted_key:
        return d.get(dotted_key)
    parent_key, sub_key = dotted_key.split(".", 1)
    parent = d.get(parent_key)
    return parent.get(sub_key) if isinstance(parent, dict) else None


def _list_to_text(value: object) -> str:
    if not value:
        return ""
    return ", ".join(str(v) for v in value)


def _text_to_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


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


def _apply_update(entry: dict, dotted_key: str, value: object) -> None:
    """Write `value` (or clear, if None) into `entry` at `dotted_key`.

    A top-level key: set it, or pop it entirely if `value` is None (revert
    to inherited/default). A `permissions.*` key: read-modify-write the
    `permissions` sub-dict, dropping it entirely once its last explicit
    sub-key is cleared, so a fully-cleared `permissions` block reverts to
    inheriting from `agent_defaults.permissions` again, not an empty `{}`
    stub sitting in the entry forever.
    """
    if "." not in dotted_key:
        if value is None:
            entry.pop(dotted_key, None)
        else:
            entry[dotted_key] = value
        return
    parent_key, sub_key = dotted_key.split(".", 1)
    parent = entry.get(parent_key)
    parent = dict(parent) if isinstance(parent, dict) else {}
    if value is None:
        parent.pop(sub_key, None)
    else:
        parent[sub_key] = value
    if parent:
        entry[parent_key] = parent
    else:
        entry.pop(parent_key, None)


class AgentDetailScreen(DetailScreen):
    BODY_ID = "agent-detail-body"

    BINDINGS = [
        Binding("e", "edit", "Edit", show=True),
        Binding("ctrl+s", "save", "Save", show=True),
    ]

    DEFAULT_CSS = """
    AgentDetailScreen #agent-form {
        padding: 1 2;
    }
    AgentDetailScreen .field-row {
        height: auto;
        margin-bottom: 1;
    }
    AgentDetailScreen .field-label {
        width: 30;
        padding-top: 1;
    }
    AgentDetailScreen .field-provenance {
        padding-top: 1;
        margin-left: 2;
    }
    AgentDetailScreen Checkbox {
        width: auto;
    }
    """

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
        self._form_dirty = False
        self._initial_values: dict[str, object] = {}
        # Input/Select fire their own Changed message once at initial mount
        # with whatever value the constructor was given (confirmed
        # empirically — Checkbox does not, but Input/Select do). Without this
        # guard, simply OPENING the edit form would immediately mark it
        # dirty, incorrectly prompting a discard-confirmation on Escape even
        # though the user never touched anything. Cleared via
        # call_after_refresh (see on_mount/action_edit), which runs after
        # that initial burst of Changed messages has already been processed.
        self._populating = False
        if self.mode != "view":
            self._compute_initial_values()
            self._populating = True

    def on_mount(self) -> None:
        if self._populating:
            self.call_after_refresh(self._stop_populating)

    def _stop_populating(self) -> None:
        self._populating = False

    # ── view mode ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        if self.mode == "view":
            yield VerticalScroll(Static(self._body_text(), id=self.BODY_ID))
        else:
            yield from self._compose_form()
        yield Footer()

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

    def _compute_initial_values(self) -> None:
        """Snapshot the effective value of every form field, exactly as it
        would display right now — captured once, when entering edit/create
        mode, and used both to prefill widgets and (on Save) to detect which
        fields the user actually changed. See module docstring."""
        try:
            merged = self.cfg.merged_entry("agent_defaults", self.entry)
        except (ValueError, FileNotFoundError):
            merged = dict(self.entry)
        for spec in _ALL_FORM_FIELDS:
            value = _get_nested(merged, spec.key)
            if value is None:
                value = _AGENT_DATACLASS_DEFAULTS.get(spec.key)
            self._initial_values[spec.key] = value
        self._initial_values["description"] = self.entry.get("description")

    def _field_provenance(self, spec: _FieldSpec) -> Provenance | None:
        top_key = spec.key.split(".", 1)[0]
        try:
            return self.cfg.field_provenance("agent_defaults", self.entry, top_key)
        except (ValueError, FileNotFoundError):
            return None

    def _compose_form(self) -> ComposeResult:
        with _AgentForm(id="agent-form"):
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
                yield from self._compose_field_row(spec)
                if spec.key == "working_directory":
                    yield Static(
                        _working_directory_warning(self.cfg.path, str(self._initial_values.get(spec.key) or "")),
                        id="wd-warning",
                    )

            yield Static("[bold]Permissions[/bold]")
            for spec in _PERMISSIONS_FORM_FIELDS:
                yield from self._compose_field_row(spec)

    def _compose_field_row(self, spec: _FieldSpec) -> ComposeResult:
        provenance = self._field_provenance(spec)
        prov_text = f"[dim]({provenance_label(provenance)})[/dim]" if provenance else ""
        initial = self._initial_values.get(spec.key)
        with Horizontal(classes="field-row"):
            yield Static(spec.label, classes="field-label")
            if spec.kind == "bool":
                yield Checkbox(value=bool(initial), id=_widget_id(spec.key))
            elif spec.kind == "enum":
                yield Select(
                    [(o, o) for o in (spec.options or ())],
                    value=initial if initial in (spec.options or ()) else (spec.options or (None,))[0],
                    allow_blank=False,
                    id=_widget_id(spec.key),
                )
            elif spec.kind == "list":
                yield Input(value=_list_to_text(initial), id=_widget_id(spec.key))
            else:
                yield Input(value="" if initial is None else str(initial), id=_widget_id(spec.key))
            yield Static(prov_text, classes="field-provenance")

    # ── dirty tracking (per-screen, not EditableConfig.dirty — nothing is
    # written to `document` until Save, so cfg.dirty stays False the whole
    # time the user is mid-form; this local flag is what Escape checks) ──────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == _widget_id("working_directory"):
            self.query_one("#wd-warning", Static).update(
                _working_directory_warning(self.cfg.path, event.input.value)
            )
        if self._populating:
            return
        self._form_dirty = True

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._populating:
            return
        self._form_dirty = True

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._populating:
            return
        self._form_dirty = True

    # ── actions ──────────────────────────────────────────────────────────────

    async def action_edit(self) -> None:
        if self.mode != "view":
            return
        self.mode = "edit"
        self._form_dirty = False
        self._compute_initial_values()
        # recompose() does NOT re-trigger on_mount (that only fires once, for
        # the screen's own initial push) — so the populating guard has to be
        # armed and disarmed around this recompose explicitly, the same way
        # on_mount handles it for a screen pushed directly in create mode.
        self._populating = True
        await self.recompose()
        self.call_after_refresh(self._stop_populating)
        # Footer subscribes to Screen.bindings_updated_signal in ITS OWN
        # on_mount — recompose() mounts a brand-new Footer instance, but
        # nothing re-publishes that signal just because a new subscriber
        # showed up, so the fresh Footer's `_bindings_ready` reactive stays
        # False forever and it renders as a blank bar (confirmed empirically:
        # reproduced by view -> edit -> back -> edit and inspecting Footer's
        # FooterKey children directly — 4 at first mount, 0 after this
        # recompose, permanently). refresh_bindings() is Screen's own public
        # method for exactly this: it re-publishes the signal so every
        # current subscriber (including the new Footer) recomputes.
        self.refresh_bindings()

    @work
    async def action_back(self) -> None:
        if self.mode == "view":
            self.app.pop_screen()
            return
        if self._form_dirty:
            discard = await self.app.push_screen_wait(
                ConfirmModal("Discard unsaved changes to this agent?", confirm_label="Discard")
            )
            if not discard:
                return
        if self.mode == "create":
            self.app.pop_screen()
        else:
            self.mode = "view"
            self._form_dirty = False
            await self.recompose()
            self.refresh_bindings()  # see action_edit()'s comment — same fix

    def _collect_field_updates(self) -> dict[str, object] | None:
        """Diff every form widget against `_initial_values`. Returns
        {dotted_key: new_value_or_None} for changed fields only (None means
        "clear it — revert to inherited/default"), or None (having already
        called self.notify()) if a field fails to parse."""
        updates: dict[str, object] = {}
        for spec in _ALL_FORM_FIELDS:
            widget = self.query_one("#" + _widget_id(spec.key))
            try:
                new_value = self._read_widget_value(spec, widget)
            except ValueError as exc:
                self.notify(str(exc), severity="error")
                return None
            if new_value != self._initial_values.get(spec.key):
                updates[spec.key] = new_value

        desc_widget = self.query_one("#field-description", Input)
        new_desc = desc_widget.value.strip() or None
        if new_desc != self._initial_values.get("description"):
            updates["description"] = new_desc
        return updates

    def _read_widget_value(self, spec: _FieldSpec, widget: object) -> object:
        if spec.kind == "bool":
            return widget.value
        if spec.kind == "enum":
            return widget.value
        if spec.kind == "list":
            return _text_to_list(widget.value) or None
        if spec.kind == "int":
            text = widget.value.strip()
            if not text:
                return None
            try:
                return int(text)
            except ValueError:
                raise ValueError(f"{spec.label}: must be a whole number, got {text!r}") from None
        text = widget.value.strip()
        return text or None

    async def action_save(self) -> None:
        if self.mode == "view":
            return

        updates = self._collect_field_updates()
        if updates is None:
            return  # a field failed to parse; notify() already shown

        name = self.agent_name
        if self.mode == "create":
            name = self.query_one("#field-name", Input).value.strip()
            if not name:
                self.notify("Name is required.", severity="error")
                return
            if name in self.cfg.agents_raw:
                self.notify(f"An agent named '{name}' already exists.", severity="error")
                return

        target_entry = self.entry if self.mode == "edit" else dict(self.entry)
        for key, value in updates.items():
            _apply_update(target_entry, key, value)

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
            self.notify(f"Could not save: {exc}", severity="error")
            return

        self.app.pop_screen()
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.notify(f"Saved agent '{name}'.", severity="information")
        app.reload_config()
