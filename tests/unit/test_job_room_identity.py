"""A scheduled job carries its room's id, and can resurrect a room `expire` reclaimed.

A job used to name only a watcher HANDLE (`<connector>:<room label>`). That is a
pure function of `(connector, room)` and free to change — the design says so in
as many words — so it is a label, not an identity. Two consequences followed
from treating it as one:

* `expire` deletes the record, the record is what the wake resolved through, and
  the job then fired at nothing until someone spoke in the room. That is why
  `expire` used to delete the jobs, which destroyed something recoverable.
* A rename frees a room's name for another room, so a handle can come to mean a
  DIFFERENT room while the job still points at it.

The fix is the one the record layer already made: `room_id` is the identity and
the name is display. The job persists the id, the wake re-resolves the room from
the connector by that id, and creation runs against the CURRENT rules through
the same `get_or_create` a message uses — so pause, the creation cap and rule
matching all decide identically whether a message or a job asked.

Run with:
    uv run python -m pytest tests/unit/test_job_room_identity.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.connector import Connector
from gateway.core.watcher_manager import RoomRef
from gateway.core.watcher_rule import RoomKind
from gateway.schedule_types import ScheduledJob


class TestTheContractExists(unittest.TestCase):
    """`room_ref_by_id` is on the base, so a caller may always ask."""

    def test_the_base_answers_none_rather_than_raising(self):
        """A connector that cannot look a room up by id cannot resurrect one —
        its callers degrade, they do not break."""

        import asyncio

        # Unbound: `Connector` is an ABC with other abstract members, and the
        # default's whole point is that it needs no state.
        self.assertIsNone(
            asyncio.run(Connector.room_ref_by_id(object(), "r1"))  # type: ignore[arg-type]
        )

    def test_it_returns_a_room_ref_not_a_room(self):
        """Creating a watcher needs the KIND and, for DMs, the PARTICIPANTS —
        neither of which a `Room` carries. Pinned on the annotation because the
        base returns None and cannot demonstrate it."""
        import inspect

        annotation = inspect.signature(Connector.room_ref_by_id).return_annotation
        self.assertIn("RoomRef", str(annotation))


class TestTheJobCarriesTheRoom(unittest.TestCase):
    def test_room_id_defaults_empty_for_jobs_that_predate_it(self):
        """Runtime state is not converted (§5.3). An old job keeps the old
        behaviour rather than being silently reinterpreted."""
        self.assertEqual(ScheduledJob(watcher="rc:general").room_id, "")

    def test_it_is_persisted_alongside_the_handle_not_instead_of_it(self):
        """The handle stays: it is the display and CLI identity, and `list`,
        `pause` and `expire` all speak it."""
        job = ScheduledJob(watcher="rc:general", connector="rc", room_id="room-1")
        self.assertEqual(job.watcher, "rc:general")
        self.assertEqual(job.room_id, "room-1")


def _manager(*, record, room_ref, created_record=None):
    """A SessionManager stub exercising only `inject_message`'s wake branch.

    `created_record` is what `get_watcher_state` answers AFTER a successful
    creation — `inject_message` re-reads it to address the reply, and in
    production `get_or_create` has written one by then. Modelling that keeps the
    stub honest about the order of operations rather than about one call.
    """
    from gateway.core.session_manager import SessionManager

    processor = MagicMock()
    processor.enqueue = AsyncMock(return_value=True)

    manager = SessionManager.__new__(SessionManager)
    manager._connector_name = "rc"
    manager._lifecycle = MagicMock()
    manager._lifecycle.get_processor = MagicMock(return_value=None)
    states = [record, created_record if created_record is not None else record]
    manager._lifecycle.get_watcher_state = MagicMock(side_effect=lambda _n: states.pop(0) if states else record)
    manager._watcher_manager = MagicMock()
    manager._watcher_manager.get_or_create = AsyncMock(return_value=processor)
    manager._connector = MagicMock()
    manager._connector.room_ref_by_id = AsyncMock(return_value=room_ref)
    return manager


def _record(**kw):
    defaults = dict(room_id="room-1", room_name="general", participants=[],
                    room_kind="channel", room_type="channel")
    return MagicMock(**{**defaults, **kw})


class TestTheWakeResolvesByIdWhenNoRecordSurvives(unittest.IsolatedAsyncioTestCase):
    ROOM = RoomRef(id="room-1", kind=RoomKind.CHANNEL, name="general")

    async def test_a_job_resurrects_an_expired_room(self):
        """The case `expire` used to delete jobs to avoid."""
        manager = _manager(record=None, room_ref=self.ROOM, created_record=_record())

        await manager.inject_message("rc:general", "poke", room_id="room-1")

        manager._connector.room_ref_by_id.assert_awaited_once_with("room-1")
        manager._watcher_manager.get_or_create.assert_awaited_once()
        _, room = manager._watcher_manager.get_or_create.await_args.args
        self.assertEqual(room, self.ROOM)

    async def test_it_goes_through_the_same_entry_point_a_message_uses(self):
        """Owner's requirement: the wake is the lifecycle's, and must not mean
        something different because a job asked. Pause, the creation cap and the
        rule match all live inside `get_or_create`, so asserting the call IS
        asserting that a job cannot reach a room a message could not."""
        manager = _manager(record=None, room_ref=self.ROOM, created_record=_record())

        await manager.inject_message("rc:general", "poke", room_id="room-1")

        self.assertEqual(manager._watcher_manager.get_or_create.await_count, 1)

    async def test_a_record_still_wins_over_the_id(self):
        """An idle room's record carries its frozen provenance — participants a
        by-id lookup may no longer see, and the kind it was classified as. The id
        is the fallback for when that record is gone, not a replacement."""
        record = _record(participants=["alice"], room_kind="dm", room_type="dm")
        manager = _manager(record=record, room_ref=self.ROOM)

        await manager.inject_message("rc:general", "poke", room_id="room-1")

        manager._connector.room_ref_by_id.assert_not_awaited()
        _, room = manager._watcher_manager.get_or_create.await_args.args
        self.assertEqual(room.participants, ("alice",))

    async def test_an_old_job_without_a_room_id_behaves_exactly_as_before(self):
        """No id, no record — no wake, and nothing consulted. The migration
        story for jobs that predate the field."""
        manager = _manager(record=None, room_ref=self.ROOM)

        result = await manager.inject_message("rc:general", "poke")

        self.assertFalse(result)
        manager._connector.room_ref_by_id.assert_not_awaited()
        manager._watcher_manager.get_or_create.assert_not_awaited()

    async def test_a_room_the_connector_disowns_is_not_resurrected(self):
        """`None` means answered-and-absent: removed from the room, or never
        ours. The fire fails and the job survives to retry (owner: "let the
        schedule job throw an error, it is a lot better than auto remove it")."""
        manager = _manager(record=None, room_ref=None)

        result = await manager.inject_message("rc:general", "poke", room_id="room-1")

        self.assertFalse(result)
        manager._watcher_manager.get_or_create.assert_not_awaited()

    async def test_a_transport_failure_does_not_crash_the_fire(self):
        """A raise means "I could not ask", which is not "the room is gone" — it
        must not propagate into the scheduler's tick, and it must be logged as
        the different thing it is."""
        manager = _manager(record=None, room_ref=None)
        manager._connector.room_ref_by_id = AsyncMock(side_effect=OSError("network"))

        result = await manager.inject_message("rc:general", "poke", room_id="room-1")

        self.assertFalse(result)

    async def test_a_connector_without_the_capability_is_not_an_error(self):
        """voice/script never resurrect a room — their rooms are literals in
        config — so the absent method is a degradation, not a failure."""
        manager = _manager(record=None, room_ref=None)
        del manager._connector.room_ref_by_id

        result = await manager.inject_message("rc:general", "poke", room_id="room-1")

        self.assertFalse(result)


class TestTheSchedulerPassesItThrough(unittest.IsolatedAsyncioTestCase):
    async def test_the_fire_hands_the_jobs_room_id_to_the_wake(self):
        from gateway.core.scheduler import JobScheduler

        scheduler = JobScheduler.__new__(JobScheduler)
        manager = MagicMock()
        manager.inject_message = AsyncMock(return_value=True)
        scheduler._session_managers = {"rc": manager}

        job = ScheduledJob(
            watcher="rc:general", connector="rc", room_id="room-1", message="poke",
        )
        await scheduler._inject(job)

        manager.inject_message.assert_awaited_once_with(
            "rc:general", "poke", room_id="room-1",
        )


if __name__ == "__main__":
    unittest.main()


class TestTheConnectorsResolveByIdWithoutASecondClassifier(unittest.IsolatedAsyncioTestCase):
    """Each implementation reuses the classifier its message path already has.

    The DM branch is the reason that matters: on Rocket.Chat the type letter `d`
    covers both DM kinds, and the difference decides whether the mention gate
    applies (§6.4) — so a second, by-id-only classifier would be a second place
    to get that wrong. These assert the reuse by its observable effect: the
    participants a DM needs for its handle come back, without the caller asking.
    """

    async def test_rocketchat_classifies_a_channel_from_its_subscription(self):
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        connector = RocketChatConnector.__new__(RocketChatConnector)
        connector._rest = MagicMock()
        connector._rest.get_subscription = AsyncMock(
            return_value={"t": "c", "name": "general"})

        room = await connector.room_ref_by_id("room-1")

        self.assertEqual(room, RoomRef(id="room-1", kind=RoomKind.CHANNEL, name="general"))

    async def test_rocketchat_asks_who_is_in_a_direct_room(self):
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        connector = RocketChatConnector.__new__(RocketChatConnector)
        connector._rest = MagicMock()
        connector._rest.get_subscription = AsyncMock(return_value={"t": "d"})
        connector._direct_room_identity = AsyncMock(
            return_value=(RoomKind.DM, ("alice",)))

        room = await connector.room_ref_by_id("room-1")

        self.assertEqual(room.kind, RoomKind.DM)
        self.assertEqual(room.participants, ("alice",))

    async def test_rocketchat_answers_none_for_a_room_it_has_no_subscription_to(self):
        """Removed from the room, or never in it — the honest answer for a
        caller asking whether it can still serve it."""
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        connector = RocketChatConnector.__new__(RocketChatConnector)
        connector._rest = MagicMock()
        connector._rest.get_subscription = AsyncMock(return_value=None)

        self.assertIsNone(await connector.room_ref_by_id("room-1"))

    async def test_mattermost_keeps_the_members_as_participants(self):
        """`resolve_room_by_id` flattens them into a display name because a
        `Room` has nowhere else to put them; this keeps the structure, which is
        what a 1:1 DM's handle is built from."""
        from gateway.connectors.mattermost.connector import MattermostConnector
        from gateway.core.connector import Room

        connector = MattermostConnector.__new__(MattermostConnector)
        connector.resolve_room_by_id = AsyncMock(
            return_value=Room(id="d1", name="alice", type="dm"))
        connector._rest = MagicMock()
        connector._rest.bot_user_id = "bot"
        connector._rest.channel_member_usernames = AsyncMock(return_value=["alice"])

        room = await connector.room_ref_by_id("d1")

        self.assertEqual(room.kind, RoomKind.DM)
        self.assertEqual(room.participants, ("alice",))

    async def test_mattermost_names_a_channel_and_asks_no_members(self):
        from gateway.connectors.mattermost.connector import MattermostConnector
        from gateway.core.connector import Room

        connector = MattermostConnector.__new__(MattermostConnector)
        connector.resolve_room_by_id = AsyncMock(
            return_value=Room(id="c1", name="general", type="channel"))
        connector._rest = MagicMock()
        connector._rest.channel_member_usernames = AsyncMock()

        room = await connector.room_ref_by_id("c1")

        self.assertEqual(room, RoomRef(id="c1", kind=RoomKind.CHANNEL, name="general"))
        connector._rest.channel_member_usernames.assert_not_awaited()


class TestAnOldJobMigratesItselfOnTheNextFire(unittest.IsolatedAsyncioTestCase):
    """The compatibility path, done lazily (owner, 2026-08-31).

    A job created before `room_id` existed would resolve by handle forever. But
    every fire that finds a record has the id in hand, so it is written back and
    the job is migrated from then on — one extra step, on one fire, per old job.

    Lazily is also the only place it CAN be done. A boot-time converter would
    read records that may not be loaded yet, and could say nothing about a job
    whose room is already reclaimed — which is precisely the job the id would
    have saved.
    """

    def _scheduler(self, *, state, update=None):
        from gateway.core.scheduler import JobScheduler

        scheduler = JobScheduler.__new__(JobScheduler)
        manager = MagicMock()
        manager.inject_message = AsyncMock(return_value=True)
        manager.get_watcher_state = MagicMock(return_value=state)
        scheduler._session_managers = {"rc": manager}
        scheduler._store = MagicMock()
        if update is not None:
            scheduler._store.update = update
        return scheduler, manager

    async def test_the_first_fire_records_the_room_id(self):
        scheduler, manager = self._scheduler(state=_record(room_id="room-1"))
        job = ScheduledJob(watcher="rc:general", connector="rc", message="poke")

        await scheduler._inject(job)

        self.assertEqual(job.room_id, "room-1")
        scheduler._store.update.assert_called_once_with(job)
        # And THIS fire already benefits — not just the next one.
        manager.inject_message.assert_awaited_once_with(
            "rc:general", "poke", room_id="room-1",
        )

    async def test_a_job_that_already_has_one_is_not_rewritten(self):
        """No store write on every fire forever — the migration happens once."""
        scheduler, _ = self._scheduler(state=_record(room_id="room-1"))
        job = ScheduledJob(
            watcher="rc:general", connector="rc", room_id="room-1", message="poke")

        await scheduler._inject(job)

        scheduler._store.update.assert_not_called()

    async def test_no_record_means_nothing_to_learn_and_no_write(self):
        """The room is already reclaimed. There is no id to recover, and the
        fire fails exactly as it would have — the backfill adds no new failure
        mode."""
        scheduler, manager = self._scheduler(state=None)
        job = ScheduledJob(watcher="rc:general", connector="rc", message="poke")

        await scheduler._inject(job)

        self.assertEqual(job.room_id, "")
        scheduler._store.update.assert_not_called()
        manager.inject_message.assert_awaited_once_with(
            "rc:general", "poke", room_id="",
        )

    async def test_a_store_that_cannot_be_written_does_not_fail_the_fire(self):
        """Best-effort: the in-memory field stays set so this fire still
        benefits, and only the persistence is retried next time."""
        def _boom(_job):
            raise OSError("read-only filesystem")

        scheduler, manager = self._scheduler(
            state=_record(room_id="room-1"), update=MagicMock(side_effect=_boom))
        job = ScheduledJob(watcher="rc:general", connector="rc", message="poke")

        result = await scheduler._inject(job)

        self.assertTrue(result, "the fire is not failed by a migration problem")
        self.assertEqual(job.room_id, "room-1")

    async def test_a_lookup_that_raises_does_not_fail_the_fire_either(self):
        scheduler, manager = self._scheduler(state=None)
        scheduler._session_managers["rc"].get_watcher_state = MagicMock(
            side_effect=RuntimeError("state store unavailable"))
        job = ScheduledJob(watcher="rc:general", connector="rc", message="poke")

        result = await scheduler._inject(job)

        self.assertTrue(result)
        self.assertEqual(job.room_id, "")
