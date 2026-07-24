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

Tool lists (`owner_allowed_tools`/`guest_allowed_tools`) render read-only in
view mode (`_body_text()`), but are directly editable in edit/create mode via
two `ListView`s ('a' adds — `PresetOrInlineModal` — 'x' removes the focused
item). They live OUTSIDE the `FieldSpec`/`apply_update()` diffing pipeline
(that machinery is scalar-field-shaped; a list of preset-references/inline-
rule-dicts doesn't fit it) — `_tool_list_state()` snapshots the MERGED
starting value (same "what's currently in effect" semantics
`_compute_initial_values()` uses for every other field) and `action_save()`
diffs the FINAL local list against that snapshot itself, writing an explicit
override only if it actually changed (matching decision 2: "editing an
inherited field always writes an explicit per-entry override" — untouched
stays untouched, exactly as `_collect_field_updates()` already does for
every scalar field).

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
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Label, ListItem, ListView, Static

from ..formatting import format_value, provenance_label
from ..modals import InlineToolRuleModal, MessageModal, PresetOrInlineModal, TextPromptModal
from ..model import EditableConfig
from .form_common import FieldSpec, FormScreen, apply_update, find_referencing_watcher_labels
from .tool_presets import ToolPresetsScreen

if TYPE_CHECKING:
    from ..app import ConfigToolApp

# The two agent tool-list keys the editor below handles, and the ListView
# widget id each renders into (kept as one dict, not two, so the shared
# code below never has to enumerate them separately from their ids).
_TOOL_LIST_WIDGET_IDS: dict[str, str] = {
    "owner_allowed_tools": "owner-tools-list",
    "guest_allowed_tools": "guest-tools-list",
}


def _format_tool_rule(item: object) -> str:
    if isinstance(item, str):
        return f"→ preset: {item}"
    if isinstance(item, dict):
        tool = item.get("tool", "?")
        params = item.get("params")
        return f"{tool} / {params or '(any)'}"
    return str(item)

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
# field needs to show what it would actually evaluate to right now. Public
# (no leading underscore): DefaultsScreen reuses this dict too, as the
# "no shared default set" fallback value for editing agent_defaults itself.
AGENT_DATACLASS_DEFAULTS: dict[str, object] = {
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
# Public (no leading underscore): also reused by DefaultsScreen to edit
# agent_defaults with the exact same field set, schema-derived-so-zero-
# drift-risk reasoning applies just as much there — every one of these keys
# is legal in agent_defaults too (gateway/config.py's forbidden-keys set for
# agent_defaults is empty).
AGENT_FORM_FIELDS = (*_FORM_FIELDS, *_PERMISSIONS_FORM_FIELDS)


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

    BINDINGS = [
        Binding("a", "add_tool_rule", "Add tool rule", show=True),
        Binding("x", "remove_tool_rule", "Remove tool rule", show=True),
    ]

    DEFAULT_CSS = """
    AgentDetailScreen #owner-tools-list, AgentDetailScreen #guest-tools-list {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
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
        self._tool_lists: dict[str, list] = {}
        self._tool_lists_initial: dict[str, list] = {}
        if self.mode != "view":
            self._compute_initial_values(self.entry)
            self._tool_list_state()
            self._populating = True

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("add_tool_rule", "remove_tool_rule"):
            return self.mode != "view"
        return super().check_action(action, parameters)

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

    def _install_trial_entry(self, target_entry: dict) -> None:
        self.cfg.document.setdefault("agents", {})[self.agent_name] = target_entry

    def _rollback_trial_entry(self) -> None:
        self.cfg.document.setdefault("agents", {})[self.agent_name] = self.entry

    def _on_enter_edit_mode(self) -> None:
        self._compute_initial_values(self.entry)
        self._tool_list_state()

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        return AGENT_FORM_FIELDS

    def _defaults_kind(self) -> str:
        return "agent_defaults"

    def _dataclass_defaults(self) -> dict[str, object]:
        return AGENT_DATACLASS_DEFAULTS

    # ── tool-list editor (owner_allowed_tools / guest_allowed_tools) ────────

    def _tool_list_state(self) -> None:
        """(Re)snapshot both tool lists to their MERGED (effective) value —
        same semantics `_compute_initial_values()` uses for every scalar
        field: the form shows what's ACTUALLY in effect right now (inherited
        from agent_defaults or explicit on this entry), and `action_save()`
        below only writes an explicit override if the final list actually
        differs from this snapshot."""
        try:
            merged = self.cfg.merged_entry(self._defaults_kind(), self.entry)
        except (ValueError, FileNotFoundError):
            merged = dict(self.entry)
        self._tool_lists = {
            key: list(merged.get(key) or []) for key in _TOOL_LIST_WIDGET_IDS
        }
        self._tool_lists_initial = {k: list(v) for k, v in self._tool_lists.items()}

    def _tool_list_items(self, key: str) -> list[ListItem]:
        return [
            ListItem(Label(_format_tool_rule(item)), name=str(i))
            for i, item in enumerate(self._tool_lists[key])
        ]

    def _refresh_tool_list(self, key: str) -> None:
        list_view = self.query_one(f"#{_TOOL_LIST_WIDGET_IDS[key]}", ListView)
        list_view.clear()
        for i, item in enumerate(self._tool_lists[key]):
            list_view.append(ListItem(Label(_format_tool_rule(item)), name=str(i)))

    def _focused_tool_list_key(self) -> str | None:
        return getattr(self.focused, "tool_list_key", None)

    @work
    async def action_add_tool_rule(self) -> None:
        if self.mode == "view":
            return
        key = self._focused_tool_list_key()
        if key is None:
            self.notify("Focus the owner or guest tool list first.", severity="warning")
            return

        preset_names = sorted(self.cfg.tool_presets_raw.keys())
        choice = await self.app.push_screen_wait(PresetOrInlineModal(preset_names))
        if choice is None:
            return
        kind, preset_name = choice

        if kind == "preset":
            item: object = preset_name
        elif kind == "inline":
            rule = await self.app.push_screen_wait(InlineToolRuleModal())
            if rule is None:
                return
            item = rule
        elif kind == "new_preset":
            name = await self.app.push_screen_wait(TextPromptModal("New tool preset — name"))
            if name is None:
                return
            if name in self.cfg.tool_presets_raw:
                await self.app.push_screen_wait(
                    MessageModal(f"A tool preset named '{name}' already exists.", title="Could not create")
                )
                return
            # A one-way detour, not a return-with-result flow (see
            # PresetOrInlineModal's docstring): the user adds rules to the
            # new preset over there, then presses Escape to come back HERE
            # and reference it via "preset" like any other existing preset.
            self.app.push_screen(ToolPresetsScreen(self.cfg, name))
            return
        else:
            return

        self._tool_lists[key].append(item)
        self._form_dirty = True
        self._refresh_tool_list(key)

    def action_remove_tool_rule(self) -> None:
        if self.mode == "view":
            return
        key = self._focused_tool_list_key()
        if key is None:
            self.notify("Focus the owner or guest tool list first.", severity="warning")
            return
        list_view = self.focused
        if list_view.index is None:
            self.notify("No item selected.", severity="warning")
            return
        idx = list_view.index
        if idx >= len(self._tool_lists[key]):
            return
        del self._tool_lists[key][idx]
        self._form_dirty = True
        self._refresh_tool_list(key)

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
                lines.append(f"  {_format_tool_rule(item)}")

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

            for key, label in (
                ("owner_allowed_tools", "Owner allowed tools"),
                ("guest_allowed_tools", "Guest allowed tools"),
            ):
                yield Static(f"[bold]{label}[/bold]  [dim]('a' add / 'x' remove)[/dim]")
                list_view = ListView(*self._tool_list_items(key), id=_TOOL_LIST_WIDGET_IDS[key])
                # Tagged so _focused_tool_list_key() can map the currently
                # focused widget back to which of the two lists it is,
                # without unmunging the widget id — same pattern
                # _compose_field_row() uses to tag an Input with its
                # FieldSpec via `.field_key`.
                list_view.tool_list_key = key
                yield list_view

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

        # ALWAYS a trial copy, never self.entry directly — even for "edit",
        # where self.entry is the SAME object already living in
        # cfg.document. Mutating it here, before save() has even run, would
        # leave invalid data sitting in the document if save() then fails
        # (a real bug: reported as "Save failed, but Back still showed the
        # invalid value" — the fix is never mutating the original until
        # save() has actually succeeded).
        target_entry = dict(self.entry)
        for key, value in updates.items():
            apply_update(target_entry, key, value)

        # Tool lists live outside the FieldSpec/apply_update() pipeline (see
        # module docstring) — diffed here, directly, against the MERGED
        # snapshot _tool_list_state() took when the form opened. Untouched
        # stays untouched (no key written at all, preserving whatever
        # explicit/inherited state the entry already had); a genuinely
        # changed list is always written in full, as an explicit override
        # (never popped back to "inherited" on empty — an agent explicitly
        # narrowing itself to zero allowed tools is meaningfully different
        # from never having set the key at all, so this never silently
        # reinterprets "cleared the list" as "revert to defaults").
        for key in _TOOL_LIST_WIDGET_IDS:
            if self._tool_lists[key] != self._tool_lists_initial[key]:
                target_entry[key] = list(self._tool_lists[key])

        if self.mode == "create":
            self.cfg.document.setdefault("agents", {})[name] = target_entry
        else:
            self._install_trial_entry(target_entry)
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create":
                # Nothing existed under this name before this screen ever
                # ran — remove it so a failed save doesn't leave a phantom
                # half-created agent sitting in memory.
                del self.cfg.document["agents"][name]
            else:
                self._rollback_trial_entry()
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return

        self.entry = target_entry
        self.app.pop_screen()
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.notify(f"Saved agent '{name}'.", severity="information")
        app.reload_config()
