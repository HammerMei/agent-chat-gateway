"""TemplateDetailScreen — view, edit, create, and delete one NAMED template
(an `agent_templates:`/`connector_templates:`/`watcher_templates:` entry),
across all three kinds. Replaces the deleted `defaults.py`/`DefaultsScreen`
(the old, pre-v0.3 single-global-block-per-kind editor).

Unlike the block `DefaultsScreen` used to edit, a NAMED template genuinely
IS a creatable/deletable/nameable entity now (that's the whole point of the
`*_templates:`/`inherits:` redesign — multiple templates can coexist per
kind), so this extends `FormScreen` directly, reusing its create/delete/
dirty-tracking machinery exactly like `AgentDetailScreen`/`ConnectorDetailScreen`
do. `DefaultsScreen`'s OWN reasoning for NOT extending `FormScreen` ("a
defaults block has no entity to create or delete, and its own fields have no
provenance concept") simply no longer holds once the "block" became a set of
independently named templates.

A template has no `inherits:` of its own (forbidden — no nested templates,
enforced by `gateway/config.py`'s `_parse_templates_block`), so this overrides
three of `FormScreen`'s hooks that would otherwise try to merge against one:
  - `_compute_initial_values()` reads straight off the template's own entry +
    the dataclass defaults (`AGENT_DATACLASS_DEFAULTS`/etc.) — no
    `merged_entry()` call. Falsy-empty ("" / []) values are normalized to
    `None` here (mirroring the old `DefaultsScreen`'s own normalization) —
    without it, a field whose only value is a falsy dataclass default would
    look "changed" on every Save purely because of how `read_widget_value()`
    reads back an untouched str/list Input, producing a false-positive
    blast-radius confirm below for a field nobody actually edited.
  - `_field_provenance()` always returns `None` — a template's own fields
    have no "explicit vs. inherited" distinction (nothing for a template to
    inherit FROM).
  - `_compose_field_row()` renders a BLAST-RADIUS count instead of a
    provenance label — "N inherit, M override", counting entries (of this
    same kind) whose `inherits:` names THIS specific template
    (`find_entries_referencing_template()`, form_common.py) and don't
    already set the field themselves. This is the actual point of the
    whole redesign: blast radius is scoped to ONE named template, not
    (as `DefaultsScreen` computed it) "every entry in the whole config."
  - `action_reset_field()` resets straight to the dataclass default (no
    merge — there's nothing to merge against).

Connector templates require picking a `type` up front, exactly like a brand
new connector does (`OverviewScreen.action_new_entity()`/
`ConnectorDetailScreen._open_inherits_picker()`'s "new_template" branch both
already do this before ever constructing this screen) — `type` itself is
never a `_field_specs()` row (immutable, shown as a banner only), matching
`ConnectorDetailScreen`'s own type-immutability precedent. This screen has
no generic tree editor (see `connector_detail.py`'s own module docstring for
why); reusing connectors' existing per-type field lists is the only
practical option here too — a genuinely type-agnostic shared block stays
hand-editable via `$EDITOR`.
"""

from __future__ import annotations

from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Select, Static

from ..modals import ConfirmModal, MessageModal
from ..model import EditableConfig
from .agent_detail import AGENT_DATACLASS_DEFAULTS, AGENT_FORM_FIELDS
from .connector_detail import DATACLASS_DEFAULTS_BY_TYPE, FIELDS_BY_TYPE
from .form_common import (
    FieldSpec,
    FormScreen,
    apply_update,
    find_entries_referencing_template,
    get_nested,
    list_to_text,
    read_widget_value,
    set_widget_value,
    widget_id,
)
from .tool_list_editor import TOOL_LIST_WIDGET_IDS, ToolListEditorMixin, format_tool_rule
from .watcher_detail import WATCHER_TEMPLATE_DATACLASS_DEFAULTS, WATCHER_TEMPLATE_FIELDS

# kind -> (field specs, dataclass-defaults fallback). Connector is a
# function of the template's own `type`, so it's resolved per-instance
# (_field_specs()/_dataclass_defaults() below), not a static dict entry.
_STATIC_FIELDS_BY_KIND: dict[str, tuple[tuple[FieldSpec, ...], dict[str, object]]] = {
    "agent": (AGENT_FORM_FIELDS, AGENT_DATACLASS_DEFAULTS),
    "watcher": (WATCHER_TEMPLATE_FIELDS, WATCHER_TEMPLATE_DATACLASS_DEFAULTS),
}

TEMPLATE_KINDS = ("agent", "connector", "watcher")


class TemplateDetailScreen(ToolListEditorMixin, FormScreen):
    BODY_ID = "template-detail-body"

    def __init__(
        self,
        cfg: EditableConfig,
        kind: Literal["agent", "connector", "watcher"],
        template_name: str,
        entry: dict,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        self.kind = kind
        self.template_name = template_name
        self.entry = entry
        self.mode = mode
        self._init_tool_lists()
        if self.mode != "view":
            self._compute_initial_values(self.entry)
            self._tool_list_state()
            self._populating = True

    def _on_enter_edit_mode(self) -> None:
        self._compute_initial_values(self.entry)
        self._tool_list_state()

    def _entity_noun(self) -> str:
        return f"{self.kind} template"

    def _entity_label(self) -> str:
        return self.template_name

    def _current_entry(self) -> dict:
        return self.entry

    def _template_kind(self) -> str:
        # Defensive only — _compute_initial_values()/_field_provenance()/
        # action_reset_field() are all overridden below to never call
        # cfg.merged_entry()/field_provenance() (a template has nothing to
        # merge against), so this should never actually be reached.
        return self.kind

    def _connector_type(self) -> str:
        # A template's own raw entry is authoritative (never merged/
        # inherited — templates can't nest), unlike
        # ConnectorDetailScreen._connector_type()'s merged-value read.
        return self.entry.get("type", "rocketchat")

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        if self.kind == "connector":
            return FIELDS_BY_TYPE.get(self._connector_type(), ())
        return _STATIC_FIELDS_BY_KIND[self.kind][0]

    def _dataclass_defaults(self) -> dict[str, object]:
        if self.kind == "connector":
            return DATACLASS_DEFAULTS_BY_TYPE.get(self._connector_type(), {})
        return _STATIC_FIELDS_BY_KIND[self.kind][1]

    def _tool_list_starting_values(self) -> dict[str, list]:
        # No merge (a template has nothing to merge against, unlike
        # AgentDetailScreen's own version of this hook) — the template's
        # OWN raw value directly, same "read straight off entry" philosophy
        # as _compute_initial_values() above. A no-op for connector/watcher
        # templates (neither key is ever set on those, so this is just
        # {key: []} for both — harmless; _compose_form() below only
        # actually renders the tool-list rows for kind == "agent").
        return {key: self.entry.get(key) or [] for key in TOOL_LIST_WIDGET_IDS}

    # ── no inherits: concept for a template's own fields ─────────────────────

    def _compute_initial_values(self, entry: dict) -> None:
        self._reset_keys = {}
        dataclass_defaults = self._dataclass_defaults()
        for spec in self._field_specs():
            value = get_nested(entry, spec.key)
            if value is None:
                value = dataclass_defaults.get(spec.key)
            # See module docstring: normalizes a falsy dataclass-default
            # value ("" / []) to None so it matches read_widget_value()'s
            # own untouched-field readback — prevents a false-positive
            # blast-radius confirm for a field nobody actually edited.
            if spec.kind in ("str", "list") and value is not None and not value:
                value = None
            self._initial_values[spec.key] = value
        self._initial_values["description"] = entry.get("description")

    def _field_provenance(self, spec: FieldSpec, entry: dict):
        return None

    def _compose_field_row(self, spec: FieldSpec, entry: dict) -> ComposeResult:
        referencing = find_entries_referencing_template(self.cfg, self.kind, self.template_name)
        top_key = spec.key.split(".", 1)[0]
        inherit_count = sum(1 for _, e in referencing if top_key not in e)
        override_count = len(referencing) - inherit_count
        blast_text = f"[dim]({inherit_count} inherit, {override_count} override)[/dim]"
        initial = self._initial_values.get(spec.key)
        with Horizontal(classes="field-row"):
            yield Static(spec.label, classes="field-label")
            if spec.kind == "bool":
                widget = Checkbox(value=bool(initial), id=widget_id(spec.key))
            elif spec.kind == "enum":
                options = spec.options or ()
                widget = Select(
                    [(o, o) for o in options],
                    value=initial if initial in options else (options or (None,))[0],
                    allow_blank=False,
                    id=widget_id(spec.key),
                )
            elif spec.kind == "list":
                widget = Input(value=list_to_text(initial), id=widget_id(spec.key))
            else:
                widget = Input(
                    value="" if initial is None else str(initial),
                    id=widget_id(spec.key),
                    password=spec.secret,
                )
            widget.field_key = spec.key
            yield widget
            yield Static(blast_text, classes="field-provenance")

    def action_reset_field(self) -> None:
        """ctrl+r: clear the FOCUSED field back to "this template doesn't
        set it" (pop it from the template on Save) — the template-scoped
        equivalent of FormScreen's own ctrl+r "revert to inherited": a
        template has no further parent to revert TO, so the target state
        here is simply absent, straight from the dataclass default."""
        widget = self.focused
        field_key = getattr(widget, "field_key", None)
        if field_key is None:
            return
        spec = next((s for s in self._field_specs() if s.key == field_key), None)
        if spec is None:
            return
        value = self._dataclass_defaults().get(spec.key)
        set_widget_value(spec, widget, value)
        self._reset_keys[spec.key] = read_widget_value(spec, widget)
        self._form_dirty = True
        self.notify(
            f"{spec.label}: will clear this template's own value on Save.",
            severity="information",
        )

    # ── document mutation (name-keyed, like agents; unlike connectors' list) ─

    def _remove_entry_from_document(self) -> None:
        templates = self.cfg.document.get(f"{self.kind}_templates") or {}
        templates.pop(self.template_name, None)

    def _reinsert_entry_into_document(self) -> None:
        self.cfg.document.setdefault(f"{self.kind}_templates", {})[self.template_name] = self.entry

    def _install_trial_entry(self, target_entry: dict) -> None:
        self.cfg.document.setdefault(f"{self.kind}_templates", {})[self.template_name] = target_entry

    def _rollback_trial_entry(self) -> None:
        self.cfg.document.setdefault(f"{self.kind}_templates", {})[self.template_name] = self.entry

    def _referencing_watcher_labels(self) -> list[str]:
        # Not actually watchers unless self.kind == "watcher" — see
        # _delete_blocker_noun() below, which corrects the message wording.
        return [
            name
            for name, _ in find_entries_referencing_template(
                self.cfg, self.kind, self.template_name
            )
        ]

    def _delete_blocker_noun(self) -> str:
        return self.kind

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        referencing = find_entries_referencing_template(self.cfg, self.kind, self.template_name)
        used_by = ", ".join(name for name, _ in referencing) if referencing else "(none)"
        type_suffix = f"  (type: {self._connector_type()})" if self.kind == "connector" else ""
        lines = [f"[bold]{self.template_name}[/bold]{type_suffix}  ({self.kind} template)"]
        description = self.entry.get("description")
        if description:
            lines.append(f"[dim]{description}[/dim]")
        lines.append(f"used by: {used_by}")
        lines.append("")

        # Raw dump of this template's own top-level fields (mirrors
        # ConnectorDetailScreen._body_text()'s approach for the same
        # type-flexible-raw reason) — 'type'/'description' are shown in the
        # header above already. Each key's blast-radius count is scoped to
        # entries that reference THIS template (see module docstring).
        shown_any = False
        for key, value in self.entry.items():
            if key in ("type", "description"):
                continue
            shown_any = True
            inherit_count = sum(1 for _, e in referencing if key not in e)
            override_count = len(referencing) - inherit_count
            blast_text = f"[dim]({inherit_count} entries inherit, {override_count} override)[/dim]"
            if key in TOOL_LIST_WIDGET_IDS:
                # Same one-rule-per-line style AgentDetailScreen's own view
                # mode uses (format_tool_rule()), not a raw Python list dump.
                lines.append(f"{key}:  {blast_text}")
                for item in value or []:
                    lines.append(f"  {format_tool_rule(item)}")
            else:
                lines.append(f"{key}: {value}  {blast_text}")
        if not shown_any:
            lines.append("(empty — this template sets no fields yet)")

        return "\n".join(lines)

    # ── edit/create form ─────────────────────────────────────────────────────

    def _compose_form(self) -> ComposeResult:
        type_suffix = f"  (type: {self._connector_type()})" if self.kind == "connector" else ""
        with VerticalScroll(classes="entity-form", can_focus=False):
            if self.mode == "create":
                yield Static(f"[bold]New {self.kind} template[/bold]{type_suffix}")
                with Horizontal(classes="field-row"):
                    yield Static("Name", classes="field-label")
                    # Pre-filled with the name already chosen via the
                    # TextPromptModal step in OverviewScreen.action_new_entity()
                    # — still editable here (same as Agent/ConnectorDetailScreen's
                    # own Name Input), just not blank: the user already typed it
                    # once, re-typing it here would be pure friction.
                    yield Input(
                        id="field-name", value=self.template_name, placeholder="template name"
                    )
            else:
                yield Static(f"[bold]{self.template_name}[/bold]{type_suffix}  (editing)")

            with Horizontal(classes="field-row"):
                yield Static("Description", classes="field-label")
                yield Input(
                    id="field-description",
                    value=self._initial_values.get("description") or "",
                )

            if not self._field_specs():
                yield Static(
                    f"[dim]'{self.kind}' templates have no fields to configure here.[/dim]"
                )

            for spec in self._field_specs():
                yield from self._compose_field_row(spec, self.entry)

            if self.kind == "agent":
                referencing = find_entries_referencing_template(
                    self.cfg, self.kind, self.template_name
                )
                for key, label in (
                    ("owner_allowed_tools", "Owner allowed tools"),
                    ("guest_allowed_tools", "Guest allowed tools"),
                ):
                    inherit_count = sum(1 for _, e in referencing if key not in e)
                    override_count = len(referencing) - inherit_count
                    yield Static(
                        f"[bold]{label}[/bold]  "
                        f"[dim]({inherit_count} inherit, {override_count} override)[/dim]"
                    )
                    yield from self._compose_tool_list_widget(key)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._dispatch_tool_list_button(event.button.id or "")

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

        name = self.template_name
        if self.mode == "create":
            name = self.query_one("#field-name", Input).value.strip()
            if not name:
                await self.app.push_screen_wait(
                    MessageModal("Name is required.", title="Could not save")
                )
                return
            if name in self.cfg.templates(self.kind):
                await self.app.push_screen_wait(
                    MessageModal(
                        f"A {self.kind} template named '{name}' already exists.",
                        title="Could not save",
                    )
                )
                return

        target_entry = dict(self.entry)
        for key, value in updates.items():
            apply_update(target_entry, key, value)

        # Tool lists (owner_allowed_tools/guest_allowed_tools, agent
        # templates only) live outside the FieldSpec/apply_update()
        # pipeline (see tool_list_editor.py) — diffed directly against the
        # snapshot _tool_list_state() took when the form opened.
        self._collect_tool_list_updates(target_entry)
        changed_tool_list_keys = [
            key for key in TOOL_LIST_WIDGET_IDS
            if self._tool_lists[key] != self._tool_lists_initial[key]
        ]

        # Blast-radius confirm (the actual regression test this whole
        # redesign exists for): scoped to entries that inherit THIS specific
        # template (find_entries_referencing_template), not "every entry in
        # the config" the way the old DefaultsScreen computed it. A key that
        # changes but affects nobody (every referencing entry already
        # overrides it, or nothing references this template at all — always
        # true in create mode) needs no confirmation.
        referencing = find_entries_referencing_template(self.cfg, self.kind, self.template_name)
        affected: dict[str, list[str]] = {}
        for key in (*updates, *changed_tool_list_keys):
            if key == "description":
                continue
            # PR review finding: `key` can be a dotted FieldSpec.key
            # (e.g. "permissions.timeout", "agent_chain.max_turns") for a
            # one-level-nested field, but a referencing entry's raw dict
            # never has a literal top-level key equal to that dotted
            # string — only `top_key` (the nested group itself). Without
            # this split, `key not in e` was unconditionally True for every
            # nested field, so every referencing entry was listed as
            # "affected" even when it already overrides the whole nested
            # group — matching `_compose_field_row()`'s own
            # `top_key = spec.key.split(".", 1)[0]` a few lines below, which
            # got this right. (Tool-list keys are already top-level, so the
            # split is a no-op for them — same expression covers both.)
            top_key = key.split(".", 1)[0]
            names = [n for n, e in referencing if top_key not in e]
            if names:
                affected[key] = names

        if affected:
            lines = [f"{key}: {', '.join(names)}" for key, names in affected.items()]
            confirmed = await self.app.push_screen_wait(
                ConfirmModal(
                    "This changes the EFFECTIVE value for —\n" + "\n".join(lines) + "\n\nContinue?",
                    confirm_label="Save",
                )
            )
            if not confirmed:
                return

        if self.mode == "create":
            self.cfg.document.setdefault(f"{self.kind}_templates", {})[name] = target_entry
        else:
            self._install_trial_entry(target_entry)
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create":
                templates = self.cfg.document.get(f"{self.kind}_templates") or {}
                templates.pop(name, None)
            else:
                self._rollback_trial_entry()
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return

        self.entry = target_entry
        self.template_name = name
        self.app.pop_screen()
        app = self.app
        app.notify(f"Saved {self.kind} template '{name}'.", severity="information")
        app.reload_config()  # type: ignore[attr-defined]
