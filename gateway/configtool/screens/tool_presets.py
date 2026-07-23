"""ToolPresetsScreen — view and edit one named tool_presets entry (add/
remove individual rules). Deleting the WHOLE preset happens from the
Overview's Tool Presets tab directly (`d`, mirroring the Connectors/Agents
list's direct-delete shortcut — see OverviewScreen.action_delete_row()) —
this screen only edits ONE existing (or not-yet-materialized) preset's rule
list.

Presets are global/shared across every agent, structurally flat (a preset's
own rule list may only contain inline rules — gateway/config.py's
_parse_tool_presets rejects a preset referencing another preset), so there
is no separate "edit mode" the way AgentDetailScreen/ConnectorDetailScreen
need one for provenance-tracked scalar fields: every add/remove here is a
direct, immediately-saved mutation (validate-before-write via
EditableConfig.save(), same rollback-on-failure idiom every other mutation
in this app uses — see _do_delete() in form_common.py) — matching the
simplicity of a bare list of rules.

A brand-new preset (pushed by OverviewScreen.action_new_entity() before
`tool_presets[name]` exists yet in the document) is handled naturally:
`tool_presets_raw.get(name, [])` is empty until the FIRST rule is actually
added, at which point action_add_rule()'s `setdefault(name, [])` creates the
entry — so escaping out of a "new preset" flow before adding anything at all
leaves no trace in the document, no separate rollback path needed for that
case.

Because add/delete-rule here never pop this screen (unlike every other
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
        Binding("d", "delete_rule", "Delete rule", show=True),
    ]

    def __init__(self, cfg: EditableConfig, preset_name: str):
        super().__init__()
        self.cfg = cfg
        self.preset_name = preset_name

    def _header_text(self) -> str:
        rules = self.cfg.tool_presets_raw.get(self.preset_name, [])
        used_by = find_agents_referencing_preset(self.cfg, self.preset_name)
        lines = [f"[bold]{self.preset_name}[/bold]  ({len(rules)} rule(s))"]
        lines.append(
            f"used by: {', '.join(used_by)}" if used_by else "used by: (no agent references it)"
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

    def _refresh_rules(self) -> None:
        rules = self.cfg.tool_presets_raw.get(self.preset_name, [])
        list_view = self.query_one("#preset-rules-list", ListView)
        list_view.clear()
        for i, rule in enumerate(rules):
            list_view.append(ListItem(Label(_format_tool_rule(rule)), name=str(i)))
        self.query_one(f"#{self.BODY_ID}", Static).update(self._header_text())

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
