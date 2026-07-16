"""Pilot-based tests for gateway/configtool/app.py — the config TUI.

No prior Textual-app test precedent existed in this repo before this file;
the pattern here (async test functions using `App.run_test()`/`Pilot`,
plain pytest style rather than unittest.TestCase — pytest-asyncio's
asyncio_mode="auto" does not play well with unittest's own event-loop
management) is the one to follow for future config TUI phases.

Known, empirically-confirmed limitation: `App.suspend()` raises
`SuspendNotSupported` under `run_test()`'s headless driver (verified while
building this suite) — so the $EDITOR round-trip's actual suspend+subprocess
line cannot be exercised here (see `# pragma: no cover` in app.py). What
*is* tested here: that `open_editor_and_reload()` handles that failure
gracefully (notifies, doesn't crash) rather than propagating it, and that
`reload_config()`/`refresh_overview()` — the part of the round-trip that
matters for correctness — work correctly on their own.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.screens.agent_detail import AgentDetailScreen
from gateway.configtool.screens.connector_detail import ConnectorDetailScreen
from gateway.configtool.screens.defaults import DefaultsScreen
from gateway.configtool.screens.overview import OverviewScreen
from gateway.configtool.screens.tool_presets import ToolPresetsScreen
from gateway.configtool.screens.watcher_detail import WatcherDetailScreen


def _write_config(tmp_path: Path, yaml_text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(yaml_text))
    return str(path)


def _valid_config_text(work_dir: Path) -> str:
    return f"""\
        tool_presets:
          readonly:
            - tool: Read
        connector_defaults:
          type: rocketchat
        connectors:
          - name: rc-home
            description: "Main bot"
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
            allowed_users: {{owners: [alice], guests: []}}
        agent_defaults:
          type: claude
          timeout: 1800
          permissions: {{enabled: true, timeout: 300}}
          owner_allowed_tools: [readonly]
        agents:
          my-agent:
            working_directory: {work_dir}
        watchers:
          - connector: rc-home
            agent: my-agent
            rooms: [general, dev, "@alice"]
    """


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


class TestOverviewRender:
    """Instantiate a fresh App per test (Textual issue #4998: run_test() is
    awkward to share across tests via a fixture — construct/enter it inside
    each test function instead)."""

    async def test_valid_config_renders_all_tables(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path, lint=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

            banner = app.screen.query_one("#banner", Static)
            assert "valid" in str(banner.render())
            assert "3 watcher" in str(banner.render())

            assert app.screen.query_one("#connectors-table", DataTable).row_count == 1
            assert app.screen.query_one("#agents-table", DataTable).row_count == 1
            assert app.screen.query_one("#watchers-table", DataTable).row_count == 3
            assert app.screen.query_one("#defaults-table", DataTable).row_count == 3
            assert app.screen.query_one("#presets-table", DataTable).row_count == 1

    async def test_tab_binding_is_visible_in_footer_and_moves_focus_into_table(
        self, tmp_path, work_dir
    ):
        """On mount, focus starts on the tab bar, not the list — 'tab' is
        rebound here (same app.focus_next action Screen already binds, just
        with show=True) so the footer surfaces the one key needed to reach
        the list at all. Regression for a real UX gap a user hit."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            bound = app.screen.active_bindings.get("tab")
            assert bound is not None
            assert bound.binding.show is True

            await pilot.press("tab")
            await pilot.pause()
            assert isinstance(app.focused, DataTable)

    async def test_q_key_is_a_visible_quit_binding(self, tmp_path, work_dir):
        """'q' is the documented, discoverable quit key (App's own ctrl+q
        default stays too, but is hidden — show=False — so it doesn't count
        as discoverable on its own)."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            bound = app.screen.active_bindings.get("q")
            assert bound is not None
            assert bound.binding.show is True
            assert bound.binding.action == "app.quit"

            await pilot.press("q")
            await pilot.pause()
            assert app.is_running is False

    async def test_connector_type_inherited_from_defaults_is_shown_not_a_placeholder(
        self, tmp_path, work_dir
    ):
        """Regression: the connector row must show the MERGED type even when
        'type' is only set via connector_defaults, not on the entry itself —
        this crashed/showed '?' before the merged_entry() fix."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path, lint=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            row = table.get_row_at(0)
            assert row[1] == "rocketchat"

    async def test_missing_config_shows_global_error_banner_not_a_crash(self, tmp_path):
        config_path = str(tmp_path / "does-not-exist.yaml")
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.screen.query_one("#banner", Static)
            assert "does not currently load" in str(banner.render())
            # Tables must be empty, not crash the screen.
            assert app.screen.query_one("#connectors-table", DataTable).row_count == 0

    async def test_invalid_config_from_file_failure_does_not_crash_watchers_table(
        self, tmp_path, work_dir
    ):
        """Regression: a config that parses as YAML but fails
        GatewayConfig.from_file (here: unresolved $VAR) must not crash while
        populating the watchers table via expanded_watchers() -> validated_view()."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$UNRESOLVED_VAR_XYZ", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - name: w1
                room: general
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.screen.query_one("#banner", Static)
            assert "error" in str(banner.render()).lower()
            watchers_table = app.screen.query_one("#watchers-table", DataTable)
            assert watchers_table.row_count == 1
            row = watchers_table.get_row_at(0)
            assert "unavailable" in row[0]

    async def test_refresh_action_picks_up_on_disk_changes(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#watchers-table", DataTable).row_count == 3

            with open(config_path, "a") as f:
                f.write("  - connector: rc-home\n    agent: my-agent\n    room: extra\n")

            await pilot.press("r")
            await pilot.pause()
            assert app.screen.query_one("#watchers-table", DataTable).row_count == 4

    async def test_lint_findings_counted_in_banner_only_when_lint_enabled(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
                timeout: 360
            watchers:
              - name: w1
                room: general
        """)
        app_no_lint = ConfigToolApp(config_path, lint=False)
        async with app_no_lint.run_test() as pilot:
            await pilot.pause()
            banner = str(app_no_lint.screen.query_one("#banner", Static).render())
            assert "lint finding" not in banner

        app_lint = ConfigToolApp(config_path, lint=True)
        async with app_lint.run_test() as pilot:
            await pilot.pause()
            banner = str(app_lint.screen.query_one("#banner", Static).render())
            assert "lint finding" in banner


class TestDetailScreenNavigation:
    async def test_connector_row_pushes_and_pops_detail_screen(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#connectors-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)
            body = str(app.screen.query_one("#connector-detail-body", Static).render())
            assert "rc-home" in body
            assert "pw" not in body  # password must be masked

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    async def test_agent_row_pushes_detail_screen_with_provenance(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#agents-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            body = str(app.screen.query_one("#agent-detail-body", Static).render())
            assert "inherited from defaults" in body
            assert "preset: readonly" in body

    async def test_watcher_row_pushes_detail_with_group_banner(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#watchers-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, WatcherDetailScreen)
            body = str(app.screen.query_one("#watcher-detail-body", Static).render())
            assert "shared rooms: group" in body

    async def test_watcher_row_without_group_has_no_group_banner(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - name: standalone
                room: dev
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#watchers-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            body = str(app.screen.query_one("#watcher-detail-body", Static).render())
            assert "shared rooms: group" not in body

    async def test_defaults_row_pushes_detail_with_blast_radius(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#defaults-table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # agent_defaults
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DefaultsScreen)
            body = str(app.screen.query_one("#defaults-detail-body", Static).render())
            assert "entries inherit" in body

    async def test_preset_row_pushes_detail_showing_used_by(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#presets-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ToolPresetsScreen)
            body = str(app.screen.query_one("#preset-detail-body", Static).render())
            assert "my-agent" in body


class TestEditorEscapeHatch:
    async def test_suspend_not_supported_is_caught_and_notified_not_crashed(
        self, tmp_path, work_dir
    ):
        """App.suspend() raises SuspendNotSupported under run_test()'s
        headless driver (confirmed empirically) — open_editor_and_reload()
        must catch that (or any) failure and notify rather than propagate."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Must not raise.
            app.open_editor_and_reload()
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    async def test_reload_config_after_external_edit_refreshes_overview(
        self, tmp_path, work_dir
    ):
        """The part of the $EDITOR round-trip that matters for correctness —
        reloading from disk and repainting — tested directly, since the
        suspend+subprocess line itself can't run under a headless driver."""
        config_path = _write_config(tmp_path, _valid_config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#watchers-table", DataTable).row_count == 3

            with open(config_path, "a") as f:
                f.write("  - connector: rc-home\n    agent: my-agent\n    room: extra\n")

            app.reload_config()
            await pilot.pause()
            assert app.screen.query_one("#watchers-table", DataTable).row_count == 4


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
