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
            type: claude
            working_directory: {work_dir}
            owner_allowed_tools: [preset-a]
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watcher_rules:
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

    async def test_the_rules_list_is_focused_on_mount_with_no_tab_needed(
        self, tmp_path, work_dir
    ):
        """User-reported: landing here required an explicit Tab press
        before 'a'/'e'/'d' or the arrow keys did anything — DOM focus
        started on nothing in particular. Covers BOTH a preset with rules
        already in it and a brand-new, still-empty one (pushed via
        OverviewScreen.action_new_entity()'s "new preset" flow) — a
        focused, empty ListView is still a valid, useful state ('a' adds
        the first rule)."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)  # preset-a, has 1 rule

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            assert list_view.has_focus

            # No Tab press — 'd' should act immediately.
            await pilot.press("d")
            await pilot.pause()
            assert len(list_view.children) == 0

    async def test_selection_survives_a_refresh_not_just_initial_mount(
        self, tmp_path, work_dir
    ):
        """PR review finding: the row-0 auto-select must live in
        _refresh_rules() itself, not just on_mount() — `ListView.clear()`
        (called by every add/edit/delete, via _refresh_rules()) resets
        `.index` back to None, and re-appending items afterward does NOT
        restore an auto-selection. Fixing this only in on_mount() made the
        FIRST entry into this screen work, but the exact same "nothing
        selected, 'd'/'e' silently no-op" bug reappeared after the very
        first mutation — reproduced here by adding a rule, then deleting
        immediately with no navigation keypress in between."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)  # preset-a, has 1 rule

            await pilot.press("a")
            await pilot.pause()
            app.screen.query_one("#rule-tool", Input).value = "Edit"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            assert len(list_view.children) == 2

            # No manual navigation — 'd' should still act immediately,
            # matching what test_the_rules_list_is_focused_on_mount_with_no_
            # tab_needed already pins for the very first entry.
            await pilot.press("d")
            await pilot.pause()
            assert len(list_view.children) == 1

    async def test_the_rules_list_is_focused_on_mount_when_empty(self, tmp_path, work_dir):
        text = _config_text(work_dir).replace(
            "tool_presets:\n          preset-a:\n            - tool: Bash\n"
            '              params: "ls .*"\n',
            "tool_presets:\n          preset-a: []\n",
        )
        config_path = _write_config(tmp_path, text)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)  # preset-a, empty

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            assert list_view.has_focus


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


class TestToolPresetsScreenEditRule:
    """User-reported gap: only Add/Delete existed for an individual rule —
    no way to edit one in place short of deleting and re-adding it."""

    async def test_edit_rule_persists_the_change_and_prefills_the_modal(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            list_view.focus()
            list_view.index = 0
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, InlineToolRuleModal)
            # Pre-filled from the existing rule (preset-a's only rule:
            # {tool: Bash, params: "ls .*"}), not blank.
            assert app.screen.query_one("#rule-tool", Input).value == "Bash"
            assert app.screen.query_one("#rule-params", Input).value == "ls .*"

            app.screen.query_one("#rule-tool", Input).value = "Bash2"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)
            list_view = app.screen.query_one("#preset-rules-list", ListView)
            assert len(list_view.children) == 1  # replaced in place, not appended

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["tool_presets"]["preset-a"] == [{"tool": "Bash2", "params": "ls .*"}]

    async def test_edit_with_nothing_selected_notifies_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        # Same setup as the sibling delete-rule test: ListView.index
        # defaults to 0 (not None) the instant it mounts with any children,
        # so "nothing selected" can only be forced with a genuinely EMPTY
        # rule list.
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

            await pilot.press("e")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)  # no crash

    async def test_cancelling_the_edit_modal_leaves_the_rule_untouched(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            list_view.focus()
            list_view.index = 0
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, ToolPresetsScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["tool_presets"]["preset-a"] == [{"tool": "Bash", "params": "ls .*"}]


class TestToolPresetsScreenCursorPositionAfterMutation:
    """PR review finding: the row-0 auto-select fix (in _refresh_rules(),
    see TestToolPresetsScreenView's own tests) used to fire unconditionally
    on EVERY refresh — not just when nothing was selected — silently
    snapping the cursor back to row 0 after every single mutation. A user
    editing/deleting several rows in sequence, expecting the cursor to
    roughly track position, would have every subsequent action land on the
    WRONG row with no error or visual cue."""

    async def _preset_with_three_rules(self, tmp_path, work_dir):
        text = _config_text(work_dir).replace(
            "tool_presets:\n          preset-a:\n            - tool: Bash\n"
            '              params: "ls .*"\n',
            "tool_presets:\n          preset-a:\n            - tool: Bash\n"
            '              params: "ls .*"\n'
            "            - tool: Read\n"
            "            - tool: Write\n",
        )
        return _write_config(tmp_path, text)

    async def test_editing_the_last_row_keeps_the_cursor_on_it(self, tmp_path, work_dir):
        config_path = await self._preset_with_three_rules(tmp_path, work_dir)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            list_view.focus()
            list_view.index = 2  # 'Write', the last row
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            app.screen.query_one("#rule-tool", Input).value = "Write2"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            assert len(list_view.children) == 3  # not appended/removed
            assert list_view.index == 2  # cursor stayed put, not reset to 0

    async def test_deleting_a_middle_row_clamps_the_cursor_sensibly(self, tmp_path, work_dir):
        config_path = await self._preset_with_three_rules(tmp_path, work_dir)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_preset_detail(pilot, app, row=0)

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            list_view.focus()
            list_view.index = 1  # 'Read', the middle row
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()

            list_view = app.screen.query_one("#preset-rules-list", ListView)
            assert len(list_view.children) == 2  # 'Bash' and 'Write' remain
            # index 1 is still a valid position (now 'Write', which slid up
            # into the deleted row's slot) — not reset to 0.
            assert list_view.index == 1


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
