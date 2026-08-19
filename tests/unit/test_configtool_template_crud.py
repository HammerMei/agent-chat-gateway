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
from gateway.configtool.modals import (
    ConfirmModal,
    InlineToolRuleModal,
    MessageModal,
    PresetOrInlineModal,
    TextPromptModal,
    TypePickerModal,
)
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
            description: Our standard claude profile
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
            history_handoff:
              fetch_count: 25
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
            history_handoff:
              fetch_count: 40
            inherits: wstd
    """


# Row order in the flat Templates tab: TEMPLATE_KINDS = (agent, connector,
# watcher); within a kind, sorted BY NAME (user-requested — see
# OverviewScreen.repaint_from_memory()), not dict insertion order.
# row0=agent:other row1=agent:standard row2=agent:unused row3=watcher:wstd
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
            await _open_template_edit(pilot, app, row=1)  # agent:standard
            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.mode == "edit"
            assert app.screen.query_one("#field-timeout", Input).value == "1800"
            assert app.screen.query_one("#field-type", Select).value == "claude"

    async def test_description_is_prefilled_in_edit_mode_and_shown_in_view_mode(
        self, tmp_path, work_dir
    ):
        """PR review finding: OverviewScreen used to construct
        TemplateDetailScreen with cfg.templates(kind).get(name) — a dict
        with 'description' already stripped (EditableConfig.templates()'s
        own docstring: stripped so it never deep-merges into an inheriting
        entry) — so a template's description never displayed anywhere in
        this screen, view or edit. Fixed via EditableConfig.raw_template(),
        used at both of OverviewScreen's TemplateDetailScreen call sites."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # agent:standard
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.mode == "view"
            assert "Our standard claude profile" in app.screen._body_text()

            await pilot.press("e")
            await pilot.pause()
            assert (
                app.screen.query_one("#field-description", Input).value
                == "Our standard claude profile"
            )


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
            await _open_template_edit(pilot, app, row=1)

            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["timeout"] == 1800
            assert raw["agent_templates"]["standard"]["type"] == "claude"
            # PR review finding: EditableConfig.templates() deliberately
            # strips 'description' (it must never deep-merge into an
            # inheriting entry) — but TemplateDetailScreen used to be
            # constructed FROM that stripped dict as its own self.entry, so
            # action_save()'s target_entry = dict(self.entry) + updates had
            # no description to carry, and its wholesale overwrite of
            # document["agent_templates"]["standard"] silently deleted the
            # on-disk description on every save, even this untouched one.
            assert raw["agent_templates"]["standard"]["description"] == (
                "Our standard claude profile"
            )

    async def test_changing_a_nested_field_excludes_entries_that_already_override_it(
        self, tmp_path, work_dir
    ):
        """PR review finding: the blast-radius confirm checked the raw
        dotted FieldSpec.key ("permissions.timeout") for membership in each
        referencing entry's raw dict — but a dict never has a literal
        top-level key equal to that dotted string, only the nested group
        itself ("permissions"). Without splitting to the top-level key
        (matching _compose_field_row()'s own top_key = spec.key.split(".",
        1)[0] a few lines below in the same file), EVERY referencing entry
        was listed as affected, even agent-b here, which already overrides
        the whole 'permissions' group and is genuinely unaffected by an
        edit to the template's permissions.timeout."""
        config_path = _write_config(
            tmp_path,
            f"""\
            agent_templates:
              nested-std:
                type: claude
                permissions: {{enabled: true, timeout: 300}}
            agents:
              agent-a:
                inherits: nested-std
                working_directory: {work_dir}
              agent-b:
                inherits: nested-std
                working_directory: {work_dir}
                permissions: {{enabled: true, timeout: 999}}
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            watchers:
              - connector: rc
                agent: agent-a
                room: general
              - connector: rc
                agent: agent-b
                room: dev
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # only agent template: nested-std
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()

            app.screen.query_one("#field-permissions-timeout", Input).value = "60"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
            body = str(app.screen.query_one("#confirm-message", Static).render())
            assert "agent-a" in body  # inherits nested-std, no own permissions — affected
            assert "agent-b" not in body  # already overrides the whole permissions group

    async def test_changing_a_field_requires_confirm_scoped_to_this_template_only(
        self, tmp_path, work_dir
    ):
        """The actual regression test this whole redesign exists for."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=1)  # agent:standard

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
            await _open_template_edit(pilot, app, row=1)

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
            await _open_template_edit(pilot, app, row=0)  # agent:other

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
            await _open_template_edit(pilot, app, row=1)  # agent:standard

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
            await _open_template_edit(pilot, app, row=1)

            app.screen.query_one("#field-timeout", Input).value = "not-a-number"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)


class TestTemplateEditWatcherTemplates:
    async def test_editing_a_watcher_template_field_requires_confirm_naming_the_watcher(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=3)  # watcher:wstd
            assert app.screen.query_one(
                "#field-history_handoff-fetch_count", Input).value == "25"

            app.screen.query_one(
                "#field-history_handoff-fetch_count", Input).value = "30"
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
            assert raw["watcher_templates"]["wstd"]["history_handoff"]["fetch_count"] == 30


class TestTemplateEditDiscard:
    async def test_escape_with_unsaved_changes_prompts_discard_confirm(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=1)

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
            await _open_template_edit(pilot, app, row=1)

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
            table.move_cursor(row=1)
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
            table.move_cursor(row=1)
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
            table.move_cursor(row=1)
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
            table.move_cursor(row=1)  # agent:standard — used by agent-a/agent-b
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


def _config_with_agent_template_tools(work_dir: Path) -> str:
    """'standard' sets owner_allowed_tools: [preset-a] — agent-a inherits it
    without overriding (genuinely affected by an edit to it), agent-b
    inherits it but already overrides its own owner_allowed_tools (should
    NOT show up in a blast-radius confirm)."""
    return f"""\
        tool_presets:
          preset-a:
            - tool: Bash
          preset-b:
            - tool: WebFetch
        agent_templates:
          standard:
            type: claude
            owner_allowed_tools: [preset-a]
        agents:
          agent-a:
            inherits: standard
            working_directory: {work_dir}
          agent-b:
            inherits: standard
            working_directory: {work_dir}
            owner_allowed_tools: [preset-b]
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - connector: rc
            agent: agent-a
            room: general
          - connector: rc
            agent: agent-b
            room: dev
    """


async def _click_template_tool_button(pilot, app, action: str, key: str = "owner_allowed_tools") -> None:
    button = app.screen.query_one(f"#{action}-tool-{key}")
    button.scroll_visible(animate=False)
    await pilot.pause()
    await pilot.click(f"#{action}-tool-{key}")
    await pilot.pause()


class TestTemplateToolListEditor:
    """User-reported: 'agent template does not have ways to edit
    owner_allowed_tools and guest_allowed_tools' — a real gap
    (gateway/config.py's agent_templates forbidden-keys is frozenset(), so
    both fields are already legal on a template; the config TUI's
    TemplateDetailScreen simply never grew an editor for them). Fixed by
    extracting AgentDetailScreen's own tool-list editor into
    ToolListEditorMixin (tool_list_editor.py) and reusing it here."""

    async def test_view_mode_shows_the_tool_rule_with_blast_radius(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_agent_template_tools(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # agent:standard
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            body = app.screen._body_text()
            assert "preset-a" in body
            assert "1 entries inherit, 1 override" in body  # agent-a / agent-b

    async def test_edit_mode_prefills_the_owner_tools_list(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_agent_template_tools(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            list_view = app.screen.query_one("#owner-tools-list")
            assert len(list_view.children) == 1

    async def test_edit_tool_button_starts_disabled(self, tmp_path, work_dir):
        """ToolListEditorMixin's "Edit" button starts disabled until a
        genuine selection is made (see test_configtool_agent_tool_list.py's
        TestAgentToolListEditButtonAvailability for the full regression
        suite on AgentDetailScreen's side of this shared mixin) — this one
        pins the same behavior for TemplateDetailScreen, the mixin's other
        host, which resets `_tool_list_ever_selected` in its own
        `_on_enter_edit_mode()` for the identical reason."""
        config_path = _write_config(tmp_path, _config_with_agent_template_tools(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            button = app.screen.query_one("#edit-tool-owner_allowed_tools")
            assert button.disabled is True

    async def test_adding_an_inline_rule_and_saving_confirms_and_writes_it(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_agent_template_tools(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            await _click_template_tool_button(pilot, app, "add")
            assert isinstance(app.screen, PresetOrInlineModal)
            # preset-a (0, already referenced), preset-b (1), inline (2), new_preset (3).
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, InlineToolRuleModal)
            app.screen.query_one("#rule-tool", Input).value = "Edit"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            list_view = app.screen.query_one("#owner-tools-list")
            assert len(list_view.children) == 2

            await pilot.press("ctrl+s")
            await pilot.pause()

            # Blast-radius confirm: agent-a inherits and doesn't override —
            # affected; agent-b already overrides owner_allowed_tools —
            # must NOT appear.
            assert isinstance(app.screen, ConfirmModal)
            body = str(app.screen.query_one("#confirm-message", Static).render())
            assert "agent-a" in body
            assert "agent-b" not in body
            await pilot.press("tab", "enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["owner_allowed_tools"] == [
                "preset-a",
                {"tool": "Edit"},
            ]

    async def test_untouched_tool_list_saves_without_a_blast_radius_confirm(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_agent_template_tools(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)  # no confirm at all
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["owner_allowed_tools"] == ["preset-a"]

    async def test_removing_the_only_item_writes_an_explicit_empty_list(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_agent_template_tools(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            list_view = app.screen.query_one("#owner-tools-list")
            list_view.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click(list_view.children[0])
            await pilot.pause()
            await _click_template_tool_button(pilot, app, "remove")
            assert len(list_view.children) == 0

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)  # agent-a is affected
            await pilot.press("tab", "enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["agent_templates"]["standard"]["owner_allowed_tools"] == []

    async def test_selection_survives_a_refresh_not_just_initial_selection(
        self, tmp_path, work_dir
    ):
        """PR review finding, coverage gap: TemplateDetailScreen mixes in
        the same ToolListEditorMixin AgentDetailScreen uses, and so shares
        the exact fix
        test_configtool_agent_tool_list.py's own
        test_selection_survives_a_refresh_not_just_initial_selection pins —
        but had no dedicated regression test of its own. Reproduced here
        with NO manual list_view.index = ... reselect between the Add and
        the Remove."""
        config_path = _write_config(tmp_path, _config_with_agent_template_tools(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=0)

            await _click_template_tool_button(pilot, app, "add")
            await pilot.press("down", "down", "enter")  # inline (index 2)
            await pilot.pause()
            app.screen.query_one("#rule-tool", Input).value = "Edit"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            list_view = app.screen.query_one("#owner-tools-list")
            assert len(list_view.children) == 2

            # No manual reselect — Remove should still act, not silently
            # no-op with "Select an item in the list first."
            await _click_template_tool_button(pilot, app, "remove")
            assert len(list_view.children) == 1

    async def test_connector_and_watcher_templates_have_no_tool_list_section(
        self, tmp_path, work_dir
    ):
        """owner_allowed_tools/guest_allowed_tools are agent-only concepts —
        connector/watcher templates must not render the section at all."""
        config_path = _write_config(tmp_path, _config_text(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template_edit(pilot, app, row=3)  # watcher:wstd

            assert not app.screen.query("#owner-tools-list")
            assert not app.screen.query("#add-tool-owner_allowed_tools")


class TestTemplateFormSharesTheEntryFormRenderer:
    """Codex review of #129, round 7. `TemplateDetailScreen` used to override
    `_compute_initial_values()` and `_compose_field_row()` wholesale to change
    one input each, so every improvement to field handling had to be applied
    twice — and one copy was forgotten (the raw delimiter-bearing display
    reached the entry forms, not this one). Both overrides are now two hooks
    (`_snapshot_source`, `_field_annotation`) over a single implementation.
    These pin both halves: the shared behaviour arrives here, and this
    screen's own difference still holds."""

    async def test_a_delimiter_bearing_value_is_displayed_raw_here_too(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, f"""\
            watcher_templates:
              shared:
                context_inject_files: ["my,notes.md"]
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TemplateDetailScreen)
            box = app.screen.query_one("#field-context_inject_files", Input)
            # One path, shown as one item — not the split `my, notes.md`.
            assert box.value == "my,notes.md"

    async def test_the_blast_radius_annotation_still_renders_after_the_unfork(
        self, tmp_path, work_dir
    ):
        """This screen annotates rows with blast radius, not provenance —
        the hook must keep that, including through the live-refresh path
        that previously only knew how to write provenance text."""
        config_path = _write_config(tmp_path, f"""\
            agent_templates:
              shared:
                timeout: 1800
            agents:
              a1:
                inherits: shared
                working_directory: {work_dir}
              a2:
                inherits: shared
                working_directory: {work_dir}
                timeout: 60
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            watchers: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TemplateDetailScreen)

            annotation = app.screen.query_one("#prov-field-timeout", Static)
            assert "1 inherit, 1 override" in str(annotation.render())

            # And it survives an edit (the refresh path).
            app.screen.query_one("#field-timeout", Input).value = "900"
            await pilot.pause()
            assert "1 inherit, 1 override" in str(
                app.screen.query_one("#prov-field-timeout", Static).render()
            )
