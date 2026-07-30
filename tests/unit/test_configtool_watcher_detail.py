"""Pilot-based tests for WatcherDetailScreen's edit/create/delete flow —
Config TUI Phase 3 (watcher CRUD, docs/design/config-tool.md).

Pins the merge-on-add / split-on-edit design: editing a GROUP-SHARED field
(connector/agent/inherits) edits a `rooms:` group in place; editing a
PER-ROOM field (room itself, name, session_id, online/offline_notification,
context_inject_files, history_handoff.*) auto-splits that one room out into
its own entry (reusing the exact same merge-or-create primitive new-watcher
creation and "Clone for rooms" use). See gateway/configtool/model.py's
`add_watcher_rooms()`/`remove_watcher_room()` for the two primitives
everything here composes from, and test_configtool_model.py's
`TestWatcherCrudPrimitives` for direct, non-TUI tests of those primitives.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Input, Select

from gateway.configtool.app import ConfigToolApp
from gateway.configtool.modals import ConfirmModal, MessageModal, TextPromptModal
from gateway.configtool.screens.overview import OverviewScreen
from gateway.configtool.screens.watcher_detail import WatcherDetailScreen


def _write_config(tmp_path: Path, yaml_text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(yaml_text))
    return str(path)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _config_with_a_group(work_dir: Path) -> str:
    """One shared rooms: group (general + dev) under rc/default, plus a
    second, unrelated connector+agent pairing to exercise creation/merge
    scenarios against."""
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
          - connector: rc
            agent: default
            rooms: [general, dev]
    """


def _config_with_no_watchers(work_dir: Path) -> str:
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


def _config_with_a_standalone_watcher(work_dir: Path) -> str:
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
          - connector: rc
            agent: default
            room: general
    """


async def _open_watchers_tab(pilot, app) -> None:
    app.screen.query_one("TabbedContent").active = "tab-watchers"
    await pilot.pause()


async def _open_watcher_row(pilot, app, row: int, key: str) -> None:
    """key: 'e' to edit directly, 'enter' to view first."""
    await _open_watchers_tab(pilot, app)
    table = app.screen.query_one("#watchers-table", DataTable)
    table.focus()
    table.move_cursor(row=row)
    await pilot.press(key)
    await pilot.pause()


class TestWatcherEditSharedField:
    async def test_editing_connector_splits_this_room_out_of_the_group(
        self, tmp_path, work_dir
    ):
        """User-reported bug, fixed: connector/agent used to be classified
        as GROUP-SHARED ("move the whole group in place") — editing one
        room's connector silently reassigned every SIBLING room in the
        group too (2 watchers sharing 'rc', editing one to 'rc2' moved
        BOTH — never the intent when the user is looking at one specific
        room). connector/agent are stored as a single value on the shared
        raw entry, exactly like online_notification etc. (already
        correctly per-room) — reassigning one room's connector must split
        it out, not drag its siblings along."""
        config_path = _write_config(tmp_path, _config_with_a_group(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=1, key="e")  # row0=rc-dev, row1=rc-general
            assert isinstance(app.screen, WatcherDetailScreen)
            assert app.screen.room == "general"

            app.screen.query_one("#field-connector", Select).value = "rc2"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            # 'general' split out onto rc2 in its own entry; 'dev' stays
            # behind on the original connector, untouched.
            watchers = {
                (w.get("connector"), w.get("room") or tuple(w.get("rooms", [])))
                for w in raw["watchers"]
            }
            assert watchers == {("rc2", "general"), ("rc", "dev")}

    async def test_editing_connector_and_room_together_does_not_collide_with_the_sibling(
        self, tmp_path, work_dir
    ):
        """User-reported: this exact combination used to fail with
        "Room 'general' already exists under this connector/agent" — a pure
        side effect of the bug fixed above. Before the fix, changing
        connector moved the WHOLE group (including the sibling room, still
        named 'general') onto rc2 first; renaming THIS room 'dev' -> 'general'
        then collided with that just-relocated sibling. With connector
        correctly treated as per-room, the sibling never moves — it stays
        behind on the original connector, so renaming 'dev' to 'general' on
        rc2 doesn't collide with anything (same room NAME on a DIFFERENT
        connector is not a conflict)."""
        config_path = _write_config(tmp_path, _config_with_a_group(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")  # row0=rc-dev
            assert app.screen.room == "dev"

            app.screen.query_one("#field-connector", Select).value = "rc2"
            app.screen.query_one("#field-room", Input).value = "general"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)  # no "already exists" error
            raw = yaml.safe_load(Path(config_path).read_text())
            watchers = {
                (w.get("connector"), w.get("room") or tuple(w.get("rooms", [])))
                for w in raw["watchers"]
            }
            assert watchers == {("rc2", "general"), ("rc", "general")}

    async def test_editing_description_in_place_does_not_split_or_lose_the_edit(
        self, tmp_path, work_dir
    ):
        """PR review finding: 'description' is entry-level/shared (a
        free-text annotation _parse_one_watcher_entry() never even reads),
        but was originally missing from _SHARED_FIELD_KEYS — it fell into
        per_room_updates instead, which (a) needlessly triggered a group
        split for an edit that never needed one, and (b) silently LOST the
        edit entirely, since split_entry's own field loop never copies
        'description'."""
        config_path = _write_config(tmp_path, _config_with_a_group(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")

            app.screen.query_one("#field-description", Input).value = "my note"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            # Still ONE group entry (no needless split) with the note applied.
            assert raw["watchers"] == [
                {
                    "connector": "rc", "agent": "default", "rooms": ["general", "dev"],
                    "description": "my note",
                }
            ]


class TestWatcherInPlaceEditTransition:
    """PR review finding: FormScreen.action_edit() (the screen's OWN 'e'
    key — the in-place view-to-edit transition, as opposed to
    OverviewScreen's direct-edit-from-list shortcut which constructs a NEW
    screen already in mode="edit") calls `_on_enter_edit_mode()` before
    recomposing. WatcherDetailScreen originally never overrode this hook
    (unlike every other FormScreen subclass), leaving `_initial_values`
    empty for the whole edit session — every field then looked "changed"
    relative to nothing, silently rewriting connector/agent and spuriously
    splitting groups on a completely untouched Save."""

    async def test_saving_with_no_edits_via_the_in_place_transition_changes_nothing(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(
            tmp_path,
            f"""\
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
              - connector: rc2
                agent: other
                rooms: [general, dev]
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # View first (Enter), THEN edit in place ('e') — NOT the list's
            # direct-edit shortcut, which already worked correctly before
            # this fix (it constructs WatcherDetailScreen straight in
            # mode="edit", where __init__ already computes initial values).
            await _open_watcher_row(pilot, app, row=0, key="enter")
            assert app.screen.mode == "view"
            await pilot.press("e")
            await pilot.pause()
            assert app.screen.mode == "edit"

            await pilot.press("ctrl+s")  # no edits at all
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {"connector": "rc2", "agent": "other", "rooms": ["general", "dev"]}
            ]


class TestWatcherEditPerRoomField:
    async def test_editing_online_notification_splits_the_room_out(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_a_group(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")  # 'dev' or 'general', row 0

            edited_room = app.screen.room
            app.screen.query_one("#field-online_notification", Input).value = "hi"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            entries = {e.get("room"): e for e in raw["watchers"] if "room" in e}
            assert entries[edited_room]["online_notification"] == "hi"
            # The OTHER room stays in the (now-shrunk, normalized) group,
            # completely untouched.
            remaining_room = "general" if edited_room == "dev" else "dev"
            assert "online_notification" not in entries[remaining_room]

    async def test_split_out_entry_is_inserted_adjacent_to_the_source_group(
        self, tmp_path, work_dir
    ):
        """docs/design/config-tool.md decision 3: a split-out entry lands
        where the source group used to be, not appended to the bottom of
        the file."""
        config_path = _write_config(tmp_path, _config_with_a_group(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-online_notification", Input).value = "hi"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["watchers"]) == 2  # not appended past some other entry

    async def test_editing_a_per_room_field_on_a_standalone_watcher_stays_correct(
        self, tmp_path, work_dir
    ):
        """No group to split FROM — editing in place (via remove+recreate
        under the hood) must still produce exactly one, correctly-updated
        entry, not two."""
        config_path = _write_config(tmp_path, _config_with_a_standalone_watcher(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-online_notification", Input).value = "hi"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {"connector": "rc", "agent": "default", "room": "general", "online_notification": "hi"}
            ]

    async def test_splitting_a_room_out_of_a_group_keeps_the_shared_description(
        self, tmp_path, work_dir
    ):
        """Bug fix regression: split_entry (built when a per-room field edit
        forces a room to split out of a group) was assembled from a
        hardcoded field list that carried forward 'inherits' but not
        'description', silently dropping the group's description from the
        split-off room."""
        config_path = _write_config(
            tmp_path,
            f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - connector: rc
                agent: default
                rooms: [general, dev]
                description: shared note
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")

            edited_room = app.screen.room
            app.screen.query_one("#field-online_notification", Input).value = "hi"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            entries = {e.get("room"): e for e in raw["watchers"] if "room" in e}
            assert entries[edited_room]["online_notification"] == "hi"
            # The split-off room must keep the description it inherited
            # from the group it was split out of.
            assert entries[edited_room]["description"] == "shared note"


class TestWatcherRoomRename:
    async def test_renaming_the_room_moves_it(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_a_standalone_watcher(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-room", Input).value = "lobby"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [{"connector": "rc", "agent": "default", "room": "lobby"}]

    async def test_renaming_the_room_keeps_the_description(self, tmp_path, work_dir):
        """Bug fix regression: renaming a standalone watcher's room goes
        through the same remove+re-add split path as a group split
        (split_entry's hardcoded field list never carried 'description'),
        which used to silently drop the description on save."""
        config_path = _write_config(
            tmp_path,
            f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - connector: rc
                agent: default
                room: general
                description: my note
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-room", Input).value = "lobby"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {
                    "connector": "rc", "agent": "default", "room": "lobby",
                    "description": "my note",
                }
            ]

    async def test_renaming_to_an_empty_room_is_rejected(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_a_standalone_watcher(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="e")
            app.screen.query_one("#field-room", Input).value = ""
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [{"connector": "rc", "agent": "default", "room": "general"}]

    async def test_retrying_a_rename_after_a_rejected_save_does_not_leave_the_old_room_behind(
        self, tmp_path, work_dir
    ):
        """Self-caught regression: a rename is remove-old + add-new under
        the hood. If the FIRST attempt is rejected by save() (e.g. it
        collides with another watcher) and rolls back by restoring the
        whole watchers: list from a snapshot, `self.raw_entry` used to be
        left pointing at an ORPHANED object no longer `is`-identical to
        anything in the restored list — a SECOND attempt (fixing the typo
        and retrying) would then silently fail to find/remove the OLD room
        by identity, while the "add the new room" half still succeeded —
        leaving the old room behind as an unintended extra sibling instead
        of being replaced."""
        config_path = _write_config(
            tmp_path,
            f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - connector: rc
                agent: default
                room: general
              - connector: rc
                agent: default
                room: dev
                online_notification: "x"
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            table = app.screen.query_one("#watchers-table", DataTable)
            table.focus()
            for r in range(table.row_count):
                if table.get_row_at(r)[2] == "general":
                    table.move_cursor(row=r)
                    break
            await pilot.press("e")
            await pilot.pause()

            # First attempt: rename to a colliding name — rejected.
            app.screen.query_one("#field-room", Input).value = "dev"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()
            assert isinstance(app.screen, WatcherDetailScreen)

            # Second attempt: a genuinely new, non-colliding name.
            app.screen.query_one("#field-room", Input).value = "lobby"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            rooms = {e.get("room") for e in raw["watchers"]}
            assert rooms == {"lobby", "dev"}  # 'general' replaced, not left behind


class TestWatcherDelete:
    async def test_deleting_a_room_from_a_group_normalizes_to_singular(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_a_group(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            table = app.screen.query_one("#watchers-table", DataTable)
            deleted_room = table.get_row_at(0)[2]
            table.focus()
            table.move_cursor(row=0)

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("tab", "enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert len(raw["watchers"]) == 1
            remaining = raw["watchers"][0]
            assert "rooms" not in remaining
            assert remaining["room"] != deleted_room

    async def test_deleting_the_only_room_removes_the_whole_entry(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_a_standalone_watcher(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            table = app.screen.query_one("#watchers-table", DataTable)
            table.focus()
            table.move_cursor(row=0)

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("tab", "enter")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw.get("watchers") in (None, [])

    async def test_cancelling_the_delete_confirm_leaves_the_watcher_untouched(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_a_standalone_watcher(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            table = app.screen.query_one("#watchers-table", DataTable)
            table.focus()
            table.move_cursor(row=0)

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")  # Cancel is focused by default
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [{"connector": "rc", "agent": "default", "room": "general"}]


class TestWatcherCreate:
    async def test_creating_a_single_watcher(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_no_watchers(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, WatcherDetailScreen)
            assert app.screen.mode == "create"

            app.screen.query_one("#field-connector", Select).value = "rc"
            app.screen.query_one("#field-agent", Select).value = "default"
            app.screen.query_one("#field-room", Input).value = "ops"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [{"connector": "rc", "agent": "default", "room": "ops"}]

    async def test_creating_with_a_comma_list_of_rooms_makes_one_group(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_no_watchers(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()

            app.screen.query_one("#field-connector", Select).value = "rc"
            app.screen.query_one("#field-agent", Select).value = "default"
            app.screen.query_one("#field-room", Input).value = "qa, staging, prod"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {"connector": "rc", "agent": "default", "rooms": ["qa", "staging", "prod"]}
            ]

    async def test_creating_a_room_matching_an_existing_standalone_watcher_merges_into_it(
        self, tmp_path, work_dir
    ):
        """A plain 'new watcher' create, not just explicit Clone-for-rooms,
        goes through the SAME merge-on-add primitive — a new room whose
        connector/agent/shared fields match an existing entry consolidates
        into it automatically."""
        config_path = _write_config(tmp_path, _config_with_a_standalone_watcher(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()

            app.screen.query_one("#field-connector", Select).value = "rc"
            app.screen.query_one("#field-agent", Select).value = "default"
            app.screen.query_one("#field-room", Input).value = "ops"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {"connector": "rc", "agent": "default", "rooms": ["general", "ops"]}
            ]

    async def test_creating_a_room_matching_an_existing_group_merges_into_it(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_a_group(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()

            app.screen.query_one("#field-connector", Select).value = "rc"
            app.screen.query_one("#field-agent", Select).value = "default"
            app.screen.query_one("#field-room", Input).value = "ops"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {"connector": "rc", "agent": "default", "rooms": ["general", "dev", "ops"]}
            ]

    async def test_missing_room_is_rejected(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_no_watchers(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()

            app.screen.query_one("#field-connector", Select).value = "rc"
            app.screen.query_one("#field-agent", Select).value = "default"
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)


class TestWatcherCloneForRooms:
    """Owner-requested bulk-add — the alternative to a separate
    RoomListEditorScreen (never built, see watcher_detail.py's module
    docstring)."""

    async def test_clone_adds_new_rooms_and_merges_into_the_source_group(
        self, tmp_path, work_dir
    ):
        config_path = _write_config(tmp_path, _config_with_a_standalone_watcher(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="enter")
            assert isinstance(app.screen, WatcherDetailScreen)
            assert app.screen.mode == "view"

            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptModal)
            app.screen.query_one("#prompt-input", Input).value = "dev, ops"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {"connector": "rc", "agent": "default", "rooms": ["general", "dev", "ops"]}
            ]

    async def test_clone_silently_skips_a_room_already_in_the_source_group(
        self, tmp_path, work_dir
    ):
        """User-requested: typing a room that's already in the group you're
        cloning FROM is a no-op for that room, not an error — the end
        state is correct either way."""
        config_path = _write_config(tmp_path, _config_with_a_group(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=0, key="enter")

            await pilot.press("c")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "general, dev, ops"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)  # no error modal
            raw = yaml.safe_load(Path(config_path).read_text())
            assert raw["watchers"] == [
                {"connector": "rc", "agent": "default", "rooms": ["general", "dev", "ops"]}
            ]

    async def test_clone_colliding_with_an_unrelated_watcher_shows_a_clean_error(
        self, tmp_path, work_dir
    ):
        """A room colliding with a DIFFERENT, non-mergeable watcher (same
        connector, different shared fields) is NOT silently handled —
        add_watcher_rooms() creates a conflicting new entry that then fails
        save()'s normal duplicate-name validation, surfacing the existing
        clean error path rather than a new bespoke check."""
        config_path = _write_config(
            tmp_path,
            f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - connector: rc
                agent: default
                room: general
              - connector: rc
                agent: default
                room: dev
                online_notification: "different settings, never mergeable"
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=1, key="enter")  # row0=rc-dev, row1=rc-general (sorted by name)
            assert app.screen.room == "general"

            await pilot.press("c")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "dev"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, MessageModal)
            raw = yaml.safe_load(Path(config_path).read_text())
            # Untouched — the rejected save never reached disk.
            assert len(raw["watchers"]) == 2

    async def test_clone_is_not_available_in_create_mode(self, tmp_path, work_dir):
        config_path = _write_config(tmp_path, _config_with_a_standalone_watcher(work_dir))
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watchers_tab(pilot, app)
            await pilot.press("n")
            await pilot.pause()
            assert app.screen.mode == "create"
            assert app.screen.check_action("clone_for_rooms", ()) is False

    async def test_editing_after_a_rejected_clone_still_finds_the_watcher(
        self, tmp_path, work_dir
    ):
        """Same self-caught staleness class as
        TestWatcherRoomRename's own retry regression test — a REJECTED
        Clone-for-rooms rolls back by restoring the whole watchers: list
        from a snapshot. Clone-for-rooms itself never dereferences
        self.raw_entry BY IDENTITY (add_watcher_rooms() matches on
        connector/agent/shared VALUES), so this doesn't break a second
        Clone attempt — but a SUBSEQUENT Edit+Save in the same screen
        session DOES look self.raw_entry up by identity
        (remove_watcher_room()), and would otherwise silently no-op
        against the now-orphaned pre-rollback object."""
        config_path = _write_config(
            tmp_path,
            f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {work_dir}
            watchers:
              - connector: rc
                agent: default
                room: general
              - connector: rc
                agent: default
                room: dev
                online_notification: "different settings, never mergeable"
            """,
        )
        app = ConfigToolApp(config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_watcher_row(pilot, app, row=1, key="enter")  # row0=rc-dev, row1=rc-general (sorted by name)
            assert app.screen.room == "general"

            # Rejected clone: 'dev' has different shared fields, not mergeable.
            await pilot.press("c")
            await pilot.pause()
            app.screen.query_one("#prompt-input", Input).value = "dev"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, MessageModal)
            await pilot.press("enter")  # dismiss
            await pilot.pause()
            assert isinstance(app.screen, WatcherDetailScreen)

            # Now switch to editing and rename 'general' -> 'lobby'. If
            # self.raw_entry were left stale, remove_watcher_room() would
            # silently find nothing, leaving 'general' behind while 'lobby'
            # merges in as an unintended extra sibling instead of replacing it.
            await pilot.press("e")
            await pilot.pause()
            app.screen.query_one("#field-room", Input).value = "lobby"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, OverviewScreen)
            raw = yaml.safe_load(Path(config_path).read_text())
            rooms = {e.get("room") for e in raw["watchers"]}
            assert rooms == {"lobby", "dev"}  # 'general' replaced, not left behind
