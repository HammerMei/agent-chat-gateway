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

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from gateway.core.job_store import JobStore
from gateway.schedule_types import JobStatus, ScheduledJob

# A value for every field that is distinguishable from the dataclass default, so
# "survived" cannot be satisfied by the default happening to match.
FULLY_POPULATED = dict(
    id="acg-deadbeef",
    watcher="rc:general",
    connector="rc",
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


if __name__ == "__main__":
    unittest.main()
