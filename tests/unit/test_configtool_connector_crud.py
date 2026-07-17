"""Pilot-based tests for ConnectorDetailScreen's create/edit flow.

The security-critical property here (advisor-flagged: treat with the same
weight as EditableConfig.save()'s $VAR round-trip test) is
`test_editing_an_unrelated_field_leaves_an_existing_secret_placeholder_untouched`
below — a connector's `server.password` set to `"${RC_PASSWORD}"` must
survive an edit to a DIFFERENT field completely unresolved, never masked
data, never the actual env var's real value.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Input

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal, TypePickerModal
from gateway.configtool.screens.connector_detail import ConnectorDetailScreen
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


def _config_with_one_rocketchat_connector(work_dir: Path, password: str = "pw") -> str:
    return f"""\
        agents:
          default:
            type: claude
            working_directory: {work_dir}
        connectors:
          - name: rc-existing
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: "{password}"}}
        watchers:
          - connector: rc-existing
            agent: default
            room: general
    """


async def _open_connector_in_edit_mode(pilot, app) -> None:
    table = app.screen.query_one("#connectors-table", DataTable)
    table.focus()
    table.move_cursor(row=0)
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("e")
    await pilot.pause()


async def _open_type_picker_for_connectors(pilot, app) -> None:
    app.screen.query_one("TabbedContent").active = "tab-connectors"
    await pilot.pause()
    await pilot.press("n")
    await pilot.pause()


class TestNewConnectorEntryPoint:
    async def test_n_key_on_connectors_tab_opens_type_picker_with_all_4_types(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            assert isinstance(app.screen, TypePickerModal)
            assert app.screen.options == ["rocketchat", "mattermost", "voice", "script"]


class TestCreateConnector:
    async def test_creating_a_rocketchat_connector_persists_it(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("enter")  # first option: rocketchat
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)
            assert app.screen.mode == "create"

            app.screen.query_one("#field-name", Input).value = "rc-second"
            app.screen.query_one("#field-server-url", Input).value = "http://rc2.local"
            app.screen.query_one("#field-server-username", Input).value = "bot2"
            app.screen.query_one("#field-server-password", Input).value = "pw2"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            names = {c["name"]: c for c in raw["connectors"]}
            assert names["rc-second"]["server"]["url"] == "http://rc2.local"
            assert names["rc-second"]["server"]["password"] == "pw2"
            assert list(Path(config_path).parent.glob("config.yaml.bak.*"))

    async def test_creating_a_voice_connector_uses_the_flat_field_list(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("down", "down", "enter")  # voice (3rd option)
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)

            app.screen.query_one("#field-name", Input).value = "voice-1"
            app.screen.query_one("#field-port", Input).value = "9999"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            names = {c["name"]: c for c in raw["connectors"]}
            assert names["voice-1"]["port"] == 9999

    async def test_creating_a_script_connector_has_no_type_specific_fields(
        self, tmp_path, work_dir
    ):
        """ScriptConnector never reads raw — the form should just show name/
        description and an explanatory note, not crash on an empty field list."""
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("down", "down", "down", "enter")  # script (4th option)
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)

            app.screen.query_one("#field-name", Input).value = "script-1"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            names = {c["name"]: c for c in raw["connectors"]}
            assert names["script-1"]["type"] == "script"

    async def test_creating_with_a_duplicate_name_shows_an_error_and_rolls_back(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("enter")  # rocketchat
            await pilot.pause()

            app.screen.query_one("#field-name", Input).value = "rc-existing"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            assert isinstance(app.screen, ConnectorDetailScreen)
            assert app.screen.mode == "create"
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["connectors"]) == 1  # nothing was appended

    async def test_creating_with_a_blank_name_shows_an_error(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("enter")
            await pilot.pause()

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)
            assert app.screen.mode == "create"

    async def test_mattermost_auth_xor_violation_fails_save_and_rolls_back(
        self, tmp_path, work_dir
    ):
        """Configuring BOTH token and username/password violates
        MattermostConfig.__post_init__ — validate_config() (run by save())
        is the real enforcement; the form doesn't reimplement it."""
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("down", "enter")  # mattermost
            await pilot.pause()

            app.screen.query_one("#field-name", Input).value = "mm-bad"
            app.screen.query_one("#field-server-url", Input).value = "http://mm.local"
            app.screen.query_one("#field-server-team", Input).value = "team"
            app.screen.query_one("#field-server-token", Input).value = "tok"
            app.screen.query_one("#field-server-username", Input).value = "u"
            app.screen.query_one("#field-server-password", Input).value = "p"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            assert isinstance(app.screen, ConnectorDetailScreen)
            assert app.screen.mode == "create"
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["connectors"]) == 1  # rolled back, nothing appended


class TestEditConnector:
    async def test_editing_an_unrelated_field_leaves_an_existing_secret_placeholder_untouched(
        self, tmp_path, work_dir
    ):
        """THE keystone test for this screen (see module docstring)."""
        os.environ["RC_PASSWORD_CONNECTOR_TEST"] = "the-real-secret-value"
        try:
            config_path = _write_config(
                tmp_path,
                _config_with_one_rocketchat_connector(
                    work_dir, password="${RC_PASSWORD_CONNECTOR_TEST}"
                ),
            )
            app = ConfigToolApp(config_path)
            async with app.run_test() as pilot:
                await pilot.pause()
                await _open_connector_in_edit_mode(pilot, app)

                pw_input = app.screen.query_one("#field-server-password", Input)
                # The widget must show the literal placeholder, never the
                # resolved secret and never a masked "****" at the data level
                # (masking is display-only via Input(password=True); .value
                # is always the real underlying string).
                assert pw_input.value == "${RC_PASSWORD_CONNECTOR_TEST}"

                app.screen.query_one("#field-server-username", Input).value = "renamed-bot"
                await pilot.pause()
                await pilot.press("ctrl+s")
                await pilot.pause()

                assert isinstance(app.screen, OverviewScreen)
                raw = yaml.safe_load(Path(config_path).read_text())
                connector = raw["connectors"][0]
                assert connector["server"]["username"] == "renamed-bot"
                assert connector["server"]["password"] == "${RC_PASSWORD_CONNECTOR_TEST}"
        finally:
            os.environ.pop("RC_PASSWORD_CONNECTOR_TEST", None)

    async def test_editing_and_changing_the_secret_writes_the_new_plaintext_value(
        self, tmp_path, work_dir
    ):
        """Documented v1 scope: there is no '.env toggle' yet (deferred — see
        docs/design/config-tool.md) — typing a new secret directly writes it
        to config.yaml in plaintext, exactly like the existing $EDITOR escape
        hatch already allows. Not a regression, just not-yet-automated."""
        config_path = _write_config(
            tmp_path, _config_with_one_rocketchat_connector(work_dir, password="oldpw")
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            app.screen.query_one("#field-server-password", Input).value = "brand-new-secret"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["connectors"][0]["server"]["password"] == "brand-new-secret"

    async def test_name_and_type_are_not_editable_fields(self, tmp_path, work_dir):
        """Renaming would silently orphan referencing watchers; changing type
        would require reshaping the whole field list. Both immutable post-
        creation in this UI (see module docstring) — $EDITOR remains for it."""
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            assert not app.screen.query("#field-name")
            assert not app.screen.query("#field-type")

    async def test_list_field_round_trips_owners(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            app.screen.query_one("#field-allowed_users-owners", Input).value = "alice, bob"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["connectors"][0]["allowed_users"]["owners"] == ["alice", "bob"]

    async def test_untouched_fields_are_not_written_as_explicit(self, tmp_path, work_dir):
        """Regression for decision 2: connector_defaults-inherited fields
        must stay inherited if the form is opened and something ELSE is
        changed — displaying a merged/effective value must not itself count
        as "explicit"."""
        config_path = _write_config(
            tmp_path,
            f"""\
                connector_defaults:
                  require_mention: false
                agents:
                  default:
                    type: claude
                    working_directory: {work_dir}
                connectors:
                  - name: rc-existing
                    type: rocketchat
                    server: {{url: "http://localhost:3000", username: bot, password: pw}}
                watchers:
                  - connector: rc-existing
                    agent: default
                    room: general
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            require_mention = app.screen.query_one("#field-require_mention")
            assert require_mention.value is False  # inherited from connector_defaults

            app.screen.query_one("#field-timezone", Input).value = "America/Los_Angeles"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            connector = raw["connectors"][0]
            assert connector["timezone"] == "America/Los_Angeles"
            assert "require_mention" not in connector  # still inherited, not explicit

    async def test_a_save_that_fails_validate_config_does_not_mutate_the_live_entry(
        self, tmp_path, work_dir
    ):
        """Same bug class as AgentDetailScreen's equivalent test: edit mode
        used to apply Save's updates directly to self.entry (the SAME dict
        object already living in cfg.document), so a rejected save still
        left the invalid data sitting in memory (and, if Back was pressed
        without a further successful save, visibly shown). Clearing the
        password field reverts it to inherited (empty, since no
        connector_defaults sets it) — _check_connectors then rejects the
        empty password, and BOTH the password clear AND the unrelated
        username change made in the same edit session must roll back
        together, atomically."""
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            app.screen.query_one("#field-server-password", Input).value = ""
            app.screen.query_one("#field-server-username", Input).value = "changed-username"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "password" in body
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            assert isinstance(app.screen, ConnectorDetailScreen)
            assert app.screen.mode == "edit"

            entry = app.editable_config.connectors_raw[0]
            assert entry["server"]["password"] == "pw"  # original, untouched
            assert entry["server"]["username"] == "bot"  # unrelated change also rolled back

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["connectors"][0]["server"]["password"] == "pw"
            assert raw["connectors"][0]["server"]["username"] == "bot"


class TestConnectorEscapeConfirmation:
    async def test_escape_with_unsaved_changes_shows_confirm_modal(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            app.screen.query_one("#field-server-url", Input).value = "http://changed"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

    async def test_create_mode_escape_with_unsaved_changes_discards_cleanly(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("enter")
            await pilot.pause()

            app.screen.query_one("#field-name", Input).value = "abandoned"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

            await pilot.press("tab", "enter")  # Discard
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["connectors"]) == 1  # nothing added


def _config_with_two_connectors(work_dir: Path) -> str:
    """'rc-referenced' is used by the watcher; 'rc-orphan' is not — used to
    test both the delete-succeeds and delete-blocked paths."""
    return f"""\
        agents:
          default:
            type: claude
            working_directory: {work_dir}
        connectors:
          - name: rc-referenced
            type: rocketchat
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
          - name: rc-orphan
            type: rocketchat
            server: {{url: "http://localhost:3001", username: bot2, password: pw2}}
        watchers:
          - connector: rc-referenced
            agent: default
            room: general
    """


async def _open_connector_in_view_mode(pilot, app, row: int = 0) -> None:
    table = app.screen.query_one("#connectors-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press("enter")
    await pilot.pause()


class TestDeleteConnector:
    async def test_d_key_on_an_unreferenced_connector_shows_confirm_modal(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_view_mode(pilot, app, row=1)  # rc-orphan

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

    async def test_cancelling_the_delete_keeps_the_connector(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_view_mode(pilot, app, row=1)  # rc-orphan

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")  # Cancel is focused by default
            await pilot.pause()
            assert isinstance(app.screen, ConnectorDetailScreen)
            assert app.screen.mode == "view"
            names = {c.get("name") for c in app.editable_config.connectors_raw}
            assert "rc-orphan" in names

    async def test_confirming_delete_of_an_unreferenced_connector_succeeds(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_view_mode(pilot, app, row=1)  # rc-orphan

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("tab", "enter")  # Delete
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            names = {c["name"] for c in raw["connectors"]}
            assert "rc-orphan" not in names
            assert "rc-referenced" in names
            assert list(Path(config_path).parent.glob("config.yaml.bak.*"))

    async def test_deleting_a_referenced_connector_is_blocked_before_the_confirm(
        self, tmp_path, work_dir
    ):
        """A watcher still references 'rc-referenced' — the pre-delete check
        catches this BEFORE even offering the destructive confirm, naming
        the referencing watcher in a MessageModal rather than relying on
        save()'s generic validator error."""
        config_path = _write_config(tmp_path, _config_with_two_connectors(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_view_mode(pilot, app, row=0)  # rc-referenced

            await pilot.press("d")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            body = str(app.screen.query_one("#message-body").render())
            assert "rc-referenced" in body
            await pilot.press("enter")  # dismiss
            await pilot.pause()

            assert isinstance(app.screen, ConnectorDetailScreen)
            assert app.screen.mode == "view"
            names = {c.get("name") for c in app.editable_config.connectors_raw}
            assert "rc-referenced" in names
            raw = yaml.safe_load(Path(config_path).read_text())
            assert any(c["name"] == "rc-referenced" for c in raw["connectors"])
