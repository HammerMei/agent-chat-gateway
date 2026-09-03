"""The TUI calls a watcher rule a "watcher rule", and never shows the internal kind.

Two names were leaking into the interface, both because a string doing one job
was reused for another:

* The Rules tab, the rule screen and its messages said "rule". The Tool Presets
  tab has rules too, so "Delete rule 'x'?" did not say which kind was going, and
  neither matched the `watcher_rules:` key an operator edits by hand.
* Template kinds double as config-key fragments — every writer builds
  `f"{kind}_templates"` — so any screen interpolating a bare `kind` into a
  sentence displayed the internal spelling. The Templates tab offered "watcher"
  as a kind to create, next to a tab labelled "Watcher Rules".

`kind_label()` splits the two roles apart. What these tests protect is the split
itself: the label has to reach the operator, and the kind has to reach the
document. A rename that changed the picker's dismissed value instead of its
displayed label would write a `watcher rule_templates:` key, so the pilot test
below asserts BOTH ends of that one flow rather than the label alone.

Run with:
    uv run python -m pytest tests/unit/test_configtool_watcher_rule_naming.py -v
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
from textual.widgets import Input, Label, ListView, Static, TabbedContent

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.formatting import kind_label
from gateway.configtool.modals import TextPromptModal, TypePickerModal
from gateway.configtool.screens.template_detail import TEMPLATE_KINDS, TemplateDetailScreen

CONFIGTOOL_DIR = Path(__file__).resolve().parents[2] / "gateway" / "configtool"


def _write_config(tmp_path: Path, work_dir: Path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(f"""\
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: http://localhost:3000, username: bot, password: pw}}
        agents:
          my-agent:
            type: claude
            working_directory: {work_dir}
        watcher_rules:
          - name: w1
            connector: rc
            agent: my-agent
            rooms: {{include: [general]}}
    """))
    return str(path)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


class TestTheLabelHelper:
    def test_watcher_displays_as_watcher_rule(self):
        assert kind_label("watcher") == "watcher rule"

    def test_the_other_kinds_are_unchanged(self):
        assert kind_label("agent") == "agent"
        assert kind_label("connector") == "connector"

    def test_it_is_lowercase_so_it_reads_mid_sentence(self):
        """Callers capitalise when it starts a sentence; a helper returning
        "Watcher rule" would produce "Delete Watcher rule template 'x'?"."""
        for kind in TEMPLATE_KINDS:
            assert kind_label(kind) == kind_label(kind).lower()

    def test_every_kind_has_a_label(self):
        """A kind added to TEMPLATE_KINDS without a label entry falls through to
        its own name, which is right for `agent`/`connector` and would be wrong
        silently for a future multi-word kind — so this asserts the fallback is
        reached deliberately, not that it is absent."""
        for kind in TEMPLATE_KINDS:
            assert kind_label(kind), f"{kind} renders as empty"


class TestNoScreenInterpolatesABareKind:
    """The mechanical half. `f"{kind} template"` is the exact pattern that put
    "watcher" on screen, and it is invisible in review because it looks like
    every other f-string. Anything left has to be a config key or a row key."""

    ALLOWED = (
        "_templates",   # f"{kind}_templates" — the document key
        ":{name}",      # f"{kind}:{name}"    — a DataTable row key
    )

    def test_a_kind_reaches_display_only_through_kind_label(self):
        offenders = []
        for path in sorted(CONFIGTOOL_DIR.rglob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for match in re.finditer(r"\{(?:self\.)?kind\}", line):
                    tail = line[match.end():match.end() + 12]
                    if any(tail.startswith(a) for a in self.ALLOWED):
                        continue
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
        assert not offenders, (
            "these interpolate the internal kind into display text; wrap in "
            "kind_label():\n  " + "\n  ".join(offenders)
        )

    def test_the_guard_would_catch_a_bare_interpolation(self):
        """Proves the regex above matches the shape it claims to — otherwise a
        clean run means nothing."""
        planted = 'yield Static(f"New {kind} template")'
        assert re.search(r"\{(?:self\.)?kind\}", planted)
        tail = planted[planted.index("{kind}") + len("{kind}"):]
        assert not any(tail.startswith(a) for a in self.ALLOWED)


class TestTheOverviewNamesTheTabForWatcherRules:
    async def test_the_tab_is_labelled_watcher_rules(self, tmp_path, work_dir):
        app = ConfigToolApp(_write_config(tmp_path, work_dir))
        async with app.run_test() as pilot:
            await pilot.pause()
            pane = app.screen.query_one("TabbedContent").get_pane("tab-rules")
            assert "Watcher Rules" in str(pane._title)

    async def test_the_tab_id_is_unchanged(self, tmp_path, work_dir):
        """The id is what `check_action()`, the reorder binding and every test
        select on — renaming the label must not move it."""
        app = ConfigToolApp(_write_config(tmp_path, work_dir))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-rules"
            await pilot.pause()
            assert app.screen.query_one(TabbedContent).active == "tab-rules"


class TestTheTemplatePickerShowsTheLabelAndReturnsTheKind:
    """The flow the owner asked about: Templates tab → 'n' → pick a kind."""

    async def test_the_picker_offers_watcher_rule_not_watcher(self, tmp_path, work_dir):
        app = ConfigToolApp(_write_config(tmp_path, work_dir))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-templates"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, TypePickerModal)

            labels = [
                str(item.query_one(Label).render())
                for item in app.screen.query_one(ListView).children
            ]
            assert "watcher rule" in labels
            assert "watcher" not in labels, "the internal kind must not be offered"

    async def test_choosing_it_still_creates_a_watcher_template(self, tmp_path, work_dir):
        """The half that a label-only rename would break: the value dismissed by
        the picker is what builds the document key."""
        app = ConfigToolApp(_write_config(tmp_path, work_dir))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-templates"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()

            # agent, connector, watcher — walk to the third option.
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptModal)
            assert "watcher rule template" in str(
                app.screen.query_one("#prompt-title", Static).render()
            ), "the name prompt uses the label too"

            app.screen.query_one("#prompt-input", Input).value = "shared"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TemplateDetailScreen)
            assert app.screen.kind == "watcher", "the internal kind survived the rename"
            assert app.screen.mode == "create"

    async def test_the_create_screen_titles_itself_with_the_label(self, tmp_path, work_dir):
        app = ConfigToolApp(_write_config(tmp_path, work_dir))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one(TabbedContent).active = "tab-templates"
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "shared"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            screen: TemplateDetailScreen = app.screen  # type: ignore[assignment]
            assert screen._entity_noun() == "watcher rule template"
            assert screen._delete_blocker_noun() == "watcher rule"


class TestTheTemplatesTableShowsTheLabel:
    async def test_the_kind_column_reads_watcher_rule(self, tmp_path, work_dir):
        path = Path(_write_config(tmp_path, work_dir))
        path.write_text(path.read_text() + textwrap.dedent("""\
            watcher_templates:
              shared:
                session_expire_days: 30
        """))
        app = ConfigToolApp(str(path))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#templates-table")
            kinds = [str(table.get_row(key)[0]) for key in table.rows]
            assert "watcher rule" in kinds
            assert "watcher" not in kinds

    async def test_the_row_key_still_carries_the_internal_kind(self, tmp_path, work_dir):
        """Edit and delete split the row key on ':' to reach the document, so the
        key is the one place the internal spelling must stay."""
        path = Path(_write_config(tmp_path, work_dir))
        path.write_text(path.read_text() + textwrap.dedent("""\
            watcher_templates:
              shared:
                session_expire_days: 30
        """))
        app = ConfigToolApp(str(path))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#templates-table")
            assert "watcher:shared" in [str(k.value) for k in table.rows]
