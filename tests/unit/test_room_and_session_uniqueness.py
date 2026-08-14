"""One room, one processor — and one session, one room (design §4.1).

Two enforcement points in opposite directions, which is why neither covers the other.
Reject-or-replace on registration stops two processors serving one room; it says nothing
about one session bound to two rooms, and per-room locks cannot help there either, since
the hazard is two *different* rooms resuming the same session id.

A reused session is a cross-room leak: the session carries its room in the identity
header re-supplied every turn, in the transcript's prior `[#room | from: …]` prefixes,
and in the single-valued session→room map that decides where a permission prompt for its
tool calls appears.
"""

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gateway.core.session_maps import SessionAlreadyBoundError, SessionMaps
from gateway.core.state import (
    DuplicateSessionError,
    WatcherState,
    check_session_uniqueness,
    connector_name_of,
    save_state,
)


class TestOneSessionOneRoom(unittest.TestCase):
    """`bind_session` is the only writer of the session→room map, so the rule lives there
    rather than in a check at its call site."""

    def _maps(self):
        return SessionMaps()

    def test_a_second_room_cannot_take_a_bound_session(self):
        maps = self._maps()
        maps.bind_session("ses-1", "room-a", MagicMock(), "claude:/w")

        with self.assertRaises(SessionAlreadyBoundError) as cm:
            maps.bind_session("ses-1", "room-b", MagicMock(), "claude:/w")

        self.assertIn("room-a", str(cm.exception))
        self.assertIn("room-b", str(cm.exception))

    def test_the_refusal_leaves_the_incumbent_untouched(self):
        """The property that matters more than the raise.

        `_start_watcher`'s rollback calls `remove_session(session_id)`, keyed by the id
        alone against a map shared by every connector. A refusal that happened *after*
        the write — or that let the loser reach its own rollback — would have the second
        watcher tearing down the first watcher's live routing, breaking a healthy
        watcher's permission notifications.
        """
        maps = self._maps()
        first_connector = MagicMock()
        maps.bind_session("ses-1", "room-a", first_connector, "claude:/w")

        with self.assertRaises(SessionAlreadyBoundError):
            maps.bind_session("ses-1", "room-b", MagicMock(), "claude:/w")

        self.assertEqual(maps.room["ses-1"], "room-a")
        self.assertIs(maps.connector["ses-1"], first_connector)

    def test_rebinding_the_same_room_is_allowed(self):
        """A watcher restarting re-binds what it already held; refusing that would make
        a reset unrecoverable without a daemon restart."""
        maps = self._maps()
        maps.bind_session("ses-1", "room-a", MagicMock(), "claude:/w")
        maps.bind_session("ses-1", "room-a", MagicMock(), "claude:/w")

    def test_the_same_id_from_a_different_backend_is_a_different_session(self):
        """Ids are unique within the store that issued them, not globally — the same
        reason `backend_identity` exists at all."""
        maps = self._maps()
        maps.bind_session("ses-1", "room-a", MagicMock(), "claude:/w")
        maps.bind_session("ses-1", "room-b", MagicMock(), "opencode:/w")

    def test_removal_releases_the_reservation(self):
        """Otherwise a watcher could never rebind after a reset: the check would refuse
        it against its own stale entry."""
        maps = self._maps()
        maps.bind_session("ses-1", "room-a", MagicMock(), "claude:/w")
        maps.remove_session("ses-1")
        maps.bind_session("ses-1", "room-b", MagicMock(), "claude:/w")
        self.assertEqual(maps.room["ses-1"], "room-b")


class TestLoadTimeSessionUniqueness(unittest.TestCase):
    """Catch it in the files, before either watcher starts.

    The runtime check fires only when the second watcher gets that far — after the first
    is already answering, with the winner decided by start order.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = patch("gateway.core.state.RUNTIME_DIR", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, connector, records):
        save_state(connector, records)

    def _record(self, name, room, session_id, identity="claude:/w"):
        return WatcherState(
            watcher_name=name,
            session_id=session_id,
            room_id=room,
            backend_identity=identity,
        )

    def test_one_session_two_rooms_is_refused(self):
        self._write("rc", [
            self._record("w1", "room-a", "ses-1"),
            self._record("w2", "room-b", "ses-1"),
        ])
        with self.assertRaises(DuplicateSessionError) as cm:
            check_session_uniqueness()
        msg = str(cm.exception)
        self.assertIn("w1", msg)
        self.assertIn("w2", msg)

    def test_it_reads_every_connector_file(self):
        """`SessionMaps` is one instance shared by all connectors, so two connectors'
        records can collide with each other — checking only the one being started would
        miss exactly the case that needs a shared map to go wrong."""
        self._write("rc", [self._record("w1", "room-a", "ses-1")])
        self._write("mm", [self._record("w2", "room-b", "ses-1")])
        with self.assertRaises(DuplicateSessionError):
            check_session_uniqueness()

    def test_the_same_room_twice_is_not_this_check_s_business(self):
        """That is a duplicate watcher, refused when the second claims the room. Raising
        here would report one fault as another."""
        self._write("rc", [
            self._record("w1", "room-a", "ses-1"),
            self._record("w2", "room-a", "ses-1"),
        ])
        check_session_uniqueness()

    def test_records_without_an_identity_are_skipped(self):
        """Not leniency — such a record cannot reuse its session at all.

        `_provision_session` treats an unverifiable identity as a mismatch and starts
        fresh, so two of them never share a live session. Refusing them would reject a
        state that heals itself on the next start, which is the expensive direction.
        """
        self._write("rc", [
            self._record("w1", "room-a", "ses-1", identity=""),
            self._record("w2", "room-b", "ses-1", identity=""),
        ])
        check_session_uniqueness()

    def test_different_backends_do_not_collide(self):
        self._write("rc", [
            self._record("w1", "room-a", "ses-1", identity="claude:/w"),
            self._record("w2", "room-b", "ses-1", identity="opencode:/w"),
        ])
        check_session_uniqueness()

    def test_a_connector_name_containing_dots_still_parses(self):
        """The file name is sliced, not split, and this is the case that tells them
        apart — a check that read the wrong connector name would silently examine
        nothing and pass."""
        self._write("my.rc.prod", [
            self._record("w1", "room-a", "ses-1"),
            self._record("w2", "room-b", "ses-1"),
        ])
        self.assertEqual(
            connector_name_of(self.runtime / "state.my.rc.prod.json"), "my.rc.prod")
        with self.assertRaises(DuplicateSessionError):
            check_session_uniqueness()


class TestConfigRefusesARoomTwice(unittest.TestCase):
    """The cheap half: name both entries at load instead of failing whichever starts
    second."""

    def _config(self, second_room="general", second_connector="rc"):
        return textwrap.dedent(f"""
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: https://chat.example.com
                  username: bot-a
                  password: secret
              - name: rc2
                type: rocketchat
                server:
                  url: https://chat.example.com
                  username: bot-b
                  password: secret
            agents:
              default:
                type: claude
                working_directory: {{root}}
            watchers:
              - name: w1
                connector: rc
                room: general
              - name: w2
                connector: {second_connector}
                room: {second_room}
        """)

    def _load(self, text):
        from gateway.config import GatewayConfig

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "config.yaml"
            path.write_text(text.replace("{root}", str(root)))
            return GatewayConfig.from_file(str(path))

    def test_two_watchers_on_one_connector_and_room_are_refused(self):
        with self.assertRaises(ValueError) as cm:
            self._load(self._config())
        msg = str(cm.exception)
        self.assertIn("w1", msg)
        self.assertIn("w2", msg)
        self.assertIn("general", msg)

    def test_the_same_room_on_another_connector_is_fine(self):
        """The supported multi-agent shape: each agent has its own bot account, so its
        own connector and its own dispatcher. Refusing this would break the deployment
        model the project recommends."""
        self._load(self._config(second_connector="rc2"))

    def test_two_rooms_on_one_connector_are_fine(self):
        self._load(self._config(second_room="random"))

    def test_config_validate_reports_it_too(self):
        """Same rule, called from the fault-tolerant path, so `acg config validate`
        reports the pair rather than only the daemon refusing to boot."""
        from gateway.config_validate import validate_config

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "config.yaml"
            path.write_text(self._config().replace("{root}", str(root)))
            result = validate_config(str(path))

        self.assertTrue(
            any("both watch room" in e for e in result.errors),
            f"expected a room-collision error, got {result.errors}",
        )


if __name__ == "__main__":
    unittest.main()
