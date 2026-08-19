"""ToolPresetsScreen — view and edit one named tool_presets entry (add/
edit/remove individual rules). Deleting the WHOLE preset happens from the
Overview's Tool Presets tab directly (`d`, mirroring the Connectors/Agents
list's direct-delete shortcut — see OverviewScreen.action_delete_row()) —
this screen only edits ONE existing (or not-yet-materialized) preset's rule
list.

Presets are global/shared across every agent, structurally flat (a preset's
own rule list may only contain inline rules — gateway/config.py's
_parse_tool_presets rejects a preset referencing another preset), so there
is no separate "edit mode" the way AgentDetailScreen/ConnectorDetailScreen
need one for provenance-tracked scalar fields: every add/edit/remove here is
a direct, immediately-saved mutation (validate-before-write via
EditableConfig.save(), same rollback-on-failure idiom every other mutation
in this app uses — see _do_delete() in form_common.py) — matching the
simplicity of a bare list of rules. `action_edit_rule()` replaces the rule
at the selected index in place (via InlineToolRuleModal(initial=...),
pre-filled — previously unused anywhere, the only way to change an existing
rule was delete-then-re-add).

A brand-new preset (pushed by OverviewScreen.action_new_entity() before
`tool_presets[name]` exists yet in the document) is handled naturally:
`tool_presets_raw.get(name, [])` is empty until the FIRST rule is actually
added, at which point action_add_rule()'s `setdefault(name, [])` creates the
entry — so escaping out of a "new preset" flow before adding anything at all
leaves no trace in the document, no separate rollback path needed for that
case.

Because add/edit/delete-rule here never pop this screen (unlike every other
mutation in the app, which pops back to Overview and calls
`app.reload_config()` to repaint it), OverviewScreen.on_screen_resume()
repaints from memory whenever Overview becomes the active screen again —
covers the "user added/removed rules, then pressed Escape" case without
this screen needing to reach into Overview itself.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ..formatting import markup_safe
from ..modals import InlineToolRuleModal, MessageModal
from ..model import EditableConfig
from .base import DetailScreen
from .form_common import find_agents_referencing_preset


def _format_tool_rule(rule: object) -> str:
    if isinstance(rule, dict):
        tool = rule.get("tool", "?")
        params = rule.get("params")
        return f"{tool} / {params or '(any)'}"
    return str(rule)


class ToolPresetsScreen(DetailScreen):
    BODY_ID = "preset-detail-body"

    BINDINGS = [
        *DetailScreen.BINDINGS,
        Binding("a", "add_rule", "Add rule", show=True),
        Binding("e", "edit_rule", "Edit rule", show=True),
        Binding("d", "delete_rule", "Delete rule", show=True),
    ]

    def __init__(self, cfg: EditableConfig, preset_name: str):
        super().__init__()
        self.cfg = cfg
        self.preset_name = preset_name

    def _header_text(self) -> str:
        rules = self.cfg.tool_presets_raw.get(self.preset_name, [])
        used_by = find_agents_referencing_preset(self.cfg, self.preset_name)
        lines = [f"[bold]{markup_safe(self.preset_name)}[/bold]  ({len(rules)} rule(s))"]
        lines.append(
            f"used by: {', '.join(markup_safe(u) for u in used_by)}"
            if used_by
            else "used by: (no agent references it)"
        )
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static(self._header_text(), id=self.BODY_ID)
            yield ListView(id="preset-rules-list")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_rules()
        # User-reported: landing here required an explicit Tab press before
        # 'e'/'d' (or arrow keys) did anything, since DOM focus starts on
        # nothing in particular — Header/Footer aren't focusable and the
        # ListView doesn't auto-focus itself just by existing. Focusing it
        # directly is harmless even when the preset has zero rules yet (a
        # focused, empty ListView is a normal, valid state — 'a' still
        # works to add the first one). The row-0 selection itself (the
        # OTHER half of this fix) lives in `_refresh_rules()` below, not
        # here — see its own comment for why.
        self.query_one("#preset-rules-list", ListView).focus()

    def _refresh_rules(self) -> None:
        rules = self.cfg.tool_presets_raw.get(self.preset_name, [])
        list_view = self.query_one("#preset-rules-list", ListView)
        # PR review finding: captured BEFORE .clear() below (which always
        # resets `.index` to None) so a mutation at some row N > 0 doesn't
        # silently snap the cursor back to row 0 — a user editing/deleting
        # several rules in a row, expecting the cursor to roughly track
        # position, would otherwise have every subsequent action land on
        # the WRONG row with no error or visual cue.
        prev_index = list_view.index
        list_view.clear()
        for i, rule in enumerate(rules):
            # Label parses markup, and a tool rule is operator-authored
            # (a regex may legitimately contain `[...]`).
            list_view.append(
                ListItem(Label(markup_safe(_format_tool_rule(rule))), name=str(i))
            )
        self.query_one(f"#{self.BODY_ID}", Static).update(self._header_text())
        # This must be done HERE, every time — not just once in on_mount()
        # — because `list_view.clear()` above resets `.index` back to
        # `None`, and re-`.append()`ing items afterward does not restore an
        # auto-selection the way a `ListView` composed WITH its children up
        # front would get once, at ITS OWN mount (see tool_list_editor.py's
        # own comment on that distinction). Since every mutation (add/edit/
        # delete) calls this same method afterward, doing this only in
        # on_mount() fixed the FIRST entry into this screen but let the
        # exact same "nothing selected, 'e'/'d' silently no-op" bug
        # reappear after the very first add/edit/delete. Clamped to the new
        # last index when a delete shrank the list out from under the old
        # position; falls back to 0 only when nothing was ever selected.
        if list_view.children:
            list_view.index = min(prev_index, len(list_view.children) - 1) if prev_index is not None else 0

    @work
    async def action_add_rule(self) -> None:
        rule = await self.app.push_screen_wait(InlineToolRuleModal())
        if rule is None:
            return
        presets = self.cfg.document.setdefault("tool_presets", {})
        existed_before = self.preset_name in presets
        rules = presets.setdefault(self.preset_name, [])
        rules.append(rule)
        self.cfg.mark_dirty()
        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            rules.pop()
            if not existed_before and not rules:
                del presets[self.preset_name]
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return
        self._refresh_rules()
        self.app.notify(f"Added a rule to '{self.preset_name}'.", severity="information")

    @work
    async def action_edit_rule(self) -> None:
        """User-reported gap: only Add/Delete existed for an individual
        rule — no way to edit one in place short of deleting and re-adding
        it. InlineToolRuleModal already accepted an `initial:` dict to
        pre-fill the form (unused until now)."""
        list_view = self.query_one("#preset-rules-list", ListView)
        if list_view.index is None:
            self.app.notify("No rule selected.", severity="warning")
            return
        idx = list_view.index
        presets = self.cfg.document.get("tool_presets", {})
        rules = presets.get(self.preset_name, [])
        if idx >= len(rules):
            return
        original = rules[idx]
        edited = await self.app.push_screen_wait(InlineToolRuleModal(initial=original))
        if edited is None:
            return
        rules[idx] = edited
        self.cfg.mark_dirty()
        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            rules[idx] = original
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return
        self._refresh_rules()
        self.app.notify(f"Updated a rule in '{self.preset_name}'.", severity="information")

    @work
    async def action_delete_rule(self) -> None:
        list_view = self.query_one("#preset-rules-list", ListView)
        if list_view.index is None:
            self.app.notify("No rule selected.", severity="warning")
            return
        idx = list_view.index
        presets = self.cfg.document.get("tool_presets", {})
        rules = presets.get(self.preset_name, [])
        if idx >= len(rules):
            return
        removed = rules.pop(idx)
        self.cfg.mark_dirty()
        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            rules.insert(idx, removed)
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return
        self._refresh_rules()
        self.app.notify("Rule removed.", severity="information")
