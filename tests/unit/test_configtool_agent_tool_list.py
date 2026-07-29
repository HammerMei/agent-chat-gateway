"""Pilot-based tests for AgentDetailScreen's owner/guest_allowed_tools
editor — the per-agent half of the tool-list editor (docs/design/
config-tool.md's tool-list-editor work; ToolPresetsScreen's own add/delete-
rule flow and OverviewScreen's create/delete-preset actions have their own
test file, tests/unit/test_configtool_tool_presets.py).

Pins the same "editing an inherited field always writes an explicit
per-entry override, untouched stays untouched" contract
test_configtool_agent_crud.py already pins for scalar fields — tool lists
live outside that generic diffing machinery (see agent_detail.py's module
docstring), so they need their own regression coverage for it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Input, Label, ListView

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import InlineToolRuleModal, PresetOrInlineModal, TextPromptModal
from gateway.configtool.screens.agent_detail import AgentDetailScreen
from gateway.configtool.screens.tool_presets import ToolPresetsScreen


def _write_config(tmp_path: Path, yaml_text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(yaml_text))
    return str(path)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _config_text(work_dir: Path) -> str:
    """agent-a's owner_allowed_tools: [preset-a] comes purely from its
    inherits: template (nothing explicit on the agent entry itself) — the
    config TUI's tool-list prefill correctly resolves this via the real
    agent_templates:/inherits: mechanism (EditableConfig.merged_entry()),
    so an inherited starting list is exactly what these tests exercise:
    "untouched" must mean it stays inherited, not that it gets rewritten
    explicitly onto the agent. preset-b exists but is unreferenced by
    anyone, available for the "reference an existing preset" tests."""
    return f"""\
        tool_presets:
          preset-a:
            - tool: Bash
          preset-b:
            - tool: WebFetch
        agent_templates:
          standard:
            type: claude
            owner_allowed_tools: [preset-a]
        agents:
          agent-a:
            inherits: standard
            working_directory: {work_dir}
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - connector: rc
            agent: agent-a
            room: general
    """


async def _open_agent_edit(pilot, app, row: int = 0) -> None:
    app.screen.query_one("TabbedContent").active = "tab-agents"
    await pilot.pause()
    table = app.screen.query_one("#agents-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("e")
    await pilot.pause()


async def _click_tool_button(pilot, app, action: str, key: str = "owner_allowed_tools") -> None:
    """Click the "+ Add"/"- Remove" button beside a tool list — `action` is
    "add" or "remove". Scrolls the button into view first: the form can be
    taller than the (headless) test terminal, and Pilot.click() raises
    OutOfBounds for a widget that's currently scrolled off-screen."""
    button = app.screen.query_one(f"#{action}-tool-{key}")
    button.scroll_visible(animate=False)
    await pilot.pause()
    await pilot.click(f"#{action}-tool-{key}")
    await pilot.pause()


class TestAgentToolListDisplay:
    async def test_edit_mode_prefills_owner_tools_with_the_effective_value(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)
            assert isinstance(app.screen, AgentDetailScreen)
            list_view = app.screen.query_one("#owner-tools-list", ListView)
            assert len(list_view.children) == 1
            label_text = str(list_view.children[0].query_one(Label).render())
            assert "preset-a" in label_text

    async def test_add_remove_buttons_do_not_exist_in_view_mode(self, tmp_path, work_dir):
        """View mode only ever composes body text (`_body_text()`), never
        the form — so the Add/Remove buttons (and the whole tool-list
        editor) simply don't exist there at all, rather than existing but
        being disabled/no-op."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            table = app.screen.query_one("#agents-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "view"
            assert not app.screen.query("#add-tool-owner_allowed_tools")
            assert not app.screen.query("#owner-tools-list")


class TestAgentToolListSave:
    async def test_untouched_tool_list_writes_no_explicit_override(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert "owner_allowed_tools" not in raw["agents"]["agent-a"]

    async def test_remove_without_ever_selecting_an_item_is_a_no_op(self, tmp_path, work_dir):
        """Regression: ListView's own `.index` reactive defaults to 0 the
        instant it mounts with children — not None — so clicking "- Remove"
        as the very first action (before ever clicking/arrow-keying into
        the list) used to silently delete item 0 with no warning."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            assert len(list_view.children) == 1

            await _click_tool_button(pilot, app, "remove")

            assert len(list_view.children) == 1  # untouched

    async def test_removing_the_only_item_writes_an_explicit_empty_override(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            # A real click (not just setting .index programmatically) — the
            # item is already highlighted by default, so this is exactly
            # the "click the already-highlighted item" case
            # on_descendant_focus() (agent_detail.py) exists to still count
            # as a genuine selection.
            list_view.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click(list_view.children[0])
            await pilot.pause()
            await _click_tool_button(pilot, app, "remove")
            assert len(list_view.children) == 0

            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            # Explicit empty list, NOT absent — a deliberate "no tools
            # allowed" is different from "never set, inherit defaults".
            assert raw["agents"]["agent-a"]["owner_allowed_tools"] == []

    async def test_referencing_an_existing_preset(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            await _click_tool_button(pilot, app, "add")
            assert isinstance(app.screen, PresetOrInlineModal)
            # Sorted preset names: preset-a (0, already referenced),
            # preset-b (1), then the two fixed actions.
            await pilot.press("down", "enter")
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agents"]["agent-a"]["owner_allowed_tools"] == ["preset-a", "preset-b"]

    async def test_writing_an_inline_rule(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            await _click_tool_button(pilot, app, "add")
            # preset-a (0), preset-b (1), inline (2), new_preset (3).
            await pilot.press("down", "down", "enter")
            await pilot.pause()

            assert isinstance(app.screen, InlineToolRuleModal)
            app.screen.query_one("#rule-tool", Input).value = "Edit"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agents"]["agent-a"]["owner_allowed_tools"] == [
                "preset-a",
                {"tool": "Edit"},
            ]

    async def test_cancelling_the_add_flow_leaves_the_list_untouched(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            list_view = app.screen.query_one("#owner-tools-list", ListView)

            await _click_tool_button(pilot, app, "add")
            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            assert len(list_view.children) == 1

            await pilot.press("ctrl+s")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            # Untouched means exactly that -- the inherited value (from
            # agent_templates.standard) is neither cleared nor written
            # explicitly onto the agent.
            assert "owner_allowed_tools" not in raw["agents"]["agent-a"]

    async def test_creating_a_new_preset_detours_to_tool_presets_screen(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            list_view = app.screen.query_one("#owner-tools-list", ListView)

            await _click_tool_button(pilot, app, "add")
            # preset-a (0), preset-b (1), inline (2), new_preset (3).
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()

            assert isinstance(app.screen, TextPromptModal)
            app.screen.query_one("#prompt-input", Input).value = "preset-c"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)
            assert app.screen.preset_name == "preset-c"

            # The one-way detour never touched the owner_allowed_tools list
            # back on the agent form still underneath on the stack.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            assert len(list_view.children) == 1


class TestAgentToolListEdit:
    """User-reported gap: only Add/Remove existed for an individual
    owner/guest_allowed_tools rule — no way to edit an inline rule in place
    short of removing and re-adding it."""

    async def test_editing_an_inline_rule_replaces_it_in_place(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            # Add an inline rule first (preset-a (0) is a preset reference,
            # not editable here) — same flow as test_writing_an_inline_rule.
            await _click_tool_button(pilot, app, "add")
            await pilot.press("down", "down", "enter")  # inline (index 2)
            await pilot.pause()
            app.screen.query_one("#rule-tool", Input).value = "Edit"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            list_view.focus()
            list_view.index = 1  # the just-added {tool: Edit} inline rule
            await pilot.pause()

            await _click_tool_button(pilot, app, "edit")
            await pilot.pause()
            assert isinstance(app.screen, InlineToolRuleModal)
            assert app.screen.query_one("#rule-tool", Input).value == "Edit"  # pre-filled
            app.screen.query_one("#rule-tool", Input).value = "Read"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            assert len(list_view.children) == 2  # replaced, not appended

            await pilot.press("ctrl+s")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agents"]["agent-a"]["owner_allowed_tools"] == [
                "preset-a",
                {"tool": "Read"},
            ]

    async def test_selection_survives_a_refresh_not_just_initial_selection(
        self, tmp_path, work_dir
    ):
        """PR review finding: `_refresh_tool_list()` (called by every Add/
        Edit/Remove) does `ListView.clear()`, which resets `.index` back to
        `None` — re-`.append()`ing items afterward does NOT restore an
        auto-selection. Without a fix inside `_refresh_tool_list()` itself
        (mirroring the identical fix in tool_presets.py's
        `_refresh_rules()`), the FIRST mutation left the list permanently
        unselected: the very next Remove/Edit would silently no-op on
        `list_view.index is None`, even with an item still visibly present.
        Reproduced here with NO manual `list_view.index = ...` in between —
        exactly the gap a prior version of this fix missed."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            await _click_tool_button(pilot, app, "add")
            await pilot.press("down", "down", "enter")  # inline (index 2)
            await pilot.pause()
            app.screen.query_one("#rule-tool", Input).value = "Edit"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            assert len(list_view.children) == 2

            # No manual reselect — Remove should still act, not silently
            # no-op with "Select an item in the list first."
            await _click_tool_button(pilot, app, "remove")
            assert len(list_view.children) == 1

    async def test_editing_a_preset_reference_notifies_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            # A real click (not just setting .index programmatically) — the
            # item is already highlighted by default (index 0), so this is
            # exactly the "click the already-highlighted item" case
            # on_descendant_focus() exists to still count as a genuine
            # selection (see test_removing_the_only_item_writes_an_explicit_
            # empty_override's identical comment).
            list_view.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click(list_view.children[0])  # 'preset-a' — a bare string reference
            await pilot.pause()

            await _click_tool_button(pilot, app, "edit")
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)  # no crash
            assert len(list_view.children) == 1  # untouched


class TestAgentToolListCursorPositionAfterMutation:
    """PR review finding: the row-0 auto-select fix (in _refresh_tool_list(),
    see test_selection_survives_a_refresh_not_just_initial_selection) used
    to fire unconditionally on EVERY refresh — not just when nothing was
    selected — silently snapping the cursor back to row 0 after every
    single mutation. A user editing/removing several rows in sequence,
    expecting the cursor to roughly track position, would have every
    subsequent action land on the WRONG row with no error or visual cue."""

    async def _add_inline_rule(self, pilot, app, tool_name: str) -> None:
        await _click_tool_button(pilot, app, "add")
        await pilot.press("down", "down", "enter")  # inline (index 2)
        await pilot.pause()
        app.screen.query_one("#rule-tool", Input).value = tool_name
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    async def test_editing_the_last_row_keeps_the_cursor_on_it(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            # preset-a (0) is already there; add two more inline rules.
            await self._add_inline_rule(pilot, app, "Read")
            await self._add_inline_rule(pilot, app, "Write")

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            assert len(list_view.children) == 3
            list_view.index = 2  # 'Write', the last row

            await _click_tool_button(pilot, app, "edit")
            await pilot.pause()
            app.screen.query_one("#rule-tool", Input).value = "Write2"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            assert len(list_view.children) == 3  # not appended/removed
            assert list_view.index == 2  # cursor stayed put, not reset to 0

    async def test_removing_a_middle_row_clamps_the_cursor_sensibly(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            # preset-a (0) is already there; add two more inline rules.
            await self._add_inline_rule(pilot, app, "Read")
            await self._add_inline_rule(pilot, app, "Write")

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            assert len(list_view.children) == 3
            list_view.index = 1  # 'Read', the middle row

            await _click_tool_button(pilot, app, "remove")

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            assert len(list_view.children) == 2  # 'preset-a' and 'Write' remain
            # index 1 is still a valid position (now 'Write', which slid up
            # into the removed row's slot) — not reset to 0.
            assert list_view.index == 1
