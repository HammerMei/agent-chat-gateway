"""Every declared field on a `ScheduledJob` survives a real store round-trip.

Written because one did not, and nothing noticed. `room_id` was added to the
dataclass and to the create path but not to `to_dict`/`from_dict`, so it was
dropped on every save and came back `""` at the next daemon start. The feature
that field existed for was a no-op after a restart, and a safety net had already
been removed on the strength of it.

**This walks the declared fields rather than listing them**, which is the whole
point: a test that enumerates field names by hand has to be updated by the same
person who forgot the serializer, and would have been forgotten in the same
breath. `dataclasses.fields()` cannot be forgotten.

Round-tripped through a REAL `JobStore` against a real file, not through
`to_dict`/`from_dict` alone: the gap was between the dataclass and the
persistence layer, so the assertion has to cross it.

Run with:
    uv run python -m pytest tests/unit/test_job_store_roundtrip.py -v
"""

from __future__ import annotations

import copy
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from gateway.core.job_store import JobStore
from gateway.schedule_types import (
    CANCELLATION_OWNED_FIELDS,
    CREATION_OWNED_FIELDS,
    FIRE_OWNED_FIELDS,
    MIGRATION_OWNED_FIELDS,
    JobStatus,
    ScheduledJob,
)

# A value for every field that is distinguishable from the dataclass default, so
# "survived" cannot be satisfied by the default happening to match.
FULLY_POPULATED = dict(
    id="acg-deadbeef",
    watcher="rc:general",
    connector="rc",
    room_id="room-abc123",
    message="the scheduled message",
    cron="0 9 * * 1-5",
    timezone="Asia/Taipei",
    times=7,
    run_count=3,
    status=JobStatus.PAUSED,
    created_at="2026-01-01T00:00:00+00:00",
    next_run="2026-01-02T09:00:00+00:00",
    last_run="2026-01-01T09:00:00+00:00",
    last_attempted_at="2026-01-01T09:00:01+00:00",
    completed_at="2026-01-03T09:00:00+00:00",
    cancelled_at="2026-01-04T09:00:00+00:00",
    cancel_reason="the bot was removed from the room",
)


class TestEveryFieldSurvivesTheStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "jobs.json"

    def _store(self) -> JobStore:
        store = JobStore(self.path)
        store.load()
        return store

    def test_the_fixture_covers_every_declared_field(self):
        """Guard on the guard. A field added to the dataclass and not to
        `FULLY_POPULATED` would make the round-trip assertion silently skip it —
        the same shape of omission this file exists to catch."""
        declared = {f.name for f in dataclasses.fields(ScheduledJob)}
        self.assertEqual(
            declared - set(FULLY_POPULATED), set(),
            "add the new field to FULLY_POPULATED (and to to_dict/from_dict)",
        )

    def test_no_field_is_left_at_its_default(self):
        """So a dropped field cannot pass by coinciding with the default."""
        job = ScheduledJob(**FULLY_POPULATED)
        for field in dataclasses.fields(ScheduledJob):
            if field.default is dataclasses.MISSING:
                continue
            with self.subTest(field=field.name):
                self.assertNotEqual(
                    getattr(job, field.name), field.default,
                    f"{field.name}'s fixture value equals its default — the "
                    f"round-trip assertion cannot detect it being dropped",
                )

    def test_every_field_survives_save_and_reload(self):
        original = ScheduledJob(**FULLY_POPULATED)
        store = self._store()
        store.add(original)

        reloaded_store = self._store()
        reloaded = reloaded_store.get(original.id)

        self.assertIsNotNone(reloaded, "the job did not survive at all")
        for field in dataclasses.fields(ScheduledJob):
            with self.subTest(field=field.name):
                self.assertEqual(
                    getattr(reloaded, field.name), getattr(original, field.name),
                    f"{field.name} did not survive the round trip",
                )

    def test_every_field_is_actually_written_to_the_file(self):
        """Separate from the reload, because a field that round-trips through a
        cached in-memory dict without reaching disk would pass the test above on
        a store that never reloaded."""
        store = self._store()
        store.add(ScheduledJob(**FULLY_POPULATED))

        on_disk = json.loads(self.path.read_text())
        entries = on_disk["jobs"] if isinstance(on_disk, dict) else on_disk
        (entry,) = [j for j in entries if j.get("id") == "acg-deadbeef"]

        declared = {f.name for f in dataclasses.fields(ScheduledJob)}
        self.assertEqual(
            declared - set(entry), set(),
            "these declared fields never reach jobs.json",
        )

    def test_an_update_persists_a_changed_field(self):
        """The path a lazy migration would use: mutate one field, `update`, and
        expect it on disk. A serializer that omits the field fails here even
        though `add` looked fine."""
        job = ScheduledJob(**FULLY_POPULATED)
        store = self._store()
        store.add(job)

        job.message = "changed after creation"
        store.update(job)

        reloaded = self._store().get(job.id)
        self.assertEqual(reloaded.message, "changed after creation")


class TestEveryFieldHasExactlyOneOwner(unittest.TestCase):
    """The net for the write-collision class, in the same shape as the
    round-trip net above: it walks the declared fields, so it cannot be
    forgotten by whoever forgets to think about ownership.

    Three writers touch a persisted job — a fire, `schedule migrate`, and
    `schedule pause`/`resume`. All of them used `update`, which replaces the
    stored object wholesale, and that is safe only while there is ONE. `room_id`
    made a second, and a fire holding `copy.copy(job)` across its inject await
    discarded the migration's write — after the migration had reported success
    and stamped the version, so the job became permanently unmigratable.

    A new field that picks no owner silently joins whichever writer replaces
    last. That is what these two assertions make impossible.
    """

    def test_the_sets_partition_every_declared_field(self):
        declared = {f.name for f in dataclasses.fields(ScheduledJob)}
        union = (FIRE_OWNED_FIELDS | MIGRATION_OWNED_FIELDS | CREATION_OWNED_FIELDS
                 | CANCELLATION_OWNED_FIELDS)

        self.assertEqual(
            declared - union, set(),
            "a new ScheduledJob field must declare who writes it: add it to "
            "FIRE_OWNED_FIELDS, MIGRATION_OWNED_FIELDS, CREATION_OWNED_FIELDS or "
            "CANCELLATION_OWNED_FIELDS "
            "in gateway/schedule_types.py",
        )
        self.assertEqual(
            union - declared, set(),
            "an ownership set names a field the dataclass no longer has",
        )

    def test_no_field_is_claimed_by_two_writers(self):
        """`status` and `next_run` are the interesting case: a fire writes them
        on a transition it made, and the operator writes them through
        `schedule pause`/`resume`. They belong to the FIRE set, and the operator
        writes the live object rather than a copy — so the contest is real but
        one-directional, and it is named in `schedule_types.py` rather than
        hidden behind an overlap here."""
        pairs = [
            ("fire", FIRE_OWNED_FIELDS, "migration", MIGRATION_OWNED_FIELDS),
            ("fire", FIRE_OWNED_FIELDS, "creation", CREATION_OWNED_FIELDS),
            ("migration", MIGRATION_OWNED_FIELDS, "creation", CREATION_OWNED_FIELDS),
            ("cancellation", CANCELLATION_OWNED_FIELDS, "fire", FIRE_OWNED_FIELDS),
            ("cancellation", CANCELLATION_OWNED_FIELDS, "migration", MIGRATION_OWNED_FIELDS),
            ("cancellation", CANCELLATION_OWNED_FIELDS, "creation", CREATION_OWNED_FIELDS),
        ]
        for a_name, a, b_name, b in pairs:
            with self.subTest(pair=f"{a_name}/{b_name}"):
                self.assertEqual(a & b, frozenset(),
                                 f"{a_name} and {b_name} both claim these")

    def test_the_migration_never_claims_a_field_a_fire_writes(self):
        """The specific direction that produced the defect: if `room_id` were in
        the fire's set, the fire's pre-await copy would legitimately write it
        back as `""`."""
        self.assertIn("room_id", MIGRATION_OWNED_FIELDS)
        self.assertNotIn("room_id", FIRE_OWNED_FIELDS)
        self.assertNotIn("connector", FIRE_OWNED_FIELDS)


class TestWriteFieldsIsWhatAFireUses(unittest.TestCase):
    """Reproduces the real fire's semantics, which the previous concurrency
    tests did not.

    `tests/unit/test_job_migrate.py` simulated a fire as
    `j = store.get(id); j.run_count = 1; store.update(j)` — a RE-READ of the
    live object, which cannot hold a stale copy and so cannot lose anything.
    `core/scheduler.py::_fire_once` does `copy.copy(job)` on entry and writes it
    back after the inject await. That is the difference between a green suite and
    a green suite over a live defect.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "jobs.json"
        job = ScheduledJob(**{**FULLY_POPULATED, "room_id": ""})
        self.path.write_text(json.dumps({"version": 1, "jobs": [job.to_dict()]}))
        self.store = JobStore(self.path)
        self.store.load()

    def _fire_copy(self) -> ScheduledJob:
        """What `_fire_once` holds across its await (`scheduler.py:343`)."""
        return copy.copy(self.store.get("acg-deadbeef"))

    def _on_disk(self) -> dict:
        return json.loads(self.path.read_text())["jobs"][0]

    def test_a_fires_write_back_does_not_erase_a_room_id_written_during_it(self):
        fire = self._fire_copy()
        self.assertEqual(fire.room_id, "", "the copy predates the migration")

        self.store.set_room_id("acg-deadbeef", "room-migrated")
        fire.run_count = 7
        self.store.write_fields(fire, FIRE_OWNED_FIELDS)

        self.assertEqual(self.store.get("acg-deadbeef").room_id, "room-migrated")
        self.assertEqual(self._on_disk()["room_id"], "room-migrated")
        self.assertEqual(self.store.get("acg-deadbeef").run_count, 7,
                         "and the fire's own progress still landed")

    def test_update_is_what_lost_it(self):
        """The counterfactual, so the fix above is not mistaken for a no-op."""
        fire = self._fire_copy()
        self.store.set_room_id("acg-deadbeef", "room-migrated")
        fire.run_count = 7
        self.store.update(fire)

        self.assertEqual(self.store.get("acg-deadbeef").room_id, "",
                         "if this ever passes, `update` became safe and this "
                         "whole ownership scheme can be revisited")

    def test_it_writes_nothing_else(self):
        """A targeted write must be targeted: a fire that mangled a field it does
        not own would be exactly as bad in the other direction."""
        fire = self._fire_copy()
        fire.message = "the fire has no business changing this"
        fire.cron = "* * * * *"
        fire.run_count = 1

        self.store.write_fields(fire, FIRE_OWNED_FIELDS)

        stored = self.store.get("acg-deadbeef")
        self.assertEqual(stored.message, FULLY_POPULATED["message"])
        self.assertEqual(stored.cron, FULLY_POPULATED["cron"])
        self.assertEqual(stored.run_count, 1)

    def test_a_job_deleted_during_the_fire_is_not_resurrected(self):
        fire = self._fire_copy()
        self.store.remove("acg-deadbeef")
        fire.run_count = 1

        self.assertFalse(self.store.write_fields(fire, FIRE_OWNED_FIELDS))
        self.assertIsNone(self.store.get("acg-deadbeef"))

    def test_it_does_not_stamp_the_schema_version(self):
        self.store.write_fields(self._fire_copy(), FIRE_OWNED_FIELDS)
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)


class TestSetRoomId(unittest.TestCase):
    """`set_room_id` exists so the migration can write two fields without
    replacing the whole job. It had no direct test — found by mutation testing:
    changing `if connector and not job.connector` to `if connector` survived the
    entire 3969-test suite.

    Its contract is three claims, and each gets a case here rather than being
    exercised incidentally through the migration:

    1. it writes `room_id`, and persists;
    2. it fills `connector` in ONLY when the job has none — an operator's value
       is not overwritten by a handle-derived guess;
    3. it returns `False`, rather than raising, for a job deleted mid-run.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "jobs.json"

    def _store_with(self, **overrides) -> JobStore:
        job = ScheduledJob(**{**FULLY_POPULATED, "room_id": "", **overrides})
        self.path.write_text(json.dumps({"version": 1, "jobs": [job.to_dict()]}))
        store = JobStore(self.path)
        store.load()
        return store

    def _on_disk(self) -> dict:
        return json.loads(self.path.read_text())["jobs"][0]

    def test_it_writes_the_room_id_and_persists_it(self):
        store = self._store_with()

        self.assertTrue(store.set_room_id("acg-deadbeef", "room-new"))

        self.assertEqual(store.get("acg-deadbeef").room_id, "room-new")
        self.assertEqual(self._on_disk()["room_id"], "room-new",
                         "an in-memory-only write is lost at the next restart")

    def test_an_existing_connector_is_not_overwritten(self):
        """The migration falls back to the connector name derived from the
        watcher HANDLE when the job's own value names nothing configured. If that
        fallback then overwrote the operator's value, a command whose entire
        product is a report of what it changed would silently rebind the job to a
        different connector and not mention it."""
        store = self._store_with(connector="mm")

        store.set_room_id("acg-deadbeef", "room-new", connector="rc")

        self.assertEqual(store.get("acg-deadbeef").connector, "mm")
        self.assertEqual(self._on_disk()["connector"], "mm")

    def test_an_empty_connector_is_filled_in(self):
        """The other direction: a job with neither field cannot be routed to a
        session manager at all, so the migration supplies one."""
        store = self._store_with(connector="")

        store.set_room_id("acg-deadbeef", "room-new", connector="rc")

        self.assertEqual(store.get("acg-deadbeef").connector, "rc")

    def test_no_connector_argument_leaves_the_field_alone(self):
        store = self._store_with(connector="")

        store.set_room_id("acg-deadbeef", "room-new")

        self.assertEqual(store.get("acg-deadbeef").connector, "")

    def test_a_job_that_is_gone_returns_false_instead_of_raising(self):
        """`update` raises `KeyError` here, which aborts the whole migration and
        loses the report for every job already done."""
        store = self._store_with()
        store.remove("acg-deadbeef")

        self.assertFalse(store.set_room_id("acg-deadbeef", "room-new"))
        self.assertIsNone(store.get("acg-deadbeef"), "and stays gone")

    def test_it_does_not_stamp_the_schema_version(self):
        """It saves, and a save must not claim a migration that has not finished
        — the version moves only in `stamp_version`."""
        store = self._store_with()

        store.set_room_id("acg-deadbeef", "room-new")

        self.assertEqual(self._on_disk_version(), 1)

    def _on_disk_version(self) -> int:
        return json.loads(self.path.read_text())["version"]


if __name__ == "__main__":
    unittest.main()


class TestASaveIsOneOperation(unittest.TestCase):
    """A fire saves from a `to_thread` worker; the migration saves on the loop
    thread. `_lock` made each snapshot consistent but not the save: a worker
    that had snapshotted the jobs and then lost the CPU while the migration
    wrote every `room_id` and stamped the version read the NEW version and
    replaced the file with the OLD snapshot — a version-2 file whose job had
    no room (Codex, PR #140 round 3). Whole saves now exclude each other."""

    def test_a_save_that_started_first_finishes_before_the_migration_writes(self):
        import json as _json
        import threading
        from unittest.mock import patch

        path = Path(tempfile.mkdtemp()) / "jobs.json"
        store = JobStore(path)
        store.load()
        store.add(ScheduledJob(**{**FULLY_POPULATED, "room_id": ""}))
        store._file_version = 1

        snapshotted, release = threading.Event(), threading.Event()
        real_dumps = _json.dumps
        first = [True]

        def slow_dumps(data, **kw):
            # The first save (the "fire") has taken its snapshot and now stalls
            # between snapshot and write — the window the race lives in.
            if first[0]:
                first[0] = False
                snapshotted.set()
                assert release.wait(5), "test deadlocked"
            return real_dumps(data, **kw)

        migrated = threading.Event()

        def migrate():
            assert store.set_room_id(FULLY_POPULATED["id"], "R-1", connector="rc")
            store.stamp_version(2)
            migrated.set()

        with patch("gateway.core.job_store.json.dumps", side_effect=slow_dumps):
            fire = threading.Thread(target=store.save)
            fire.start()
            assert snapshotted.wait(5)
            mig = threading.Thread(target=migrate)
            mig.start()
            try:
                self.assertFalse(migrated.wait(0.3), "the migration wrote while a save was mid-flight")
            finally:
                release.set()   # never leave the stalled save hanging, pass or fail
                fire.join(5)
                mig.join(5)

        on_disk = _json.loads(path.read_text())
        self.assertEqual(on_disk["version"], 2)
        self.assertEqual(on_disk["jobs"][0]["room_id"], "R-1",
                         "a version-2 file must contain the migration it claims")


class TestACancelledJobIsKeptNotRemoved(unittest.TestCase):
    """Owner, 2026-09-02: the gateway's own cancellations used to `remove`,
    leaving one log line as the only trace of a job that may have been killed by
    mistake. The record now stays — marked, dated, with the reason — for the
    same TTL as a completed job, hidden from `list` by default, restorable."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "jobs.json"
        self.store = JobStore(self.path)
        self.store.load()
        # An ACTIVE job as a fire would find it: no completion, no cancellation.
        # `FULLY_POPULATED` carries an old `completed_at`, which would let a
        # purge keyed on the WRONG field pass (internal review).
        self.store.add(ScheduledJob(**{**FULLY_POPULATED, "status": JobStatus.ACTIVE,
                                       "completed_at": None,
                                       "cancelled_at": None, "cancel_reason": ""}))
        self.job_id = FULLY_POPULATED["id"]

    def test_cancel_marks_dates_and_explains(self):
        self.assertTrue(self.store.cancel(self.job_id, reason="the bot was removed"))

        on_disk = json.loads(self.path.read_text())["jobs"][0]
        self.assertEqual(on_disk["status"], "cancelled")
        self.assertTrue(on_disk["cancelled_at"])
        self.assertEqual(on_disk["cancel_reason"], "the bot was removed")

    def test_a_cancelled_job_is_hidden_by_default_and_never_due(self):
        self.store.cancel(self.job_id, reason="r")

        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual([j.id for j in self.store.list_jobs(include_completed=True)], [self.job_id])
        self.assertEqual(self.store.list_due(), [], "a cancelled job must not fire")

    def test_a_fire_in_flight_cannot_resurrect_a_cancelled_job(self):
        """The fire copied the job as ACTIVE before its await; the cancellation
        landed during it. The fire's write-back is refused, like a deletion's."""
        fire = copy.copy(self.store.get(self.job_id))
        self.store.cancel(self.job_id, reason="r")
        fire.run_count += 1

        self.assertFalse(self.store.write_fields(fire, FIRE_OWNED_FIELDS))
        self.assertEqual(self.store.get(self.job_id).status, JobStatus.CANCELLED)
        self.assertEqual(self.store.get(self.job_id).run_count, FULLY_POPULATED["run_count"])

    def test_it_is_purged_after_the_ttl_from_its_cancellation(self):
        self.store.cancel(self.job_id, reason="r")
        self.store.get(self.job_id).cancelled_at = "2020-01-01T00:00:00+00:00"

        self.assertEqual(self.store.remove_expired_completed(7), 1)
        self.assertIsNone(self.store.get(self.job_id))

    def test_a_fresh_cancellation_survives_the_purge(self):
        self.store.cancel(self.job_id, reason="r")

        self.assertEqual(self.store.remove_expired_completed(7), 0)

    def test_the_purge_ages_a_cancelled_job_from_its_cancellation_not_its_completion(self):
        self.store.cancel(self.job_id, reason="r")
        job = self.store.get(self.job_id)
        job.completed_at = "2020-01-01T00:00:00+00:00"   # stale, and not the field that counts

        self.assertEqual(self.store.remove_expired_completed(7), 0)

    def test_a_second_cancellation_keeps_the_first_evidence(self):
        self.store.cancel(self.job_id, reason="first")
        first_at = self.store.get(self.job_id).cancelled_at

        self.assertTrue(self.store.cancel(self.job_id, reason="second"))
        self.assertEqual(self.store.get(self.job_id).cancel_reason, "first")
        self.assertEqual(self.store.get(self.job_id).cancelled_at, first_at)

    def test_cancelling_a_gone_job_is_false_not_an_error(self):
        self.assertFalse(self.store.cancel("acg-nope", reason="r"))

