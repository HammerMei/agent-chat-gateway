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
    # Masks the widget's display (Input(password=True)). docs/design/
    # config-tool.md decision 6, final revision: secrets are stored
    # directly in config.yaml (chmod 0600) and $VAR/${VAR} is never
    # resolved by anything but the one-time migration
    # (gateway/config_migrate.py) — by the time this screen opens, a
    # pre-existing .env-backed config has already been migrated (the TUI
    # launch path triggers it, same as `agent-chat-gateway start`), so a
    # secret field's value is always its real, literal value here.
    secret: bool = False


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


def set_widget_value(spec: FieldSpec, widget: object, value: object) -> None:
    """Set `widget`'s displayed value for `spec` — the inverse of
    `read_widget_value()`. Used by `action_reset_field()` to show what a
    field would display with zero explicit override (pure `*_defaults` /
    dataclass fallback), on an ALREADY-MOUNTED, focused widget — unlike
    `_compose_field_row()`, which sets the initial value via constructor
    kwargs before the widget ever mounts."""
    if spec.kind == "bool":
        widget.value = bool(value)
    elif spec.kind == "enum":
        options = spec.options or ()
        widget.value = value if value in options else (options or (None,))[0]
    elif spec.kind == "list":
        widget.value = list_to_text(value)
    else:
        widget.value = "" if value is None else str(value)


def find_referencing_watcher_labels(
    cfg: EditableConfig, *, connector_name: str | None = None, agent_name: str | None = None
) -> list[str]:
    """Which EXPANDED watchers currently reference the given connector and/or
    agent name — one label per real watcher, using `expanded_watchers()`
    (the same real loader that names them everywhere else in the TUI, e.g.
    the Overview's Watchers tab) rather than re-deriving names from the raw
    entry. Two things this gets right that a raw-entry-only approach
    wouldn't: (1) an unnamed watcher's real name is `_auto_watcher_name()`'s
    `"<connector>-<room>"` (gateway/config.py), not the bare room string;
    (2) a `rooms: [a, b]` group is N separate real watchers with N separate
    names, not one joined "a, b" label. If the config doesn't currently
    load at all, returns [] — `save()`'s own validation remains the backstop
    for whatever's actually broken; a delete pre-check has nothing useful to
    say about referencing watchers in a config that doesn't parse.
    """
    try:
        expanded = cfg.expanded_watchers()
    except (ValueError, FileNotFoundError):
        return []
    labels = []
    for ew in expanded:
        w = ew.watcher
        if connector_name is not None and w.connector != connector_name:
            continue
        if agent_name is not None and w.agent != agent_name:
            continue
        labels.append(w.name)
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
        # User-reported gap: str/int/list fields can revert an explicit
        # override back to inherited by clearing the box to blank, but a
        # Checkbox/Select has no "blank" state — once touched, a bool/enum
        # field stayed explicit forever, even set back to the same value
        # the default already has. ctrl+r (not a plain letter — those get
        # swallowed by whichever Input has focus, same reason Up/Down
        # navigation didn't work) resets the FOCUSED field specifically,
        # regardless of kind.
        # Footer label deliberately says "to default", not just "Reset
        # field" (user-reported: the shorter wording reads as "undo to
        # whatever this field was before you started editing," which is
        # NOT what this does — see action_reset_field()'s own docstring).
        Binding("ctrl+r", "reset_field", "Reset to default", show=True),
        # User-requested (nice-to-have, not a bug): a way to check what's
        # actually in a masked secret field before saving. ctrl+t (NOT
        # ctrl+p — that's Textual's own App.COMMAND_PALETTE_BINDING, which
        # takes priority over any screen-level binding for the same key and
        # silently ate every keypress until this was caught by a failing
        # test) toggles the FOCUSED field's Input.password reactive —
        # masking is display-only (Input(password=True) never affects
        # .value), so this is purely cosmetic and doesn't touch anything
        # read_widget_value() or the diff logic sees.
        Binding("ctrl+t", "toggle_password_visibility", "Show/hide password", show=True),
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
        width: auto;
    }
    FormScreen Checkbox {
        width: auto;
    }
    /* Input's own DEFAULT_CSS is `width: 100%` — inside a Horizontal
    field-row, that claims the ENTIRE row's width, pushing every sibling
    that comes after it (the "Store in .env" Checkbox, the provenance
    marker) off past the terminal's right edge. Confirmed as the actual,
    user-reported cause of a "missing" checkbox — it was rendering, just
    off-screen; the SAME bug had already been silently hiding every
    provenance marker on every field row since Phase 2's agent form
    shipped (a dim decorative label going unnoticed off-screen is a lot
    less obvious than an interactive control). `1fr` matches Select's own
    DEFAULT_CSS (which never had this problem) — share the row's
    remaining space with fixed/auto-width siblings instead of claiming
    all of it. */
    FormScreen .field-row Input {
        width: 1fr;
    }
    """

    def __init__(self):
        super().__init__()
        self.mode: Literal["view", "edit", "create"] = "view"
        # True when this screen was pushed ALREADY in edit mode (the list
        # page's direct-edit shortcut — see OverviewScreen.action_edit_row())
        # rather than reached via view mode's own 'e' key. Consulted by
        # action_back(): a screen that skipped view mode entirely has no
        # view state to "fall back" to — Escape (or a successful/cancelled
        # delete) must pop straight back to the list, not flip to a view
        # rendering of a screen the user never asked to see.
        self._started_in_edit_mode = False
        self._form_dirty = False
        self._initial_values: dict[str, object] = {}
        self._last_field_error: str | None = None
        # Fields explicitly reset via ctrl+r (action_reset_field()), mapped
        # to the value the field was set to display AT reset time. Consulted
        # by _collect_field_updates(): if the widget's CURRENT value still
        # matches, the field is written as "clear/revert to inherited"
        # regardless of what _initial_values says — the fix for the
        # bool/enum revert-to-inherited gap (str/int/list already had this
        # via clearing the box to blank). If the widget has since changed
        # away from the reset value, normal diffing takes back over.
        self._reset_keys: dict[str, object] = {}
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
        if action in ("save", "reset_field", "toggle_password_visibility"):
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

    def _install_trial_entry(self, target_entry: dict) -> None:
        """EDIT mode only: temporarily substitute `target_entry` (a COPY of
        `self.entry` with this Save's updates already applied) into
        `self.cfg.document`, in place of the original. This runs BEFORE
        `save()` — never mutate `self.entry` itself here, or a rejected
        save leaves invalid data sitting in the document even though
        nothing was ever written to disk (a real bug: user-reported that
        setting an invalid value, having Save fail, then pressing Back
        still showed the invalid value — because the old code mutated
        the SAME dict object `document` already held). Call
        `_rollback_trial_entry()` if `save()` rejects it."""
        raise NotImplementedError

    def _rollback_trial_entry(self) -> None:
        """Undo `_install_trial_entry()` — restore the ORIGINAL `self.entry`
        (untouched) into `document`. Called when `save()` rejects the trial."""
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
        self._reset_keys = {}  # fresh edit session — no lingering reset markers
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
            # Tagged so a focused widget can be mapped back to its FieldSpec
            # (action_reset_field()) without unmunging widget_id()'s
            # dot-to-dash id transform, which would be ambiguous for any
            # future field key containing a literal dash.
            widget.field_key = spec.key
            yield widget
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

    def action_reset_field(self) -> None:
        """ctrl+r: reset the FOCUSED field to its pure-defaults value (no
        explicit override) — see the `_reset_keys` field comment for how
        this becomes an actual "revert to inherited" on Save, regardless of
        field kind. A no-op if focus isn't on a resettable field (e.g. the
        Name/Description inputs, which aren't tagged with `field_key` — see
        `_compose_field_row()` — since neither has a `*_defaults` concept)."""
        widget = self.focused
        field_key = getattr(widget, "field_key", None)
        if field_key is None:
            return
        spec = next((s for s in self._field_specs() if s.key == field_key), None)
        if spec is None:
            return

        try:
            defaults_only = self.cfg.merged_entry(self._defaults_kind(), {})
        except (ValueError, FileNotFoundError):
            defaults_only = {}
        value = get_nested(defaults_only, spec.key)
        if value is None:
            value = self._dataclass_defaults().get(spec.key)

        set_widget_value(spec, widget, value)
        self._reset_keys[spec.key] = read_widget_value(spec, widget)
        self._form_dirty = True
        self.notify(f"{spec.label}: will revert to inherited on Save.", severity="information")

    def action_toggle_password_visibility(self) -> None:
        """ctrl+t: reveal/re-mask the FOCUSED secret field. A no-op if focus
        isn't on a masked Input (Input.password is always False for a
        non-secret field, so toggling it there would be a silent, confusing
        no-visible-effect action — checked explicitly via the field's own
        FieldSpec.secret, not just "is this an Input")."""
        widget = self.focused
        field_key = getattr(widget, "field_key", None)
        if field_key is None or not isinstance(widget, Input):
            return
        spec = next((s for s in self._field_specs() if s.key == field_key), None)
        if spec is None or not spec.secret:
            return
        widget.password = not widget.password

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
        if self.mode == "create" or self._started_in_edit_mode:
            # create: no view state to fall back to (never had one).
            # started_in_edit_mode: this screen skipped view mode entirely
            # (list page's direct-edit shortcut) — falling back to a view
            # rendering here would show the user a screen they never asked
            # to see instead of returning them to the list, as every other
            # exit from this shortcut (Save, a blocked/cancelled delete)
            # already does.
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
        """'d', view mode only (see check_action). Thin @work wrapper around
        _do_delete() — kept separate so OverviewScreen's direct-delete-from-
        the-list shortcut (action_delete_row()) can call _do_delete()
        directly as a plain coroutine instead of nesting one @work worker
        inside another. Nesting a second @work call and awaiting it via
        Worker.wait() was tried first and found to be fragile: if the outer
        worker (or the whole app/test) is torn down while the inner one is
        still suspended at a push_screen_wait(), Worker.wait() re-raises
        that as WorkerCancelled INSIDE the outer worker's own body — an
        unrelated-looking crash with no bug in the delete logic itself.
        A plain awaited coroutine has no such failure mode."""
        await self._do_delete()

    async def _do_delete(self) -> None:
        """Checks for referencing watchers FIRST (a clear, specific reason
        beats a generic validator error) — if any exist, shows that reason
        and stops before even offering the destructive confirm. Otherwise:
        confirm -> remove from `document` -> save(). save() remains the
        backstop even after the pre-check (belt-and-suspenders, not a
        replacement for it) — if it still rejects the deletion for some
        reason the pre-check didn't anticipate, the entry is reinserted so
        a rejected delete never leaves `document` silently missing
        something that's still on disk.
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
            # A field ctrl+r-reset earlier this session (action_reset_field())
            # always clears to inherited on Save, REGARDLESS of
            # _initial_values — this is what makes bool/enum fields able to
            # revert to inherited at all (they have no "blank" state to
            # clear, unlike str/int/list). Only holds if the widget still
            # shows the value reset set it to; if the user changed it again
            # since, this falls through to the normal diff below.
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
