"""Pilot-based tests for OverviewScreen's direct edit/delete shortcuts —
'e'/'d' acting on the row under the cursor on the Connectors/Agents tabs,
without first selecting into a view-mode detail screen.

User-reported UX gap: 'e' on the list page used to be shadowed by
OverviewScreen's OWN 'e' binding for the $EDITOR escape hatch (now
ctrl+e) — pressing 'e' hoping to edit the selected connector/agent instead
opened $EDITOR on the whole config.yaml.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Input

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal, TextPromptModal
from gateway.configtool.screens.agent_detail import AgentDetailScreen
from gateway.configtool.screens.connector_detail import ConnectorDetailScreen
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


def _config_with_two_connectors(work_dir: Path) -> str:
    """'rc-referenced' is used by the watcher; 'rc-orphan' is not."""
    return f"""\
        agents:
          default:
            type: claude
            working_directory: {work_dir}
        connectors:
          - name: rc-referenced
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
          - name: rc-orphan
            type: rocketchat
            server: {{url: "http://localhost:3001", username: bot2, password: pw2}}
        watchers:
          - connector: rc-referenced
            agent: default
            room: general
    """


def _config_with_two_agents(work_dir: Path) -> str:
    """'existing-agent' is used by the watcher; 'unused-agent' is not."""
    return f"""\
        agents:
          existing-agent:
            type: claude
            working_directory: {work_dir}
          unused-agent:
            type: claude
            working_directory: {work_dir}
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - connector: rc
            agent: existing-agent
            room: general
    """


def _config_with_a_preset(work_dir: Path) -> str:
    return f"""\
        tool_presets:
          preset-a:
            - tool: Bash
        agents:
          default:
            type: claude
            working_directory: {work_dir}
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - connector: rc
            agent: default
            room: general
    """


class TestDirectEditFromConnectorsList:
    async def test_e_on_connectors_tab_opens_edit_mode_directly(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # rc-orphan (sorted before rc-referenced)

            await pilot.press("e")
            await pilot.pause()

            assert isinstance(app.screen, ConnectorDetailScreen)
            assert app.screen.mode == "edit"
            assert app.screen.entry.get("name") == "rc-orphan"

    async def test_escape_from_direct_edit_pops_straight_to_the_list(self, tmp_path, work_dir):
        """Skipping view mode entirely means there's no view state to fall
        back to — Escape must pop back to the list, not flip to a view
        rendering of a screen the user never asked to see."""
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # rc-orphan (sorted before rc-referenced)
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)

            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)

    async def test_saving_from_direct_edit_returns_to_the_list(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # rc-orphan (sorted before rc-referenced)
            await pilot.press("e")
            await pilot.pause()

            from textual.widgets import Input

            app.screen.query_one("#field-timezone", Input).value = "America/Denver"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            names = {c["name"]: c for c in raw["connectors"]}
            assert names["rc-orphan"]["timezone"] == "America/Denver"

    async def test_e_is_a_no_op_when_config_does_not_load(self, tmp_path):
        config_path = str(tmp_path / "does-not-exist.yaml")
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("e")  # must not raise
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)


class TestDirectEditFromAgentsList:
    async def test_e_on_agents_tab_opens_edit_mode_directly(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_agents(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            table = app.screen.query_one("#agents-table", DataTable)
            table.focus()
            # dict order: existing-agent, unused-agent
            table.move_cursor(row=1)

            await pilot.press("e")
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "edit"
            assert app.screen.agent_name == "unused-agent"


class TestDirectEditFromPresetsList:
    """User-requested, for consistency with every other tab having an 'e'
    shortcut: ToolPresetsScreen has no separate view/edit mode at all (see
    its own module docstring), so 'e' here is just an alias for Enter —
    unlike the Connectors/Agents tabs' 'e', which skips a real view-mode
    detour."""

    async def test_e_on_presets_tab_opens_the_same_screen_as_enter(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_a_preset(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()
            table = app.screen.query_one("#presets-table", DataTable)
            table.focus()
            table.move_cursor(row=0)

            await pilot.press("e")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)
            assert app.screen.preset_name == "preset-a"


class TestEditDeleteVisibleOnAllTabs:
    """Config TUI Phase 3: watcher edit/delete are now supported too — every
    tab (Connectors/Agents/Watchers/Templates/Tool Presets) advertises 'e'/
    'd' in the footer. There is no longer an "unsupported tab" for either
    action."""

    async def test_e_and_d_are_visible_on_watchers_tab(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-watchers"
            await pilot.pause()

            assert app.screen.check_action("edit_row", ()) is True
            assert app.screen.check_action("delete_row", ()) is True

    async def test_e_and_d_are_visible_on_connectors_tab(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.check_action("edit_row", ()) is True
            assert app.screen.check_action("delete_row", ()) is True

    async def test_e_and_d_are_visible_on_presets_tab(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_a_preset(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-presets"
            await pilot.pause()

            assert app.screen.check_action("edit_row", ()) is True
            assert app.screen.check_action("delete_row", ()) is True


class TestDirectDeleteFromConnectorsList:
    async def test_d_shows_confirm_modal_for_an_unreferenced_connector(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # rc-orphan (sorted before rc-referenced)

            await pilot.press("d")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)

    async def test_cancelling_direct_delete_returns_to_the_list(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # rc-orphan (sorted before rc-referenced)

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")  # Cancel is focused by default
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            names = {c.get("name") for c in app.editable_config.connectors_raw}
            assert "rc-orphan" in names

    async def test_confirming_direct_delete_removes_the_connector_and_returns_to_the_list(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # rc-orphan (sorted before rc-referenced)

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            names = {c["name"] for c in raw["connectors"]}
            assert "rc-orphan" not in names
            assert "rc-referenced" in names

    async def test_direct_delete_of_a_referenced_connector_is_blocked_and_returns_to_the_list(
        self, tmp_path, work_dir
    ):
        """Blocked-by-referencing-watcher path: FormScreen.action_delete()
        shows a MessageModal and leaves the screen in place (correct for
        its own view-mode entry point) — reached directly from the list,
        dismissing that modal must send the user back to the list, not
        strand them on a view-mode screen they never asked to see."""
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # rc-referenced (sorted after rc-orphan)

            await pilot.press("d")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "rc-referenced" in body
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            names = {c.get("name") for c in app.editable_config.connectors_raw}
            assert "rc-referenced" in names


class TestDirectDeleteFromAgentsList:
    async def test_confirming_direct_delete_removes_the_agent_and_returns_to_the_list(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_agents(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            table = app.screen.query_one("#agents-table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # unused-agent

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "unused-agent" not in raw["agents"]
            assert "existing-agent" in raw["agents"]


class TestDirectCloneFromWatchersList:
    """'c' on the Watchers tab: run the row under the cursor's own "Clone
    for rooms" bulk-add directly, no "open the watcher first" detour —
    user-requested, code-review finding: this shortcut shipped with no
    dedicated test of its own (only WatcherDetailScreen's own 'c' binding,
    reached after already opening a watcher, was covered)."""

    async def test_cloning_directly_from_the_list_adds_rooms_without_opening_the_watcher(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-watchers"
            await pilot.pause()
            table = app.screen.query_one("#watchers-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # only one watcher: rc-referenced/general

            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptModal)
            app.screen.query_one("#prompt-input", Input).value = "dev, ops"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {
                    "connector": "rc-referenced", "agent": "default",
                    "rooms": ["general", "dev", "ops"],
                }
            ]

    async def test_cancelling_the_clone_prompt_leaves_the_list_untouched(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-watchers"
            await pilot.pause()
            table = app.screen.query_one("#watchers-table", DataTable)
            table.focus()
            table.move_cursor(row=0)

            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptModal)
            await pilot.press("escape")
            await pilot.pause()

            # Cancelled at the prompt — WatcherDetailScreen (pushed silently
            # underneath) never asked-for by the user; back to the list.
            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {"connector": "rc-referenced", "agent": "default", "room": "general"}
            ]

    async def test_clone_hidden_on_a_non_watchers_tab(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("TabbedContent").active == "tab-connectors"
            assert app.screen.check_action("clone_for_rooms", ()) is False

            app.screen.query_one("TabbedContent").active = "tab-watchers"
            await pilot.pause()
            assert app.screen.check_action("clone_for_rooms", ()) is True
