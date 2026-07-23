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
from textual.widgets import DataTable

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal
from gateway.configtool.screens.agent_detail import AgentDetailScreen
from gateway.configtool.screens.connector_detail import ConnectorDetailScreen
from gateway.configtool.screens.overview import OverviewScreen


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


class TestDirectEditFromConnectorsList:
    async def test_e_on_connectors_tab_opens_edit_mode_directly(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # rc-orphan

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
            table.move_cursor(row=1)
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
            table.move_cursor(row=1)
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


class TestEditDeleteHiddenOnUnsupportedTabs:
    async def test_e_and_d_are_hidden_from_the_footer_on_watchers_tab(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-watchers"
            await pilot.pause()

            assert app.screen.check_action("edit_row", ()) is False
            assert app.screen.check_action("delete_row", ()) is False

    async def test_e_and_d_are_visible_on_connectors_tab(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
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
            table.move_cursor(row=1)  # rc-orphan

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
            table.move_cursor(row=1)

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
            table.move_cursor(row=1)

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
            table.move_cursor(row=0)  # rc-referenced

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
