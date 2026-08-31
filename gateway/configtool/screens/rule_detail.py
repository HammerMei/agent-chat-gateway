"""RuleDetailScreen — view, edit, and create a single watcher RULE.

A `watchers:` entry is a rule (gateway/core/watcher_rule.py): a required
unique `name`, a `connector`/`agent` pair, and a `rooms:` matcher
(`include`/`except_for` globs plus the `direct`/`group_direct` DM opt-ins).
It names no room — which rooms it claims is only known at runtime — so this
screen is a plain one-entry-in/one-entry-out form over one element of the
`document["watcher_rules"]` list, using the exact trial-entry install/rollback
pattern `ConnectorDetailScreen` already uses for the connectors list. The
merge-on-add / split-on-edit machinery the old expanded-watcher screen
needed does not exist here because the problem it solved (N rooms sharing
one raw dict) no longer exists as data.

`name` IS editable, unlike a connector's (which watchers reference by
name). Nothing in config.yaml references a rule by name; persisted session
records do (`rule_name`), but a rename is equivalent to delete+create from
the runtime's perspective and both halves of that are legal operations —
the delete-rule warning below is the same disclosure a rename implies.

Deleting a rule warns with what it strands (design §5.5): the persisted
session records carrying this rule's name and the scheduled jobs targeting
those records' watchers — counted read-only off the daemon's own files
(gateway/configtool/state_peek.py), never via the control socket (owner
decision 2026-08-18: the config tool operates on config.yaml only).
"""

from __future__ import annotations

from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Select, Static

from ...config import HistoryHandoffConfig
from ...core.watcher_rule import WatcherRule
from ..formatting import format_value, markup_safe, provenance_label
from ..modals import ConfirmModal, InheritsPickerModal, MessageModal, TextPromptModal
from ..model import EditableConfig
from ..state_peek import stranded_by_rule
from .form_common import (
    FieldSpec,
    FormScreen,
    apply_update,
    sort_required_first,
    widget_id,
)

# A watcher template's own field list — reused by TemplateDetailScreen.
# gateway/config.py forbids {name, room, rooms} on a watcher template, since
# each of those pins one SPECIFIC rule's identity; everything else a rule
# carries is legitimately
# shareable, which now includes the two session TTLs — they became
# first-class rule fields at the dynamic-watcher cutover.
WATCHER_TEMPLATE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("session_idle_days", "int", "Session idle days"),
    FieldSpec("session_expire_days", "int", "Session expire days"),
    FieldSpec("context_inject_files", "list", "Context inject files (comma-separated)"),
    FieldSpec("history_handoff.enabled", "bool", "History handoff enabled"),
    FieldSpec("history_handoff.fetch_count", "int", "History handoff fetch count"),
    FieldSpec("history_handoff.verbatim_tail", "int", "History handoff verbatim tail"),
)
# Defaults are read live from the owning dataclasses (HistoryHandoffConfig,
# WatcherRule) rather than re-typed as literals, so this preview can never
# drift out of sync with the loader — the drift already happened once
# (commit 31f966d flipped only the dataclass default and missed the loader).
_HH_DEFAULTS = HistoryHandoffConfig()
_RULE_FIELD_DEFAULTS = WatcherRule.__dataclass_fields__

_WATCHER_TEMPLATE_DEFAULT_VALUES: dict[str, object] = {
    "session_idle_days": _RULE_FIELD_DEFAULTS["session_idle_days"].default,
    "session_expire_days": _RULE_FIELD_DEFAULTS["session_expire_days"].default,
    "context_inject_files": [],
    "history_handoff.enabled": _HH_DEFAULTS.enabled,
    "history_handoff.fetch_count": _HH_DEFAULTS.fetch_count,
    "history_handoff.verbatim_tail": _HH_DEFAULTS.verbatim_tail,
}
# The key set is derived from WATCHER_TEMPLATE_FIELDS so the two halves of
# what is really one table cannot drift apart — a spec without a default
# raises KeyError at import, not at some later render.
WATCHER_TEMPLATE_DATACLASS_DEFAULTS: dict[str, object] = {
    spec.key: _WATCHER_TEMPLATE_DEFAULT_VALUES[spec.key]
    for spec in WATCHER_TEMPLATE_FIELDS
}

# The `rooms:` matcher fields — rule-only (a template may not carry `rooms`,
# so these never inherit; their provenance is always explicit-or-default).
_ROOMS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("rooms.include", "list", "Rooms: include patterns (comma-separated)"),
    FieldSpec("rooms.except_for", "list", "Rooms: except-for patterns (comma-separated)"),
    FieldSpec("rooms.direct", "bool", "Rooms: claim 1:1 DMs (direct)"),
    FieldSpec("rooms.group_direct", "bool", "Rooms: claim group DMs (group_direct)"),
)

_RULE_REQUIRED_FIELD_KEYS = frozenset({"name", "connector", "agent"})


def rule_rooms_summary(entry: dict) -> str:
    """One-line summary of a raw entry's `rooms:` matcher for table rows and
    the view body — e.g. `general, dev-* (except: *-noise) +dm +group_dm`.
    Defensive against a malformed entry (rooms not a mapping): shows what it
    can, the row's own Status column carries the actual error."""
    rooms = entry.get("rooms")
    if not isinstance(rooms, dict):
        return "?" if rooms is not None else "(none)"
    parts: list[str] = []
    include = rooms.get("include")
    if isinstance(include, list) and include:
        parts.append(", ".join(str(p) for p in include))
    except_for = rooms.get("except_for")
    if isinstance(except_for, list) and except_for:
        parts.append(f"(except: {', '.join(str(p) for p in except_for)})")
    if rooms.get("direct"):
        parts.append("+dm")
    if rooms.get("group_direct"):
        parts.append("+group_dm")
    return " ".join(parts) if parts else "(none)"


class RuleDetailScreen(FormScreen):
    BODY_ID = "rule-detail-body"

    @staticmethod
    def missing_prerequisites(cfg: EditableConfig) -> str | None:
        """Why this screen's edit/create form cannot open right now, or None.

        Internal review (lens B): the connector/agent dropdowns are the
        first enum FieldSpecs whose options come from a config list that can
        be EMPTY — and Textual's `Select(options, allow_blank=False)` raises
        EmptySelectError at construction on empty options, mid-compose,
        which takes the whole app down. Every entry point into a non-view
        mode (Overview's 'n'/'e' and this screen's own view→edit 'e') asks
        here first and notifies instead of composing."""
        missing = [
            noun
            for noun, present in (
                ("connector", bool(cfg.connectors_raw)),
                ("agent", bool(cfg.agents_raw)),
            )
            if not present
        ]
        if not missing:
            return None
        return (
            f"Watcher rules need at least one {' and one '.join(missing)} to exist "
            "first — create those before editing watcher rules."
        )

    async def action_edit(self) -> None:
        # The view→edit transition composes the Select widgets too — same
        # guard as the Overview's entry points (see missing_prerequisites()).
        message = self.missing_prerequisites(self.cfg)
        if message is not None and self.mode == "view":
            self.notify(message, severity="error")
            return
        await super().action_edit()

    def __init__(
        self,
        cfg: EditableConfig,
        entry: dict | None,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        # Create mode: nothing exists yet. The entry is never installed into
        # `cfg.document` until action_save() actually succeeds.
        self.entry = entry if entry is not None else {}
        self.mode = mode
        self._inherits_initial: str | None = self.cfg.entry_template_name(self.entry)
        self._inherits_current: str | None = self._inherits_initial
        # Whether the user actually touched the Name field this session —
        # `_name_live` alone can't distinguish "cleared to empty" from
        # "never touched" (both read ""), and a cleared name must survive a
        # template switch just like a typed one (Codex review of #129,
        # round 2). Reset alongside the other per-session state in
        # _on_enter_edit_mode().
        self._name_edited = False
        if self.mode != "view":
            self._compute_initial_values(self._current_entry())
            self._description_live = self._initial_values.get("description") or ""
            self._populating = True

    # ── FormScreen hooks ─────────────────────────────────────────────────────

    def _entity_noun(self) -> str:
        # "watcher rule", not "rule": the Tool Presets tab has rules too, and a
        # bare "Delete rule 'x'?" does not say which kind is about to go.
        return "watcher rule"

    def _entity_label(self) -> str:
        name = self.entry.get("name")
        if isinstance(name, str) and name:
            return name
        return "(new rule)" if self.mode == "create" else "?"

    def _current_entry(self) -> dict:
        """The PROBE entry: `self.entry` with `inherits:` swapped to whatever
        the Inherits picker currently has selected — see
        ConnectorDetailScreen._current_entry()'s identical docstring."""
        probe = dict(self.entry)
        if self._inherits_current is None:
            probe.pop("inherits", None)
        else:
            probe["inherits"] = self._inherits_current
        return probe

    def _find_own_index(self) -> int:
        # By object IDENTITY, not equality — same reasoning as
        # ConnectorDetailScreen._find_own_index(): two broken entries could
        # be byte-identical; identity is the only way to be sure this is
        # the exact entry this screen was opened on.
        watchers = self.cfg.watcher_entries
        return next(i for i, w in enumerate(watchers) if w is self.entry)

    def _remove_entry_from_document(self) -> None:
        self._deleted_index = self._find_own_index()
        del self.cfg.document["watcher_rules"][self._deleted_index]

    def _reinsert_entry_into_document(self) -> None:
        watchers = self.cfg.document.setdefault("watcher_rules", [])
        watchers.insert(self._deleted_index, self.entry)

    def _install_trial_entry(self, target_entry: dict) -> None:
        self._edit_index = self._find_own_index()
        self.cfg.document["watcher_rules"][self._edit_index] = target_entry

    def _rollback_trial_entry(self) -> None:
        self.cfg.document["watcher_rules"][self._edit_index] = self.entry

    def _referencing_watcher_labels(self) -> list[str]:
        # Nothing in config.yaml references a rule by name — the delete
        # pre-check has nothing to block on. What a deletion STRANDS at
        # runtime is a warning, not a blocker: see _delete_confirm_message().
        return []

    def _delete_confirm_message(self) -> str:
        base = super()._delete_confirm_message()
        name = self.entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return base
        # Stripped, matching _parse_one_watcher_rule's canonicalization —
        # persisted records carry the STRIPPED name, so an externally-
        # authored `name: " my-rule "` would otherwise count zero strands
        # and silently suppress the disclosure (Codex review of #129).
        records, jobs = stranded_by_rule(name.strip())
        if not records and not jobs:
            return base
        strands = [f"{records} persisted session record(s)"]
        if jobs:
            strands.append(f"{jobs} scheduled job(s)")
        message = (
            base
            + f"\n\nThis rule currently has {' and '.join(strands)} "
            "on disk. They stop being claimed by any rule; idle sessions "
            "are reclaimed by the daemon's lifecycle sweeps."
        )
        if jobs:
            # Not a vague caveat — the expiry sweep genuinely refuses a
            # record with pending jobs (WatcherLifecycle.expire_idle's job
            # exemption), so a stranded session KEEPS its jobs firing until
            # the operator removes them.
            message += (
                " A session with pending scheduled jobs is exempt from "
                "expiry — its jobs keep running until removed "
                "('acg schedule delete <job_id>')."
            )
        return message

    def _required_field_keys(self) -> frozenset[str]:
        return _RULE_REQUIRED_FIELD_KEYS

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        connector_names = tuple(sorted(c.get("name", "?") for c in self.cfg.connectors_raw))
        agent_names = tuple(sorted(self.cfg.agents_raw))
        return sort_required_first(
            (
                FieldSpec("name", "str", "Watcher rule name"),
                FieldSpec("connector", "enum", "Connector", options=connector_names),
                FieldSpec("agent", "enum", "Agent", options=agent_names),
                *_ROOMS_FIELDS,
                *WATCHER_TEMPLATE_FIELDS,
            ),
            _RULE_REQUIRED_FIELD_KEYS,
        )

    def _template_kind(self) -> str:
        return "watcher"

    def _dataclass_defaults(self) -> dict[str, object]:
        # connector/agent mirror gateway/config.py's own fallback: first
        # connector in document order; the explicit top-level
        # `default_agent:` when set and valid, first agent otherwise (Codex
        # review of #129: `next(iter(agents))` alone preselected the FIRST
        # agent even when `default_agent:` named another — and create mode
        # force-writes the selection explicitly, so an untouched Agent field
        # silently bound the new rule to the wrong backend). name has no
        # default: None (blank) is the honest answer for a required
        # identity field.
        defaults: dict[str, object] = dict(WATCHER_TEMPLATE_DATACLASS_DEFAULTS)
        defaults["name"] = None
        defaults["connector"] = (
            self.cfg.connectors_raw[0].get("name", "") if self.cfg.connectors_raw else ""
        )
        raw_default = self.cfg.document.get("default_agent")
        defaults["agent"] = (
            raw_default
            if isinstance(raw_default, str) and raw_default in self.cfg.agents_raw
            else next(iter(self.cfg.agents_raw), "")
        )
        defaults["rooms.include"] = []
        defaults["rooms.except_for"] = []
        defaults["rooms.direct"] = False
        defaults["rooms.group_direct"] = False
        return defaults

    # ── inherits: picker (mirrors agent/connector — watchers have no 'type') ──

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (event.button.id or "") == "inherits-change-button":
            self._open_inherits_picker()

    @work
    async def _open_inherits_picker(self) -> None:
        if self.mode == "view":
            return
        template_names = sorted(self.cfg.templates("watcher"))
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
                TextPromptModal("New watcher rule template — name")
            )
            if new_name is None:
                return
            if new_name in self.cfg.templates("watcher"):
                await self.app.push_screen_wait(
                    MessageModal(
                        f"A watcher rule template named '{markup_safe(new_name)}' "
                        "already exists.",
                        title="Could not create",
                    )
                )
                return
            from .template_detail import TemplateDetailScreen

            self.app.push_screen(
                TemplateDetailScreen(self.cfg, "watcher", new_name, {}, mode="create")
            )
            return
        else:
            return

        if new_value == self._inherits_current:
            return

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
        await self._recompute_form()
        self._form_dirty = True

    async def _recompute_form(self) -> None:
        """Codex review of #129: unlike the other forms, this screen models
        `name` as a generic FieldSpec, so the base recompute rebuilt the
        name Input from `self.entry` — silently discarding a typed-but-
        unsaved name on every `inherits:` switch (in create mode it went
        blank and Save was then refused). A watcher template can never
        supply `name` (forbidden key), so restoring the live value can't
        mask a template-provided one. `_name_live` is already maintained by
        FormScreen.on_input_changed() because widget_id("name") happens to
        be the exact "#field-name" id it tracks. Gated on `_name_edited`,
        not on `_name_live`'s truthiness (round 2): a name CLEARED to empty
        is an edit too, and restoring the old name over it would silently
        resurrect the identity the user just removed — while an untouched
        form ("" because nothing was ever typed) must keep showing the
        entry's own name."""
        await super()._recompute_form()
        if self._name_edited:
            try:
                self.query_one("#" + widget_id("name"), Input).value = self._name_live
            except NoMatches:
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self._populating and (event.input.id or "") == "field-name":
            self._name_edited = True
        super().on_input_changed(event)

    def _on_enter_edit_mode(self) -> None:
        self._inherits_initial = self.cfg.entry_template_name(self.entry)
        self._inherits_current = self._inherits_initial
        self._name_edited = False  # fresh edit session — see __init__
        self._compute_initial_values(self._current_entry())
        self._description_live = self._initial_values.get("description") or ""

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        entry = self.entry
        description = entry.get("description")
        template_name = self.cfg.entry_template_name(entry)

        # Every dynamic value here is operator-authored and reaches a
        # markup-parsing Static — rule names are unrestricted and patterns
        # legitimately contain `[…]` (see markup_safe()).
        lines = [f"[bold]{markup_safe(self._entity_label())}[/bold]"]
        if description:
            lines.append(f"[dim]{markup_safe(description)}[/dim]")
        try:
            merged = self.cfg.merged_entry("watcher", entry)
        except (ValueError, FileNotFoundError) as exc:
            # markup_safe: this message quotes the value that caused it —
            # `inherits: "[/]"` names an unknown template, and the loader's
            # error repeats that name, so an unescaped interpolation raised
            # MarkupError and crashed the row instead of EXPLAINING it
            # (Codex review of #129, round 8). The explanation path must not
            # be the one that fails.
            lines.append(
                f"[red]Could not compute effective values: {markup_safe(exc)}[/red]"
            )
            return "\n".join(lines)

        defaults = self._dataclass_defaults()
        lines.append(
            f"connector: {markup_safe(merged.get('connector') or defaults['connector'])}"
        )
        lines.append(f"agent: {markup_safe(merged.get('agent') or defaults['agent'])}")
        lines.append(f"rooms: {markup_safe(rule_rooms_summary(entry))}")
        lines.append(
            f"inherits: {markup_safe(template_name) if template_name else '(none)'}"
        )
        lines.append("")

        for spec in WATCHER_TEMPLATE_FIELDS:
            top_key = spec.key.split(".", 1)[0]
            provenance = self.cfg.field_provenance("watcher", entry, top_key)
            value = merged.get(top_key)
            if "." in spec.key and isinstance(value, dict):
                value = value.get(spec.key.split(".", 1)[1])
            if value is None:
                value = WATCHER_TEMPLATE_DATACLASS_DEFAULTS.get(spec.key)
            lines.append(
                f"{spec.key}: {markup_safe(format_value(value))}  "
                f"[dim]({provenance_label(provenance, template_name)})[/dim]"
            )
        return "\n".join(lines)

    # ── edit/create form ─────────────────────────────────────────────────────

    def _compose_form(self) -> ComposeResult:
        with VerticalScroll(classes="entity-form", can_focus=False):
            if self.mode == "create":
                yield Static("[bold]New watcher rule[/bold]")
            else:
                yield Static(f"[bold]{markup_safe(self._entity_label())}[/bold]  (editing)")

            with Horizontal(classes="field-row"):
                yield Static("Description", classes="field-label")
                yield Input(id="field-description", value=self._description_live)

            with Horizontal(classes="field-row"):
                yield Static("Inherits", classes="field-label")
                yield Static(
                    # A template name is operator-authored and this Static
                    # parses markup (see markup_safe()).
                    markup_safe(self._inherits_current) if self._inherits_current
                    else "(none)",
                    id="inherits-value",
                    classes="field-value",
                )
                yield Button("Change…", id="inherits-change-button")

            for spec in self._field_specs():
                yield from self._compose_field_row(spec, self._current_entry())

    # ── save ─────────────────────────────────────────────────────────────────

    @work
    async def action_save(self) -> None:
        if self.mode == "view":
            return

        updates = self._collect_field_updates()
        if updates is None:
            await self.app.push_screen_wait(
                MessageModal(
                    # read_widget_value() quotes the operator's own text back
                    # ("must be a whole number, got '[/]'"), so the message
                    # reporting a bad value must not itself be parsed as
                    # markup (Codex review of #129, round 10).
                    markup_safe(self._last_field_error or "Invalid field."),
                    title="Could not save",
                )
            )
            return

        # ALWAYS a trial copy, never self.entry directly — see
        # ConnectorDetailScreen.action_save()'s identical comment.
        target_entry = dict(self.entry)
        for key, value in updates.items():
            apply_update(target_entry, key, value)
        if self._inherits_current != self._inherits_initial:
            apply_update(target_entry, "inherits", self._inherits_current)

        # connector/agent are ALWAYS written explicitly at creation
        # (bypassing diff semantics) — they're required, and a value the
        # user never touched must still be recorded rather than left to the
        # loader's config-order-dependent fallback. Same rule the old
        # create form had.
        if self.mode == "create":
            for key in ("connector", "agent"):
                value = self.query_one("#" + widget_id(key), Select).value
                if not value:
                    await self.app.push_screen_wait(
                        MessageModal("Connector and agent are required.", title="Could not save")
                    )
                    return
                target_entry[key] = value

        name = target_entry.get("name")
        if not isinstance(name, str) or not name.strip():
            await self.app.push_screen_wait(
                MessageModal("Watcher rule name is required.", title="Could not save")
            )
            return
        duplicate = any(
            w.get("name") == name
            for w in self.cfg.watchers_raw
            if w is not self.entry
        )
        if duplicate:
            await self.app.push_screen_wait(
                MessageModal(
                    f"A rule named '{markup_safe(name)}' already exists.",
                    title="Could not save",
                )
            )
            return

        # A rule that can never match anything is a typo, not an intention
        # (the loader refuses it too — this just says it in form terms
        # before a generic parser message would).
        rooms = target_entry.get("rooms") or {}
        if not isinstance(rooms, dict) or not (
            rooms.get("include") or rooms.get("direct") or rooms.get("group_direct")
        ):
            await self.app.push_screen_wait(
                MessageModal(
                    "A rule needs at least one rooms include pattern, or one "
                    "of the DM opt-ins (direct / group_direct).",
                    title="Could not save",
                )
            )
            return

        inserted_index: int | None = None
        was_dirty = self.cfg.dirty  # captured BEFORE any mutation; see below
        watchers_key_was_absent = False
        watchers_original_value: object = None
        if self.mode == "create":
            existing = self.cfg.document.get("watcher_rules")
            if existing is not None and not isinstance(existing, list):
                # REFUSED, not normalized (Codex review of #129, round 3):
                # a malformed non-list `watchers:` can hold RECOVERABLE rule
                # data — the classic shape is a mapping from an operator
                # omitting the '-' before an otherwise complete rule — and
                # replacing it with [] would pass the save gate (the
                # structural error disappears along with the data) and
                # silently delete work the user only asked to add to.
                # Only an absent or explicit-null key is normalized below.
                await self.app.push_screen_wait(
                    MessageModal(
                        "config.yaml's 'watchers:' is not a list "
                        f"(got {type(existing).__name__}) — often a missing "
                        "'-' before a rule. Repair it in $EDITOR (ctrl+e on "
                        "the list screen) first; creating a rule here would "
                        "overwrite whatever it holds.",
                        title="Could not save",
                    )
                )
                return
            # KEY MEMBERSHIP, not the value: `document.get("watcher_rules")`
            # returns None both for an absent key and for an explicit
            # `watchers:` (null), so round 10's `existing is None` popped a
            # key the operator had actually written — and a later unrelated
            # successful save then wrote the file without it (Codex review of
            # #129, round 11). Both the presence and the original value are
            # captured so a rejected create restores the document exactly as
            # loaded.
            watchers_key_was_absent = "watcher_rules" not in self.cfg.document
            watchers_original_value = self.cfg.document.get("watcher_rules")
            if not isinstance(existing, list):
                self.cfg.document["watcher_rules"] = []
            watchers = self.cfg.document["watcher_rules"]
            watchers.append(target_entry)
            inserted_index = len(watchers) - 1
        else:
            self._install_trial_entry(target_entry)
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create" and inserted_index is not None:
                del self.cfg.document["watcher_rules"][inserted_index]
                if watchers_key_was_absent:
                    self.cfg.document.pop("watcher_rules", None)
                elif not isinstance(watchers_original_value, list):
                    # It was there, holding something this form replaced with
                    # a list (an explicit null). Put that value back rather
                    # than the empty list standing in for it.
                    self.cfg.document["watcher_rules"] = watchers_original_value
            else:
                self._rollback_trial_entry()
            # Same rollback contract the reorder and malformed-row delete
            # already honour: restoring the document is only half of it, or
            # the quit gate goes on offering to discard changes that no
            # longer exist. This site was MINE and I missed it when scoping
            # round 8's fix — the sibling pre-existing paths are #131.
            self.cfg.dirty = was_dirty
            await self.app.push_screen_wait(MessageModal(markup_safe(exc), title="Could not save"))
            return

        self.entry = target_entry
        self.app.pop_screen()
        app = self.app
        app.notify(f"Saved watcher rule '{name}'.", severity="information")
        app.reload_config()  # type: ignore[attr-defined]
