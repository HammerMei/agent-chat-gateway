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
        """Every save writes the code's version into the file, so an unmigrated
        store would otherwise mark itself current the first time a job fired."""
        self._write_file(1, [self._job()])
        store = self._store()

        store.update(ScheduledJob.from_dict(self._job()))  # an ordinary save

        self.assertEqual(store.file_version, 1, "the in-memory version moved")
        self.assertTrue(store.needs_migration())


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
