"""Pilot-based tests for the tool-list editor's preset-level pieces:
ToolPresetsScreen's own add/delete-rule flow, plus OverviewScreen's direct
create/delete-preset actions on the Tool Presets tab (docs/design/
config-tool.md's tool-list-editor work).

The per-agent owner/guest_allowed_tools editor (AgentDetailScreen) has its
own test file, tests/unit/test_configtool_agent_tool_list.py.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Input, ListView, Static

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import (
    ConfirmModal,
    InlineToolRuleModal,
    MessageModal,
    TextPromptModal,
)
from gateway.configtool.screens.overview import OverviewScreen
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
    """preset-a is referenced by agent-a (owner_allowed_tools); preset-b is
    unreferenced by anything — used by the delete tests to cover both the
    blocked and the allowed path."""
    return f"""\
        tool_presets:
          preset-a:
            - tool: Bash
              params: "ls .*"
          preset-b:
            - tool: WebFetch
        agents:
          agent-a:
            working_directory: {work_dir}
            owner_allowed_tools: [preset-a]
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - connector: rc
            agent: agent-a
            room: general
    """


async def _open_preset_detail(pilot, app, row: int = 0) -> None:
    app.screen.query_one("TabbedContent").active = "tab-presets"
    await pilot.pause()
    table = app.screen.query_one("#presets-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press("enter")
    await pilot.pause()


class TestToolPresetsScreenView:
    async def test_selecting_a_preset_row_shows_its_rules_and_used_by(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)
            assert isinstance(app.screen, ToolPresetsScreen)
            assert app.screen.preset_name == "preset-a"
            list_view = app.screen.query_one("#preset-rules-list", ListView)
            assert len(list_view.children) == 1
            header = str(app.screen.query_one("#preset-detail-body", Static).render())
            assert "agent-a" in header


class TestToolPresetsScreenAddRule:
    async def test_add_rule_persists_to_disk_and_shows_in_the_list(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, InlineToolRuleModal)
            app.screen.query_one("#rule-tool", Input).value = "Edit"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)
            list_view = app.screen.query_one("#preset-rules-list", ListView)
            assert len(list_view.children) == 2

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["tool_presets"]["preset-a"][-1] == {"tool": "Edit"}

    async def test_invalid_tool_regex_shows_inline_error_and_does_not_dismiss(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            await pilot.press("a")
            await pilot.pause()
            app.screen.query_one("#rule-tool", Input).value = "["  # unbalanced regex
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, InlineToolRuleModal)  # still open
            error_text = str(app.screen.query_one("#rule-error", Static).render())
            assert "Invalid tool regex" in error_text

            # Never touched disk.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["tool_presets"]["preset-a"]) == 1

    async def test_cancelling_the_rule_modal_leaves_the_preset_untouched(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            await pilot.press("a")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["tool_presets"]["preset-a"]) == 1


class TestToolPresetsScreenDeleteRule:
    async def test_delete_rule_persists_the_removal(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            list_view.focus()
            list_view.index = 0
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()

            assert len(list_view.children) == 0
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["tool_presets"]["preset-a"] == []

    async def test_delete_with_nothing_selected_notifies_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        text = _config_text(work_dir).replace(
            "tool_presets:\n          preset-a:\n            - tool: Bash\n"
            '              params: "ls .*"\n',
            "tool_presets:\n          preset-a: []\n",
        )
        config_path = _write_config(tmp_path, text)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            await pilot.press("d")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)  # no crash


class TestOverviewCreatePreset:
    async def test_n_prompts_for_a_name_and_pushes_the_editor(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()

            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptModal)

            app.screen.query_one("#prompt-input", Input).value = "preset-c"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)
            assert app.screen.preset_name == "preset-c"

    async def test_escaping_a_new_preset_before_adding_a_rule_leaves_no_trace(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "preset-c"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ToolPresetsScreen)

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

            raw = yaml.safe_load(Path(config_path).read_text())
            assert "preset-c" not in raw["tool_presets"]

    async def test_duplicate_preset_name_shows_an_error_and_does_not_navigate(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "preset-a"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)  # never navigated


class TestOverviewDeletePreset:
    async def test_delete_blocked_when_used_by_an_agent(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()
            table = app.screen.query_one("#presets-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # preset-a, used by agent-a
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body", Static).render())
            assert "agent-a" in body
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

            raw = yaml.safe_load(Path(config_path).read_text())
            assert "preset-a" in raw["tool_presets"]

    async def test_delete_confirmed_when_unused_removes_it(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()
            table = app.screen.query_one("#presets-table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # preset-b, unused
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "preset-b" not in raw["tool_presets"]

    async def test_cancelling_the_delete_confirm_leaves_it_untouched(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()
            table = app.screen.query_one("#presets-table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # preset-b, unused
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")  # Cancel is focused by default
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "preset-b" in raw["tool_presets"]
