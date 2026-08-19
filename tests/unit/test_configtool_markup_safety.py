"""Every screen must survive markup-hostile, operator-authored config.

Rich markup in operator data has now produced findings in three separate
Codex rounds on PR #129 (5, 7 and 8), each time at a site the previous
sweep missed — a per-site escape pass demonstrably does not converge. So
this walks the whole TUI against a config in which *every* author-supplied
string is hostile, which is what makes the next unescaped interpolation fail
here rather than in a review round.

Two hostile spellings, because they fail differently:

* ``[ab]`` looks like a valid tag: Rich silently SWALLOWS it, so the value
  is displayed wrong (a rule named ``[ab]`` rendered as ``''``, and a
  legitimate character-class room pattern ``eng-[ab]`` rendered as
  ``eng-``, concealing the rule's real routing).
* ``[/]`` is an unbalanced closing tag: Rich RAISES ``MarkupError``, taking
  down whatever was rendering — which in the worst case was the very screen
  or message meant to explain the problem.

``[…]`` is not exotic input here: it is documented, first-class
room-pattern syntax (``gateway/core/room_pattern.py``), and names are
unrestricted strings.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.screens.overview import OverviewScreen

# The two spellings, and what each one does when it is NOT escaped.
SWALLOWED = "[ab]"
RAISES = "[/]"

_TAB_TABLES = {
    "tab-connectors": "connectors-table",
    "tab-agents": "agents-table",
    "tab-rules": "rules-table",
    "tab-templates": "templates-table",
    "tab-presets": "presets-table",
}


def _write_config(tmp_path: Path, yaml_text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(yaml_text))
    return str(path)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _hostile_config(work_dir: Path) -> str:
    """One config carrying a hostile spelling in every author-supplied
    string: entity names, descriptions, room patterns, template and preset
    names, injected paths, and an `inherits:` pointing at a template that
    does not exist (so the caught loader error quotes a hostile name too)."""
    return f"""\
        tool_presets:
          "{RAISES}":
            - tool: Read
        agent_templates:
          "{SWALLOWED}":
            timeout: 1800
        watcher_templates:
          "{RAISES}":
            context_inject_files: ["notes-{SWALLOWED}.md"]
        connectors:
          - name: "conn-{SWALLOWED}"
            type: rocketchat
            description: "desc {RAISES}"
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
            allowed_users: {{owners: ["alice-{SWALLOWED}"], guests: []}}
        agents:
          "agent-{RAISES}":
            type: claude
            description: "desc {SWALLOWED}"
            working_directory: {work_dir}
            context_inject_files: ["ctx-{RAISES}.md"]
        watchers:
          - name: "{RAISES}"
            connector: "conn-{SWALLOWED}"
            agent: "agent-{RAISES}"
            description: "desc {RAISES}"
            context_inject_files: ["notes-{RAISES}.md"]
            rooms:
              include: ["eng-{SWALLOWED}", "ops-{RAISES}"]
          - name: "rule-{SWALLOWED}"
            connector: "conn-{SWALLOWED}"
            agent: "agent-{RAISES}"
            inherits: "no-such-template-{RAISES}"
            rooms:
              include: ["general"]
    """


class TestEveryTabPaintsHostileConfig:
    async def test_the_overview_paints_all_five_tabs_without_crashing(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _hostile_config(work_dir))
        app = ConfigToolApp(config_path, lint=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for tab, table_id in _TAB_TABLES.items():
                app.screen.query_one(TabbedContent).active = tab
                await pilot.pause()
                assert app.is_running is True, f"{tab} killed the app"
                # Touch every cell, which is what forces a render.
                table = app.screen.query_one(f"#{table_id}", DataTable)
                for row in range(table.row_count):
                    table.get_row_at(row)
            # The banner renders validation text for this config too.
            assert app.is_running is True
            app.screen.query_one("#banner", Static)

    async def test_the_validation_details_modal_renders(self, tmp_path, work_dir):
        """'v' shows the validator's own messages, which quote the hostile
        names back."""
        config_path = _write_config(tmp_path, _hostile_config(work_dir))
        app = ConfigToolApp(config_path, lint=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            assert app.is_running is True


class TestEveryDetailScreenOpensOnHostileConfig:
    """View mode renders a body of interpolated values; edit mode renders a
    row per field. Both are exercised for every tab that has a form."""

    @pytest.mark.parametrize(
        "tab",
        ["tab-connectors", "tab-agents", "tab-rules", "tab-templates", "tab-presets"],
    )
    @pytest.mark.parametrize("key", ["enter", "e"])
    async def test_opening_every_row_survives(self, tmp_path, work_dir, tab, key):
        config_path = _write_config(tmp_path, _hostile_config(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = tab
            await pilot.pause()
            table = app.screen.query_one(f"#{_TAB_TABLES[tab]}", DataTable)
            row_count = table.row_count
            assert row_count, f"{tab} rendered no rows — the fixture is not exercising it"
            for row in range(row_count):
                table.focus()
                table.move_cursor(row=row)
                await pilot.press(key)
                await pilot.pause()
                assert app.is_running is True, (
                    f"{tab} row {row} killed the app on {key!r}"
                )
                # Back to the list for the next row. BOUNDED: a screen that
                # refuses to pop must fail the test, not hang it (a `while`
                # here span forever on the first such screen).
                for _ in range(6):
                    if isinstance(app.screen, OverviewScreen):
                        break
                    await pilot.press("escape")
                    await pilot.pause()
                    if not isinstance(app.screen, OverviewScreen):
                        await pilot.press("tab", "enter")  # confirm any discard
                        await pilot.pause()
                assert isinstance(app.screen, OverviewScreen), (
                    f"{tab} row {row}: could not get back to the list "
                    f"(stuck on {type(app.screen).__name__})"
                )


class TestHostileValuesAreDisplayedVerBatim:
    """Not crashing is half of it — a swallowed value displays WRONG, which
    for a room pattern means the UI misreports the rule's routing."""

    async def test_a_character_class_pattern_survives_into_the_rules_table(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _hostile_config(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-rules"
            await pilot.pause()
            table = app.screen.query_one("#rules-table", DataTable)
            from rich.markup import render

            rooms_cell = render(str(table.get_row_at(0)[4])).plain
            assert f"eng-{SWALLOWED}" in rooms_cell
            assert f"ops-{RAISES}" in rooms_cell

    async def test_a_hostile_rule_name_survives_into_its_detail_body(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _hostile_config(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-rules"
            await pilot.pause()
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            body = str(app.screen.query_one("#rule-detail-body", Static).render())
            assert RAISES in body

    async def test_a_hostile_inherits_error_explains_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        """Row 1's `inherits:` names a template that does not exist, so the
        body takes its 'Could not compute effective values' branch — and that
        message quotes the hostile name."""
        config_path = _write_config(tmp_path, _hostile_config(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-rules"
            await pilot.pause()
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()
            assert app.is_running is True
            body = str(app.screen.query_one("#rule-detail-body", Static).render())
            assert "Could not compute effective values" in body


class TestNotificationsAreP1ainText:
    async def test_a_hostile_name_in_a_notification_does_not_kill_the_toast(
        self, tmp_path, work_dir
    ):
        """Notifications name operator data too, and Textual parses their
        text as markup by default — `[/]` raised MarkupError from inside the
        toast. ConfigToolApp.notify() defaults markup=False for exactly this."""
        config_path = _write_config(tmp_path, _hostile_config(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.notify(f"Deleted rule '{RAISES}'.", severity="information")
            await pilot.pause()
            assert app.is_running is True
            (notification,) = list(app._notifications)
            assert notification.markup is False
            assert RAISES in notification.message


if __name__ == "__main__":
    pytest.main([__file__])
