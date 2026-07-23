"""Pilot-based tests for DefaultsScreen's edit mode — making agent_defaults/
watcher_defaults editable (docs/design/config-tool.md decision 2:
"editing a shared default must show its blast radius before commit").

connector_defaults intentionally stays view-only (see defaults.py's module
docstring for why) — covered by the existing view-mode test in
test_configtool_app.py, not duplicated here.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Input, Select, Static

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal
from gateway.configtool.screens.defaults import DefaultsScreen
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


def _config_text(work_dir: Path) -> str:
    """agent-a inherits agent_defaults.timeout (not overridden); agent-b
    overrides it. w1 inherits watcher_defaults.online_notification; w2
    overrides it. Gives every test a mix of affected/unaffected entries to
    exercise the blast-radius confirm."""
    return f"""\
        agent_defaults:
          type: claude
          timeout: 1800
        watcher_defaults:
          online_notification: "hi"
        agents:
          agent-a:
            working_directory: {work_dir}
          agent-b:
            working_directory: {work_dir}
            timeout: 60
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - name: w1
            connector: rc
            agent: agent-a
            room: general
          - name: w2
            connector: rc
            agent: agent-b
            room: dev
            online_notification: "custom"
    """


async def _open_defaults_edit(pilot, app, row: int) -> None:
    app.screen.query_one("TabbedContent").active = "tab-defaults"
    await pilot.pause()
    table = app.screen.query_one("#defaults-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("e")
    await pilot.pause()


class TestDefaultsEditVisibility:
    async def test_e_opens_edit_mode_for_agent_defaults(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)  # agent_defaults
            assert isinstance(app.screen, DefaultsScreen)
            assert app.screen.mode == "edit"
            assert app.screen.query_one("#field-timeout", Input).value == "1800"
            assert app.screen.query_one("#field-type", Select).value == "claude"

    async def test_e_is_a_no_op_for_connector_defaults(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-defaults"
            await pilot.pause()
            table = app.screen.query_one("#defaults-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # connector_defaults
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, DefaultsScreen)
            assert app.screen.mode == "view"
            await pilot.press("e")
            await pilot.pause()
            assert app.screen.mode == "view"  # unchanged — nothing editable


class TestDefaultsSaveDiffing:
    async def test_untouched_fields_are_not_rewritten(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        original = Path(config_path).read_text()
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)

            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, DefaultsScreen)
            assert app.screen.mode == "view"  # returned to view, nothing to confirm
            assert Path(config_path).read_text() == original

    async def test_changing_a_field_with_affected_entries_requires_confirm(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)  # agent_defaults

            app.screen.query_one("#field-timeout", Input).value = "900"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
            body = str(app.screen.query_one("#confirm-message", Static).render())
            assert "agent-a" in body  # doesn't override timeout — affected
            assert "agent-b" not in body  # already overrides it — unaffected

            await pilot.press("tab", "enter")  # confirm
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_defaults"]["timeout"] == 900

    async def test_cancelling_the_blast_radius_confirm_leaves_it_unsaved(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)

            app.screen.query_one("#field-timeout", Input).value = "900"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            await pilot.press("enter")  # Cancel is focused by default
            await pilot.pause()

            assert isinstance(app.screen, DefaultsScreen)
            assert app.screen.mode == "edit"
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_defaults"]["timeout"] == 1800  # untouched

    async def test_changing_a_field_nobody_inherits_saves_without_confirm(
        self, tmp_path, work_dir
    ):
        """Both agents already override 'type'? No -- craft a case where
        EVERY agent overrides the changed key, so the blast radius is
        empty and no confirm should appear at all."""
        text = _config_text(work_dir).replace(
            "          agent-a:\n            working_directory: " + str(work_dir) + "\n",
            f"          agent-a:\n            working_directory: {work_dir}\n            timeout: 120\n",
        )
        config_path = _write_config(tmp_path, text)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)

            app.screen.query_one("#field-timeout", Input).value = "900"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            # Straight through to Overview -- no ConfirmModal, both agents
            # already had their own explicit timeout.
            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_defaults"]["timeout"] == 900

    async def test_clearing_a_field_via_ctrl_r_removes_it_from_the_block(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)

            app.screen.query_one("#field-timeout", Input).focus()
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)  # agent-a is affected
            await pilot.press("tab", "enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "timeout" not in raw["agent_defaults"]

    async def test_invalid_int_shows_a_message_modal(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)

            app.screen.query_one("#field-timeout", Input).value = "not-a-number"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)


class TestDefaultsEditWatcherDefaults:
    async def test_editing_online_notification_requires_confirm_naming_the_watcher(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=2)  # watcher_defaults
            assert app.screen.query_one("#field-online_notification", Input).value == "hi"

            app.screen.query_one("#field-online_notification", Input).value = "bye"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
            body = str(app.screen.query_one("#confirm-message", Static).render())
            assert "w1" in body
            assert "w2" not in body  # w2 already overrides it

            await pilot.press("tab", "enter")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watcher_defaults"]["online_notification"] == "bye"


class TestDefaultsEditDiscard:
    async def test_escape_with_unsaved_changes_prompts_discard_confirm(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)

            app.screen.query_one("#field-timeout", Input).value = "42"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # Discard
            await pilot.pause()

            assert isinstance(app.screen, DefaultsScreen)
            assert app.screen.mode == "view"
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_defaults"]["timeout"] == 1800  # untouched

    async def test_escape_without_changes_returns_to_view_directly(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_defaults_edit(pilot, app, row=1)

            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, DefaultsScreen)
            assert app.screen.mode == "view"
