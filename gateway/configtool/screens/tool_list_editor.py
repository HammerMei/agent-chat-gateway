"""Shared tool-list (`owner_allowed_tools`/`guest_allowed_tools`) editor —
originally built for `AgentDetailScreen` only; extracted here once
`TemplateDetailScreen` became a second concrete user (user-reported: "agent
template does not have ways to edit owner_allowed_tools and
guest_allowed_tools" — a real gap, not a deliberate cut: `gateway/config.py`'s
`agent_templates` forbidden-keys set is `frozenset()`, so both fields are
already legal on a template). Matches `FormScreen`'s own "extract once a
second user exists, not guessed at up front" precedent (see its module
docstring).

`ToolListEditorMixin` is mixed into a `FormScreen` subclass alongside
`FormScreen` itself (`class AgentDetailScreen(ToolListEditorMixin,
FormScreen)`). The host class must:
  - call `_init_tool_lists()` once, at the same point it would otherwise
    initialize `self._tool_lists`/`self._tool_lists_initial` (constructor,
    guarded the same way `_compute_initial_values()` is)
  - implement `_tool_list_starting_values() -> dict[str, list]` — the
    snapshot each list starts from and is diffed against at Save.
    `AgentDetailScreen` uses the MERGED effective value (same "what's
    currently in effect" semantics `_compute_initial_values()` uses for
    every scalar field); `TemplateDetailScreen` uses the template's own raw
    value directly (a template has nothing to merge against — same
    reasoning as its own `_compute_initial_values()` override)
  - call `self._tool_list_state()` at the same points `_compute_initial_values()`
    is called (`__init__`, `_on_enter_edit_mode()`, and after an `inherits:`
    switch, where applicable)
  - route its own `on_button_pressed()` through `_dispatch_tool_list_button()`
    for any button id it doesn't otherwise recognize
  - call `_collect_tool_list_updates(target_entry)` from `action_save()`
  - fold `_any_tool_list_overridden()` into its own `_any_field_overridden()`
    override, if it has one (only relevant where switching `inherits:`
    needs to warn about discarding unsaved tool-list edits too)

Two real Textual gotchas already hardened here (see the method docstrings
below for the full stories): `ListView.index` defaults to `0` — not
`None` — the instant it mounts with any children, and clicking an
already-highlighted item fires `DescendantFocus` but not `Highlighted`.
"""

from __future__ import annotations

from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Label, ListItem, ListView

from ..modals import InlineToolRuleModal, MessageModal, PresetOrInlineModal, TextPromptModal
from .tool_presets import ToolPresetsScreen

# The two tool-list keys this editor handles, and the ListView widget id
# each renders into (kept as one dict, not two, so the shared code below
# never has to enumerate them separately from their ids).
TOOL_LIST_WIDGET_IDS: dict[str, str] = {
    "owner_allowed_tools": "owner-tools-list",
    "guest_allowed_tools": "guest-tools-list",
}


def format_tool_rule(item: object) -> str:
    if isinstance(item, str):
        return f"→ preset: {item}"
    if isinstance(item, dict):
        tool = item.get("tool", "?")
        params = item.get("params")
        return f"{tool} / {params or '(any)'}"
    return str(item)


class ToolListEditorMixin:
    def _init_tool_lists(self) -> None:
        self._tool_lists: dict[str, list] = {}
        self._tool_lists_initial: dict[str, list] = {}
        # Real-Bug-fixed: ListView's own `.index` reactive defaults to 0 the
        # INSTANT it mounts with any children — not None — so "index is not
        # None" cannot distinguish "the user actually selected an item" from
        # "nobody has touched this list yet." Tracked here instead, per
        # list, set True only by a genuine on_list_view_highlighted() event
        # (below) while NOT `_populating` — the same guard on_input_changed()
        # already uses to ignore the initial mount-time burst of Changed
        # events, reused here for the identical reason.
        self._tool_list_ever_selected: dict[str, bool] = dict.fromkeys(TOOL_LIST_WIDGET_IDS, False)

    def _tool_list_starting_values(self) -> dict[str, list]:
        raise NotImplementedError

    def _tool_list_state(self) -> None:
        """(Re)snapshot both tool lists to `_tool_list_starting_values()` —
        `action_save()` (via `_collect_tool_list_updates()`) only writes an
        explicit override if the final local list actually differs from
        this snapshot."""
        starting = self._tool_list_starting_values()
        self._tool_lists = {key: list(starting.get(key) or []) for key in TOOL_LIST_WIDGET_IDS}
        self._tool_lists_initial = {k: list(v) for k, v in self._tool_lists.items()}

    def _any_tool_list_overridden(self) -> bool:
        return any(
            self._tool_lists[key] != self._tool_lists_initial[key]
            for key in TOOL_LIST_WIDGET_IDS
        )

    def _tool_list_items(self, key: str) -> list[ListItem]:
        return [
            ListItem(Label(format_tool_rule(item)), name=str(i))
            for i, item in enumerate(self._tool_lists[key])
        ]

    def _refresh_tool_list(self, key: str) -> None:
        list_view = self.query_one(f"#{TOOL_LIST_WIDGET_IDS[key]}", ListView)
        # PR review finding: captured BEFORE .clear() below (which always
        # resets `.index` to None) so a mutation at some row N > 0 doesn't
        # silently snap the cursor back to row 0 — a user editing/removing
        # several rows in a row, expecting the cursor to roughly track
        # position, would otherwise have every subsequent action land on
        # the WRONG row with no error or visual cue.
        prev_index = list_view.index
        list_view.clear()
        for i, item in enumerate(self._tool_lists[key]):
            list_view.append(ListItem(Label(format_tool_rule(item)), name=str(i)))
        # Re-`.append()`ing items after `.clear()` does NOT restore an
        # auto-selection the way a `ListView` composed WITH its children up
        # front does (see `on_list_view_highlighted()`'s own comment on
        # that distinction — and `tool_presets.py`'s `_refresh_rules()`,
        # which has the exact same fix for the exact same reason). Every
        # Add/Edit/Remove calls this method afterward, so without this, the
        # very first mutation left the list permanently unselected —
        # `_edit_tool_rule()`/`_remove_tool_rule()`'s own `list_view.index is
        # None` guard then silently no-ops on the NEXT action, even though
        # an item is still visibly present, until the user manually
        # arrows/clicks back into the list. Clamped to the new last index
        # when a removal shrank the list out from under the old position;
        # falls back to 0 only when nothing was ever selected to begin with.
        if list_view.children:
            list_view.index = min(prev_index, len(list_view.children) - 1) if prev_index is not None else 0

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

    def _dispatch_tool_list_button(self, button_id: str) -> bool:
        """Returns True if `button_id` was a tool-list Add/Edit/Remove
        button (and has been handled) — False otherwise, so the host
        screen's own `on_button_pressed()` can fall through to whatever
        else it recognizes (e.g. an Inherits "Change…" button)."""
        if button_id.startswith("add-tool-"):
            self._add_tool_rule(button_id.removeprefix("add-tool-"))
            return True
        if button_id.startswith("edit-tool-"):
            self._edit_tool_rule(button_id.removeprefix("edit-tool-"))
            return True
        if button_id.startswith("remove-tool-"):
            self._remove_tool_rule(button_id.removeprefix("remove-tool-"))
            return True
        return False

    @work
    async def _add_tool_rule(self, key: str) -> None:
        """Triggered by the "+ Add" button beside the owner/guest list —
        `key` comes straight from the button's own id (`add-tool-<key>`,
        see `_dispatch_tool_list_button()`), not from focus/keybinding
        guessing."""
        if self.mode == "view" or key not in TOOL_LIST_WIDGET_IDS:
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
                    MessageModal(
                        f"A tool preset named '{name}' already exists.", title="Could not create"
                    )
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

    @work
    async def _edit_tool_rule(self, key: str) -> None:
        """Triggered by the "Edit" button beside the owner/guest list —
        user-reported gap: InlineToolRuleModal already accepted an
        `initial:` dict to pre-fill the form (used nowhere until now), but
        there was no way to actually invoke it for an EXISTING rule — only
        Add (always blank) and Remove existed. Only meaningful for an
        inline rule (a dict); a preset reference (a bare string) has
        nothing to edit here — remove and re-add to point at a different
        preset instead."""
        if self.mode == "view" or key not in TOOL_LIST_WIDGET_IDS:
            return
        list_view = self.query_one(f"#{TOOL_LIST_WIDGET_IDS[key]}", ListView)
        if not self._tool_list_ever_selected.get(key) or list_view.index is None:
            self.notify("Select an item in the list first.", severity="warning")
            return
        idx = list_view.index
        if idx >= len(self._tool_lists[key]):
            return
        item = self._tool_lists[key][idx]
        if not isinstance(item, dict):
            self.notify(
                "Preset references aren't edited here — remove and re-add to "
                "point at a different preset.",
                severity="warning",
            )
            return
        edited = await self.app.push_screen_wait(InlineToolRuleModal(initial=item))
        if edited is None:
            return
        self._tool_lists[key][idx] = edited
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
        if self.mode == "view" or key not in TOOL_LIST_WIDGET_IDS:
            return
        list_view = self.query_one(f"#{TOOL_LIST_WIDGET_IDS[key]}", ListView)
        if not self._tool_list_ever_selected.get(key) or list_view.index is None:
            self.notify("Select an item in the list first.", severity="warning")
            return
        idx = list_view.index
        if idx >= len(self._tool_lists[key]):
            return
        del self._tool_lists[key][idx]
        self._form_dirty = True
        self._refresh_tool_list(key)

    def _compose_tool_list_widget(self, key: str) -> ComposeResult:
        """Yields the ListView + "+ Add"/"- Remove" Buttons for one tool
        list — NOT the bold header line above it, which callers compose
        themselves (AgentDetailScreen's is a plain label; TemplateDetailScreen's
        also shows a blast-radius count, matching every other field's row)."""
        list_view = ListView(*self._tool_list_items(key), id=TOOL_LIST_WIDGET_IDS[key])
        # Tagged so on_list_view_highlighted()/on_descendant_focus() above
        # can map the event back to which of the two lists it is, without
        # unmunging the widget id.
        list_view.tool_list_key = key
        yield list_view
        with Horizontal(classes="tool-list-buttons"):
            yield Button("+ Add", id=f"add-tool-{key}")
            yield Button("Edit", id=f"edit-tool-{key}")
            yield Button("- Remove", id=f"remove-tool-{key}")

    def _collect_tool_list_updates(self, target_entry: dict) -> None:
        """Diffs the FINAL local list against the snapshot `_tool_list_state()`
        took when the form opened, writing directly into `target_entry`.
        Untouched stays untouched (no key written at all, preserving
        whatever explicit/inherited state the entry already had); a
        genuinely changed list is always written in full, as an explicit
        override (never popped back to "unset" on empty — explicitly
        narrowing to zero allowed tools is meaningfully different from
        never having set the key at all)."""
        for key in TOOL_LIST_WIDGET_IDS:
            if self._tool_lists[key] != self._tool_lists_initial[key]:
                target_entry[key] = list(self._tool_lists[key])
