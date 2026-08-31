"""The TUI can edit an inheritable `rooms:`, and reports the matcher a rule really has.

`rooms` became inheritable from a `watcher_templates:` entry when the loader
stopped sniffing an entry's shape (see tests/unit/test_rooms_inheritance.py for
the loader side). The config tool did not follow, and the two halves of that
failed differently:

* **The template form had no rooms fields**, so an inheritable matcher could only
  be written by hand in `$EDITOR`. Visible, and merely missing.
* **The Rules table and the rule view summarised the RAW entry**, so a rule
  inheriting its matcher displayed `rooms: (none)` while actually serving
  `eng-*` and every 1:1 DM. Silent, and the worse of the two: it does not
  withhold the routing, it states the wrong one. The connector and agent cells
  beside it already read the merged entry — `rooms` was the single field that
  did not.

The first two classes below pin the display against the LOADER's own answer
rather than against an expected string, so the assertion cannot drift into
agreeing with a wrong summary.

Run with:
    uv run python -m pytest tests/unit/test_configtool_rooms_inheritance.py -v
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import Checkbox, DataTable, Input, Static, TabbedContent

from gateway.config import GatewayConfig
from gateway.configtool.app import ConfigToolApp
from gateway.configtool.model import EditableConfig
from gateway.configtool.screens.rule_detail import (
    WATCHER_TEMPLATE_DATACLASS_DEFAULTS,
    WATCHER_TEMPLATE_FIELDS,
    rule_rooms_summary,
)
from gateway.configtool.modals import ConfirmModal
from gateway.configtool.screens.template_detail import TemplateDetailScreen

ROOMS_KEYS = ("rooms.include", "rooms.except_for", "rooms.direct", "rooms.group_direct")


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _write(tmp_path: Path, body: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body))
    return str(path)


def _config(work_dir: Path, template_rooms: str, rules: str) -> str:
    return f"""\
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: http://localhost:3000, username: bot, password: pw}}
        agents:
          a:
            type: claude
            working_directory: {work_dir}
        watcher_templates:
          channels:
            connector: rc
            agent: a
            rooms: {template_rooms}
        watcher_rules:
{textwrap.indent(textwrap.dedent(rules), "          ")}
"""


class TestTheRoomsFieldsAreEditableOnATemplate:
    """The owner's report: no rooms fields in the watcher-rule template form."""

    def test_the_template_field_list_carries_all_four(self):
        keys = [spec.key for spec in WATCHER_TEMPLATE_FIELDS]
        for key in ROOMS_KEYS:
            assert key in keys, f"{key} is not offered on a watcher-rule template"

    def test_each_has_a_default_so_the_form_can_render_it(self):
        """`WATCHER_TEMPLATE_DATACLASS_DEFAULTS` is derived from the field list and
        raises KeyError at import for a spec with no default — this asserts the
        four are present rather than that the module merely imported."""
        for key in ROOMS_KEYS:
            assert key in WATCHER_TEMPLATE_DATACLASS_DEFAULTS

    def test_the_defaults_are_the_loader_s_own(self):
        """Read off `RoomMatcher()`, not retyped — the same anti-drift rule the
        session TTLs already follow (a past commit changed one side only)."""
        from gateway.core.watcher_rule import RoomMatcher

        matcher = RoomMatcher()
        assert WATCHER_TEMPLATE_DATACLASS_DEFAULTS["rooms.include"] == list(matcher.include)
        assert WATCHER_TEMPLATE_DATACLASS_DEFAULTS["rooms.direct"] is matcher.direct
        assert WATCHER_TEMPLATE_DATACLASS_DEFAULTS["rooms.group_direct"] is matcher.group_direct

    def test_the_rule_form_does_not_render_them_twice(self):
        """The rule form used to add the rooms specs itself; now they arrive with
        the template fields, and listing both would duplicate every widget id."""
        keys = [spec.key for spec in WATCHER_TEMPLATE_FIELDS]
        assert len(keys) == len(set(keys)), "duplicate field keys in the form"


class TestTheDisplayReportsTheMatcherTheRuleActuallyHas:
    """Pinned against the loader, so a wrong summary cannot become the expectation."""

    def _summaries(self, config_path: str) -> list[tuple[str, str, str]]:
        cfg = EditableConfig.load(config_path)
        loaded = GatewayConfig.from_file(config_path).watcher_rules
        rows = []
        for entry, rule in zip(cfg.watchers_raw, loaded):
            merged = cfg.merged_entry("watcher", entry)
            expected_parts = [p.raw for p in rule.rooms.include]
            rows.append((entry["name"], rule_rooms_summary(merged), ",".join(expected_parts)))
        return rows

    def test_an_inheriting_rule_does_not_display_as_serving_nothing(self, tmp_path, work_dir):
        """The regression. Before the fix this row read "(none)"."""
        path = _write(tmp_path, _config(
            work_dir, "{include: [eng-*], direct: true}", "- {name: eng, inherits: channels}",
        ))
        (name, summary, includes) = self._summaries(path)[0]
        assert summary != "(none)"
        assert "eng-*" in summary
        assert "+dm" in summary, "the inherited DM opt-in is part of the routing too"

    def test_every_pattern_the_loader_resolved_is_shown(self, tmp_path, work_dir):
        path = _write(tmp_path, _config(
            work_dir,
            "{include: [eng-*], except_for: ['*-secret'], direct: true}",
            """\
            - {name: eng, inherits: channels}
            - {name: ops, inherits: channels, rooms: {include: [ops-*]}}
            """,
        ))
        for name, summary, includes in self._summaries(path):
            for pattern in includes.split(","):
                assert pattern in summary, f"{name}: {pattern!r} missing from {summary!r}"

    def test_an_overriding_rule_shows_its_own_list_and_the_inherited_rest(
        self, tmp_path, work_dir
    ):
        """The key-by-key merge, as displayed: `ops` replaces `include` and keeps
        the template's `except_for` and `direct`."""
        path = _write(tmp_path, _config(
            work_dir,
            "{include: [eng-*], except_for: ['*-secret'], direct: true}",
            "- {name: ops, inherits: channels, rooms: {include: [ops-*]}}",
        ))
        (_, summary, _) = self._summaries(path)[0]
        assert "ops-*" in summary
        assert "eng-*" not in summary, "the replaced list must not linger"
        assert "*-secret" in summary
        assert "+dm" in summary

    def test_a_rule_with_no_matcher_anywhere_still_reads_none(self, tmp_path, work_dir):
        """The "(none)" case has to survive — it is correct when there really is
        no matcher, and a fix that removed it would hide a broken rule. (This
        config does not load; the TUI must still render it, which is the whole
        reason the summary is defensive.)"""
        path = _write(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              a:
                type: claude
                working_directory: {work_dir}
            watcher_templates:
              channels:
                connector: rc
                agent: a
            watcher_rules:
              - {{name: eng, inherits: channels}}
        """)
        cfg = EditableConfig.load(path)
        entry = cfg.watchers_raw[0]
        assert rule_rooms_summary(cfg.merged_entry("watcher", entry)) == "(none)"


class TestTheRulesTableUsesTheMergedMatcher:
    async def test_the_rooms_cell_is_not_none_for_an_inheriting_rule(self, tmp_path, work_dir):
        path = _write(tmp_path, _config(
            work_dir, "{include: [eng-*], direct: true}", "- {name: eng, inherits: channels}",
        ))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-rules"
            await pilot.pause()
            table = app.screen.query_one("#rules-table", DataTable)
            row = [str(c) for c in table.get_row_at(0)]
            assert "eng-*" in " ".join(row), row
            assert "(none)" not in " ".join(row), row


class TestSavingRoomsOnATemplateWritesThem:
    async def test_the_form_writes_rooms_under_watcher_templates(self, tmp_path, work_dir):
        """End to end: type patterns into the template form, save, and the
        document gains `watcher_templates.channels.rooms.include`."""
        path = _write(tmp_path, _config(
            work_dir, "{include: [eng-*]}", "- {name: eng, inherits: channels}",
        ))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            target = next(
                r for r in range(table.row_count)
                if str(table.get_row_at(r)[1]) == "channels"
            )
            table.move_cursor(row=target)
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TemplateDetailScreen)

            field = app.screen.query_one("#field-rooms-include", Input)
            assert field.value == "eng-*", "the existing value is loaded, not blank"
            field.value = "eng-*, ops-*"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            # Changing a template field that an entry inherits without
            # overriding raises the blast-radius confirm — `rooms` is no
            # exception, and the rule it will re-route is named.
            assert isinstance(app.screen, ConfirmModal)
            assert "eng" in str(app.screen.query_one("#confirm-message", Static).render())
            await pilot.press("tab", "enter")
            await pilot.pause()

        document = yaml.safe_load(Path(path).read_text())
        assert document["watcher_templates"]["channels"]["rooms"]["include"] == [
            "eng-*", "ops-*",
        ]

    async def test_a_dm_flag_saves_on_a_template(self, tmp_path, work_dir):
        path = _write(tmp_path, _config(
            work_dir, "{include: [eng-*]}", "- {name: eng, inherits: channels}",
        ))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-templates"
            await pilot.pause()
            table = app.screen.query_one("#templates-table", DataTable)
            table.focus()
            target = next(
                r for r in range(table.row_count)
                if str(table.get_row_at(r)[1]) == "channels"
            )
            table.move_cursor(row=target)
            await pilot.press("e")
            await pilot.pause()

            app.screen.query_one("#field-rooms-direct", Checkbox).value = True
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            if isinstance(app.screen, ConfirmModal):
                await pilot.press("tab", "enter")
                await pilot.pause()

        document = yaml.safe_load(Path(path).read_text())
        assert document["watcher_templates"]["channels"]["rooms"]["direct"] is True


class TestRevertingAFieldToInheritedActuallyWorks:
    """Owner-reported: "there is no way for me to change explicit to inherited?
    I press Ctrl-r, but it is still showing (explicit)".

    Two independent defects were behind that, both fallout from `rooms` becoming
    inheritable, and the visible one was not the blocking one.
    """

    async def _open_rule_form(self, app, pilot):
        app.screen.query_one(TabbedContent).active = "tab-rules"
        await pilot.pause()
        table = app.screen.query_one("#rules-table", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("e")
        await pilot.pause()

    async def test_the_save_gate_judges_the_merged_rule_not_the_raw_entry(
        self, tmp_path, work_dir
    ):
        """THE BLOCKER. The gate refuses a rule that can never match anything,
        and it read the raw entry — so a rule whose `include` lives in its
        template was refused the moment its own DM flags reverted, with
        "A rule needs at least one rooms include pattern...". The loader judges
        the merged rule; the form was stricter than the thing it speaks for."""
        path = _write(tmp_path, _config(
            work_dir,
            "{include: ['*'], direct: true}",
            "- {name: r1, inherits: channels, rooms: {direct: true, group_direct: true}}",
        ))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._open_rule_form(app, pilot)
            for key in ("field-rooms-direct", "field-rooms-group_direct"):
                app.screen.query_one("#" + key, Checkbox).focus()
                await pilot.pause()
                await pilot.press("ctrl+r")
                await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert not isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.__class__.__name__ == "OverviewScreen", (
                f"save was refused: {app.screen.__class__.__name__}"
            )

        entry = yaml.safe_load(Path(path).read_text())["watcher_rules"][0]
        assert "rooms" not in entry, "both reverted keys emptied the block, so it is gone"
        rooms = GatewayConfig.from_file(path).watcher_rules[0].rooms
        assert [p.raw for p in rooms.include] == ["*"], "the rule now inherits the matcher"
        assert rooms.direct is True

    async def test_a_rule_with_no_matcher_and_no_template_is_still_refused(
        self, tmp_path, work_dir
    ):
        """The case the gate exists for. Merging must not weaken it: with no
        template supplying a matcher, merged == raw and the refusal stands."""
        path = _write(tmp_path, f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              a:
                type: claude
                working_directory: {work_dir}
            watcher_rules:
              - {{name: r1, connector: rc, agent: a, rooms: {{direct: true}}}}
        """)
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._open_rule_form(app, pilot)
            app.screen.query_one("#field-rooms-direct", Checkbox).focus()
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert app.screen.__class__.__name__ == "MessageModal", "must be refused"
            body = str(app.screen.query_one("#message-body").render())
            assert "include pattern" in body

        entry = yaml.safe_load(Path(path).read_text())["watcher_rules"][0]
        assert entry["rooms"] == {"direct": True}, "the rejected save wrote nothing"

    async def test_the_label_updates_even_when_the_value_does_not_change(
        self, tmp_path, work_dir
    ):
        """THE VISIBLE ONE, and the reason the blocker was misdiagnosed.

        `action_reset_field()` used to leave the provenance label to the
        widget's own Changed event. Here the entry's only `rooms` key is
        `direct` and the template sets the same value, so ctrl+r changes no
        widget value, fires no event — and the label kept saying "(explicit)"
        beside a toast promising it would revert.
        """
        path = _write(tmp_path, _config(
            work_dir, "{include: ['*'], direct: true}",
            "- {name: r1, inherits: channels, rooms: {direct: true}}",
        ))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._open_rule_form(app, pilot)
            checkbox = app.screen.query_one("#field-rooms-direct", Checkbox)
            checkbox.focus()
            await pilot.pause()

            def label() -> str:
                return str(
                    app.screen.query_one("#prov-field-rooms-direct", Static).render()
                )

            before_value, before_label = checkbox.value, label()
            assert "explicit" in before_label

            await pilot.press("ctrl+r")
            await pilot.pause()

            assert checkbox.value == before_value, "the premise: no value change"
            assert "channels" in label(), (
                f"label did not follow the reset: {label()!r}"
            )

    async def test_the_normal_value_changing_reset_still_updates(self, tmp_path, work_dir):
        """The path that already worked, kept working — the direct call must not
        depend on the event no longer firing."""
        path = _write(tmp_path, _config(
            work_dir, "{include: ['*']}",
            "- {name: r1, inherits: channels, rooms: {direct: true}}",
        ))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._open_rule_form(app, pilot)
            checkbox = app.screen.query_one("#field-rooms-direct", Checkbox)
            checkbox.focus()
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert checkbox.value is False, "the template does not set direct"
            assert "channels" in str(
                app.screen.query_one("#prov-field-rooms-direct", Static).render()
            )
