"""Pilot-based tests for AgentDetailScreen's create/edit flow — the first
entity CRUD screen in Phase 2 (docs/design/config-tool.md decision 2:
"editing an inherited field always writes an explicit per-entry override").

These pin the write-back diffing mechanism specifically (advisor-flagged as
the highest-risk code in this change): an untouched inherited field must
never silently become explicit, a changed field must persist, and clearing
a field must revert it to inherited rather than writing an explicit null.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import Checkbox, DataTable, Footer, Input, Select, Static
from textual.widgets._footer import FooterKey

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal, TypePickerModal
from gateway.configtool.screens.agent_detail import AgentDetailScreen
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


def _config_with_one_agent(work_dir: Path, agent_extra: str = "") -> str:
    return f"""\
        agent_defaults:
          type: claude
          timeout: 1800
        agents:
          existing-agent:
            working_directory: {work_dir}
{textwrap.indent(agent_extra, "            ") if agent_extra else ""}
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - connector: rc
            agent: existing-agent
            room: general
    """


async def _open_agent_in_edit_mode(pilot, app) -> None:
    table = app.screen.query_one("#agents-table", DataTable)
    table.focus()
    table.move_cursor(row=0)
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("e")
    await pilot.pause()


class TestNewAgentEntryPoint:
    async def test_n_key_on_agents_tab_opens_type_picker(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()

            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, TypePickerModal)

    async def test_n_key_on_watchers_tab_notifies_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        """Watcher creation is Phase 3, not built yet — pressing 'n' on that
        tab must be a friendly no-op, not a crash or silent no-op. (Connector
        creation, tested elsewhere, now IS supported on tab-connectors.)"""
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-watchers"
            await pilot.pause()

            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)  # did not navigate anywhere

    async def test_cancelling_the_type_picker_returns_to_overview(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, TypePickerModal)

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)


class TestCreateAgent:
    async def test_creating_an_agent_persists_it_and_returns_to_overview(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")  # first option ("claude")
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "create"

            app.screen.query_one("#field-name", Input).value = "brand-new"
            app.screen.query_one("#field-working_directory", Input).value = str(work_dir)
            await pilot.pause()

            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agents"]["brand-new"]["type"] == "claude"
            assert raw["agents"]["brand-new"]["working_directory"] == str(work_dir)
            # A backup of the pre-save file must exist (EditableConfig.save()).
            assert list(Path(config_path).parent.glob("config.yaml.bak.*"))

    async def test_creating_with_a_duplicate_name_shows_an_error_and_stays_in_the_form(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            app.screen.query_one("#field-name", Input).value = "existing-agent"
            app.screen.query_one("#field-working_directory", Input).value = str(work_dir)
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "create"
            # The original agent must be untouched.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agents"]["existing-agent"] == {"working_directory": str(work_dir)}

    async def test_creating_with_a_blank_name_shows_an_error_and_stays_in_the_form(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "create"

    async def test_a_save_failure_rolls_back_the_phantom_created_entry(
        self, tmp_path, work_dir
    ):
        """working_directory is required to exist for validate_config() to
        pass (GatewayConfig.from_file hard-enforces it) — creating an agent
        pointed at a nonexistent directory must fail save() and must NOT
        leave a half-created agent sitting in cfg.document."""
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            app.screen.query_one("#field-name", Input).value = "doomed-agent"
            app.screen.query_one("#field-working_directory", Input).value = str(
                tmp_path / "does-not-exist"
            )
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "create"
            assert "doomed-agent" not in app.editable_config.agents_raw
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "doomed-agent" not in raw.get("agents", {})


class TestEditAgent:
    async def test_edit_mode_prefills_the_effective_merged_value(self, tmp_path, work_dir):
        config_path = _write_config(
            tmp_path, _config_with_one_agent(work_dir, "session_prefix: custom-prefix\n")
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            assert app.screen.query_one("#field-session_prefix", Input).value == "custom-prefix"
            # timeout is absent from the entry itself but set in agent_defaults —
            # the form must show the INHERITED effective value (1800), not blank.
            assert app.screen.query_one("#field-timeout", Input).value == "1800"

    async def test_changing_one_field_writes_only_that_field(self, tmp_path, work_dir):
        config_path = _write_config(
            tmp_path, _config_with_one_agent(work_dir, "session_prefix: custom-prefix\n")
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            app.screen.query_one("#field-command", Input).value = "custom-claude-binary"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            entry = app.editable_config.agents_raw["existing-agent"]
            assert entry["command"] == "custom-claude-binary"
            # untouched fields must be exactly as they were — timeout must
            # NOT have become an explicit "1800" on the entry just because
            # it was displayed (that would defeat agent_defaults entirely).
            assert "timeout" not in entry
            assert entry["session_prefix"] == "custom-prefix"
            assert entry["working_directory"] == str(work_dir)

    async def test_clearing_a_field_reverts_it_to_inherited(self, tmp_path, work_dir):
        config_path = _write_config(
            tmp_path, _config_with_one_agent(work_dir, "timeout: 500\n")
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            timeout_input = app.screen.query_one("#field-timeout", Input)
            assert timeout_input.value == "500"
            timeout_input.value = ""
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            entry = app.editable_config.agents_raw["existing-agent"]
            assert "timeout" not in entry  # reverted to inherited, not explicit null

    async def test_toggling_a_checkbox_and_back_to_its_original_value_is_a_no_op(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            checkbox = app.screen.query_one("#field-lazy_instruction_loading", Checkbox)
            original = checkbox.value
            checkbox.value = not original
            await pilot.pause()
            checkbox.value = original
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            entry = app.editable_config.agents_raw["existing-agent"]
            assert "lazy_instruction_loading" not in entry

    async def test_permissions_checkbox_subfield_write(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            app.screen.query_one("#field-permissions-enabled", Checkbox).value = True
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            entry = app.editable_config.agents_raw["existing-agent"]
            assert entry["permissions"] == {"enabled": True}

            # Toggling AWAY from the value shown when the form opened always
            # writes an explicit value — a checkbox has no third "revert to
            # inherited" state (unlike str/int/list fields, which revert when
            # cleared to blank; see the int-field test below). Re-opening
            # with enabled=True shown, then unchecking it, persists an
            # explicit False, not a cleared key.
            await _open_agent_in_edit_mode(pilot, app)
            assert app.screen.query_one("#field-permissions-enabled", Checkbox).value is True
            app.screen.query_one("#field-permissions-enabled", Checkbox).value = False
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            entry = app.editable_config.agents_raw["existing-agent"]
            assert entry["permissions"] == {"enabled": False}

    async def test_permissions_int_subfield_clears_and_drops_the_whole_dict(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(
            tmp_path, _config_with_one_agent(work_dir, "permissions: {timeout: 120}\n")
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            timeout_input = app.screen.query_one("#field-permissions-timeout", Input)
            assert timeout_input.value == "120"
            timeout_input.value = ""
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            entry = app.editable_config.agents_raw["existing-agent"]
            # The whole permissions dict is dropped, not left behind as {} —
            # timeout was its only explicit key.
            assert "permissions" not in entry

    async def test_invalid_integer_shows_error_and_does_not_touch_document(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            app.screen.query_one("#field-timeout", Input).value = "not-a-number"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "edit"
            assert app.editable_config.dirty is False
            entry = app.editable_config.agents_raw["existing-agent"]
            assert "timeout" not in entry

    async def test_working_directory_warning_appears_for_a_missing_directory(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            wd_input = app.screen.query_one("#field-working_directory", Input)
            wd_input.value = str(tmp_path / "does-not-exist-yet")
            await pilot.pause()
            warning = str(app.screen.query_one("#wd-warning", Static).render())
            assert "does not exist yet" in warning

            wd_input.value = str(work_dir)
            await pilot.pause()
            warning = str(app.screen.query_one("#wd-warning", Static).render())
            assert "does not exist yet" not in warning

    async def test_type_select_supports_switching_and_persists(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            select = app.screen.query_one("#field-type", Select)
            assert select.value == "claude"
            select.value = "opencode"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            entry = app.editable_config.agents_raw["existing-agent"]
            assert entry["type"] == "opencode"


class TestEscapeConfirmation:
    async def test_escape_with_no_changes_returns_to_view_with_no_modal(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "view"

    async def test_escape_with_unsaved_changes_shows_confirm_modal(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            app.screen.query_one("#field-command", Input).value = "changed"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

    async def test_cancelling_the_confirm_modal_keeps_editing(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            app.screen.query_one("#field-command", Input).value = "changed"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            await pilot.press("enter")  # Cancel is focused by default
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "edit"
            assert app.screen.query_one("#field-command", Input).value == "changed"

    async def test_confirming_discard_reverts_to_the_original_view(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            app.screen.query_one("#field-command", Input).value = "changed"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            await pilot.press("tab", "enter")  # move focus to Discard, press it
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "view"
            # Nothing was ever written — command was never actually saved.
            assert "command" not in app.editable_config.agents_raw["existing-agent"]

    async def test_create_mode_escape_with_unsaved_changes_pops_the_screen_on_discard(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)

            app.screen.query_one("#field-name", Input).value = "abandoned"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            await pilot.press("tab", "enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert "abandoned" not in app.editable_config.agents_raw


class TestFooterSurvivesRecompose:
    """Regression: Footer subscribes to Screen.bindings_updated_signal in
    its OWN on_mount — recompose() (used for every view<->edit transition)
    mounts a brand-new Footer instance, but nothing re-publishes that signal
    just because a new subscriber showed up, so the fresh Footer's
    `_bindings_ready` reactive stayed False forever and it rendered as a
    blank bar with zero FooterKey children, permanently, from the first
    view->edit transition onward (user-reported). Fixed by calling
    Screen.refresh_bindings() — its own public API for exactly this — right
    after every recompose() in action_edit()/action_back()."""

    async def test_footer_keys_survive_view_edit_view_edit_cycle(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#agents-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()

            def footer_key_count() -> int:
                footer = app.screen.query_one(Footer)
                return len(list(footer.query(FooterKey)))

            assert footer_key_count() > 0  # initial view mode

            await pilot.press("e")
            await pilot.pause()
            assert footer_key_count() > 0  # edit mode — this used to be 0

            await pilot.press("escape")
            await pilot.pause()
            assert footer_key_count() > 0  # back to view — this used to be 0

            await pilot.press("e")
            await pilot.pause()
            assert footer_key_count() > 0  # re-entered edit — this used to be 0


class TestFooterHintsMatchAvailableActions:
    """User-reported: the footer showed 'e: Edit' even while already in edit
    mode, where pressing 'e' is a no-op (action_edit() only does something
    from view mode) — confusing. Fixed via check_action(): 'Edit' is hidden
    once mode != "view"; 'Save' is hidden while mode == "view" (nothing to
    save yet); 'Tab: Next field' is shown throughout, mirroring
    OverviewScreen's existing tab-hint pattern, as the one reliable,
    discoverable way to move between fields (an Up/Down alternative was
    tried and reverted after user testing surfaced inconsistent behavior in
    a real terminal that Pilot's headless driver didn't catch)."""

    def _visible_keys(self, app) -> set[str]:
        from textual.widgets import Footer
        from textual.widgets._footer import FooterKey

        return {k.key for k in app.screen.query_one(Footer).query(FooterKey)}

    async def test_view_mode_shows_edit_not_save(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#agents-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()

            keys = self._visible_keys(app)
            assert "e" in keys
            assert "ctrl+s" not in keys
            assert "tab" in keys

    async def test_edit_mode_shows_save_not_edit(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            keys = self._visible_keys(app)
            assert "ctrl+s" in keys
            assert "e" not in keys
            assert "tab" in keys

    async def test_create_mode_shows_save_not_edit(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            keys = self._visible_keys(app)
            assert "ctrl+s" in keys
            assert "e" not in keys
            assert "tab" in keys


class TestFirstFieldFocus:
    """Regression: the form's VerticalScroll container was itself the first
    stop in the Tab cycle (it's focusable by default, for keyboard
    scrolling) — user-reported needing to press Tab TWICE after entering
    edit mode to reach the first real field. Fixed with can_focus=False on
    the container; scrolling still works via mouse wheel/PageUp/PageDown."""

    async def test_create_mode_auto_focuses_the_name_field_with_zero_tabs(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-agents"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert app.screen.focused is not None
            assert app.screen.focused.id == "field-name"

    async def test_edit_mode_reaches_the_first_field_with_a_single_tab(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_agent(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            await pilot.press("tab")
            await pilot.pause()
            assert app.screen.focused is not None
            assert app.screen.focused.id == "field-description"


def _config_with_two_agents(work_dir: Path) -> str:
    """'existing-agent' is referenced by the watcher; 'unused-agent' is not
    — used to test both the delete-succeeds and delete-blocked paths."""
    return f"""\
        agents:
          existing-agent:
            working_directory: {work_dir}
          unused-agent:
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


async def _open_agent_in_view_mode(pilot, app, row: int = 0) -> None:
    table = app.screen.query_one("#agents-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press("enter")
    await pilot.pause()


class TestDeleteAgent:
    async def test_d_key_on_an_unreferenced_agent_shows_confirm_modal(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_agents(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_view_mode(pilot, app, row=1)  # unused-agent

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

    async def test_cancelling_the_delete_keeps_the_agent(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_agents(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_view_mode(pilot, app, row=1)  # unused-agent

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")  # Cancel is focused by default
            await pilot.pause()
            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "view"
            assert "unused-agent" in app.editable_config.agents_raw

    async def test_confirming_delete_of_an_unreferenced_agent_succeeds(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_agents(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # row 1 is "unused-agent" (dict order: existing-agent, unused-agent)
            await _open_agent_in_view_mode(pilot, app, row=1)
            assert app.screen.agent_name == "unused-agent"

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "unused-agent" not in raw["agents"]
            assert "existing-agent" in raw["agents"]
            assert list(Path(config_path).parent.glob("config.yaml.bak.*"))

    async def test_deleting_a_referenced_agent_is_blocked_before_the_confirm(
        self, tmp_path, work_dir
    ):
        """A watcher still references 'existing-agent' — the pre-delete
        check catches this BEFORE even offering the destructive confirm,
        naming the referencing watcher in a MessageModal rather than
        relying on save()'s generic validator error."""
        config_path = _write_config(tmp_path, _config_with_two_agents(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_view_mode(pilot, app, row=0)
            assert app.screen.agent_name == "existing-agent"

            await pilot.press("d")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "existing-agent" in body
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            # Stays on the (still-view-mode) detail screen — nothing was
            # ever removed from `document` in the first place.
            assert isinstance(app.screen, AgentDetailScreen)
            assert app.screen.mode == "view"
            assert "existing-agent" in app.editable_config.agents_raw
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "existing-agent" in raw["agents"]  # untouched on disk too

    async def test_delete_is_hidden_from_the_footer_while_editing(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_agents(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_agent_in_edit_mode(pilot, app)

            from textual.widgets import Footer
            from textual.widgets._footer import FooterKey

            keys = {k.key for k in app.screen.query_one(Footer).query(FooterKey)}
            assert "d" not in keys
