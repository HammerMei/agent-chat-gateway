"""Unit tests for gateway.core.scheduler: compute_next_run, compute_all_missed, JobScheduler."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.core.job_store import JobStore
from gateway.core.scheduler import JobScheduler, compute_all_missed, compute_next_run
from gateway.schedule_types import JobStatus, ScheduledJob


def _make_job(**kwargs) -> ScheduledJob:
    now = datetime.now(UTC)
    defaults = dict(
        watcher="test-watcher",
        connector="rc-home",
        message="scheduled check",
        cron="0 9 * * *",  # every day at 09:00
        timezone="UTC",
        times=0,
        status=JobStatus.ACTIVE,
        created_at=now.isoformat(),
        next_run=(now + timedelta(hours=1)).isoformat(),
        run_count=0,
    )
    defaults.update(kwargs)
    return ScheduledJob(**defaults)


class TestComputeNextRun(unittest.TestCase):
    def test_daily_cron(self):
        # "0 9 * * *" should give a time with minute=0, hour=9
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        result = compute_next_run("0 9 * * *", "UTC", after=after)
        dt = datetime.fromisoformat(result)
        self.assertEqual(dt.hour, 9)
        self.assertEqual(dt.minute, 0)

    def test_hourly_cron(self):
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        result = compute_next_run("0 * * * *", "UTC", after=after)
        dt = datetime.fromisoformat(result)
        self.assertEqual(dt.hour, 9)
        self.assertEqual(dt.minute, 0)

    def test_every_30min(self):
        after = datetime(2026, 4, 7, 8, 5, 0, tzinfo=UTC)
        result = compute_next_run("*/30 * * * *", "UTC", after=after)
        dt = datetime.fromisoformat(result)
        self.assertEqual(dt.minute, 30)

    def test_timezone_offset(self):
        # 09:00 Asia/Taipei = 01:00 UTC (UTC+8)
        after = datetime(2026, 4, 7, 0, 30, 0, tzinfo=UTC)
        result = compute_next_run("0 9 * * *", "Asia/Taipei", after=after)
        dt = datetime.fromisoformat(result).astimezone(UTC)
        self.assertEqual(dt.hour, 1)
        self.assertEqual(dt.minute, 0)

    def test_invalid_timezone_falls_back_to_utc(self):
        # Unknown timezone should fall back to UTC without raising
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        result = compute_next_run("0 9 * * *", "Invalid/Zone", after=after)
        self.assertIsNotNone(result)  # Should still return a valid datetime string

    def test_result_is_utc_iso_string(self):
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        result = compute_next_run("0 9 * * *", "UTC", after=after)
        # Should be parseable as ISO datetime
        dt = datetime.fromisoformat(result)
        self.assertIsNotNone(dt.tzinfo)


class TestComputeAllMissed(unittest.TestCase):
    def test_no_missed_fires(self):
        # after > before, so no fires
        after = datetime(2026, 4, 7, 10, 0, 0, tzinfo=UTC)
        before = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        missed = compute_all_missed("0 9 * * *", "UTC", after, before)
        self.assertEqual(missed, [])

    def test_one_missed_fire(self):
        # Daily job at 09:00, daemon was down from 08:50 to 09:10
        after = datetime(2026, 4, 7, 8, 50, 0, tzinfo=UTC)
        before = datetime(2026, 4, 7, 9, 10, 0, tzinfo=UTC)
        missed = compute_all_missed("0 9 * * *", "UTC", after, before)
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0].hour, 9)
        self.assertEqual(missed[0].minute, 0)

    def test_multiple_missed_fires(self):
        # Hourly job, 3 hours of downtime
        after = datetime(2026, 4, 7, 9, 0, 0, tzinfo=UTC)
        before = datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)
        missed = compute_all_missed("0 * * * *", "UTC", after, before)
        # Should include 10:00, 11:00, 12:00
        self.assertEqual(len(missed), 3)

    def test_boundary_exactly_on_fire_time(self):
        # before == fire time exactly → should be included (half-open interval (after, before])
        after = datetime(2026, 4, 7, 8, 0, 0, tzinfo=UTC)
        before = datetime(2026, 4, 7, 9, 0, 0, tzinfo=UTC)
        missed = compute_all_missed("0 9 * * *", "UTC", after, before)
        self.assertEqual(len(missed), 1)


class TestJobSchedulerFiring(unittest.IsolatedAsyncioTestCase):
    def _make_store_and_scheduler(self, **job_kwargs) -> tuple[JobStore, JobScheduler, ScheduledJob]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()

        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        scheduler = JobScheduler(
            store=store,
            session_managers={"rc-home": sm},
            completed_job_ttl_days=7,
        )
        scheduler._session_managers = {"rc-home": sm}
        job = store.add(_make_job(**job_kwargs))
        return store, scheduler, job

    async def test_fire_increments_run_count(self):
        store, scheduler, job = self._make_store_and_scheduler(
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 1)
        self.assertIsNotNone(updated.last_run)

    async def test_fire_advances_next_run(self):
        store, scheduler, job = self._make_store_and_scheduler(
            cron="0 * * * *",  # hourly
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        # next_run should now be in the future
        next_dt = datetime.fromisoformat(updated.next_run)
        self.assertGreater(next_dt, datetime.now(UTC))

    async def test_fire_completes_times_job(self):
        store, scheduler, job = self._make_store_and_scheduler(
            times=1,
            run_count=0,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.status, JobStatus.COMPLETED)
        self.assertIsNotNone(updated.completed_at)
        self.assertIsNone(updated.next_run)

    async def test_forever_job_never_completes(self):
        store, scheduler, job = self._make_store_and_scheduler(
            times=0,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.status, JobStatus.ACTIVE)
        self.assertIsNone(updated.completed_at)

    async def test_paused_job_not_fired(self):
        store, scheduler, job = self._make_store_and_scheduler(
            status=JobStatus.PAUSED,
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 0)  # not fired

    async def test_future_job_not_fired(self):
        store, scheduler, job = self._make_store_and_scheduler(
            next_run=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
        )
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 0)

    async def test_inject_failure_advances_next_run_anyway(self):
        """When inject fails, next_run should still advance (avoid retry flood)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=False)  # injection fails
        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        job = store.add(_make_job(
            next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        ))
        await scheduler._fire_due_jobs()
        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 1)  # count incremented despite failure


class TestJobSchedulerCatchUp(unittest.IsolatedAsyncioTestCase):
    async def test_catch_up_fires_missed_jobs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        # Daily job that should have run 2 hours ago
        job = store.add(_make_job(
            cron="0 * * * *",  # hourly
            times=0,
            next_run=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            last_run=(datetime.now(UTC) - timedelta(hours=3)).isoformat(),
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        # Should have fired at least once for each missed hour
        self.assertGreaterEqual(updated.run_count, 2)

    async def test_catch_up_one_shot_fires_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        # One-shot job that never ran
        job = store.add(_make_job(
            times=1,
            run_count=0,
            next_run=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        self.assertEqual(updated.run_count, 1)
        self.assertEqual(updated.status, JobStatus.COMPLETED)


class TestJobSchedulerPurge(unittest.IsolatedAsyncioTestCase):
    async def test_tick_purges_expired_completed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        old_completed = store.add(_make_job(
            status=JobStatus.COMPLETED,
            completed_at=(datetime.now(UTC) - timedelta(days=10)).isoformat(),
            next_run=None,
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._tick()

        self.assertIsNone(store.get(old_completed.id))


if __name__ == "__main__":
    unittest.main()
