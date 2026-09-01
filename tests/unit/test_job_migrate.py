"""`schedule migrate` — version-aware, re-runnable, and it never guesses.

The owner's design (2026-09-01), replacing a lazy fire-time backfill. The
difference is not speed: a backfill runs at a moment nobody chose, and the 1→2
step reads each job's watcher HANDLE to find its room — which only names the
right room while nobody has renamed it. An operator can pick a moment when that
holds; a job firing once a year cannot.

Two properties, tested separately because they answer different questions:

* **Version awareness** decides WHICH steps run, so a deployment jumping 1 → 3
  gets both and one jumping 2 → 3 gets only the second.
* **Idempotence** makes each step safe to run again, so a wrong version guess
  cannot corrupt anything.

Run with:
    uv run python -m pytest tests/unit/test_job_migrate.py -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from gateway.core.job_migrate import (
    migrate,
    room_name_for_label,
    split_handle,
)
from gateway.core.job_store import _SCHEMA_VERSION, JobStore
from gateway.core.state import WatcherState
from gateway.schedule_types import JobStatus, ScheduledJob


def _entry(name="rc", *, records=None, resolves=None):
    """A `ConnectorEntry` stand-in: a record lookup and a name resolver."""
    entry = MagicMock()
    entry.name = name
    entry.session_manager.get_watcher_state = MagicMock(
        side_effect=(records or {}).get)
    entry.connector.resolve_room = AsyncMock(
        side_effect=lambda n: (resolves or {}).get(n))
    return entry


def _record(handle, room_id):
    return WatcherState(
        watcher_name=handle, session_id="", room_id=room_id,
        room_name="general", room_type="channel", room_kind="channel",
    )


def _room(room_id):
    room = MagicMock()
    room.id = room_id
    return room


class TestHandleParsing(unittest.TestCase):
    def test_the_first_colon_is_the_boundary(self):
        """A connector name may not contain `:` and the label encoder escapes it
        out of room names, so a room called `dm:alice` cannot fool this."""
        self.assertEqual(split_handle("rc:general"), ("rc", "general"))
        self.assertEqual(split_handle("rc:dm:alice"), ("rc", "dm:alice"))

    def test_a_handle_with_no_colon_has_no_connector(self):
        self.assertEqual(split_handle("legacy-name"), ("", "legacy-name"))

    def test_a_channel_label_is_the_room_name(self):
        self.assertEqual(room_name_for_label("general"), "general")

    def test_a_label_is_percent_decoded_first(self):
        """`watcher_label` escapes everything outside `[A-Za-z0-9._-]`, so a
        voice room `a/b` labels as `a%2Fb`. The voice connector echoes whatever
        name it is given, so asking it to resolve `a%2Fb` would have recorded
        that as the room id — matching nothing — and reported a success."""
        self.assertEqual(room_name_for_label("a%2Fb"), "a/b")
        self.assertEqual(room_name_for_label("dm:al%20ice"), "@al ice")

    def test_a_dm_label_becomes_the_at_spelling(self):
        """`@alice` is the spelling `resolve_room` documents for a DM."""
        self.assertEqual(room_name_for_label("dm:alice"), "@alice")

    def test_a_group_dm_label_cannot_be_resolved(self):
        """It is a digest of the room id, not a name. Reported, never guessed."""
        self.assertIsNone(room_name_for_label("gdm:8f3a2b1c"))


class _MigrateCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "jobs.json"

    def _write_file(self, version, jobs):
        self.path.write_text(json.dumps({"version": version, "jobs": jobs}))

    def _store(self) -> JobStore:
        store = JobStore(self.path)
        store.load()
        return store

    def _job(self, job_id="acg-1", watcher="rc:general", **kw):
        return ScheduledJob(
            id=job_id, watcher=watcher, connector="rc", message="poke",
            cron="0 9 * * *", status=JobStatus.ACTIVE, **kw,
        ).to_dict()


class TestVersionAwareness(_MigrateCase):
    async def test_a_current_file_reports_nothing_to_do(self):
        self._write_file(_SCHEMA_VERSION, [])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertEqual(report.steps, [])
        self.assertEqual(report.from_version, report.to_version)

    async def test_an_old_file_runs_the_step_and_is_stamped(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        report = await migrate(store, [entry])

        self.assertEqual(len(report.steps), 1)
        self.assertEqual(store.file_version, _SCHEMA_VERSION)
        self.assertEqual(
            json.loads(self.path.read_text())["version"], _SCHEMA_VERSION)

    async def test_a_newer_file_is_refused_rather_than_migrated_down(self):
        """Saving would drop the fields this version does not know."""
        self._write_file(_SCHEMA_VERSION + 5, [])
        store = self._store()

        with self.assertRaises(ValueError) as cm:
            await migrate(store, [_entry()])
        self.assertIn("newer version", str(cm.exception))

    async def test_a_missing_version_reads_as_old(self):
        """The field has been written since the first release, so an absent one
        means old. Guessing NEW would skip a migration silently."""
        self.path.write_text(json.dumps({"jobs": []}))
        store = self._store()

        self.assertEqual(store.file_version, 1)
        self.assertTrue(store.needs_migration())

    async def test_a_save_does_not_stamp_a_migration_that_did_not_run(self):
        """`save()` used to write the CODE's version unconditionally, so one
        ordinary fire marked an unmigrated file as current — silencing both the
        startup warning and the migration itself.

        The file is asserted, not just the in-memory value: the original bug was
        that `stamp_version` moved only the in-memory version while every save
        rewrote the file's, and a test looking only at `file_version` passed
        against it (review).
        """
        self._write_file(1, [self._job()])
        store = self._store()

        store.update(ScheduledJob.from_dict(self._job()))  # an ordinary save

        self.assertEqual(
            json.loads(self.path.read_text())["version"], 1,
            "the FILE was stamped by an ordinary save",
        )
        self.assertEqual(store.file_version, 1)
        self.assertTrue(self._store().needs_migration(), "a restart would skip it")

    async def test_a_save_does_not_stamp_a_newer_file_with_its_own_version(self):
        """The other direction, and the one the first fix introduced: writing
        `self._file_version` would claim version N while `to_dict` had already
        dropped the fields version N carries — a future ACG would then skip the
        migrations that restore them."""
        self._write_file(_SCHEMA_VERSION + 5, [self._job()])
        store = self._store()

        store.update(ScheduledJob.from_dict(self._job()))

        self.assertEqual(
            json.loads(self.path.read_text())["version"], _SCHEMA_VERSION,
            "never claim more than this code wrote",
        )


class TestTheOneToTwoStep(_MigrateCase):
    async def test_a_record_supplies_the_room_id(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-1")
        self.assertEqual(report.changed, 1)
        entry.connector.resolve_room.assert_not_awaited()

    async def test_without_a_record_the_connector_resolves_the_name(self):
        """The case right after an upgrade, where static-era records have been
        pruned and rule-derived ones do not exist until a room speaks."""
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(resolves={"general": _room("room-1")})

        await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-1")
        entry.connector.resolve_room.assert_awaited_once_with("general")

    async def test_a_dm_job_resolves_through_the_at_spelling(self):
        self._write_file(1, [self._job(watcher="rc:dm:alice")])
        store = self._store()
        entry = _entry(resolves={"@alice": _room("room-dm")})

        await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-dm")

    async def test_an_empty_connector_field_is_filled_in_too(self):
        """`_get_sm_for_watcher` needs it, and a job with neither field cannot be
        routed to a manager at all."""
        job = self._job()
        job["connector"] = ""
        self._write_file(1, [job])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").connector, "rc")

    async def test_a_completed_job_is_left_alone(self):
        job = self._job()
        job["status"] = JobStatus.COMPLETED.value
        self._write_file(1, [job])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertEqual(report.outcomes, [])


class TestNothingIsGuessed(_MigrateCase):
    async def test_an_unresolvable_room_leaves_the_job_untouched(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry()  # no record, and the connector knows no such room

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "")
        self.assertEqual(report.changed, 0)
        self.assertEqual(len(report.unresolved), 1)
        self.assertIn("knows no room named", report.unresolved[0].detail)

    async def test_a_group_dm_says_what_to_do_instead(self):
        self._write_file(1, [self._job(watcher="rc:gdm:8f3a2b1c")])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertEqual(store.get("acg-1").room_id, "")
        self.assertIn("recreate it", report.unresolved[0].detail)

    async def test_a_connector_that_raises_does_not_stop_the_run(self):
        """One job's resolution failure must not deny the others their migration."""
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()
        entry = _entry(resolves={"dev": _room("room-dev")})
        entry.connector.resolve_room = AsyncMock(
            side_effect=lambda n: (_ for _ in ()).throw(OSError("boom"))
            if n == "general" else _room("room-dev"))

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "")
        self.assertEqual(store.get("acg-2").room_id, "room-dev")
        self.assertEqual(report.changed, 1)

    async def test_an_unknown_connector_is_reported_not_assumed(self):
        job = self._job()
        job["connector"] = "gone-away"
        job["watcher"] = "gone-away:general"
        self._write_file(1, [job])
        store = self._store()

        report = await migrate(store, [_entry("rc")])

        self.assertEqual(store.get("acg-1").room_id, "")
        self.assertIn("no configured connector", report.unresolved[0].detail)


class TestItIsSafeToRunAgain(_MigrateCase):
    async def test_a_second_run_changes_nothing(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        await migrate(store, [entry])
        first = json.loads(self.path.read_text())

        # Reload, as a fresh daemon would, and run it again.
        again = self._store()
        report = await migrate(again, [entry])

        self.assertEqual(report.steps, [], "the version stopped it")
        self.assertEqual(json.loads(self.path.read_text())["jobs"], first["jobs"])

    async def test_the_step_itself_is_idempotent_even_if_the_version_is_wrong(self):
        """The property the version cannot provide: if a file claims to be old
        when it is not, re-running the step must still be harmless."""
        self._write_file(1, [self._job(room_id="room-already")])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-other")})

        report = await migrate(store, [entry])

        self.assertEqual(store.get("acg-1").room_id, "room-already",
                         "an existing id must not be overwritten")
        self.assertEqual(report.changed, 0)
        self.assertIn("already has", report.outcomes[0].detail)


if __name__ == "__main__":
    unittest.main()


class TestTheVersionOnlyMovesWhenAMigrationRuns(_MigrateCase):
    """Found by review: `save()` stamped the code's version unconditionally, so
    one ordinary fire marked an unmigrated file as current — the startup warning
    vanished and `schedule migrate` answered "nothing to do" while every job
    still had an empty `room_id`.

    `stamp_version`'s docstring described that hazard and only half-prevented it:
    it was the one place that moved the IN-MEMORY version, while every save
    rewrote the file's.
    """

    async def test_an_ordinary_save_leaves_the_file_version_alone(self):
        self._write_file(1, [self._job()])
        store = self._store()

        job = store.get("acg-1")
        job.run_count = 1
        store.update(job)  # what a fire does

        self.assertEqual(json.loads(self.path.read_text())["version"], 1)
        self.assertEqual(self._store().file_version, 1,
                         "a restart would see the file as migrated")
        self.assertTrue(self._store().needs_migration())

    async def test_a_removal_leaves_it_alone_too(self):
        """`add`/`update`/`remove`/`remove_expired_completed` all save."""
        self._write_file(1, [self._job()])
        store = self._store()

        store.remove("acg-1")

        self.assertEqual(json.loads(self.path.read_text())["version"], 1)

    async def test_only_the_migration_moves_it(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(records={"rc:general": _record("rc:general", "room-1")})

        await migrate(store, [entry])

        self.assertEqual(json.loads(self.path.read_text())["version"],
                         _SCHEMA_VERSION)


class TestAnUnfinishedMigrationStaysRunnable(_MigrateCase):
    """Also from review: stamping over an unresolved job would make the re-run
    the operator is TOLD to make answer "nothing to do" — and that job could
    then never be migrated at all."""

    async def test_the_version_does_not_move_while_a_job_needs_attention(self):
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()
        # 'general' resolves; 'dev' does not.
        entry = _entry(resolves={"general": _room("room-1")})

        report = await migrate(store, [entry])

        self.assertEqual(report.changed, 1)
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(json.loads(self.path.read_text())["version"], 1,
                         "an unfinished migration must stay runnable")

    async def test_the_re_run_after_a_fix_completes_it(self):
        """The whole point of not stamping early."""
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()
        await migrate(store, [_entry(resolves={"general": _room("room-1")})])

        # The operator brings the second room back, and re-runs.
        again = self._store()
        report = await migrate(
            again, [_entry(resolves={"general": _room("room-1"),
                                     "dev": _room("room-dev")})])

        self.assertEqual(again.get("acg-2").room_id, "room-dev")
        self.assertEqual(report.unresolved, [])
        self.assertEqual(json.loads(self.path.read_text())["version"],
                         _SCHEMA_VERSION)

    async def test_a_clean_re_run_is_not_mistaken_for_unfinished_work(self):
        """A job that already has an id did not change and needs nothing. The
        two used to be one state, which would have left the version stuck."""
        self._write_file(1, [self._job(room_id="room-1")])
        store = self._store()

        report = await migrate(store, [_entry()])

        self.assertEqual(report.changed, 0)
        self.assertEqual(report.unresolved, [], "'already done' is not 'stuck'")
        self.assertEqual(json.loads(self.path.read_text())["version"],
                         _SCHEMA_VERSION)


class TestAConcurrentFireCannotUndoTheMigration(_MigrateCase):
    """Found by review, reproduced both ways.

    `_migrate_1_to_2` reads a job, awaits `resolve_room` (a network round trip),
    then writes. With `store.update(job)` — a whole-object replace — a fire that
    completed during that await was silently reverted:

    * the fire's stale copy erased the `room_id` the migration had just written,
      while the report said `✓`. The command's output IS its contract, and the
      job could never be migrated again;
    * or the migration's stale object resurrected a job the fire had just
      COMPLETED, with `next_run` back in the past — one duplicate delivery.

    `set_room_id` touches only the two fields the migration owns, under the
    store's lock, on the object the store currently holds.
    """

    async def _migrate_with_a_fire_in_the_middle(self, store, entry, mutate):
        """Run the migration, letting `mutate(store)` land during resolution."""
        resolving = entry.connector.resolve_room

        async def _resolve_then_interleave(name):
            room = await resolving(name)
            mutate(store)
            return room

        entry.connector.resolve_room = _resolve_then_interleave
        return await migrate(store, [entry])

    async def test_a_fire_completing_mid_resolution_does_not_erase_the_room_id(self):
        self._write_file(1, [self._job()])
        store = self._store()
        entry = _entry(resolves={"general": _room("room-1")})

        def _a_fire_lands(s):
            fired = s.get("acg-1")
            fired.run_count = 1
            fired.last_run = "2026-09-01T09:00:00+00:00"
            s.update(fired)

        report = await self._migrate_with_a_fire_in_the_middle(
            store, entry, _a_fire_lands)

        self.assertEqual(report.changed, 1)
        self.assertEqual(store.get("acg-1").room_id, "room-1",
                         "the report said it was written")
        self.assertEqual(
            json.loads(self.path.read_text())["jobs"][0]["room_id"], "room-1")
        # And the fire's own progress survived — the migration did not roll it back.
        self.assertEqual(store.get("acg-1").run_count, 1)

    async def test_a_completed_job_is_not_resurrected(self):
        self._write_file(1, [self._job(times=5, run_count=4)])
        store = self._store()
        entry = _entry(resolves={"general": _room("room-1")})

        def _the_last_fire_completes(s):
            done = s.get("acg-1")
            done.run_count = 5
            done.status = JobStatus.COMPLETED
            done.next_run = None
            s.update(done)

        await self._migrate_with_a_fire_in_the_middle(
            store, entry, _the_last_fire_completes)

        job = store.get("acg-1")
        self.assertEqual(job.status, JobStatus.COMPLETED, "brought back to life")
        self.assertIsNone(job.next_run, "would fire again, in the past")
        self.assertEqual(job.run_count, 5)

    async def test_a_deletion_mid_run_costs_only_that_job(self):
        """`update` raised `KeyError`, which the control handler turned into a
        bare error — losing the report for every job already migrated."""
        self._write_file(1, [self._job("acg-1"), self._job("acg-2", "rc:dev")])
        store = self._store()
        entry = _entry(resolves={"general": _room("room-1"),
                                 "dev": _room("room-dev")})

        def _delete_the_first(s):
            s.remove("acg-1")

        report = await self._migrate_with_a_fire_in_the_middle(
            store, entry, _delete_the_first)

        self.assertIsNone(store.get("acg-1"), "stays deleted, not resurrected")
        self.assertEqual(store.get("acg-2").room_id, "room-dev",
                         "the other job was still migrated")
        # Reported, but NOT attention-worthy: the job is gone, so there is
        # nothing to fix and nothing to hold the schema version back for.
        deleted = [o for o in report.outcomes if "deleted while" in o.detail]
        self.assertEqual(len(deleted), 1, report.outcomes)
        self.assertFalse(deleted[0].needs_attention)
        self.assertEqual(report.unresolved, [])


class TestFinalIsSaidDifferentlyFromRetryable(_MigrateCase):
    """The operator's next move depends on which it was: delete the job, or run
    the command again. `Connector.room_ref_by_id`'s docstring makes the same
    distinction load-bearing, and this module used to collapse both into "the
    connector could not resolve X" (review).

    It matters more now that the schema version is not stamped while anything
    needs attention: a permanently unresolvable job pins the file at version 1,
    so the startup warning never clears until it is dealt with.
    """

    def _entry_raising(self, exc):
        entry = _entry()
        entry.connector.resolve_room = AsyncMock(side_effect=exc)
        return entry

    async def test_a_room_that_does_not_exist_says_delete_the_job(self):
        from gateway.connectors.rocketchat.rest import RoomNotFoundError

        self._write_file(1, [self._job()])
        store = self._store()

        report = await migrate(store, [self._entry_raising(RoomNotFoundError("no"))])

        detail = report.unresolved[0].detail
        self.assertIn("cannot be migrated", detail)
        # The exact advice, not the substring "again" — that also appears inside
        # "recreate it AGAINst a current watcher", which is how a loose
        # assertion passes for the wrong reason.
        self.assertNotIn("schedule migrate' again", detail)

    async def test_mattermosts_own_class_is_recognised_too(self):
        """There are TWO unrelated `RoomNotFoundError` classes. Importing either
        would have treated the other platform's final answer as retryable."""
        from gateway.connectors.mattermost.rest import RoomNotFoundError

        self._write_file(1, [self._job()])
        store = self._store()

        report = await migrate(store, [self._entry_raising(RoomNotFoundError("no"))])

        self.assertIn("cannot be migrated", report.unresolved[0].detail)

    async def test_a_transport_failure_says_run_it_again(self):
        self._write_file(1, [self._job()])
        store = self._store()

        report = await migrate(store, [self._entry_raising(OSError("network"))])

        detail = report.unresolved[0].detail
        self.assertIn("run 'schedule migrate' again", detail)
        self.assertNotIn("cannot be migrated", detail)

    async def test_either_way_the_version_stays_put(self):
        self._write_file(1, [self._job()])
        store = self._store()

        await migrate(store, [self._entry_raising(OSError("network"))])

        self.assertEqual(json.loads(self.path.read_text())["version"], 1)
