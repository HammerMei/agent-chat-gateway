"""Unit tests for gateway.core.job_store.JobStore."""

from __future__ import annotations

import json
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
        """Entries with an invalid 'status' value raise inside from_dict and are skipped."""
        data = {
            "version": 1,
            "jobs": [
                {"watcher": "ok-watcher", "message": "ok", "cron": "0 9 * * *", "times": 0,
                 "status": "active"},
                {"watcher": "bad", "status": "NOT_A_VALID_STATUS"},  # ValueError in JobStatus()
            ],
        }
        self.jobs_file.write_text(json.dumps(data))
        store = self._store()
        store.load()
        jobs = store.list_jobs(include_completed=True)
        # Only the valid entry should be loaded; the bad-status entry is skipped
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].watcher, "ok-watcher")

    def test_load_does_not_crash_on_unknown_fields(self):
        """Extra unknown fields in a job dict are silently ignored."""
        data = {
            "version": 1,
            "jobs": [
                {"watcher": "w", "message": "m", "cron": "0 9 * * *",
                 "status": "active", "UNKNOWN_FUTURE_FIELD": "value"},
            ],
        }
        self.jobs_file.write_text(json.dumps(data))
        store = self._store()
        store.load()
        jobs = store.list_jobs()
        self.assertEqual(len(jobs), 1)


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

    def test_malformed_completed_at_is_purged(self):
        """A completed job with an unparseable completed_at should be purged defensively."""
        job = self.store.add(_make_job(
            status=JobStatus.COMPLETED,
            completed_at="NOT-A-TIMESTAMP",
        ))
        purged = self.store.remove_expired_completed(ttl_days=7)
        self.assertEqual(purged, 1)
        self.assertIsNone(self.store.get(job.id))


class TestJobStoreLoadGuard(unittest.TestCase):
    """All public methods raise RuntimeError if load() has not been called."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(jobs_file=Path(self.tmp.name) / "jobs.json")
        # deliberately do NOT call self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_before_load_raises(self):
        with self.assertRaises(RuntimeError, msg="add() must require prior load()"):
            self.store.add(_make_job())

    def test_update_before_load_raises(self):
        with self.assertRaises(RuntimeError):
            self.store.update(_make_job())

    def test_remove_before_load_raises(self):
        with self.assertRaises(RuntimeError):
            self.store.remove("acg-00000000")

    def test_get_before_load_raises(self):
        with self.assertRaises(RuntimeError):
            self.store.get("acg-00000000")

    def test_list_jobs_before_load_raises(self):
        with self.assertRaises(RuntimeError):
            self.store.list_jobs()

    def test_list_due_before_load_raises(self):
        with self.assertRaises(RuntimeError):
            self.store.list_due()

    def test_remove_expired_before_load_raises(self):
        with self.assertRaises(RuntimeError):
            self.store.remove_expired_completed(ttl_days=7)

    def test_after_load_no_error(self):
        self.store.load()
        job = self.store.add(_make_job())
        self.assertIsNotNone(self.store.get(job.id))


class TestJobStoreConcurrency(unittest.TestCase):
    """Thread-safety smoke tests for the JobStore lock.

    These tests verify that concurrent add/list_jobs and
    remove_expired_completed/add operations do not raise
    'RuntimeError: dictionary changed size during iteration'.
    """

    def setUp(self):
        # ignore_cleanup_errors=True: Python 3.13 on macOS raises OSError when
        # cleanup runs while a background thread is still writing a temp file
        # inside the directory.  The flag was added in Python 3.10.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = JobStore(jobs_file=Path(self._tmp.name) / "jobs.json")
        self.store.load()

    def tearDown(self):
        self._tmp.cleanup()

    def test_concurrent_add_and_list_jobs_no_error(self):
        """Concurrent add() from one thread and list_jobs() from another must not raise."""
        import threading

        errors: list[Exception] = []
        stop = threading.Event()

        def _reader():
            while not stop.is_set():
                try:
                    self.store.list_jobs(include_completed=True)
                except Exception as e:
                    errors.append(e)
                    break

        def _writer():
            for _ in range(50):
                try:
                    job = self.store.add(_make_job())
                    self.store.remove(job.id)
                except Exception as e:
                    errors.append(e)
                    break
            stop.set()

        reader = threading.Thread(target=_reader, daemon=True)
        writer = threading.Thread(target=_writer)
        reader.start()
        writer.start()
        writer.join(timeout=10)
        stop.set()
        reader.join(timeout=5)

        self.assertEqual(errors, [], f"Concurrent access raised: {errors}")

    def test_concurrent_remove_expired_and_add_no_error(self):
        """remove_expired_completed() running concurrently with add() must not raise."""
        import threading
        from datetime import UTC, datetime, timedelta

        errors: list[Exception] = []

        # Pre-populate with completed jobs for the purge to iterate over
        now = datetime.now(UTC)
        for _ in range(20):
            self.store.add(_make_job(
                status=JobStatus.COMPLETED,
                completed_at=(now - timedelta(days=10)).isoformat(),
                next_run=None,
            ))

        stop = threading.Event()

        def _purger():
            while not stop.is_set():
                try:
                    self.store.remove_expired_completed(ttl_days=7)
                except Exception as e:
                    errors.append(e)
                    break

        def _adder():
            for _ in range(50):
                try:
                    job = self.store.add(_make_job())
                    self.store.remove(job.id)
                except Exception as e:
                    errors.append(e)
                    break
            stop.set()

        purger = threading.Thread(target=_purger, daemon=True)
        adder = threading.Thread(target=_adder)
        purger.start()
        adder.start()
        adder.join(timeout=10)
        stop.set()
        purger.join(timeout=5)

        self.assertEqual(errors, [], f"Concurrent access raised: {errors}")


if __name__ == "__main__":
    unittest.main()
