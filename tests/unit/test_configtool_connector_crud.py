"""Pilot-based tests for ConnectorDetailScreen's create/edit flow.

`test_editing_an_unrelated_field_leaves_a_placeholder_looking_value_untouched`
below pins docs/design/config-tool.md decision 6's final form: a
`server.password` value like `"${RC_PASSWORD}"` is just a plain string —
never resolved, never given special treatment — so an edit to a DIFFERENT
field must leave it exactly as typed.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import Checkbox, DataTable, Input, Select, Static

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import (
    ConfirmModal,
    InheritsPickerModal,
    MessageModal,
    TypePickerModal,
)
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
            assert list((Path(config_path).parent / ".config-backups").glob("config.yaml.bak.*"))

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

    async def test_mattermost_auth_method_select_defaults_to_token_with_userpass_hidden(
        self, tmp_path, work_dir
    ):
        """A brand-new mattermost connector has neither credential set yet —
        _compute_mm_auth_method() defaults to 'token' (the simpler, no-
        expiry option MattermostConfig's own docstring lists first)."""
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("down", "enter")  # mattermost
            await pilot.pause()

            select = app.screen.query_one("#mm-auth-method-select", Select)
            assert select.value == "token"
            assert app.screen.query_one("#mm-auth-token-group").display is True
            assert app.screen.query_one("#mm-auth-userpass-group").display is False

    async def test_switching_auth_method_clears_the_other_groups_fields_on_save(
        self, tmp_path, work_dir
    ):
        """The Auth method Select (not what's still sitting in a hidden
        Input) is the single source of truth for which credential group is
        active — user-reported request: a dropdown that shows only the
        relevant fields so the 'not both, not neither' validation message
        is never needed in the common case. Typing a token, then switching
        to username+password, must not silently save the stale token
        alongside the new credentials."""
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_type_picker_for_connectors(pilot, app)
            await pilot.press("down", "enter")  # mattermost
            await pilot.pause()

            app.screen.query_one("#field-name", Input).value = "mm-new"
            app.screen.query_one("#field-server-url", Input).value = "http://mm.local"
            app.screen.query_one("#field-server-team", Input).value = "team"
            app.screen.query_one("#field-server-token", Input).value = "stale-token"
            await pilot.pause()

            select = app.screen.query_one("#mm-auth-method-select", Select)
            select.value = "username_password"
            await pilot.pause()
            assert app.screen.query_one("#mm-auth-token-group").display is False
            assert app.screen.query_one("#mm-auth-userpass-group").display is True

            app.screen.query_one("#field-server-username", Input).value = "u"
            app.screen.query_one("#field-server-password", Input).value = "p"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            server = next(c for c in raw["connectors"] if c["name"] == "mm-new")["server"]
            assert server == {"url": "http://mm.local", "team": "team", "username": "u", "password": "p"}
            assert "token" not in server

    async def test_saving_an_unrelated_field_never_touches_auth_fields_the_user_didnt_edit(
        self, tmp_path, work_dir
    ):
        """Regression, found by an independent review pass: a pre-existing
        (already-invalid) entry with BOTH 'token' and 'username'+'password'
        set (e.g. hand-edited, or a half-finished migration between modes)
        opens with the Auth method Select defaulting to 'token' (the
        ambiguous-case tie-break) and the username/password group hidden —
        but still holding its real values underneath. Saving an UNRELATED
        change (never touching the Select) must NOT force-clear the hidden
        group: _apply_mm_auth_method_exclusivity() only runs when the user
        has actually picked a mode this session, precisely to avoid a
        first-cut version of this feature silently deleting credentials
        nobody asked to change."""
        config_path = _write_config(
            tmp_path,
            f"""\
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            connectors:
              - name: mm-both
                type: mattermost
                server: {{url: "http://mm.local", team: t, token: "old-stale-token",
                          username: bob, password: pw123}}
            watchers:
              - connector: mm-both
                agent: default
                room: general
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            select = app.screen.query_one("#mm-auth-method-select", Select)
            assert select.value == "token"  # ambiguous-case tie-break

            checkbox = app.screen.query_one("#field-reply_in_thread", Checkbox)
            checkbox.value = not checkbox.value
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            server = raw["connectors"][0]["server"]
            assert server["token"] == "old-stale-token"
            assert server["username"] == "bob"
            assert server["password"] == "pw123"

    async def test_neither_auth_field_filled_still_fails_the_real_validation(
        self, tmp_path, work_dir
    ):
        """The Select narrows the common case down to one valid shape, but
        it can't force the user to actually fill the active group in —
        validate_config() (MattermostConfig.__post_init__) remains the real
        backstop for 'neither configured', exactly as before this Select
        existed."""
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
            # Auth method defaults to 'token', left blank — neither mode configured.
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
    async def test_editing_an_unrelated_field_leaves_a_placeholder_looking_value_untouched(
        self, tmp_path, work_dir
    ):
        """docs/design/config-tool.md decision 6 (final revision): $VAR/
        ${VAR} is no longer resolved anywhere in the normal load path — a
        value that merely LOOKS like a placeholder is just a plain string,
        same as any other. Editing an unrelated field must leave it exactly
        as typed, never touched, never given special treatment."""
        os.environ["RC_PASSWORD_CONNECTOR_TEST"] = "unrelated-value-must-not-leak-in"
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
                # Shown exactly as written — no resolution, no hint, no
                # special-casing of a value that happens to look like a
                # placeholder.
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

    async def test_editing_and_changing_the_secret_writes_plaintext_directly(
        self, tmp_path, work_dir
    ):
        """Secrets are stored directly in config.yaml (docs/design/
        config-tool.md decision 6 revisited) — typing a new value writes it
        as plaintext, exactly like the existing $EDITOR escape hatch
        already allows. config.yaml gets chmod 0600 by EditableConfig.save()."""
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
        """Regression for decision 2: inherits:-template fields must stay
        inherited if the form is opened and something ELSE is changed —
        displaying a merged/effective value must not itself count as
        "explicit"."""
        config_path = _write_config(
            tmp_path,
            f"""\
                connector_templates:
                  standard:
                    require_mention: false
                agents:
                  default:
                    type: claude
                    working_directory: {work_dir}
                connectors:
                  - name: rc-existing
                    inherits: standard
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
            assert require_mention.value is False  # inherited from connector_templates.standard

            app.screen.query_one("#field-timezone", Input).value = "America/Los_Angeles"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            connector = raw["connectors"][0]
            assert connector["timezone"] == "America/Los_Angeles"
            assert "require_mention" not in connector  # still inherited, not explicit

    async def test_type_from_template_only_still_shows_the_right_type_specific_fields(
        self, tmp_path, work_dir
    ):
        """Regression for `_connector_type()`'s merge fix: a connector whose
        `type` is set ONLY via its `inherits:` template (never on the raw
        entry itself) must still pick the correct per-type field list in
        edit mode — before the fix, `_connector_type()` read the RAW entry
        directly and fell back to the wrong type ('rocketchat' regardless),
        picking `rocketchat`'s fields even for a mattermost connector."""
        config_path = _write_config(
            tmp_path,
            f"""\
                connector_templates:
                  standard:
                    type: mattermost
                agents:
                  default:
                    type: claude
                    working_directory: {work_dir}
                connectors:
                  - name: mm-existing
                    inherits: standard
                    server: {{url: "http://localhost:3000", team: t, username: bot, password: pw}}
                watchers:
                  - connector: mm-existing
                    agent: default
                    room: general
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            assert isinstance(app.screen, ConnectorDetailScreen)
            # 'server.team' is mattermost-only (never in rocketchat's field
            # list) — its presence here proves the merged (not raw) type
            # was used to pick the field list.
            assert app.screen.query_one("#field-server-team", Input).value == "t"

    async def test_a_save_that_fails_validate_config_does_not_mutate_the_live_entry(
        self, tmp_path, work_dir
    ):
        """Same bug class as AgentDetailScreen's equivalent test: edit mode
        used to apply Save's updates directly to self.entry (the SAME dict
        object already living in cfg.document), so a rejected save still
        left the invalid data sitting in memory (and, if Back was pressed
        without a further successful save, visibly shown). Clearing the
        password field reverts it to inherited (empty, since this connector
        has no inherits: template setting it) — _check_connectors then
        rejects the empty password, and BOTH the password clear AND the
        unrelated username change made in the same edit session must roll
        back together, atomically."""
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
            assert list((Path(config_path).parent / ".config-backups").glob("config.yaml.bak.*"))

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


class TestPasswordVisibilityToggle:
    """ctrl+t (nice-to-have, user-requested) — reveal/re-mask the FOCUSED
    secret field. Purely cosmetic: Input.password only affects display,
    never .value, so it has zero interaction with the diff/save logic."""

    async def test_ctrl_t_reveals_and_re_masks_the_focused_password_field(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            pw_input = app.screen.query_one("#field-server-password", Input)
            assert pw_input.password is True  # masked by default
            pw_input.focus()
            await pilot.pause()

            await pilot.press("ctrl+t")
            await pilot.pause()
            assert pw_input.password is False
            assert pw_input.value == "pw"  # .value was never masked to begin with

            await pilot.press("ctrl+t")
            await pilot.pause()
            assert pw_input.password is True

    async def test_ctrl_t_on_a_non_secret_field_is_a_safe_no_op(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_one_rocketchat_connector(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            url_input = app.screen.query_one("#field-server-url", Input)
            url_input.focus()
            await pilot.pause()

            await pilot.press("ctrl+t")  # must not raise
            await pilot.pause()
            assert url_input.password is False


def _config_with_two_connector_templates(work_dir: Path) -> str:
    """'standard' (rocketchat) and 'other' (mattermost) — sorted order is
    'other' before 'standard', so InheritsPickerModal's ListView is:
    0=(none), 1=other, 2=standard, 3=(new template)."""
    return f"""\
        connector_templates:
          standard:
            type: rocketchat
            require_mention: false
          other:
            type: mattermost
            require_mention: true
        agents:
          default:
            type: claude
            working_directory: {work_dir}
        connectors:
          - name: rc-existing
            inherits: standard
            server: {{url: "http://localhost:3000", username: bot, password: pw}}
        watchers:
          - connector: rc-existing
            agent: default
            room: general
    """


async def _click_inherits_button(pilot, app) -> None:
    button = app.screen.query_one("#inherits-change-button")
    button.scroll_visible(animate=False)
    await pilot.pause()
    await pilot.click("#inherits-change-button")
    await pilot.pause()


class TestConnectorInheritsPicker:
    """Same Inherits-button redesign as AgentDetailScreen (see its own
    TestInheritsPicker for the fuller coverage) — this class only pins the
    connector-specific wrinkle: switching to a template with a DIFFERENT
    `type` must reshape the form to that type's own field list."""

    async def test_inherits_button_opens_the_picker(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connector_templates(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)
            assert str(app.screen.query_one("#inherits-value", Static).render()) == "standard"

            await _click_inherits_button(pilot, app)
            assert isinstance(app.screen, InheritsPickerModal)

    async def test_switching_to_a_different_type_template_reshapes_the_form(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_two_connector_templates(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)
            assert app.screen.query_one("#field-require_mention")
            assert not app.screen.query("#field-server-team")  # rocketchat has no 'team'

            await _click_inherits_button(pilot, app)
            await pilot.press("down", "enter")  # 'other' (mattermost)
            await pilot.pause()

            assert isinstance(app.screen, ConnectorDetailScreen)  # no confirm — nothing overridden
            assert str(app.screen.query_one("#inherits-value", Static).render()) == "other"
            # mattermost-only field now present — the form reshaped to match.
            assert app.screen.query_one("#field-server-team")
            assert app.screen.query_one("#field-require_mention", Checkbox).value is True

    async def test_switching_with_an_overridden_field_confirms_first(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_two_connector_templates(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_connector_in_edit_mode(pilot, app)

            app.screen.query_one("#field-timezone", Input).value = "America/Los_Angeles"
            await pilot.pause()

            await _click_inherits_button(pilot, app)
            await pilot.press("down", "enter")  # 'other'
            await pilot.pause()

            assert isinstance(app.screen, ConfirmModal)
