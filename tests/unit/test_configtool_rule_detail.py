"""Pilot-based tests for RuleDetailScreen and the Rules tab — the config
TUI's rewrite onto watcher rules (design §5.5, `impl/config-tooling`).

These replace the old test_configtool_watcher_detail.py suite wholesale: the
behaviours that suite pinned (merge-on-add, split-on-edit, rooms: groups,
rename-as-remove-plus-add) belonged to the static shape, which no longer
exists as data — a rule is one entry in one list, so its CRUD is the plain
trial-entry pattern connectors already use, plus the two things rules have
that nothing else does: load-bearing ORDER (first match wins → '['/']'
reorder on the list) and runtime strands (the delete warning's
stranded-session/orphaned-job counts).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import Checkbox, DataTable, Input

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal
from gateway.configtool.screens.overview import OverviewScreen
from gateway.configtool.screens.rule_detail import RuleDetailScreen


def _write_config(tmp_path: Path, yaml_text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(yaml_text))
    return str(path)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _config_with_rules(work_dir: Path) -> str:
    """Two rules under rc/default (order matters between them), plus a
    second connector+agent pairing for reassignment scenarios."""
    return f"""\
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
          - name: rc2
            type: rocketchat
            server: {{url: "http://localhost:3001", username: bot2, password: pw2}}
        agents:
          default:
            type: claude
            working_directory: {work_dir}
          other:
            type: claude
            working_directory: {work_dir}
        watchers:
          - name: eng-rooms
            connector: rc
            agent: default
            rooms:
              include: ["eng-*"]
          - name: catch-all
            connector: rc
            agent: default
            rooms:
              include: ["*"]
              except_for: ["*-noise"]
    """


def _config_with_no_rules(work_dir: Path) -> str:
    return f"""\
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        agents:
          default:
            type: claude
            working_directory: {work_dir}
        watchers: []
    """


async def _open_rules_tab(pilot, app) -> None:
    app.screen.query_one("TabbedContent").active = "tab-rules"
    await pilot.pause()


async def _open_rule_row(pilot, app, row: int, key: str) -> None:
    """key: 'e' to edit directly, 'enter' to view first."""
    await _open_rules_tab(pilot, app)
    table = app.screen.query_one("#rules-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press(key)
    await pilot.pause()


class TestRulesTable:
    async def test_rules_render_in_document_order_never_sorted(self, tmp_path, work_dir):
        """Order IS the semantics (first match wins) — the one tab that
        must NOT sort by name, or the display lies about routing."""
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            names = [table.get_row_at(i)[1] for i in range(table.row_count)]
            # 'catch-all' < 'eng-rooms' alphabetically — document order wins.
            assert names == ["eng-rooms", "catch-all"]

    async def test_a_broken_rule_shows_its_error_and_the_good_rule_still_renders(
        self, tmp_path, work_dir
    ):
        """The old Watchers tab displayed OK on broken rows (findings filed
        under a name/index spelling no row key matched) and dropped rule
        rows entirely, contradicting the banner. Every entry gets a row;
        the broken one's Status column carries the error."""
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
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - name: broken-rule
                connector: rc
                agent: default
                rooms:
                  include: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 2
            rows = {table.get_row_at(i)[1]: str(table.get_row_at(i)[5]) for i in range(2)}
            assert "error" in rows["broken-rule"].lower()
            assert "error" not in rows["good-rule"].lower()

    async def test_the_rooms_column_summarizes_the_matcher(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.get_row_at(1)[4] == "* (except: *-noise)"


class TestRuleReorder:
    async def test_bracket_keys_swap_the_rule_and_persist_the_new_order(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("]")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watchers"]] == ["catch-all", "eng-rooms"]
            # The cursor follows the moved rule to its new position.
            assert table.cursor_row == 1

            await pilot.press("[")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watchers"]] == ["eng-rooms", "catch-all"]

    async def test_moving_past_the_edge_is_a_no_op(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("[")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watchers"]] == ["eng-rooms", "catch-all"]


class TestRuleCreate:
    async def test_creating_a_rule_writes_name_connector_agent_and_matcher(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_no_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "create"

            app.screen.query_one("#field-name", Input).value = "my-rule"
            app.screen.query_one("#field-rooms-include", Input).value = "general, eng-*"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            (entry,) = raw["watchers"]
            assert entry["name"] == "my-rule"
            # connector/agent are written EXPLICITLY even untouched — never
            # left to the loader's config-order-dependent fallback.
            assert entry["connector"] == "rc"
            assert entry["agent"] == "default"
            assert entry["rooms"] == {"include": ["general", "eng-*"]}

    async def test_a_dm_only_rule_needs_no_include_patterns(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_no_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()

            app.screen.query_one("#field-name", Input).value = "dm-rule"
            app.screen.query_one("#field-rooms-direct", Checkbox).value = True
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            (entry,) = raw["watchers"]
            assert entry["rooms"] == {"direct": True}

    async def test_a_rule_with_no_name_is_refused_with_a_message(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_no_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-rooms-include", Input).value = "general"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            assert "name" in str(app.screen.query_one("#message-body").render()).lower()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == []

    async def test_a_rule_that_can_never_match_is_refused_before_the_generic_gate(
        self, tmp_path, work_dir
    ):
        """No include patterns and no DM opt-in — the loader refuses it too;
        the form says it in form terms first."""
        config_path = _write_config(tmp_path, _config_with_no_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "matchless"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "include" in body or "direct" in body

    async def test_a_duplicate_rule_name_is_refused(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "eng-rooms"
            app.screen.query_one("#field-rooms-include", Input).value = "general"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            assert "already exists" in str(app.screen.query_one("#message-body").render())
            await pilot.press("enter")
            await pilot.pause()
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["watchers"]) == 2  # nothing phantom appended


class TestRuleEdit:
    async def test_editing_the_include_list_updates_the_entry_in_place(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "edit"

            app.screen.query_one("#field-rooms-include", Input).value = "eng-*, ops-*"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"][0]["rooms"]["include"] == ["eng-*", "ops-*"]
            # Still exactly two entries, order untouched.
            assert [w["name"] for w in raw["watchers"]] == ["eng-rooms", "catch-all"]

    async def test_renaming_a_rule_keeps_its_position_and_other_fields(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            app.screen.query_one("#field-name", Input).value = "engineering"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watchers"]] == ["engineering", "catch-all"]
            assert raw["watchers"][0]["rooms"] == {"include": ["eng-*"]}

    async def test_renaming_onto_an_existing_name_is_refused_and_rolls_back(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            app.screen.query_one("#field-name", Input).value = "catch-all"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)
            # Nothing reached memory or disk.
            assert app.editable_config.watchers_raw[0]["name"] == "eng-rooms"
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"][0]["name"] == "eng-rooms"

    async def test_a_rejected_save_leaves_the_document_clean_for_a_retry(
        self, tmp_path, work_dir
    ):
        """Trial-entry rollback: a save the gate refuses must not leave the
        invalid trial sitting in the document — and a corrected retry from
        the SAME screen session must succeed."""
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            # except_for without include overlap → loader refuses → gate blocks.
            app.screen.query_one("#field-rooms-except_for", Input).value = "zzz-nothing"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")
            await pilot.pause()
            assert "except_for" not in app.editable_config.watchers_raw[0]["rooms"]

            # Retry with a fixed value from the same open form.
            app.screen.query_one("#field-rooms-except_for", Input).value = "eng-noise"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"][0]["rooms"]["except_for"] == ["eng-noise"]

    async def test_the_session_ttls_are_editable_rule_fields(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            app.screen.query_one("#field-session_idle_days", Input).value = "30"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"][0]["session_idle_days"] == 30


class TestRuleView:
    async def test_view_mode_shows_the_matcher_summary(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=1, key="enter")
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "view"
            body = app.screen._body_text()
            assert "catch-all" in body
            assert "* (except: *-noise)" in body
            assert "connector: rc" in body


class TestRuleDelete:
    async def test_deleting_a_rule_removes_its_entry(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # focus Delete, press it
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watchers"]] == ["catch-all"]

    async def test_the_delete_confirm_warns_with_the_stranded_counts(
        self, tmp_path, work_dir, monkeypatch
    ):
        """Design §5.5: deleting a rule warns with the persisted sessions it
        strands and the scheduled jobs it orphans — counted read-only off
        the daemon's own files (state_peek), never the control socket."""
        import gateway.configtool.screens.rule_detail as rule_detail_mod

        monkeypatch.setattr(
            rule_detail_mod, "stranded_by_rule", lambda name: (3, 2)
        )
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            message = str(app.screen.query_one("#confirm-message").render())
            assert "3 persisted session record(s)" in message
            assert "2 scheduled job(s)" in message
            # The recovery instruction must be the PUBLIC CLI spelling —
            # `acg schedule delete <job_id>` (nested subcommand), not the
            # internal control-socket command name `schedule-delete`
            # (Codex round 3: following the displayed instruction failed
            # at argument parsing exactly when the operator needed it).
            assert "schedule delete" in message
            assert "schedule-delete" not in message
            # The job clause must not overpromise: a session with pending
            # jobs is EXEMPT from expiry (WatcherLifecycle.expire_idle), so
            # the warning says its jobs keep running, not that the sweeps
            # will clean everything up.
            assert "exempt from expiry" in message

    async def test_no_strands_means_the_plain_confirm_message(
        self, tmp_path, work_dir, monkeypatch
    ):
        import gateway.configtool.screens.rule_detail as rule_detail_mod

        monkeypatch.setattr(
            rule_detail_mod, "stranded_by_rule", lambda name: (0, 0)
        )
        config_path = _write_config(tmp_path, _config_with_rules(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            message = str(app.screen.query_one("#confirm-message").render())
            assert "session record" not in message


class TestReorderAroundABrokenRule:
    async def test_moving_a_rule_past_a_broken_one_is_refused_and_swapped_back(
        self, tmp_path, work_dir
    ):
        """Known limitation, pinned deliberately: a rule parse error embeds
        the rule's LIST INDEX in its message ("Watcher rule at index N:
        ..."), and the save gate compares exact (kind, name, message)
        tuples — so moving a rule past a pre-existing broken one shifts the
        broken rule's index, its message changes, and the gate reads the
        old problem as a NEW one and refuses the move (loud, safe, nothing
        written; the notify carries the broken rule's own error). This
        contradicts the gate's "pre-existing problems never block unrelated
        saves" intent, but the fix belongs to the gate/parser message
        contract, not to this tab — if this test starts failing because the
        move now SUCCEEDS, that contract changed: update the config-tool
        docs' known-limitations note along with this test. (Owner-ratified
        as not-a-bug, 2026-08-19; the refusal notify says outright that a
        pre-existing broken rule blocks reordering — a pure swap cannot
        genuinely introduce a new per-entry error.)"""
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
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - name: broken-rule
                connector: rc
                agent: default
                rooms:
                  include: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("]")
            await pilot.pause()

            # Refused and swapped back — disk and memory both untouched.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w["name"] for w in raw["watchers"]] == ["good-rule", "broken-rule"]
            assert [w["name"] for w in app.editable_config.watchers_raw] == [
                "good-rule", "broken-rule",
            ]


class TestEmptyPrerequisiteGuards:
    """Internal review (lens B): the connector/agent dropdowns are the first
    enum fields whose options come from a config list that can be EMPTY,
    and Textual's Select(allow_blank=False) raises EmptySelectError at
    construction on empty options — mid-compose, taking the app down. Every
    entry into a non-view mode notifies instead."""

    def _config_with_no_connectors(self, work_dir: Path) -> str:
        return f"""\
            connectors: []
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - name: orphan-rule
                connector: rc-gone
                agent: default
                rooms:
                  include: [general]
        """

    async def test_n_with_zero_connectors_notifies_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with_no_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            assert app.is_running is True
            assert isinstance(app.screen, OverviewScreen)

    async def test_e_on_a_rule_with_zero_connectors_notifies_instead_of_crashing(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with_no_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            assert app.is_running is True
            assert isinstance(app.screen, OverviewScreen)

    async def test_view_to_edit_with_zero_connectors_stays_in_view_mode(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with_no_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "view"
            await pilot.press("e")
            await pilot.pause()
            assert app.is_running is True
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "view"


class TestCodexRound1Fixes:
    async def test_an_untouched_agent_select_honors_the_configured_default_agent(
        self, tmp_path, work_dir
    ):
        """Codex review of #129 (P1): the agent prefill used next(iter(agents))
        — the FIRST agent — even when a top-level `default_agent:` named
        another, and create mode force-writes the selection explicitly, so
        an untouched Agent field silently bound the new rule to the wrong
        backend."""
        config_path = _write_config(tmp_path, f"""\
            default_agent: zother
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              aaa-first:
                type: claude
                working_directory: {work_dir}
              zother:
                type: claude
                working_directory: {work_dir}
            watchers: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "my-rule"
            app.screen.query_one("#field-rooms-include", Input).value = "general"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            (entry,) = raw["watchers"]
            assert entry["agent"] == "zother"

    async def test_a_typed_name_survives_an_inherits_template_switch(
        self, tmp_path, work_dir
    ):
        """Codex review of #129 (P2): `name` is a generic FieldSpec on this
        screen, so the base recompute rebuilt its Input from the (empty)
        entry — a typed-but-unsaved name vanished on every template switch,
        and Save was then refused in create mode."""
        config_path = _write_config(tmp_path, f"""\
            watcher_templates:
              slow:
                session_idle_days: 30
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
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "my-rule"
            await pilot.pause()

            # Switch templates the way the picker's confirm path does.
            app.screen._inherits_current = "slow"
            await app.screen._recompute_form()
            await pilot.pause()

            assert app.screen.query_one("#field-name", Input).value == "my-rule"

    async def test_the_stranded_lookup_uses_the_canonical_stripped_name(
        self, tmp_path, work_dir, monkeypatch
    ):
        """Codex review of #129 (P2): the loader strips a rule's name before
        persisting it as records' rule_name — an externally-authored
        `name: " padded "` must be stripped before the stranded lookup, or
        the disclosure is silently suppressed."""
        import gateway.configtool.screens.rule_detail as rule_detail_mod

        seen: list[str] = []

        def capture(name):
            seen.append(name)
            return (0, 0)

        monkeypatch.setattr(rule_detail_mod, "stranded_by_rule", capture)
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
              - name: " padded "
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="enter")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            assert seen == ["padded"]

    async def test_a_malformed_watchers_scalar_renders_zero_rows_and_move_is_a_no_op(
        self, tmp_path, work_dir
    ):
        """Codex review of #129 (P2): `watchers: 5` parses as YAML but is
        not a list — iterating it crashed the overview repaint with a
        TypeError, and `[`/`]` crashed move_watcher_rule. Normalized to an
        empty view everywhere (EditableConfig.watcher_entries); the banner
        still reports the config's real error."""
        config_path = _write_config(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers: 5
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.is_running is True
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 0
            await pilot.press("]")
            await pilot.pause()
            assert app.is_running is True
            # Nothing was written — the scalar is still on disk, untouched.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == 5


class TestCodexRound2Fixes:
    def _config_with_template(self, work_dir: Path) -> str:
        return f"""\
            watcher_templates:
              slow:
                session_idle_days: 30
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - name: old-name
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """

    async def test_a_cleared_name_survives_a_template_switch(self, tmp_path, work_dir):
        """Codex round 2: `_name_live` truthiness can't distinguish 'cleared
        to empty' from 'never touched' — clearing the name and then picking
        a template silently resurrected the old identity."""
        config_path = _write_config(tmp_path, self._config_with_template(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-name", Input).value = ""
            await pilot.pause()

            app.screen._inherits_current = "slow"
            await app.screen._recompute_form()
            await pilot.pause()

            assert app.screen.query_one("#field-name", Input).value == ""

    async def test_an_untouched_name_still_shows_the_entrys_own_after_a_switch(
        self, tmp_path, work_dir
    ):
        """The other half of the same gate: an untouched form ('' because
        nothing was ever typed) must keep showing the entry's own name
        after a template switch, not get blanked by an over-eager restore."""
        config_path = _write_config(tmp_path, self._config_with_template(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")

            app.screen._inherits_current = "slow"
            await app.screen._recompute_form()
            await pilot.pause()

            assert app.screen.query_one("#field-name", Input).value == "old-name"


class TestCodexRound3Fixes:
    async def test_creating_over_a_mapping_watchers_block_is_refused_not_overwritten(
        self, tmp_path, work_dir
    ):
        """Codex round 3: `watchers:` as a MAPPING (an operator omitted the
        '-' before an otherwise complete rule) holds recoverable data —
        normalizing it to [] and appending would pass the save gate (the
        structural error vanishes with the data) and silently delete the
        rule the user meant to keep."""
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
              name: forgot-the-dash
              connector: rc
              agent: default
              rooms:
                include: [general]
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#field-name", Input).value = "new-rule"
            app.screen.query_one("#field-rooms-include", Input).value = "dev"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "not a list" in body
            await pilot.press("enter")
            await pilot.pause()
            # The mapping — the user's recoverable rule — is untouched.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"]["name"] == "forgot-the-dash"

    def _config_with_a_garbage_entry(self, work_dir: Path) -> str:
        return f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - "stray yaml, not a mapping"
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """

    async def test_d_deletes_a_non_mapping_entry_by_index(self, tmp_path, work_dir):
        """Codex round 3: the non-mapping entry renders as an ERROR row on
        purpose — 'visible but unremovable' was half a fix. 'd' deletes it
        by list index (there is no dict for a detail screen to open)."""
        config_path = _write_config(tmp_path, self._config_with_a_garbage_entry(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 2
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w.get("name") for w in raw["watchers"]] == ["good-rule"]

    async def test_enter_and_e_on_a_non_mapping_entry_notify_instead_of_dead_keys(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, self._config_with_a_garbage_entry(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)  # no crash, no push
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert app.is_running is True


class TestCodexRound4Fixes:
    async def test_editing_a_rule_whose_list_field_is_a_scalar_does_not_crash(
        self, tmp_path, work_dir
    ):
        """Codex round 4: the validator marks `rooms.include: 5` an ERROR
        row and the form is how the operator is meant to repair it — but
        composing it passed the raw int to list_to_text(), raising
        TypeError mid-compose and taking the whole TUI down."""
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
              - name: broken-list
                connector: rc
                agent: default
                rooms:
                  include: 5
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rule_row(pilot, app, row=0, key="e")
            assert app.is_running is True
            assert isinstance(app.screen, RuleDetailScreen)
            assert app.screen.mode == "edit"
            # Shown verbatim as one item — visible and repairable, never
            # silently rewritten.
            assert app.screen.query_one("#field-rooms-include", Input).value == "5"

    async def test_deleting_a_malformed_row_above_another_broken_rule_explains_the_order(
        self, tmp_path, work_dir
    ):
        """Owner-ratified as the same known limitation the reorder refusal
        carries (2026-08-19): a removal renumbers later entries, so a broken
        rule BELOW has its index-embedded error shift and the save gate
        reads the pre-existing problem as new. Not fixed at the gate — the
        refusal is loud, the row is restored, and the message states the
        bottom-up repair order instead of just quoting the gate."""
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
              - "stray yaml, not a mapping"
              - name: also-broken
                connector: rc
                agent: default
                rooms:
                  include: []
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "LOWEST ERROR row first" in body
            await pilot.press("enter")
            await pilot.pause()
            # Rolled back — the garbage row is still there, nothing written.
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["watchers"]) == 2
            assert raw["watchers"][0] == "stray yaml, not a mapping"

    async def test_deleting_the_lowest_broken_row_succeeds(self, tmp_path, work_dir):
        """The other half of the documented order: bottom-up works."""
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
              - name: good-rule
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - "stray yaml, not a mapping"
        """)
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_rules_tab(pilot, app)
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert [w.get("name") for w in raw["watchers"]] == ["good-rule"]
