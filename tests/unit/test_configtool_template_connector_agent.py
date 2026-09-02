"""A watcher-rule template can set `connector` and `agent` from the TUI.

The loader has allowed a rule to take both from its `inherits:` template for a
while (gateway/config.py, "Shared with a template"), and the user guide says
so. The TUI could READ them — a rule inheriting `connector` showed
"(from 'tmpl')" — but the template form had no field for either, so the only
way to put them on a template was to hand-edit config.yaml. These tests pin the
form, the save, and the round trip through the real loader.

Run with:
    uv run python -m pytest tests/unit/test_configtool_template_connector_agent.py -v
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Select, Static, TabbedContent

from gateway.config import GatewayConfig
from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal
from gateway.configtool.screens.rule_detail import RuleDetailScreen
from gateway.configtool.screens.template_detail import TemplateDetailScreen


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _config(work_dir: Path, template_body: str, rule_extra: str = "") -> str:
    return textwrap.dedent(f"""\
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
          - name: mm
            type: mattermost
            server: {{url: "http://localhost:8065", token: t, team: eng}}
        agents:
          worker:
            type: claude
            working_directory: {work_dir}
          reviewer:
            type: claude
            working_directory: {work_dir}
        watcher_templates:
          base:
            {template_body}
        watcher_rules:
          - name: eng
            inherits: base
            {rule_extra}
            rooms:
              include: [eng-*]
    """)


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


async def _open_template(pilot, app, name: str) -> None:
    app.screen.query_one(TabbedContent).active = "tab-templates"
    await pilot.pause()
    table = app.screen.query_one("#templates-table", DataTable)
    table.focus()
    row = next(r for r in range(table.row_count) if str(table.get_row_at(r)[1]) == name)
    table.move_cursor(row=row)
    await pilot.press("e")
    await pilot.pause()
    assert isinstance(app.screen, TemplateDetailScreen)


class TestTheTemplateFormHasTheTwoFields:
    async def test_they_are_present_and_blank_when_the_template_sets_neither(
        self, tmp_path, work_dir,
    ):
        path = _write(tmp_path, _config(work_dir, "session_idle_days: 7",
                                        rule_extra="connector: rc\n            agent: worker"))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template(pilot, app, "base")

            connector = app.screen.query_one("#field-connector", Select)
            agent = app.screen.query_one("#field-agent", Select)
            assert connector.value is Select.NULL, "unset renders blank, not the first option"
            assert agent.value is Select.NULL
            # The options are the configured names, not a hard-coded list.
            assert sorted(v for _, v in connector._options if v is not Select.NULL) == ["mm", "rc"]
            assert sorted(v for _, v in agent._options if v is not Select.NULL) == ["reviewer", "worker"]

    async def test_a_template_that_sets_them_shows_them_selected(self, tmp_path, work_dir):
        path = _write(tmp_path, _config(work_dir, "connector: mm\n            agent: reviewer"))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template(pilot, app, "base")

            assert app.screen.query_one("#field-connector", Select).value == "mm"
            assert app.screen.query_one("#field-agent", Select).value == "reviewer"

    async def test_the_rule_form_still_renders_each_field_exactly_once(self, tmp_path, work_dir):
        """The two specs are built per-instance in the TEMPLATE form and not
        added to `WATCHER_TEMPLATE_FIELDS`, which the rule form spreads after
        its own connector/agent specs — adding them there would render both
        fields twice on every rule form."""
        path = _write(tmp_path, _config(work_dir, "connector: rc\n            agent: worker"))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-rules"
            await pilot.pause()
            table = app.screen.query_one("#rules-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, RuleDetailScreen)

            assert len(app.screen.query("#field-connector")) == 1
            assert len(app.screen.query("#field-agent")) == 1


class TestSavingThemOnTheTemplate:
    async def test_setting_both_writes_them_and_the_loader_accepts_the_inheriting_rule(
        self, tmp_path, work_dir,
    ):
        """The whole point: a rule that names neither field itself loads,
        because the template now carries them — through the REAL loader, not a
        YAML assertion alone."""
        path = _write(tmp_path, _config(work_dir, "session_idle_days: 7"))
        # Before: the rule has no connector/agent anywhere → the loader refuses.
        with pytest.raises(Exception):
            GatewayConfig.from_file(path)

        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template(pilot, app, "base")
            app.screen.query_one("#field-connector", Select).value = "rc"
            app.screen.query_one("#field-agent", Select).value = "worker"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            if isinstance(app.screen, ConfirmModal):  # blast-radius confirm names 'eng'
                assert "eng" in str(app.screen.query_one("#confirm-message", Static).render())
                await pilot.press("tab", "enter")
                await pilot.pause()

        document = yaml.safe_load(Path(path).read_text())
        assert document["watcher_templates"]["base"]["connector"] == "rc"
        assert document["watcher_templates"]["base"]["agent"] == "worker"
        assert "connector" not in document["watcher_rules"][0], "written to the template, not the rule"

        rule = [r for r in GatewayConfig.from_file(path).watcher_rules if r.name == "eng"][0]
        assert (rule.connector, rule.agent) == ("rc", "worker")

    async def test_leaving_them_blank_writes_no_key(self, tmp_path, work_dir):
        """Blank is "the template does not set this", so the key must be absent
        — not `null`, which the loader would read as an explicit wrong value."""
        path = _write(tmp_path, _config(work_dir, "session_idle_days: 7",
                                        rule_extra="connector: rc\n            agent: worker"))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template(pilot, app, "base")
            app.screen.query_one("#field-session_idle_days").value = "9"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            if isinstance(app.screen, ConfirmModal):
                await pilot.press("tab", "enter")
                await pilot.pause()

        base = yaml.safe_load(Path(path).read_text())["watcher_templates"]["base"]
        assert base["session_idle_days"] == 9
        assert "connector" not in base and "agent" not in base

    async def test_changing_the_templates_connector_warns_about_the_rules_it_reroutes(
        self, tmp_path, work_dir,
    ):
        """Same blast-radius confirm `rooms` gets: a rule inheriting `connector`
        without overriding it will start running on a different platform."""
        path = _write(tmp_path, _config(work_dir, "connector: rc\n            agent: worker"))
        app = ConfigToolApp(path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_template(pilot, app, "base")
            app.screen.query_one("#field-connector", Select).value = "mm"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
            assert "eng" in str(app.screen.query_one("#confirm-message", Static).render())
