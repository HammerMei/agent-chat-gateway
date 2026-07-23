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
    """agent-a has no owner_allowed_tools of its own — it inherits
    [preset-a] from agent_defaults. preset-b exists but is unreferenced by
    anyone, available for the "reference an existing preset" tests."""
    return f"""\
        tool_presets:
          preset-a:
            - tool: Bash
          preset-b:
            - tool: WebFetch
        agent_defaults:
          type: claude
          owner_allowed_tools: [preset-a]
        agents:
          agent-a:
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


class TestAgentToolListDisplay:
    async def test_edit_mode_prefills_owner_tools_with_the_inherited_value(
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

    async def test_a_and_x_are_no_ops_in_view_mode(self, tmp_path, work_dir):
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
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)  # no modal pushed
            assert app.screen.mode == "view"


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

    async def test_removing_the_only_item_writes_an_explicit_empty_override(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            list_view.focus()
            list_view.index = 0
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
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

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            list_view.focus()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
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

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            list_view.focus()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
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
            list_view.focus()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            assert len(list_view.children) == 1

            await pilot.press("ctrl+s")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "owner_allowed_tools" not in raw["agents"]["agent-a"]

    async def test_creating_a_new_preset_detours_to_tool_presets_screen(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_edit(pilot, app)

            list_view = app.screen.query_one("#owner-tools-list", ListView)
            list_view.focus()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
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
