"""WatcherDetailScreen — view, edit, and create a single EXPANDED watcher
(Config TUI Phase 3).

A raw `watchers:` entry is not always one entity: a `rooms: [a, b, c]` list
expands into 3 independent `WatcherConfig`s SHARING one raw dict
(`gateway/config.py`'s `_parse_one_watcher_entry()`,
`EditableConfig.expanded_watchers()`). This screen edits exactly ONE
expanded watcher at a time — the Watchers table already shows expanded rows,
never raw groups — and reconciles that with the shared raw dict via two
`EditableConfig` primitives (`add_watcher_rooms()`/`remove_watcher_room()`,
`gateway/configtool/model.py`) instead of `FormScreen`'s usual one-entry-in,
one-entry-out `_install_trial_entry()`/`_rollback_trial_entry()` pattern
(this screen doesn't implement those two hooks at all — nothing calls them,
since `action_save()` is fully custom here, same as every other subclass's
"entity-specific, no generic implementation" per `FormScreen`'s own module
docstring).

docs/design/config-tool.md's Phase 3 "two-tier rule" (decision 3):
  - Editing a GROUP-SHARED field (`description` — a free-text annotation
    with no bearing on which connector/agent/room is actually watched)
    edits the shared raw entry in place — the whole group moves together.
  - Editing a PER-ROOM field (`room` itself, `name`, `session_id`,
    `connector`, `agent`, `inherits`, `online_notification`,
    `offline_notification`, `context_inject_files`, `history_handoff.*`)
    auto-splits this one room out of its group into its own entry
    (`remove_watcher_room()` + `add_watcher_rooms()` — the same primitive
    pair used for a plain rename/move, and for new-watcher creation).
    User-reported bug, fixed: `connector`/`agent` used to be treated as
    GROUP-SHARED ("move the whole group in place") — every one of these
    fields is stored as a SINGLE value on the shared raw entry (exactly
    like `online_notification` etc., already correctly per-room), so
    there's no way to give one room in a group a divergent value without
    splitting; treating connector/agent as an exception silently moved an
    ENTIRE group to a different connector when the user only meant to
    redirect the one room they had open. `inherits` gets the same
    treatment for the same reason (also a single shared value).

Two owner decisions supersede the original design doc's still-unbuilt
`EntityPickerModal`/`RoomListEditorScreen`:
  - No `EntityPickerModal`: `connector`/`agent` are two plain `Select`
    `FieldSpec`s directly in this screen's own create/edit form (options
    computed live from `cfg.connectors_raw`/`cfg.agents_raw`) — same
    inline-`Select` precedent `ConnectorDetailScreen`'s Mattermost
    auth-method picker already establishes.
  - No `RoomListEditorScreen`: adding more rooms to an existing
    connector+agent pairing is `add_watcher_rooms()`'s merge-on-add rule
    (creating/cloning a room whose shared fields match an existing group
    merges into it automatically) plus the "Clone for rooms" action below
    — both route through the exact same primitive, so a separate
    room-list-management screen was never actually needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Select, Static

from ..formatting import format_value, provenance_label
from ..modals import ConfirmModal, InheritsPickerModal, MessageModal, TextPromptModal
from ..model import EditableConfig, ExpandedWatcher
from .form_common import (
    FieldSpec,
    FormScreen,
    apply_update,
    get_nested,
    sort_required_first,
    text_to_list,
    widget_id,
)

if TYPE_CHECKING:
    from ..app import ConfigToolApp

_KNOWN_FIELDS = [
    "session_id", "online_notification", "offline_notification",
    "context_inject_files", "history_handoff",
]

# A watcher template's own field list — relocated here from the deleted
# defaults.py (was WATCHER_DEFAULTS_FIELDS), reused by TemplateDetailScreen.
# gateway/config.py forbids {name, room, rooms, session_id} on a watcher
# template, since each of those pins one SPECIFIC watcher's identity and has
# no business in a block every watcher merges against. Also reused, VERBATIM,
# by WatcherDetailScreen below for the identical reason: these are exactly
# the fields _parse_one_watcher_entry() treats as entry-level/shared (per
# the two-tier split rule) rather than per-room-identity.
WATCHER_TEMPLATE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("online_notification", "str", "Online notification"),
    FieldSpec("offline_notification", "str", "Offline notification"),
    FieldSpec("context_inject_files", "list", "Context inject files (comma-separated)"),
    FieldSpec("history_handoff.enabled", "bool", "History handoff enabled"),
    FieldSpec("history_handoff.fetch_count", "int", "History handoff fetch count"),
    FieldSpec("history_handoff.verbatim_tail", "int", "History handoff verbatim tail"),
)
# Mirrors gateway/config.py's OWN `.get(key, X)` calls at the watcher-parsing
# site (NOT HistoryHandoffConfig's dataclass field defaults, which have
# already drifted from them once — the dataclass itself defaults
# history_handoff.enabled to True, but the loader's own
# `hh_raw.get("enabled", False)` actually applies False whenever the key is
# absent). Matching the LOADER, not the dataclass, is what makes this form an
# honest "what would this evaluate to right now" preview.
WATCHER_TEMPLATE_DATACLASS_DEFAULTS: dict[str, object] = {
    "online_notification": None,
    "offline_notification": None,
    "context_inject_files": [],
    "history_handoff.enabled": False,
    "history_handoff.fetch_count": 50,
    "history_handoff.verbatim_tail": 15,
}

# The ONE key that's truly safe to edit in place across a whole rooms:
# group: 'description' is a free-text annotation _parse_one_watcher_entry()
# never even reads — it has no bearing on which connector/agent/room is
# actually being watched, unlike every other entry-level field (connector,
# agent, inherits, online_notification, ...), which — despite ALSO being
# stored as a single value on the shared raw entry — identifies or
# configures the watching itself and therefore can't legitimately apply to
# one room without splitting it out first (see module docstring's two-tier
# rule). PR review finding: 'description' was originally missing from this
# set entirely, so `_collect_field_updates()`'s own hardcoded separate
# "description" diff (form_common.py) fell into per_room_updates below
# instead, which (a) needlessly triggered a group split for an edit that
# never needed one, and (b) silently LOST the edit entirely, since
# split_entry's own field loop never copies "description" (it's not one of
# name/session_id/WATCHER_TEMPLATE_FIELDS).
#
# User-reported bug, fixed: 'connector'/'agent' used to ALSO be in this set
# ("group-shared, move the whole group in place") — that's wrong for the
# same reason online_notification etc. are already NOT in this set:
# reassigning one room's connector/agent must split it out, not silently
# drag every sibling room in the group along with it.
_SHARED_FIELD_KEYS = frozenset({"description"})

# connector/agent/room can never actually be saved blank — Select widgets
# (connector/agent) have allow_blank=False, and _save_create()/_save_edit()
# both explicitly reject an empty room. User-requested: mark them '*' and
# keep them sorted first (already true today via _field_specs()'s own
# declared order, but routed through sort_required_first() too, for
# consistency with agent/connector and to stay correct if that order ever
# changes).
_WATCHER_REQUIRED_FIELD_KEYS = frozenset({"connector", "agent", "room"})


class WatcherDetailScreen(FormScreen):
    BODY_ID = "watcher-detail-body"

    BINDINGS = [
        *FormScreen.BINDINGS,
        # Owner-requested alternative to adding rooms one at a time (the
        # "RoomListEditorScreen" the original design doc never built) —
        # only meaningful for an EXISTING watcher, view or edit mode (see
        # check_action() below).
        Binding("c", "clone_for_rooms", "Clone for rooms", show=True),
    ]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "clone_for_rooms":
            return self.mode != "create"
        return super().check_action(action, parameters)

    def __init__(
        self,
        cfg: EditableConfig,
        expanded_watcher: ExpandedWatcher | None,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        self.mode = mode
        if expanded_watcher is not None:
            self.raw_entry = expanded_watcher.raw_entry
            self.room: str | None = expanded_watcher.watcher.room
            self.group_size = expanded_watcher.group_size
            self.watcher_name = expanded_watcher.watcher.name
        else:
            # Create mode: nothing exists yet. `raw_entry` is never installed
            # into `self.cfg.document` until action_save() actually succeeds.
            self.raw_entry = {}
            self.room = None
            self.group_size = 1
            self.watcher_name = None
        self._inherits_initial: str | None = self.cfg.entry_template_name(self.raw_entry)
        self._inherits_current: str | None = self._inherits_initial
        if self.mode != "view":
            self._compute_initial_values(self._current_entry())
            self._description_live = self._initial_values.get("description") or ""
            self._populating = True

    def _on_enter_edit_mode(self) -> None:
        # PR review finding: without this override, FormScreen.action_edit()
        # — the screen's OWN 'e' key, the in-place view-to-edit transition
        # every other detail screen uses this same hook for — never
        # (re)computed `_initial_values` at all. Every field then looked
        # "changed" relative to an empty snapshot, silently rewriting
        # connector/agent back to whatever the Select's own first-option
        # fallback was and spuriously splitting groups on a completely
        # untouched Save. Only the list page's direct-edit shortcut
        # (OverviewScreen.action_edit_row(), which constructs THIS screen
        # already in mode="edit") was unaffected, since __init__ above
        # already computes initial values in that case.
        self._inherits_initial = self.cfg.entry_template_name(self.raw_entry)
        self._inherits_current = self._inherits_initial
        self._compute_initial_values(self._current_entry())
        self._description_live = self._initial_values.get("description") or ""

    # ── FormScreen hooks ─────────────────────────────────────────────────────

    def _entity_noun(self) -> str:
        return "watcher"

    def _entity_label(self) -> str:
        if self.watcher_name:
            return self.watcher_name
        return self.room or "(new watcher)"

    def _current_entry(self) -> dict:
        """The PROBE entry: `self.raw_entry`'s shared fields (connector/
        agent/inherits/online_notification/etc.) with `rooms:` stripped and
        `room:` pinned to THIS specific expanded watcher's own room — so the
        generic field-rendering/diffing machinery always sees a clean
        single-room shape, regardless of whether the real underlying entry
        is grouped. Mirrors AgentDetailScreen/ConnectorDetailScreen's own
        inherits-swapped probe pattern."""
        probe = dict(self.raw_entry)
        probe.pop("rooms", None)
        if self.room is not None:
            probe["room"] = self.room
        if self._inherits_current is None:
            probe.pop("inherits", None)
        else:
            probe["inherits"] = self._inherits_current
        return probe

    def _remove_entry_from_document(self) -> None:
        # Whole-list snapshot/restore, not a precise per-field undo: this
        # mutation can touch (or remove) more than one raw entry — normalizing
        # a group down to a single room, for instance — so hand-writing an
        # exact inverse for every shape `remove_watcher_room()` can produce
        # would be its own significant surface. A snapshot is simpler and
        # provably correct.
        self._watchers_snapshot = [dict(w) for w in (self.cfg.document.get("watchers") or [])]
        self.cfg.remove_watcher_room(self.raw_entry, self.room)

    def _reinsert_entry_into_document(self) -> None:
        self.cfg.document["watchers"] = self._watchers_snapshot
        self._resync_raw_entry()

    def _resync_raw_entry(self) -> None:
        """PR review finding (self-caught): after restoring
        `document['watchers']` WHOLESALE from a snapshot, `self.raw_entry`
        is left pointing at an ORPHANED dict — `[dict(w) for w in ...]`
        snapshots are copies, never the SAME objects the restored list now
        holds. Every mutation primitive here (`remove_watcher_room()`, the
        merge lookup inside `add_watcher_rooms()`) finds its target by
        IDENTITY (`is`), not equality — a stale `self.raw_entry` would
        silently match nothing on a SECOND Save/Delete attempt in the same
        screen session (e.g. fix a validation error and retry), no-op'ing
        the "remove the old room" half of a rename/split while the "add
        the new room" half still succeeds elsewhere — reproduced directly:
        the old room was silently left behind as an extra, unintended
        sibling instead of being replaced. Re-points `self.raw_entry` at
        whichever restored entry still actually contains this room."""
        for entry in self.cfg.document.get("watchers") or []:
            rooms = entry["rooms"] if "rooms" in entry else [entry["room"]] if "room" in entry else []
            if self.room in rooms:
                self.raw_entry = entry
                return

    def _referencing_watcher_labels(self) -> list[str]:
        # Nothing in config.yaml references a WATCHER by name (unlike
        # connectors/agents, which watchers themselves reference) — this
        # delete pre-check never has anything to block on.
        return []

    def _install_trial_entry(self, target_entry: dict) -> None:
        raise NotImplementedError  # action_save() below never calls this

    def _rollback_trial_entry(self) -> None:
        raise NotImplementedError  # action_save() below never calls this

    def _required_field_keys(self) -> frozenset[str]:
        return _WATCHER_REQUIRED_FIELD_KEYS

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        connector_names = tuple(sorted(c.get("name", "?") for c in self.cfg.connectors_raw))
        agent_names = tuple(sorted(self.cfg.agents_raw))
        # 'room' accepts a comma-separated LIST only at creation (seeding a
        # whole rooms: group in one step, per the original design note:
        # "room(s) free text, single or comma-list") — editing an EXISTING
        # watcher's room is a single-value rename/move.
        room_spec = (
            FieldSpec("room", "list", "Room(s), comma-separated")
            if self.mode == "create"
            else FieldSpec("room", "str", "Room")
        )
        return sort_required_first(
            (
                FieldSpec("connector", "enum", "Connector", options=connector_names),
                FieldSpec("agent", "enum", "Agent", options=agent_names),
                room_spec,
                FieldSpec("name", "str", "Name"),
                FieldSpec("session_id", "str", "Session ID"),
                *WATCHER_TEMPLATE_FIELDS,
            ),
            _WATCHER_REQUIRED_FIELD_KEYS,
        )

    def _template_kind(self) -> str:
        return "watcher"

    def _dataclass_defaults(self) -> dict[str, object]:
        # connector/agent: approximates gateway/config.py's own fallback
        # (connectors[0].name / default_agent) for display purposes only —
        # NOT a reimplementation of that resolution (it doesn't account for
        # an explicit top-level `default_agent:` override). This only
        # matters for the rare existing entry that never set its own
        # connector:/agent: explicitly; save()'s own validate_config() call
        # remains the real backstop for whatever this approximation gets
        # wrong. name/session_id/room have no meaningful default at all —
        # None (blank) is the honest answer for an identity field nothing
        # implies a value for.
        defaults = dict(WATCHER_TEMPLATE_DATACLASS_DEFAULTS)
        defaults["connector"] = (
            self.cfg.connectors_raw[0].get("name", "") if self.cfg.connectors_raw else ""
        )
        defaults["agent"] = next(iter(self.cfg.agents_raw), "")
        defaults["name"] = None
        defaults["session_id"] = None
        defaults["room"] = None
        return defaults

    # ── inherits: picker (shared field — mirrors agent/connector exactly,
    # no type-filtering concern here since watchers have no 'type') ─────────

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
                TextPromptModal("New watcher template — name")
            )
            if new_name is None:
                return
            if new_name in self.cfg.templates("watcher"):
                await self.app.push_screen_wait(
                    MessageModal(
                        f"A watcher template named '{new_name}' already exists.",
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

    # ── "Clone for rooms" — the merge-on-add rule's other entry point ───────

    def _watcher_shared_fields_now(self) -> dict:
        """The shared-field snapshot to clone FROM — the group's CURRENT
        merged/effective values (not just self.raw_entry's own raw keys),
        so cloning from a watcher that inherits some of its shared fields
        from a template still carries them into the new room(s) — matching
        what the group's own fields would otherwise resolve to."""
        try:
            merged = self.cfg.merged_entry("watcher", self._current_entry())
        except (ValueError, FileNotFoundError):
            merged = self._current_entry()
        return {
            key: merged[key]
            for key in (
                "online_notification", "offline_notification",
                "context_inject_files", "history_handoff", "description",
            )
            if key in merged
        }

    @work
    async def action_clone_for_rooms(self) -> None:
        """'c', view/edit mode of an EXISTING watcher (see check_action()).
        Thin @work wrapper around _do_clone_for_rooms() — kept separate so
        OverviewScreen's direct-clone-from-the-list shortcut
        (action_clone_for_rooms()) can call _do_clone_for_rooms() directly as
        a plain coroutine instead of nesting one @work worker inside another,
        same reasoning as action_delete()/_do_delete()."""
        await self._do_clone_for_rooms()

    async def _do_clone_for_rooms(self) -> None:
        """Bulk-add rooms sharing this watcher's connector/agent/inherits/
        shared fields — the owner-requested alternative to adding rooms one
        at a time. Only meaningful for an EXISTING watcher (create mode has
        no watcher yet to clone FROM)."""
        if self.mode == "create" or not self.room:
            return
        text = await self.app.push_screen_wait(
            TextPromptModal("Clone to rooms (comma-separated)")
        )
        if text is None:
            return
        rooms = text_to_list(text)
        if not rooms:
            return

        connector = self.raw_entry.get("connector") or self._dataclass_defaults()["connector"]
        agent = self.raw_entry.get("agent") or self._dataclass_defaults()["agent"]
        shared = self._watcher_shared_fields_now()
        if self._inherits_current is not None:
            shared["inherits"] = self._inherits_current

        watchers_snapshot = [dict(w) for w in (self.cfg.document.get("watchers") or [])]
        added = self.cfg.add_watcher_rooms(connector, agent, rooms, shared)
        if not added:
            self.notify("Every room was already in this group — nothing to add.")
            return
        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            self.cfg.document["watchers"] = watchers_snapshot
            # Clone-for-rooms itself never dereferences self.raw_entry BY
            # IDENTITY (add_watcher_rooms() matches on connector/agent/
            # shared VALUES, not object identity) — so this resync isn't
            # fixing a bug reachable through Clone alone. It matters for
            # whatever the user does NEXT in this same screen session
            # (Edit-then-Save, Delete) — both of which DO look
            # self.raw_entry up by identity (remove_watcher_room()) and
            # would otherwise silently no-op against this now-orphaned
            # object, same class of bug as the room-rename regression this
            # mirrors.
            self._resync_raw_entry()
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return

        skipped = len(rooms) - len(added)
        message = f"Added {len(added)} room(s)."
        if skipped:
            message += f" ({skipped} already present, skipped.)"
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.notify(message, severity="information")
        self.app.pop_screen()
        app.reload_config()

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        entry = self.raw_entry
        description = entry.get("description")

        lines = [f"[bold]{self._entity_label()}[/bold]"]
        if description:
            lines.append(f"[dim]{description}[/dim]")
        lines.append(f"connector: {entry.get('connector') or self._dataclass_defaults()['connector']}")
        lines.append(f"agent: {entry.get('agent') or self._dataclass_defaults()['agent']}")
        lines.append(f"room: {self.room}")

        if self.group_size > 1:
            all_rooms = list(entry.get("rooms") or [])
            siblings = ", ".join(r for r in all_rooms if r != self.room)
            lines.append("")
            lines.append(
                f"[yellow]Part of a shared rooms: group with: {siblings} "
                f"({self.group_size - 1} other room(s))[/yellow]"
            )

        template_name = self.cfg.entry_template_name(entry)
        lines.append(f"inherits: {template_name if template_name else '(none)'}")
        lines.append("")
        try:
            merged = self.cfg.merged_entry("watcher", entry)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(f"[red]Could not compute effective values: {exc}[/red]")
            return "\n".join(lines)

        for key in _KNOWN_FIELDS:
            provenance = self.cfg.field_provenance("watcher", entry, key)
            lines.append(
                f"{key}: {format_value(merged.get(key))}  "
                f"[dim]({provenance_label(provenance, template_name)})[/dim]"
            )

        return "\n".join(lines)

    # ── edit/create form ─────────────────────────────────────────────────────

    def _compose_form(self) -> ComposeResult:
        with VerticalScroll(classes="entity-form", can_focus=False):
            if self.mode == "create":
                yield Static("[bold]New watcher[/bold]")
            else:
                yield Static(f"[bold]{self._entity_label()}[/bold]  (editing)")
                if self.group_size > 1:
                    yield Static(
                        "[yellow]Part of a shared rooms: group — editing any "
                        "field below except Description will split this room "
                        "out into its own entry.[/yellow]"
                    )

            with Horizontal(classes="field-row"):
                yield Static("Description", classes="field-label")
                yield Input(id="field-description", value=self._description_live)

            with Horizontal(classes="field-row"):
                yield Static("Inherits", classes="field-label")
                yield Static(
                    self._inherits_current or "(none)",
                    id="inherits-value",
                    classes="field-value",
                )
                yield Button("Change…", id="inherits-change-button")

            for spec in self._field_specs():
                yield from self._compose_field_row(spec, self._current_entry())
                if spec.key == "name":
                    # User-requested: it's not obvious that leaving this
                    # blank is the recommended default — an explicit name
                    # pins this room to its own single-room entry forever,
                    # opting it out of add_watcher_rooms()'s merge-on-add
                    # optimization (only worth it for the rare case of two
                    # DIFFERENT agents watching the same connector+room, an
                    # edge case, not the common one).
                    yield Static(
                        "[dim]Leave blank unless you need to avoid a name "
                        "conflict — an explicit name opts this room out of "
                        "merging with others that share the same "
                        "settings.[/dim]"
                    )

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

        if self.mode == "create":
            await self._save_create(updates)
        else:
            await self._save_edit(updates)

    async def _save_create(self, updates: dict) -> None:
        # connector/agent/room are ALWAYS read directly (bypassing the
        # diffed `updates` dict) — unlike every other field, they're
        # REQUIRED at creation, and a value the user never touched (still
        # showing whatever the Select/Input defaulted to) must still be
        # written explicitly. Every other field keeps ordinary diff
        # semantics (an untouched optional field stays omitted, resolving
        # to its own default when loaded — cleaner YAML, same behavior).
        connector = self.query_one("#" + widget_id("connector"), Select).value
        agent = self.query_one("#" + widget_id("agent"), Select).value
        room_text = self.query_one("#" + widget_id("room"), Input).value
        rooms = text_to_list(room_text)
        if not connector or not agent:
            await self.app.push_screen_wait(
                MessageModal("Connector and agent are required.", title="Could not save")
            )
            return
        if not rooms:
            await self.app.push_screen_wait(
                MessageModal("At least one room is required.", title="Could not save")
            )
            return

        target_entry: dict = {}
        for key, value in updates.items():
            if key in ("connector", "agent", "room"):
                continue
            apply_update(target_entry, key, value)
        if self._inherits_current is not None:
            target_entry["inherits"] = self._inherits_current

        watchers_snapshot = [dict(w) for w in (self.cfg.document.get("watchers") or [])]
        added = self.cfg.add_watcher_rooms(connector, agent, rooms, target_entry)
        if not added:
            self.cfg.document["watchers"] = watchers_snapshot
            await self.app.push_screen_wait(
                MessageModal(
                    "Every room listed already exists under this connector/agent.",
                    title="Could not save",
                )
            )
            return

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            self.cfg.document["watchers"] = watchers_snapshot
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return

        self.app.pop_screen()
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.notify(f"Created watcher room(s): {', '.join(added)}.", severity="information")
        app.reload_config()

    async def _save_edit(self, updates: dict) -> None:
        watchers_snapshot = [dict(w) for w in (self.cfg.document.get("watchers") or [])]

        inherits_changed = self._inherits_current != self._inherits_initial
        shared_updates = {k: v for k, v in updates.items() if k in _SHARED_FIELD_KEYS}
        per_room_updates = {k: v for k, v in updates.items() if k not in _SHARED_FIELD_KEYS}

        # 'description' (the only genuinely group-shared field — see
        # _SHARED_FIELD_KEYS's own comment) edits the shared raw entry IN
        # PLACE — the whole group moves together for this one, regardless of
        # whether anything else below also splits this room out.
        for key, value in shared_updates.items():
            apply_update(self.raw_entry, key, value)

        if per_room_updates or inherits_changed:
            # A per-room field changed (room/name/session_id/connector/agent/
            # online_notification/offline_notification/context_inject_files/
            # history_handoff.*) OR inherits changed — every one of these
            # identifies or configures the room being watched, so there's no
            # way to give just THIS room a divergent value without splitting
            # it out of its group first, per the two-tier rule. Composed from
            # the exact same add/remove primitives new-watcher creation and
            # Clone-for-rooms use — a split-out room can itself merge into
            # some OTHER pre-existing matching entry, symmetric with creation.
            new_room = per_room_updates.get("room", self.room)
            if not new_room:
                await self.app.push_screen_wait(
                    MessageModal("Room cannot be empty.", title="Could not save")
                )
                self.cfg.document["watchers"] = watchers_snapshot
                self._resync_raw_entry()
                return
            # NOT self.raw_entry.get(...) alone — connector/agent are no
            # longer applied in place before this point (see above), so a
            # JUST-CHANGED value only lives in per_room_updates until the
            # split below actually writes it into the new entry.
            new_connector = (
                per_room_updates.get("connector", self.raw_entry.get("connector"))
                or self._dataclass_defaults()["connector"]
            )
            new_agent = (
                per_room_updates.get("agent", self.raw_entry.get("agent"))
                or self._dataclass_defaults()["agent"]
            )

            split_entry: dict = {}
            for key in ("name", "session_id", *(f.key for f in WATCHER_TEMPLATE_FIELDS)):
                value = per_room_updates.get(key, get_nested(self._current_entry(), key))
                apply_update(split_entry, key, value)
            if self._inherits_current is not None:
                split_entry["inherits"] = self._inherits_current
            description = self.raw_entry.get("description")
            if description is not None:
                split_entry["description"] = description

            original_index = self.cfg.remove_watcher_room(self.raw_entry, self.room)
            added = self.cfg.add_watcher_rooms(
                new_connector, new_agent, [new_room], split_entry, insert_at=original_index,
            )
            if not added:
                self.cfg.document["watchers"] = watchers_snapshot
                self._resync_raw_entry()
                await self.app.push_screen_wait(
                    MessageModal(
                        f"Room '{new_room}' already exists under this connector/agent.",
                        title="Could not save",
                    )
                )
                return
        else:
            self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            self.cfg.document["watchers"] = watchers_snapshot
            self._resync_raw_entry()
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return

        self.app.pop_screen()
        app: "ConfigToolApp" = self.app  # type: ignore[attr-defined, assignment]
        app.notify(f"Saved watcher '{self._entity_label()}'.", severity="information")
        app.reload_config()
