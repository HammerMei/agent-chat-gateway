"""Pilot-based tests for TemplateDetailScreen — the named `*_templates:`
CRUD screen (v0.3 templates/inherits redesign) that replaced the old,
single-global-block-per-kind `DefaultsScreen`.

Unlike the old `agent_defaults:`/`watcher_defaults:` blocks (exactly one
per kind, blast radius = "every entry in the whole config"), a named
template is a creatable/deletable/nameable entity, and its blast radius is
scoped to only the entries whose own `inherits:` names THAT SPECIFIC
template — several tests below deliberately include an entry inheriting a
DIFFERENT template to prove this scoping (the actual reason this whole
redesign, and this test file, exist).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Input, Select, Static

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal, TextPromptModal, TypePickerModal
from gateway.configtool.screens.overview import OverviewScreen
from gateway.configtool.screens.template_detail import TemplateDetailScreen


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
    """agent-a/agent-b both inherit 'standard' (agent-a doesn't override
    timeout, agent-b does); agent-c inherits a DIFFERENT template 'other' —
    must never show up when editing 'standard', proving blast radius is
    scoped per-template, not "every entry in the config" the way the old
    agent_defaults block worked. 'unused' has zero referencing agents (for
    the unblocked-delete test). w1/w2 mirror the same shape for
    watcher_templates."""
    return f"""\
        agent_templates:
          standard:
            type: claude
            timeout: 1800
          other:
            type: opencode
            timeout: 300
          unused:
            type: claude
            timeout: 100
        watcher_templates:
          wstd:
            online_notification: "hi"
        agents:
          agent-a:
            inherits: standard
            working_directory: {work_dir}
          agent-b:
            inherits: standard
            working_directory: {work_dir}
            timeout: 60
          agent-c:
            inherits: other
            working_directory: {work_dir}
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - name: w1
            connector: rc
            agent: agent-a
            room: general
            inherits: wstd
          - name: w2
            connector: rc
            agent: agent-b
            room: dev
            online_notification: "custom"
            inherits: wstd
    """


# Row order in the flat Templates tab: TEMPLATE_KINDS = (agent, connector,
# watcher); within a kind, dict insertion order from the YAML above.
# row0=agent:standard row1=agent:other row2=agent:unused row3=watcher:wstd
# (no connector_templates in this fixture).


async def _open_template_edit(pilot, app, row: int) -> None:
    app.screen.query_one("TabbedContent").active = "tab-templates"
    await pilot.pause()
    table = app.screen.query_one("#templates-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("e")
    await pilot.pause()


class TestTemplateEditVisibility:
    async def test_e_opens_edit_mode_prefilled_with_template_values(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)  # agent:standard
            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.mode == "edit"
            assert app.screen.query_one("#field-timeout", Input).value == "1800"
            assert app.screen.query_one("#field-type", Select).value == "claude"


class TestTemplateSaveDiffing:
    async def test_untouched_fields_save_with_their_original_values_intact(
        self, tmp_path, work_dir
    ):
        """TemplateDetailScreen.action_save() always proceeds through
        save()+pop_screen() regardless of whether anything actually changed
        — same unconditional behavior AgentDetailScreen/ConnectorDetailScreen
        already have (unlike the old DefaultsScreen, which special-cased
        "nothing changed" to stay in view mode without writing). What must
        stay true either way: an untouched field's VALUE survives the
        round-trip unchanged (byte-identical file content is NOT asserted —
        save() always re-serializes the whole document via yaml.dump, which
        can reformat flow-style mappings even with no value changes)."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["timeout"] == 1800
            assert raw["agent_templates"]["standard"]["type"] == "claude"

    async def test_changing_a_field_requires_confirm_scoped_to_this_template_only(
        self, tmp_path, work_dir
    ):
        """The actual regression test this whole redesign exists for."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)  # agent:standard

            app.screen.query_one("#field-timeout", Input).value = "900"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
            body = str(app.screen.query_one("#confirm-message", Static).render())
            assert "agent-a" in body  # inherits standard, doesn't override — affected
            assert "agent-b" not in body  # inherits standard, already overrides — unaffected
            assert "agent-c" not in body  # inherits a DIFFERENT template entirely

            await pilot.press("tab", "enter")  # confirm
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["timeout"] == 900
            assert raw["agent_templates"]["other"]["timeout"] == 300  # untouched

    async def test_cancelling_the_blast_radius_confirm_leaves_it_unsaved(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            app.screen.query_one("#field-timeout", Input).value = "900"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            await pilot.press("enter")  # Cancel is focused by default
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.mode == "edit"
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["timeout"] == 1800  # untouched

    async def test_changing_a_field_nobody_inherits_saves_without_confirm(self, tmp_path, work_dir):
        """agent-c is the only entry inheriting 'other'; give it its own
        explicit timeout override too, so editing 'other's timeout affects
        nobody and needs no confirm at all."""
        text = _config_text(work_dir).replace(
            f"          agent-c:\n            inherits: other\n            working_directory: {work_dir}\n",
            f"          agent-c:\n            inherits: other\n            working_directory: {work_dir}\n"
            "            timeout: 45\n",
        )
        config_path = _write_config(tmp_path, text)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=1)  # agent:other

            app.screen.query_one("#field-timeout", Input).value = "900"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)  # no confirm at all
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["other"]["timeout"] == 900

    async def test_clearing_a_field_via_ctrl_r_removes_it_from_the_template(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)  # agent:standard

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
            assert "timeout" not in raw["agent_templates"]["standard"]

    async def test_invalid_int_shows_a_message_modal(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            app.screen.query_one("#field-timeout", Input).value = "not-a-number"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)


class TestTemplateEditWatcherTemplates:
    async def test_editing_online_notification_requires_confirm_naming_the_watcher(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=3)  # watcher:wstd
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
            assert raw["watcher_templates"]["wstd"]["online_notification"] == "bye"


class TestTemplateEditDiscard:
    async def test_escape_with_unsaved_changes_prompts_discard_confirm(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            app.screen.query_one("#field-timeout", Input).value = "42"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # Discard
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.mode == "view"
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["timeout"] == 1800  # untouched

    async def test_escape_without_changes_returns_to_view_directly(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            await pilot.press("escape")
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.mode == "view"


class TestDirectEditFromList:
    """'e' directly on the Templates tab's list row — no Enter-first detour,
    matching the shortcut Connectors/Agents/Tool Presets already have."""

    async def test_e_on_a_template_row_opens_edit_mode_directly(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.mode == "edit"
            assert app.screen.query_one("#field-timeout", Input).value == "1800"

    async def test_escape_from_direct_edit_with_no_changes_returns_to_the_list(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert app.screen.mode == "edit"

            await pilot.press("escape")
            await pilot.pause()

            # No view-mode fallback -- straight back to the list, since this
            # screen instance never had a view rendering to begin with.
            assert isinstance(app.screen, OverviewScreen)

    async def test_saving_a_real_change_from_direct_edit_persists_and_returns(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()

            app.screen.query_one("#field-timeout", Input).value = "120"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)  # agent-a is affected
            await pilot.press("tab", "enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["timeout"] == 120


class TestTemplateCreate:
    async def test_n_prompts_kind_then_name_and_pushes_the_editor(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()

            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, TypePickerModal)  # pick a kind

            await pilot.press("enter")  # first option ("agent")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptModal)  # name it

            app.screen.query_one("#prompt-input", Input).value = "brand-new"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.mode == "create"
            assert app.screen.kind == "agent"

    async def test_duplicate_template_name_shows_an_error_and_does_not_navigate(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")  # "agent"
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "standard"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)  # never navigated

    async def test_creating_a_template_persists_it_and_returns_to_overview(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")  # "agent"
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "brand-new"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TemplateDetailScreen)

            app.screen.query_one("#field-timeout", Input).value = "42"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["brand-new"]["timeout"] == 42


class TestTemplateDelete:
    async def test_delete_blocked_when_used_by_agents(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # agent:standard — used by agent-a/agent-b
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "agent-a" in body
            assert "agent-b" in body

    async def test_delete_confirmed_when_unused_removes_it(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=2)  # agent:unused — nothing references it
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert "unused" not in raw["agent_templates"]
