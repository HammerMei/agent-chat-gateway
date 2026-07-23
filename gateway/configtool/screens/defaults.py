"""DefaultsScreen — view and edit one `*_defaults:` block.

Shows the block's own contents plus how many connector/agent/watcher
entries currently inherit vs. override each of its keys ("blast radius") —
per docs/design/config-tool.md decision 2, editing a shared default must
show this BEFORE commit: action_save() below computes, for every key that's
about to change, which entries would see their EFFECTIVE value actually
change as a result, and blocks the write behind a ConfirmModal listing them
if that list is non-empty. A changed key that happens to affect nobody
(every entry already overrides it, or it's brand new and nothing existed
before) needs no confirmation at all.

Editable for agent_defaults and watcher_defaults only. connector_defaults
stays view-only: which fields are actually safe to share across every
connector TYPE (reply_in_thread/permission_reply_in_thread and friends,
almost certainly NOT `server:` — a rocketchat and a mattermost connector
have no business sharing one server URL) is a real, separate design
question that deserves its own pass, not a subset rushed into this one.
check_action() hides 'e' entirely when there is nothing editable for the
active kind (_field_specs() returns empty).

agent_defaults reuses AgentDetailScreen's own AGENT_FORM_FIELDS /
AGENT_DATACLASS_DEFAULTS verbatim — zero drift risk, the same reasoning
that module documents for editing a single agent entry applies identically
to editing the shared block every agent entry merges against (every key in
that list is legal in agent_defaults too — gateway/config.py's forbidden-
keys set for agent_defaults is empty). watcher_defaults gets its own small
field list below: gateway/config.py forbids {name, room, rooms,
session_id} there, since each of those pins one SPECIFIC watcher's
identity and has no business in a block every watcher merges against.

This does NOT extend FormScreen: a `*_defaults:` block has no "entity" to
create or delete (an absent block is just an empty block —
`_extract_defaults_block` returns `{}` rather than raising), and its own
fields have no provenance concept (a defaults block does not itself
inherit from anywhere — decision 2's "explicit / inherited /
explicit-suppressing" only applies to an ENTRY relative to its defaults
block). FormScreen's machinery is built around exactly those two things;
reusing it here would mean stretching its vocabulary over a shape that
doesn't fit. The free helper functions in form_common.py (`FieldSpec`,
`read_widget_value`/`set_widget_value`/`apply_update`/`widget_id`/
`get_nested`/`list_to_text`) are reused directly — only the field-row
rendering (which needs blast-radius counts, not FormScreen's explicit/
inherited provenance marker) is new. The shared `.entity-form`/`.field-row`
CSS layout lives on `DetailScreen` (base.py), the nearest common ancestor,
so this screen gets the identical layout without duplicating it.
"""

from __future__ import annotations

from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Checkbox, Footer, Header, Input, Select, Static

from ..formatting import format_value
from ..modals import ConfirmModal, MessageModal
from ..model import EditableConfig
from .agent_detail import AGENT_DATACLASS_DEFAULTS, AGENT_FORM_FIELDS
from .base import DetailScreen
from .form_common import (
    FieldSpec,
    apply_update,
    get_nested,
    list_to_text,
    read_widget_value,
    set_widget_value,
    widget_id,
)

_ENTRY_ACCESSOR = {
    "connector_defaults": lambda cfg: cfg.connectors_raw,
    "agent_defaults": lambda cfg: list(cfg.agents_raw.values()),
    "watcher_defaults": lambda cfg: cfg.watchers_raw,
}

# watcher_defaults' own field list — see module docstring for why these
# specific keys (gateway/config.py's forbidden set for this block is
# {name, room, rooms, session_id}).
WATCHER_DEFAULTS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("online_notification", "str", "Online notification"),
    FieldSpec("offline_notification", "str", "Offline notification"),
    FieldSpec("context_inject_files", "list", "Context inject files (comma-separated)"),
    FieldSpec("history_handoff.enabled", "bool", "History handoff enabled"),
    FieldSpec("history_handoff.fetch_count", "int", "History handoff fetch count"),
    FieldSpec("history_handoff.verbatim_tail", "int", "History handoff verbatim tail"),
)
# Mirrors gateway/config.py's OWN `.get(key, X)` calls at the watcher-
# parsing site (NOT HistoryHandoffConfig's dataclass field defaults, which
# have already drifted from them once — the dataclass itself defaults
# history_handoff.enabled to True, but the loader's own
# `hh_raw.get("enabled", False)` actually applies False whenever the key is
# absent). Matching the LOADER, not the dataclass, is what makes this form
# an honest "what would this evaluate to right now" preview.
WATCHER_DEFAULTS_DATACLASS_DEFAULTS: dict[str, object] = {
    "online_notification": None,
    "offline_notification": None,
    "context_inject_files": [],
    "history_handoff.enabled": False,
    "history_handoff.fetch_count": 50,
    "history_handoff.verbatim_tail": 15,
}

# kind -> (field specs, "no shared default set" fallback values). Absent
# from this dict (connector_defaults) means _field_specs() returns ()  —
# nothing to edit, check_action() hides 'e'.
_EDITABLE_KINDS: dict[str, tuple[tuple[FieldSpec, ...], dict[str, object]]] = {
    "agent_defaults": (AGENT_FORM_FIELDS, AGENT_DATACLASS_DEFAULTS),
    "watcher_defaults": (WATCHER_DEFAULTS_FIELDS, WATCHER_DEFAULTS_DATACLASS_DEFAULTS),
}


def _labeled_entries(cfg: EditableConfig, kind: str) -> list[tuple[str, dict]]:
    """Like _ENTRY_ACCESSOR, but (label, raw_entry) pairs — used only by the
    edit flow's blast-radius CONFIRM dialog, which needs to NAME affected
    entries, not just count them. Labels are best-effort, human-readable
    identifiers (not the real resolved watcher name — `_auto_watcher_name()`
    needs connector resolution this has no reason to duplicate here); good
    enough for "here's roughly what this affects," never used for lookup."""
    if kind == "connector_defaults":
        return [(e.get("name") or "?", e) for e in cfg.connectors_raw]
    if kind == "agent_defaults":
        return list(cfg.agents_raw.items())
    if kind == "watcher_defaults":
        return [
            (w.get("name") or f"{w.get('connector', '?')}/{w.get('room') or w.get('rooms')}", w)
            for w in cfg.watchers_raw
        ]
    return []


class DefaultsScreen(DetailScreen):
    BODY_ID = "defaults-detail-body"

    BINDINGS = [
        Binding("e", "edit", "Edit", show=True),
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("ctrl+r", "reset_field", "Clear field", show=True),
        Binding("tab", "app.focus_next", "Next field", show=True),
    ]

    def __init__(self, cfg: EditableConfig, kind: str, mode: Literal["view", "edit"] = "view"):
        super().__init__()
        self.cfg = cfg
        self.kind = kind
        self.mode = mode
        self._initial_values: dict[str, object] = {}
        self._reset_keys: dict[str, object] = {}
        self._form_dirty = False
        self._last_field_error: str | None = None
        self._populating = False

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        return _EDITABLE_KINDS.get(self.kind, ((), {}))[0]

    def _dataclass_defaults(self) -> dict[str, object]:
        return _EDITABLE_KINDS.get(self.kind, ((), {}))[1]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "edit":
            return self.mode == "view" and bool(self._field_specs())
        if action in ("save", "reset_field"):
            return self.mode != "view"
        return True

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
        entries = _ENTRY_ACCESSOR[self.kind](self.cfg)
        lines = [f"[bold]{self.kind}[/bold]  ({len(entries)} entries)"]

        raw_block = self.cfg.document.get(self.kind)
        description = raw_block.get("description") if isinstance(raw_block, dict) else None
        if description:
            lines.append(f"[dim]{description}[/dim]")
        lines.append("")

        try:
            block = self.cfg.defaults_block(self.kind)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(f"[red]Could not read this block: {exc}[/red]")
            return "\n".join(lines)

        if not block:
            lines.append("(empty — no shared defaults set)")
            return "\n".join(lines)

        for key, value in block.items():
            inherit_count = sum(1 for e in entries if key not in e)
            override_count = len(entries) - inherit_count
            lines.append(
                f"{key}: {format_value(value)}  "
                f"[dim]({inherit_count} entries inherit, {override_count} override)[/dim]"
            )

        return "\n".join(lines)

    # ── edit mode ────────────────────────────────────────────────────────────

    def _compute_initial_values(self) -> None:
        self._reset_keys = {}
        block = self.cfg.document.get(self.kind) or {}
        dataclass_defaults = self._dataclass_defaults()
        for spec in self._field_specs():
            value = get_nested(block, spec.key)
            if value is None:
                value = dataclass_defaults.get(spec.key)
            # read_widget_value() normalizes an untouched "str"/"list"-kind
            # box that renders as EMPTY TEXT back to None — text_to_list("")
            # or None for list, text.strip() or None for str — regardless of
            # whether the original value was None, "", or []. Without this
            # same normalization here, a field whose current effective value
            # is "" or [] (explicit, or absent and defaulting to one of
            # those) would store that falsy-but-not-None value as its
            # "initial" value, compare unequal to the widget's real
            # untouched readback of None, and look spuriously "changed" on
            # every Save even though the user never touched it —
            # false-positive blast-radius confirms for a field nobody
            # actually edited. "int" is deliberately excluded: an explicit
            # `0` renders as the text "0" (non-empty), which reads back as
            # the int `0` again — no mismatch there, and normalizing it away
            # would introduce the exact same false positive in reverse.
            if spec.kind in ("str", "list") and value is not None and not value:
                value = None
            self._initial_values[spec.key] = value
        self._initial_values["description"] = block.get("description")

    def _compose_form(self) -> ComposeResult:
        with VerticalScroll(classes="entity-form", can_focus=False):
            yield Static(f"[bold]{self.kind}[/bold]  (editing)")
            with Horizontal(classes="field-row"):
                yield Static("Description", classes="field-label")
                yield Input(
                    id="field-description",
                    value=self._initial_values.get("description") or "",
                )
            for spec in self._field_specs():
                yield from self._compose_field_row(spec)

    def _compose_field_row(self, spec: FieldSpec) -> ComposeResult:
        initial = self._initial_values.get(spec.key)
        entries = _ENTRY_ACCESSOR[self.kind](self.cfg)
        top_key = spec.key.split(".", 1)[0]
        inherit_count = sum(1 for e in entries if top_key not in e)
        blast_text = (
            f"[dim]({inherit_count} inherit, {len(entries) - inherit_count} override)[/dim]"
        )
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
                widget = Input(value="" if initial is None else str(initial), id=widget_id(spec.key))
            widget.field_key = spec.key
            yield widget
            yield Static(blast_text, classes="field-provenance")

    def on_input_changed(self, event: Input.Changed) -> None:
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

    # ── navigation ───────────────────────────────────────────────────────────

    async def action_edit(self) -> None:
        if self.mode != "view" or not self._field_specs():
            return
        self.mode = "edit"
        self._form_dirty = False
        self._compute_initial_values()
        self._populating = True
        await self.recompose()
        self.call_after_refresh(self._stop_populating)
        self.refresh_bindings()

    @work
    async def action_back(self) -> None:
        if self.mode == "view":
            self.app.pop_screen()
            return
        if self._form_dirty:
            discard = await self.app.push_screen_wait(
                ConfirmModal(
                    f"Discard unsaved changes to {self.kind}?",
                    confirm_label="Discard",
                )
            )
            if not discard:
                return
        self.mode = "view"
        self._form_dirty = False
        await self.recompose()
        self.refresh_bindings()

    def action_reset_field(self) -> None:
        """ctrl+r: clear the FOCUSED field back to "no shared default set"
        (pop it from the block on Save) — the equivalent, for a *_defaults
        block itself, of FormScreen's own ctrl+r "revert to inherited": a
        defaults block has no further parent to revert TO, so the target
        state here is simply absent. A no-op if focus isn't on a resettable
        field (the Description Input isn't tagged with `field_key`)."""
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
        self.notify(f"{spec.label}: will clear this shared default on Save.", severity="information")

    # ── save ─────────────────────────────────────────────────────────────────

    def _collect_updates(self) -> dict[str, object] | None:
        self._last_field_error = None
        updates: dict[str, object] = {}
        for spec in self._field_specs():
            widget = self.query_one("#" + widget_id(spec.key))
            try:
                new_value = read_widget_value(spec, widget)
            except ValueError as exc:
                self._last_field_error = str(exc)
                return None
            if spec.key in self._reset_keys and new_value == self._reset_keys[spec.key]:
                updates[spec.key] = None
                continue
            if new_value != self._initial_values.get(spec.key):
                updates[spec.key] = new_value

        desc_widget = self.query_one("#field-description", Input)
        new_desc = desc_widget.value.strip() or None
        if new_desc != self._initial_values.get("description"):
            updates["description"] = new_desc
        return updates

    @work
    async def action_save(self) -> None:
        if self.mode != "edit":
            return

        updates = self._collect_updates()
        if updates is None:
            await self.app.push_screen_wait(
                MessageModal(self._last_field_error or "Invalid field.", title="Could not save")
            )
            return
        if not updates:
            self.mode = "view"
            await self.recompose()
            self.refresh_bindings()
            return

        # Blast-radius confirm (docs/design/config-tool.md decision 2): a
        # changed/removed key only needs confirming if it actually affects
        # an entry that doesn't already override it — "description" is
        # informational only and is never merged into any entry, so it's
        # excluded here regardless of how many entries exist.
        entries = _labeled_entries(self.cfg, self.kind)
        affected: dict[str, list[str]] = {}
        for key in updates:
            if key == "description":
                continue
            names = [name for name, e in entries if key not in e]
            if names:
                affected[key] = names

        if affected:
            lines = [f"{key}: {', '.join(names)}" for key, names in affected.items()]
            confirmed = await self.app.push_screen_wait(
                ConfirmModal(
                    "This changes the EFFECTIVE value for —\n" + "\n".join(lines) +
                    "\n\nContinue?",
                    confirm_label="Save",
                )
            )
            if not confirmed:
                return

        original_block = self.cfg.document.get(self.kind) or {}
        target_block = dict(original_block)
        for key, value in updates.items():
            apply_update(target_block, key, value)

        had_block = self.kind in self.cfg.document
        if target_block:
            self.cfg.document[self.kind] = target_block
        else:
            self.cfg.document.pop(self.kind, None)
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if had_block:
                self.cfg.document[self.kind] = original_block
            else:
                self.cfg.document.pop(self.kind, None)
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return

        self.app.pop_screen()
        app = self.app
        app.notify(f"Saved {self.kind}.", severity="information")
        app.reload_config()
