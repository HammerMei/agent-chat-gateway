"""Shared machinery for the config TUI's entity edit/create forms.

`AgentDetailScreen` was the first (Phase 2); `ConnectorDetailScreen` is the
second, and `WatcherDetailScreen` (Phase 3) will be the third. Extracted
here once a second concrete user existed, rather than guessed at up front —
code review item 10 already flagged the cost of letting screens duplicate
this kind of machinery independently.

Implements docs/design/config-tool.md decision 2 ("editing an inherited
field always writes an explicit per-entry override"): nothing is written to
`EditableConfig.document` until Save. Every field is snapshotted when the
form opens (the effective/merged value — an inherited field displays its
real current value, not blank) and diffed against the widget's value at
Save time; only fields that actually changed get written. Clearing a field
back to blank reverts it to inherited (pops the key) rather than writing an
explicit null — see `apply_update()`.

A subclass provides:
  - `_field_specs() -> tuple[FieldSpec, ...]` — which fields this form shows
    right now (may depend on entity-specific state, e.g. connector `type`).
  - `_defaults_kind() -> str` — the `*_defaults` block this entry merges
    against (`"agent_defaults"` / `"connector_defaults"` / ...).
  - `_dataclass_defaults() -> dict[str, object]` — the true effective value
    for a field set by neither the entry nor its `*_defaults` block (a form
    needs to show what a field would actually evaluate to; view mode gets
    away with just omitting the line).
  - `_compose_form() -> ComposeResult` — the form body (typically a
    `VerticalScroll` wrapping `_compose_field_row()` calls plus whatever
    entity-specific chrome — a name Input for create mode, etc.).
  - `action_save()` — entity-specific: where a new entry gets inserted
    (`document["agents"][name]` is a dict keyed by name; `document["connectors"]`
    is a list where each entry carries its own `name` field) differs enough
    that forcing a shared implementation would be more awkward than it's
    worth. Call `self._collect_field_updates()` for the generic diff, then
    apply/insert/save however this entity needs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Checkbox, Footer, Header, Input, Select, Static

from ..formatting import provenance_label
from ..modals import ConfirmModal, MessageModal
from ..model import EditableConfig, Provenance
from .base import DetailScreen


@dataclass(frozen=True)
class FieldSpec:
    key: str  # dotted for a one-level-nested sub-field, e.g. "server.password"
    kind: Literal["str", "int", "bool", "list", "enum"]
    label: str
    options: tuple[str, ...] | None = None
    secret: bool = False  # mask the widget's display (Input(password=True))


def widget_id(key: str) -> str:
    return "field-" + key.replace(".", "-")


def get_nested(d: dict, dotted_key: str) -> object:
    if "." not in dotted_key:
        return d.get(dotted_key)
    parent_key, sub_key = dotted_key.split(".", 1)
    parent = d.get(parent_key)
    return parent.get(sub_key) if isinstance(parent, dict) else None


def list_to_text(value: object) -> str:
    if not value:
        return ""
    return ", ".join(str(v) for v in value)


def text_to_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def apply_update(entry: dict, dotted_key: str, value: object) -> None:
    """Write `value` (or clear, if None) into `entry` at `dotted_key`.

    A top-level key: set it, or pop it entirely if `value` is None (revert
    to inherited/default). A one-level-nested key (`"server.password"`):
    read-modify-write that sub-dict, dropping it entirely once its last
    explicit sub-key is cleared, so a fully-cleared sub-dict reverts to
    inheriting from the matching `*_defaults` block again, not an empty
    `{}` stub sitting in the entry forever.
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


def read_widget_value(spec: FieldSpec, widget: object) -> object:
    if spec.kind == "bool":
        return widget.value
    if spec.kind == "enum":
        return widget.value
    if spec.kind == "list":
        return text_to_list(widget.value) or None
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


def find_referencing_watcher_labels(
    cfg: EditableConfig, *, connector_name: str | None = None, agent_name: str | None = None
) -> list[str]:
    """Which watchers currently reference the given connector and/or agent
    name — checked against the MERGED value (`watcher_defaults` can set
    `connector`/`agent` too; both are allowed there, unlike `name`/`room`/
    `rooms`/`session_id`), so a watcher that only inherits its connector/agent
    from `watcher_defaults` still counts. Used to give a clear pre-delete
    warning instead of the generic validator error `save()` would otherwise
    surface after the fact.
    """
    labels = []
    for entry in cfg.watchers_raw:
        try:
            merged = cfg.merged_entry("watcher_defaults", entry)
        except (ValueError, FileNotFoundError):
            merged = entry
        if connector_name is not None and merged.get("connector") != connector_name:
            continue
        if agent_name is not None and merged.get("agent") != agent_name:
            continue
        label = entry.get("name")
        if not label:
            rooms = entry.get("rooms")
            label = ", ".join(rooms) if rooms else entry.get("room", "?")
        labels.append(label)
    return labels


class FormScreen(DetailScreen):
    """Base for the config TUI's view/edit/create entity screens. See
    module docstring for the subclass contract."""

    BINDINGS = [
        Binding("e", "edit", "Edit", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("ctrl+s", "save", "Save", show=True),
        # Screen already binds tab/shift+tab to app.focus_next/focus_previous
        # with show=False (textual/screen.py) — re-bound here with show=True
        # (same pattern OverviewScreen uses for its own tab hint) so the
        # footer tells the user how to move between fields. An Up/Down
        # alternative was tried on AgentDetailScreen and reverted after
        # real-terminal testing showed it was unreliable in a way Pilot's
        # headless driver didn't catch — Tab is the one mechanism proven to
        # actually work everywhere.
        Binding("tab", "app.focus_next", "Next field", show=True),
    ]

    DEFAULT_CSS = """
    FormScreen .entity-form {
        padding: 1 2;
    }
    FormScreen .field-row {
        height: auto;
        margin-bottom: 1;
    }
    FormScreen .field-label {
        width: 30;
        padding-top: 1;
    }
    FormScreen .field-provenance {
        padding-top: 1;
        margin-left: 2;
    }
    FormScreen Checkbox {
        width: auto;
    }
    """

    def __init__(self):
        super().__init__()
        self.mode: Literal["view", "edit", "create"] = "view"
        self._form_dirty = False
        self._initial_values: dict[str, object] = {}
        self._last_field_error: str | None = None
        # Input/Select fire their own Changed message once at initial mount
        # with whatever value the constructor was given (confirmed
        # empirically — Checkbox does not, but Input/Select do). Without this
        # guard, simply OPENING the edit form would immediately mark it
        # dirty, incorrectly prompting a discard-confirmation on Escape even
        # though the user never touched anything. Cleared via
        # call_after_refresh, which runs after that initial burst of Changed
        # messages has already been processed.
        self._populating = False

    def on_mount(self) -> None:
        if self._populating:
            self.call_after_refresh(self._stop_populating)

    def _stop_populating(self) -> None:
        self._populating = False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide 'Edit'/'Delete' from the footer once already editing/creating
        (both are no-ops there; a footer hint for a no-op key reads as
        broken, not just redundant), and hide 'Save' while still in view
        mode (nothing to save yet)."""
        if action in ("edit", "delete"):
            return self.mode == "view"
        if action == "save":
            return self.mode != "view"
        return True

    # ── abstract hooks subclasses must implement ────────────────────────────

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        raise NotImplementedError

    def _defaults_kind(self) -> str:
        raise NotImplementedError

    def _dataclass_defaults(self) -> dict[str, object]:
        raise NotImplementedError

    def _compose_form(self) -> ComposeResult:
        raise NotImplementedError

    def _entity_label(self) -> str:
        """Used in the delete-confirmation message and the post-delete
        notification (e.g. an agent/connector's name)."""
        raise NotImplementedError

    def _remove_entry_from_document(self) -> None:
        """Delete this entity's raw entry from `self.cfg.document` in place.
        Must record whatever this subclass needs (e.g. the entry's index in
        a list) to support `_reinsert_entry_into_document()` undoing it."""
        raise NotImplementedError

    def _reinsert_entry_into_document(self) -> None:
        """Undo `_remove_entry_from_document()` — called when `save()`
        rejects the deletion (e.g. a watcher still references this entity)."""
        raise NotImplementedError

    def _referencing_watcher_labels(self) -> list[str]:
        """Which watchers (if any) currently reference this entity — checked
        BEFORE the destructive confirm, so a blocked delete gets a clear
        reason instead of the generic validator error `save()` would
        otherwise surface. Subclasses call `find_referencing_watcher_labels()`
        with their own kind of name."""
        raise NotImplementedError

    async def action_save(self) -> None:
        raise NotImplementedError

    # ── compose dispatch ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        if self.mode == "view":
            yield VerticalScroll(Static(self._body_text(), id=self.BODY_ID))
        else:
            yield from self._compose_form()
        yield Footer()

    # ── generic field snapshot / provenance / row rendering ─────────────────

    def _compute_initial_values(self, entry: dict) -> None:
        try:
            merged = self.cfg.merged_entry(self._defaults_kind(), entry)
        except (ValueError, FileNotFoundError):
            merged = dict(entry)
        dataclass_defaults = self._dataclass_defaults()
        for spec in self._field_specs():
            value = get_nested(merged, spec.key)
            if value is None:
                value = dataclass_defaults.get(spec.key)
            self._initial_values[spec.key] = value
        self._initial_values["description"] = entry.get("description")

    def _field_provenance(self, spec: FieldSpec, entry: dict) -> Provenance | None:
        top_key = spec.key.split(".", 1)[0]
        try:
            return self.cfg.field_provenance(self._defaults_kind(), entry, top_key)
        except (ValueError, FileNotFoundError):
            return None

    def _compose_field_row(self, spec: FieldSpec, entry: dict) -> ComposeResult:
        provenance = self._field_provenance(spec, entry)
        prov_text = f"[dim]({provenance_label(provenance)})[/dim]" if provenance else ""
        initial = self._initial_values.get(spec.key)
        with Horizontal(classes="field-row"):
            yield Static(spec.label, classes="field-label")
            if spec.kind == "bool":
                yield Checkbox(value=bool(initial), id=widget_id(spec.key))
            elif spec.kind == "enum":
                options = spec.options or ()
                yield Select(
                    [(o, o) for o in options],
                    value=initial if initial in options else (options or (None,))[0],
                    allow_blank=False,
                    id=widget_id(spec.key),
                )
            elif spec.kind == "list":
                yield Input(value=list_to_text(initial), id=widget_id(spec.key))
            else:
                yield Input(
                    value="" if initial is None else str(initial),
                    id=widget_id(spec.key),
                    password=spec.secret,
                )
            yield Static(prov_text, classes="field-provenance")

    # ── dirty tracking (per-screen, not EditableConfig.dirty — nothing is
    # written to `document` until Save, so cfg.dirty stays False the whole
    # time the user is mid-form; this local flag is what Escape checks) ──────

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
        if self.mode != "view":
            return
        self.mode = "edit"
        self._form_dirty = False
        self._on_enter_edit_mode()
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
        # False forever and it renders as a blank bar (confirmed empirically
        # while building AgentDetailScreen: 4 FooterKey children at first
        # mount, 0 after this recompose, permanently, across every later
        # transition too). refresh_bindings() is Screen's own public method
        # for exactly this: it re-publishes the signal so every current
        # subscriber (including the new Footer) recomputes — and also
        # re-evaluates check_action() for every binding, so the 'e'/'ctrl+s'
        # visibility flips immediately on this same recompose.
        self.refresh_bindings()

    def _on_enter_edit_mode(self) -> None:
        """Hook for subclass-specific setup right before the edit-mode
        recompose (AgentDetailScreen recomputes _initial_values here)."""

    @work
    async def action_back(self) -> None:
        if self.mode == "view":
            self.app.pop_screen()
            return
        if self._form_dirty:
            discard = await self.app.push_screen_wait(
                ConfirmModal(
                    f"Discard unsaved changes to this {self._entity_noun()}?",
                    confirm_label="Discard",
                )
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

    def _entity_noun(self) -> str:
        """Used in the discard- and delete-confirmation messages ("... to
        this agent?" / "... to this connector?")."""
        return "entry"

    @work
    async def action_delete(self) -> None:
        """'d', view mode only (see check_action). Checks for referencing
        watchers FIRST (a clear, specific reason beats a generic validator
        error) — if any exist, shows that reason and stops before even
        offering the destructive confirm. Otherwise: confirm -> remove from
        `document` -> save(). save() remains the backstop even after the
        pre-check (belt-and-suspenders, not a replacement for it) — if it
        still rejects the deletion for some reason the pre-check didn't
        anticipate, the entry is reinserted so a rejected delete never
        leaves `document` silently missing something that's still on disk.
        """
        if self.mode != "view":
            return

        blockers = self._referencing_watcher_labels()
        if blockers:
            await self.app.push_screen_wait(
                MessageModal(
                    f"Cannot delete {self._entity_noun()} "
                    f"'{self._entity_label()}' — still used by watcher(s): "
                    f"{', '.join(blockers)}.",
                    title="Cannot delete",
                )
            )
            return

        confirmed = await self.app.push_screen_wait(
            ConfirmModal(
                f"Delete {self._entity_noun()} '{self._entity_label()}'? "
                "This cannot be undone.",
                confirm_label="Delete",
            )
        )
        if not confirmed:
            return

        self._remove_entry_from_document()
        self.cfg.mark_dirty()
        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            self._reinsert_entry_into_document()
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not delete"))
            return

        self.app.pop_screen()
        app = self.app
        app.notify(f"Deleted {self._entity_noun()} '{self._entity_label()}'.", severity="information")
        app.reload_config()  # type: ignore[attr-defined]

    # ── generic diff collection (Save calls this, then applies the result
    # however this entity needs to — see module docstring) ──────────────────

    def _collect_field_updates(self) -> dict[str, object] | None:
        """Diff every form widget against `_initial_values`. Returns
        {dotted_key: new_value_or_None} for changed fields only (None means
        "clear it — revert to inherited/default"), or None if a field fails
        to parse — the message is stashed in `self._last_field_error` rather
        than shown directly (this method is sync; the caller, action_save(),
        is the one in a position to `await` a `MessageModal`)."""
        self._last_field_error: str | None = None
        updates: dict[str, object] = {}
        for spec in self._field_specs():
            widget = self.query_one("#" + widget_id(spec.key))
            try:
                new_value = read_widget_value(spec, widget)
            except ValueError as exc:
                self._last_field_error = str(exc)
                return None
            if new_value != self._initial_values.get(spec.key):
                updates[spec.key] = new_value

        desc_widget = self.query_one("#field-description", Input)
        new_desc = desc_widget.value.strip() or None
        if new_desc != self._initial_values.get("description"):
            updates["description"] = new_desc
        return updates
