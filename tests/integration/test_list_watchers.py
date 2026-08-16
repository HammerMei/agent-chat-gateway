"""`list` rows come from state records, not from config (design §2.8).

The filter, the state derivation and the merged view are unit-tested in
``tests/unit/test_list_filter.py``; this module pins what a row *says* and,
more importantly, **which watchers appear at all** — which is the behaviour
this increment changes.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig, WatcherConfig
from gateway.connectors.script import ScriptConnector
from gateway.core.config import CoreConfig
from gateway.core.connector import Room
from gateway.core.session_manager import SessionManager
from gateway.core.state import StateFilter, WatcherState
from tests.helpers import IsolatedTestCase


class MockAgentBackend(AgentBackend):
    def __init__(self):
        self._session_counter = 0

    async def create_session(
        self, working_directory, extra_args=None, session_title=None
    ):
        self._session_counter += 1
        return f"mock-session-{self._session_counter:04d}"

    async def send(self, session_id, prompt, working_directory, timeout, **kwargs):
        return AgentResponse(text="ok")

    async def ensure_durable_instructions(self, *a, **kw):
        """Skip the send()-based fallback: these tests never exercise injection."""
        return None


def make_watcher(room="script", name=None, agent="default"):
    return WatcherConfig(
        name=name or room, connector="script", room=room, agent=agent
    )


def make_manager(connector, agent, watcher_configs=None):
    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)}, default_agent="default"
    )
    return SessionManager(
        connector,
        {"default": agent},
        "default",
        config,
        watcher_configs=watcher_configs or [],
    )


def _record(name, **kwargs) -> WatcherState:
    return WatcherState(
        watcher_name=name,
        session_id=kwargs.pop("session_id", f"s-{name}"),
        room_id=kwargs.pop("room_id", f"room-{name}"),
        **kwargs,
    )


class TestListEnumeratesRecords(IsolatedTestCase):
    """Which watchers appear — the half that changed."""

    def _manager(self, disk_records, watcher_configs=None):
        # Overrides tests.helpers' blanket patch for this test only: the point
        # of the increment is that disk records are what `list` reads.
        patcher = patch(
            "gateway.core.state_store.load_state", return_value=list(disk_records)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return make_manager(
            ScriptConnector(), MockAgentBackend(), watcher_configs or []
        )

    async def test_a_record_with_no_config_entry_is_listed(self):
        """Under rule-derived watchers there is no config entry to enumerate,
        so a record has to be enough on its own."""
        manager = self._manager([_record("rule-derived")], watcher_configs=[])

        names = [w["watcher_name"] for w in manager.list_watchers()]

        self.assertEqual(names, ["rule-derived"])

    async def test_a_configured_watcher_with_no_record_is_not_listed(self):
        """It has no session, no watermark and nothing to pause.

        The old `list` enumerated config and so showed it as inactive; the
        failure is now reported by startup instead, which is where a start-time
        failure belongs.
        """
        manager = self._manager([], watcher_configs=[make_watcher(name="never-ran")])

        self.assertEqual(manager.list_watchers(), [])

    async def test_a_blocked_agent_that_ran_before_keeps_its_row(self):
        """The docstring's other half: only a watcher that has *never* started
        disappears.  One blocked this boot but started on an earlier one still
        has a record on disk, with the session id that is worth seeing.
        """
        manager = self._manager(
            [_record("w1", session_id="s-from-last-boot")],
            watcher_configs=[make_watcher(name="w1")],
        )

        await manager.run_once(unavailable_agents={"default"})

        rows = manager.list_watchers()
        self.assertEqual([w["watcher_name"] for w in rows], ["w1"])
        self.assertEqual(rows[0]["session_id"], "s-from-last-boot")
        await manager.shutdown()

    async def test_a_blocked_agent_with_no_prior_record_is_reported_by_sync(self):
        """Only true when there is no record — see the test above for the case
        where an earlier boot left one."""
        manager = self._manager([], watcher_configs=[make_watcher(name="w1")])

        errors = await manager.run_once(unavailable_agents={"default"})

        self.assertEqual(manager.list_watchers(), [])
        self.assertTrue(any("w1" in e for e in errors), errors)
        await manager.shutdown()

    async def test_rows_are_ordered_by_name(self):
        """Deterministic regardless of which side of the merge a record came from."""
        manager = self._manager([_record("zulu"), _record("alpha"), _record("mike")])

        names = [w["watcher_name"] for w in manager.list_watchers()]

        self.assertEqual(names, ["alpha", "mike", "zulu"])


class TestListStateFilter(IsolatedTestCase):
    def _manager(self, disk_records):
        patcher = patch(
            "gateway.core.state_store.load_state", return_value=list(disk_records)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return make_manager(ScriptConnector(), MockAgentBackend(), [])

    async def test_default_shows_active_and_paused_but_not_idle(self):
        manager = self._manager(
            [
                _record("act"),
                _record("pause", paused=True),
                _record("idle", dropped_at="2026-08-16T00:00:00Z"),
            ]
        )

        rows = {w["watcher_name"]: w["state"] for w in manager.list_watchers()}

        self.assertEqual(rows, {"act": "active", "pause": "paused"})

    async def test_idle_is_one_flag_away(self):
        manager = self._manager([_record("idle", dropped_at="2026-08-16T00:00:00Z")])

        rows = manager.list_watchers(StateFilter.IDLE)

        self.assertEqual([w["watcher_name"] for w in rows], ["idle"])
        self.assertEqual(rows[0]["state"], "idle")

    async def test_all_returns_every_state(self):
        manager = self._manager(
            [
                _record("act"),
                _record("pause", paused=True),
                _record("idle", dropped_at="2026-08-16T00:00:00Z"),
            ]
        )

        rows = manager.list_watchers(StateFilter.ALL)

        self.assertEqual(len(rows), 3)


class TestListRowContents(IsolatedTestCase):
    def _manager(self, disk_records, watcher_configs=None):
        patcher = patch(
            "gateway.core.state_store.load_state", return_value=list(disk_records)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return make_manager(
            ScriptConnector(), MockAgentBackend(), watcher_configs or []
        )

    async def test_room_id_and_participants_are_reported(self):
        """The participants column is how a group DM is identified (§2.3)."""
        manager = self._manager(
            [
                _record(
                    "gdm",
                    room_id="cib3hjsrgpydtf6tyac7frcu6o",
                    room_kind="group_dm",
                    participants=["@alice", "@bob"],
                )
            ]
        )

        row = manager.list_watchers()[0]

        self.assertEqual(row["room_id"], "cib3hjsrgpydtf6tyac7frcu6o")
        self.assertEqual(row["participants"], ["@alice", "@bob"])
        # No `room_kind` in the row: nothing reads it — not the table, not the
        # control server — and a field with no consumer is the shape this
        # series has already been corrected on twice.
        self.assertNotIn("room_kind", row)

    async def test_a_nameless_room_falls_back_to_its_id(self):
        """A DM carries no room name, and the row still has to name the room."""
        manager = self._manager([_record("dm", room_id="rid-9", room_name="")])

        self.assertEqual(manager.list_watchers()[0]["room_name"], "rid-9")

    async def test_participants_are_copied_not_aliased(self):
        """A caller mutating a row must not edit the persisted record."""
        record = _record("gdm", participants=["@alice"])
        manager = self._manager([record])

        manager.list_watchers()[0]["participants"].append("@mallory")

        self.assertEqual(record.participants, ["@alice"])

    async def test_connector_and_agent_fall_back_while_records_are_unstamped(self):
        """The static path writes neither field; the row still has to attribute
        the watcher to a connector, or a multi-connector `list` is unreadable."""
        manager = SessionManager(
            ScriptConnector(),
            {"default": MockAgentBackend()},
            "default",
            CoreConfig(
                agents={"default": AgentConfig(timeout=10)}, default_agent="default"
            ),
            watcher_configs=[make_watcher(name="w1", agent="default")],
            state_name="rc-home",
        )
        patcher = patch(
            "gateway.core.state_store.load_state", return_value=[_record("w1")]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        row = manager.list_watchers()[0]

        # The connector entry these records belong to — the one thing a
        # per-connector lifecycle always knows, and what `state.<name>.json` is
        # named after.
        self.assertEqual(row["connector"], "rc-home")
        self.assertEqual(row["agent_name"], "default")

    async def test_a_stamped_record_uses_its_own_connector_and_agent(self):
        manager = self._manager(
            [_record("w1", connector="rc-home", agent="triage")],
            watcher_configs=[make_watcher(name="w1", agent="default")],
        )

        row = manager.list_watchers()[0]

        self.assertEqual(row["connector"], "rc-home")
        self.assertEqual(row["agent_name"], "triage")

    async def test_a_started_watcher_records_the_resolved_room_name(self):
        """`list` reads records, so the room's name has to be in the record —
        not looked back up from the config entry that rules will replace.

        The resolved room's id and name are deliberately *different* here:
        ScriptConnector returns the same string for both, so a test using its
        default would pass on the `room_name or room_id` fallback alone and
        could not tell whether the name was recorded at all.
        """
        manager = self._manager([], watcher_configs=[make_watcher(room="script")])

        async def resolve(room_name):
            return Room(id="rid-42", name="#support", type="script")

        with patch.object(manager._connector, "resolve_room", side_effect=resolve):
            await manager.run_once()
        row = manager.list_watchers()[0]

        self.assertEqual(row["room_name"], "#support")
        self.assertEqual(row["room_id"], "rid-42")
        await manager.shutdown()


if __name__ == "__main__":
    unittest.main()
