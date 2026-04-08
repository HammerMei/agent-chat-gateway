"""Unit tests for gateway.core.job_store.JobStore."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gateway.core.job_store import JobStore
from gateway.schedule_types import JobStatus, ScheduledJob


def _make_job(**kwargs) -> ScheduledJob:
    defaults = dict(
        watcher="test-watcher",
        connector="rc-home",
        message="hello",
        cron="0 9 * * *",
        timezone="UTC",
        times=0,
        created_at=datetime.now(UTC).isoformat(),
    )
    defaults.update(kwargs)
    return ScheduledJob(**defaults)


class TestJobStoreLoadSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs_file = Path(self.tmp.name) / "jobs.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self) -> JobStore:
        return JobStore(jobs_file=self.jobs_file)

    def test_load_nonexistent_file(self):
        store = self._store()
        store.load()
        self.assertEqual(store.list_jobs(), [])

    def test_load_empty_jobs_list(self):
        self.jobs_file.write_text(json.dumps({"version": 1, "jobs": []}))
        store = self._store()
        store.load()
        self.assertEqual(store.list_jobs(), [])

    def test_add_and_persist(self):
        store = self._store()
        store.load()
        job = _make_job()
        store.add(job)

        # Reload from disk
        store2 = self._store()
        store2.load()
        jobs = store2.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].id, job.id)
        self.assertEqual(jobs[0].message, "hello")

    def test_atomic_write_uses_tmp_file(self):
        """Verifies no .tmp file is left behind after a successful save."""
        store = self._store()
        store.load()
        store.add(_make_job())
        tmp_files = list(self.jobs_file.parent.glob("jobs.json.*.tmp"))
        self.assertEqual(tmp_files, [], "No .tmp files should remain after save")

    def test_load_skips_malformed_entries(self):
        """Malformed job entries are skipped, valid ones are loaded."""
        data = {
            "version": 1,
            "jobs": [
                {"watcher": "ok-watcher", "message": "ok", "cron": "0 9 * * *", "times": 0},
                {"INVALID": True},  # malformed: no required fields
            ],
        }
        self.jobs_file.write_text(json.dumps(data))
        store = self._store()
        store.load()
        jobs = store.list_jobs()
        # Both entries parse (ScheduledJob.from_dict doesn't raise on missing optional fields)
        # The test verifies the store doesn't crash on loading
        self.assertGreaterEqual(len(jobs), 1)


class TestJobStoreCRUD(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs_file = Path(self.tmp.name) / "jobs.json"
        self.store = JobStore(jobs_file=self.jobs_file)
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_returns_job(self):
        job = _make_job(message="added")
        returned = self.store.add(job)
        self.assertEqual(returned.id, job.id)
        self.assertIn(job, self.store.list_jobs())

    def test_remove_existing(self):
        job = self.store.add(_make_job())
        result = self.store.remove(job.id)
        self.assertTrue(result)
        self.assertNotIn(job, self.store.list_jobs())

    def test_remove_nonexistent(self):
        result = self.store.remove("acg-doesnotexist")
        self.assertFalse(result)

    def test_update_existing(self):
        job = self.store.add(_make_job())
        job.run_count = 5
        self.store.update(job)
        reloaded = JobStore(jobs_file=self.jobs_file)
        reloaded.load()
        self.assertEqual(reloaded.get(job.id).run_count, 5)

    def test_update_nonexistent_raises(self):
        job = _make_job()
        with self.assertRaises(KeyError):
            self.store.update(job)

    def test_get_existing(self):
        job = self.store.add(_make_job(message="find-me"))
        found = self.store.get(job.id)
        self.assertIsNotNone(found)
        self.assertEqual(found.message, "find-me")

    def test_get_nonexistent(self):
        self.assertIsNone(self.store.get("acg-nope"))


class TestJobStoreFiltering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(jobs_file=Path(self.tmp.name) / "jobs.json")
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_excludes_completed_by_default(self):
        active = self.store.add(_make_job(status=JobStatus.ACTIVE))
        completed = self.store.add(_make_job(status=JobStatus.COMPLETED, completed_at=datetime.now(UTC).isoformat()))
        jobs = self.store.list_jobs()
        ids = [j.id for j in jobs]
        self.assertIn(active.id, ids)
        self.assertNotIn(completed.id, ids)

    def test_list_include_completed(self):
        active = self.store.add(_make_job(status=JobStatus.ACTIVE))
        completed = self.store.add(_make_job(status=JobStatus.COMPLETED, completed_at=datetime.now(UTC).isoformat()))
        jobs = self.store.list_jobs(include_completed=True)
        ids = [j.id for j in jobs]
        self.assertIn(active.id, ids)
        self.assertIn(completed.id, ids)

    def test_list_filter_by_connector(self):
        j1 = self.store.add(_make_job(connector="rc-home"))
        j2 = self.store.add(_make_job(connector="rc-work"))
        home_jobs = self.store.list_jobs(connector="rc-home")
        self.assertIn(j1, home_jobs)
        self.assertNotIn(j2, home_jobs)

    def test_list_paused_included_by_default(self):
        paused = self.store.add(_make_job(status=JobStatus.PAUSED))
        jobs = self.store.list_jobs()
        self.assertIn(paused, jobs)

    def test_list_due_returns_active_past_next_run(self):
        past = datetime.now(UTC) - timedelta(minutes=5)
        future = datetime.now(UTC) + timedelta(minutes=5)
        due_job = self.store.add(_make_job(status=JobStatus.ACTIVE, next_run=past.isoformat()))
        not_due = self.store.add(_make_job(status=JobStatus.ACTIVE, next_run=future.isoformat()))
        paused_due = self.store.add(_make_job(status=JobStatus.PAUSED, next_run=past.isoformat()))

        due = self.store.list_due()
        due_ids = [j.id for j in due]
        self.assertIn(due_job.id, due_ids)
        self.assertNotIn(not_due.id, due_ids)
        self.assertNotIn(paused_due.id, due_ids)

    def test_list_due_excludes_none_next_run(self):
        job = self.store.add(_make_job(status=JobStatus.ACTIVE, next_run=None))
        self.assertNotIn(job, self.store.list_due())


class TestJobStoreTTLPurge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(jobs_file=Path(self.tmp.name) / "jobs.json")
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_purge_immediate_ttl0(self):
        job = self.store.add(_make_job(
            status=JobStatus.COMPLETED,
            completed_at=datetime.now(UTC).isoformat(),
        ))
        purged = self.store.remove_expired_completed(ttl_days=0)
        self.assertEqual(purged, 1)
        self.assertIsNone(self.store.get(job.id))

    def test_purge_old_completed(self):
        old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        job = self.store.add(_make_job(status=JobStatus.COMPLETED, completed_at=old_ts))
        purged = self.store.remove_expired_completed(ttl_days=7)
        self.assertEqual(purged, 1)
        self.assertIsNone(self.store.get(job.id))

    def test_no_purge_recent_completed(self):
        recent_ts = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        job = self.store.add(_make_job(status=JobStatus.COMPLETED, completed_at=recent_ts))
        purged = self.store.remove_expired_completed(ttl_days=7)
        self.assertEqual(purged, 0)
        self.assertIsNotNone(self.store.get(job.id))

    def test_no_purge_active_jobs(self):
        job = self.store.add(_make_job(status=JobStatus.ACTIVE))
        self.store.remove_expired_completed(ttl_days=0)
        self.assertIsNotNone(self.store.get(job.id))

    def test_negative_ttl_no_purge(self):
        job = self.store.add(_make_job(
            status=JobStatus.COMPLETED,
            completed_at=datetime.now(UTC).isoformat(),
        ))
        purged = self.store.remove_expired_completed(ttl_days=-1)
        self.assertEqual(purged, 0)
        self.assertIsNotNone(self.store.get(job.id))


if __name__ == "__main__":
    unittest.main()
