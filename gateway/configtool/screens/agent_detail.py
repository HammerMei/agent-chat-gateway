"""AgentDetailScreen — view, edit, and create a single agent.

Unlike connectors, the agent schema is complete (additionalProperties:
false in gateway/schema/config.schema.json's $defs/agent), so this shows a
fixed field list with a provenance marker per field (explicit / inherited
from the agent's own `inherits:` template / explicit-null-suppressing)
instead of a generic dump.
`_FORM_FIELDS`/`_PERMISSIONS_FORM_FIELDS` below are a manually-maintained
mirror of that schema (matching Phase 1's `_KNOWN_FIELDS`, not a runtime
JSON-schema interpreter) — safe because the schema is closed
(`additionalProperties: false`), so there's no drift risk from a field this
form doesn't know about sneaking in.

Tool lists (`owner_allowed_tools`/`guest_allowed_tools`) render read-only in
view mode (`_body_text()`), but are directly editable in edit/create mode via
two `ListView`s with dedicated "+ Add"/"- Remove" `Button`s beside each list
(user-reported: the previous 'a'/'x' single-key bindings silently typed
those letters into whatever Input happened to have focus instead of
triggering the action — a real risk of quietly corrupting an unrelated
field's value; buttons have no such conflict). They live OUTSIDE the
`FieldSpec`/`apply_update()` diffing pipeline (that machinery is scalar-
field-shaped; a list of preset-references/inline-rule-dicts doesn't fit it)
— `_tool_list_state()` snapshots the MERGED starting value (same "what's
currently in effect" semantics `_compute_initial_values()` uses for every
other field) and `action_save()` diffs the FINAL local list against that
snapshot itself, writing an explicit override only if it actually changed
(matching decision 2: "editing an inherited field always writes an explicit
per-entry override" — untouched stays untouched, exactly as
`_collect_field_updates()` already does for every scalar field).

The Inherits row is likewise a `Button` (same 'i'-key-conflict reasoning),
not a `FieldSpec`-pipeline field — picking a new template affects the
EFFECTIVE value of every OTHER field (unlike any single scalar field, which
only affects itself), so unlike every other field's "snapshot once at open,
diff at Save" semantics, picking a new template triggers a full
`_recompute_form()` (form_common.py): every field's prefilled value and
provenance label are recomputed against the NEWLY selected template and the
form is recomposed from scratch. If the user had already typed unsaved
edits into OTHER fields before switching, those would be silently discarded
by that recompute — `_any_field_overridden()` is checked first and, if
true, a `ConfirmModal` warns before proceeding. `_current_entry()` below
returns a PROBE entry (`self.entry` with `inherits:` swapped to whatever the
picker currently has selected, not yet saved) — every live-display
computation (compose, provenance labels, ctrl+r, tool-list prefill) reads
through this probe, not `self.entry` directly, so they all reflect the
picker's current state rather than only updating after Save.

Edit/create + Save/dirty/navigation machinery lives in `.form_common`
(`FormScreen`) — shared with `ConnectorDetailScreen`. This module supplies
the agent-specific pieces: which fields exist, their dataclass defaults, and
`action_save()`'s entity-shaped insertion (`document["agents"]` is a dict
keyed by name, unlike connectors' list).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from ..formatting import format_value, provenance_label
from ..modals import (
    ConfirmModal,
    InheritsPickerModal,
    InlineToolRuleModal,
    MessageModal,
    PresetOrInlineModal,
    TextPromptModal,
)
from ..model import EditableConfig
from .form_common import FieldSpec, FormScreen, apply_update, find_referencing_watcher_labels
from .tool_presets import ToolPresetsScreen

# NOTE: TemplateDetailScreen (screens/template_detail.py) is deliberately
# imported LOCALLY inside _open_inherits_picker() below, not at module level —
# template_detail.py itself imports AGENT_FORM_FIELDS/AGENT_DATACLASS_DEFAULTS
# from this module, so a module-level import here would be circular.

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
# when a field is set by neither the entry nor its inherits: template.
# Unlike view mode (which simply omits a line for an absent field), a form
# editing that field needs to show what it would actually evaluate to right
# now. Public (no leading underscore): TemplateDetailScreen reuses this dict
# too, as the "no dataclass default" fallback value for editing an agent
# template's own fields.
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
# Public (no leading underscore): also reused by TemplateDetailScreen to
# edit an agent template with the exact same field set, schema-derived-so-
# zero-drift-risk reasoning applies just as much there — every one of these
# keys is legal in an agent template too (gateway/config.py's forbidden-keys
# set for agent_templates is empty).
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

    DEFAULT_CSS = """
    AgentDetailScreen #owner-tools-list, AgentDetailScreen #guest-tools-list {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
    }
    AgentDetailScreen .tool-list-buttons {
        height: auto;
        margin-bottom: 1;
    }
    AgentDetailScreen .tool-list-buttons Button {
        margin-right: 1;
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
        # Real-Bug-fixed: ListView's own `.index` reactive defaults to 0 the
        # INSTANT it mounts with any children — not None — so "index is not
        # None" cannot distinguish "the user actually selected an item" from
        # "nobody has touched this list yet." Tracked here instead, per list,
        # set True only by a genuine on_list_view_highlighted() event (see
        # below) while NOT `_populating` — the same guard on_input_changed()
        # already uses to ignore the initial mount-time burst of Changed
        # events, reused here for the identical reason.
        self._tool_list_ever_selected: dict[str, bool] = dict.fromkeys(_TOOL_LIST_WIDGET_IDS, False)
        # See _open_inherits_picker()/action_save() below. Unlike every
        # other field, switching this one triggers a full
        # _recompute_form() (form_common.py) rather than a snapshot-once-
        # at-open value — see module docstring for why.
        self._inherits_initial: str | None = self.cfg.entry_template_name(self.entry)
        self._inherits_current: str | None = self._inherits_initial
        if self.mode != "view":
            self._compute_initial_values(self._current_entry())
            self._tool_list_state()
            self._populating = True

    def _entity_noun(self) -> str:
        return "agent"

    def _entity_label(self) -> str:
        return self.agent_name

    def _current_entry(self) -> dict:
        """The PROBE entry: `self.entry` (the on-disk explicit fields) with
        `inherits:` swapped to whatever the Inherits picker currently has
        selected — NOT necessarily what's saved yet. Every live-display
        computation (compose, provenance labels, ctrl+r, tool-list prefill)
        reads through this, not `self.entry` directly, so switching
        templates is reflected immediately rather than only after Save."""
        probe = dict(self.entry)
        if self._inherits_current is None:
            probe.pop("inherits", None)
        else:
            probe["inherits"] = self._inherits_current
        return probe

    def _any_field_overridden(self) -> bool:
        # Tool lists live outside the FieldSpec pipeline (see module
        # docstring) — FormScreen's own check only covers _field_specs(),
        # so this also counts an in-progress, unsaved tool-list edit as an
        # override the Inherits-switch confirm needs to warn about.
        if super()._any_field_overridden():
            return True
        return any(
            self._tool_lists[key] != self._tool_lists_initial[key]
            for key in _TOOL_LIST_WIDGET_IDS
        )

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
        self._inherits_initial = self.cfg.entry_template_name(self.entry)
        self._inherits_current = self._inherits_initial
        self._compute_initial_values(self._current_entry())
        self._tool_list_state()
        self._tool_list_ever_selected = dict.fromkeys(_TOOL_LIST_WIDGET_IDS, False)

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        return AGENT_FORM_FIELDS

    def _template_kind(self) -> str:
        return "agent"

    def _dataclass_defaults(self) -> dict[str, object]:
        return AGENT_DATACLASS_DEFAULTS

    # ── tool-list editor (owner_allowed_tools / guest_allowed_tools) ────────

    def _tool_list_state(self) -> None:
        """(Re)snapshot both tool lists to their MERGED (effective) value —
        same semantics `_compute_initial_values()` uses for every scalar
        field: the form shows what's ACTUALLY in effect right now (inherited
        from the agent's own `inherits:` template — or whichever template
        the Inherits picker currently has selected, see `_current_entry()`
        — or explicit on this entry), and `action_save()` below only writes
        an explicit override if the final list actually differs from this
        snapshot."""
        try:
            merged = self.cfg.merged_entry(self._template_kind(), self._current_entry())
        except (ValueError, FileNotFoundError):
            merged = dict(self._current_entry())
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

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Real-Bug-fixed: `ListView.index` defaults to 0 (not None) the
        instant the list mounts with any children — it cannot by itself
        distinguish "the user selected an item" from "nobody has touched
        this list yet." This event ALSO fires for that automatic mount-time
        highlight, so it's gated by `_populating` (the same guard
        `on_input_changed()` already uses to ignore its own mount-time
        burst) — only a highlight change that happens AFTER the initial
        populate counts as a real, user-driven selection.

        This alone isn't enough, though: clicking an item that's ALREADY
        the highlighted one (e.g. the only item in a 1-item list, or simply
        the default index-0 item) changes no reactive value, so this event
        never fires at all for that click — see `on_descendant_focus()`
        below for the other half of this fix."""
        if self._populating:
            return
        key = getattr(event.list_view, "tool_list_key", None)
        if key is not None:
            self._tool_list_ever_selected[key] = True

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """A ListView gaining real DOM focus (click, or Tab) is ALSO a
        genuine "the user is interacting with this list" signal, independent
        of on_list_view_highlighted() above — clicking an item that's
        already highlighted (common: the only item in a 1-item list) gives
        the ListView focus without changing `.index`, so that event alone
        would never fire. `DescendantFocus` bubbles up to the screen
        regardless, and isn't gated by `_populating` — focus during the
        initial populate never lands on a ListView in the first place
        (Textual doesn't auto-focus a freshly mounted, non-default widget)."""
        if not self._populating:
            key = getattr(event.widget, "tool_list_key", None)
            if key is not None:
                self._tool_list_ever_selected[key] = True

    @work
    async def _add_tool_rule(self, key: str) -> None:
        """Triggered by the "+ Add" button beside the owner/guest list —
        `key` comes straight from the button's own id (`add-tool-<key>`,
        see `on_button_pressed()`), not from focus/keybinding guessing."""
        if self.mode == "view" or key not in _TOOL_LIST_WIDGET_IDS:
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

    def _remove_tool_rule(self, key: str) -> None:
        """Triggered by the "- Remove" button beside the owner/guest list —
        removes whichever item THAT list's own ListView cursor is
        currently on (click/arrow-key to select first).

        `_tool_list_ever_selected[key]` — NOT just `list_view.index is None`
        — gates this: ListView's `.index` is already `0` the instant it
        mounts with any children, with zero user interaction, so "index is
        not None" alone can't tell a real selection apart from that
        automatic default. Without this check, clicking Remove as the very
        first action after opening the form silently deleted item 0."""
        if self.mode == "view" or key not in _TOOL_LIST_WIDGET_IDS:
            return
        list_view = self.query_one(f"#{_TOOL_LIST_WIDGET_IDS[key]}", ListView)
        if not self._tool_list_ever_selected.get(key) or list_view.index is None:
            self.notify("Select an item in the list first.", severity="warning")
            return
        idx = list_view.index
        if idx >= len(self._tool_lists[key]):
            return
        del self._tool_lists[key][idx]
        self._form_dirty = True
        self._refresh_tool_list(key)

    # ── inherits: picker ─────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "inherits-change-button":
            self._open_inherits_picker()
        elif button_id.startswith("add-tool-"):
            self._add_tool_rule(button_id.removeprefix("add-tool-"))
        elif button_id.startswith("remove-tool-"):
            self._remove_tool_rule(button_id.removeprefix("remove-tool-"))

    @work
    async def _open_inherits_picker(self) -> None:
        if self.mode == "view":
            return
        template_names = sorted(self.cfg.templates("agent"))
        choice = await self.app.push_screen_wait(
            InheritsPickerModal(template_names, self._inherits_current)
        )
        if choice is None:
            return
        kind, name = choice

        if kind == "template":
            new_value = name
        elif kind == "none":
            new_value = None
        elif kind == "new_template":
            new_name = await self.app.push_screen_wait(
                TextPromptModal("New agent template — name")
            )
            if new_name is None:
                return
            if new_name in self.cfg.templates("agent"):
                await self.app.push_screen_wait(
                    MessageModal(
                        f"An agent template named '{new_name}' already exists.",
                        title="Could not create",
                    )
                )
                return
            # One-way detour, not a return-with-result flow (see
            # InheritsPickerModal's docstring): the user fills in the new
            # template over there, presses Escape to come back HERE, then
            # clicks the Inherits button again to actually reference it.
            from .template_detail import TemplateDetailScreen

            self.app.push_screen(TemplateDetailScreen(self.cfg, "agent", new_name, {}, mode="create"))
            return
        else:
            return

        if new_value == self._inherits_current:
            return  # no actual change — nothing to warn about or recompute

        if self._any_field_overridden():
            confirmed = await self.app.push_screen_wait(
                ConfirmModal(
                    "Switching templates will reset any unsaved edits to "
                    "the fields below back to the new template's values. "
                    "Continue?",
                    confirm_label="Switch",
                )
            )
            if not confirmed:
                return

        self._inherits_current = new_value
        self._tool_list_state()
        self._tool_list_ever_selected = dict.fromkeys(_TOOL_LIST_WIDGET_IDS, False)
        await self._recompute_form()
        self._form_dirty = True

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        description = self.entry.get("description")
        lines = [f"[bold]{self.agent_name}[/bold]"]
        if description:
            lines.append(f"[dim]{description}[/dim]")
        template_name = self.cfg.entry_template_name(self.entry)
        lines.append(f"inherits: {template_name if template_name else '(none)'}")
        lines.append("")

        try:
            merged = self.cfg.merged_entry("agent", self.entry)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(f"[red]Could not compute effective values: {exc}[/red]")
            return "\n".join(lines)

        for key in _KNOWN_FIELDS:
            if key not in merged:
                continue
            provenance = self.cfg.field_provenance("agent", self.entry, key)
            lines.append(
                f"{key}: {format_value(merged[key])}  "
                f"[dim]({provenance_label(provenance, template_name)})[/dim]"
            )

        for label, field_key in (
            ("owner_allowed_tools", "owner_allowed_tools"),
            ("guest_allowed_tools", "guest_allowed_tools"),
        ):
            if field_key not in merged:
                continue
            provenance = self.cfg.field_provenance("agent", self.entry, field_key)
            lines.append("")
            lines.append(f"{label}:  [dim]({provenance_label(provenance, template_name)})[/dim]")
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

            with Horizontal(classes="field-row"):
                yield Static("Inherits", classes="field-label")
                yield Static(
                    self._inherits_current or "(none)",
                    id="inherits-value",
                    classes="field-value",
                )
                yield Button("Change…", id="inherits-change-button")

            for spec in _FORM_FIELDS:
                yield from self._compose_field_row(spec, self._current_entry())
                if spec.key == "working_directory":
                    yield Static(
                        _working_directory_warning(
                            self.cfg.path, str(self._initial_values.get(spec.key) or "")
                        ),
                        id="wd-warning",
                    )

            yield Static("[bold]Permissions[/bold]")
            for spec in _PERMISSIONS_FORM_FIELDS:
                yield from self._compose_field_row(spec, self._current_entry())

            for key, label in (
                ("owner_allowed_tools", "Owner allowed tools"),
                ("guest_allowed_tools", "Guest allowed tools"),
            ):
                yield Static(f"[bold]{label}[/bold]")
                list_view = ListView(*self._tool_list_items(key), id=_TOOL_LIST_WIDGET_IDS[key])
                # Tagged so on_list_view_highlighted() below can map the
                # event back to which of the two lists it is, without
                # unmunging the widget id.
                list_view.tool_list_key = key
                yield list_view
                with Horizontal(classes="tool-list-buttons"):
                    yield Button("+ Add", id=f"add-tool-{key}")
                    yield Button("- Remove", id=f"remove-tool-{key}")

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

        # inherits: lives outside the FieldSpec/apply_update() pipeline too
        # (it's driven by InheritsPickerModal, not an Input/Checkbox/Select
        # widget) — diffed here directly against the snapshot taken when the
        # form opened (or last re-entered edit mode), same "only write what
        # actually changed" semantics as every other field.
        if self._inherits_current != self._inherits_initial:
            apply_update(target_entry, "inherits", self._inherits_current)

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
