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

    def test_cap_prevents_oom_on_frequent_long_downtime(self):
        """compute_all_missed must not return more than _MAX_MISSED_CATCHUP entries."""
        from gateway.core.scheduler import _MAX_MISSED_CATCHUP
        # Every-minute cron, 2 years of downtime → would produce >1M entries uncapped
        after = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        before = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        missed = compute_all_missed("* * * * *", "UTC", after, before)
        self.assertLessEqual(len(missed), _MAX_MISSED_CATCHUP)


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

    async def test_broken_job_does_not_block_other_jobs(self):
        """Per-job isolation: an exception in one job must not prevent others from firing."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)
        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)

        due_time = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        # A job with a bad cron expression that will raise in compute_next_run
        bad_job = store.add(_make_job(cron="not-a-cron", next_run=due_time))
        good_job = store.add(_make_job(cron="0 * * * *", next_run=due_time))

        # Should not raise despite bad_job's broken cron
        await scheduler._fire_due_jobs()

        # The good job must have fired
        self.assertEqual(store.get(good_job.id).run_count, 1)
        # The bad job's run_count also increments (fire ran up to the cron error), but next_run is cleared
        bad_updated = store.get(bad_job.id)
        self.assertEqual(bad_updated.run_count, 1)
        self.assertIsNone(bad_updated.next_run)
        self.assertEqual(bad_updated.status, JobStatus.PAUSED)


class TestJobSchedulerCatchUp(unittest.IsolatedAsyncioTestCase):
    async def test_catch_up_fires_missed_jobs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(jobs_file=Path(tmp.name) / "jobs.json")
        store.load()
        sm = MagicMock()
        sm.inject_message = AsyncMock(return_value=True)

        # Hourly job created 4 h ago, last fired 3 h ago, next fire was 2 h ago
        now = datetime.now(UTC)
        job = store.add(_make_job(
            cron="0 * * * *",  # hourly
            times=0,
            created_at=(now - timedelta(hours=4)).isoformat(),
            next_run=(now - timedelta(hours=2)).isoformat(),
            last_run=(now - timedelta(hours=3)).isoformat(),
        ))

        scheduler = JobScheduler(store=store, session_managers={"rc-home": sm}, completed_job_ttl_days=7)
        await scheduler._catch_up_missed()

        updated = store.get(job.id)
        # last_run = now-3h; hourly fires at (now-2h), (now-1h), now → exactly 3 missed
        self.assertEqual(updated.run_count, 3)

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


# ── Tests: _build_cron_expression ────────────────────────────────────────────


class TestBuildCronExpression(unittest.TestCase):
    """Tests for the CLI helper _build_cron_expression and _parse_one_shot_at."""

    def _build(self, every=None, at=None):
        from gateway.cli import _build_cron_expression
        return _build_cron_expression(every, at)

    # ── No arguments ──────────────────────────────────────────────────────────

    def test_no_args_raises(self):
        with self.assertRaises(ValueError):
            self._build()

    # ── Basic intervals (no --at) ─────────────────────────────────────────────

    def test_1m(self):
        self.assertEqual(self._build("1m"), "* * * * *")

    def test_5m(self):
        self.assertEqual(self._build("5m"), "*/5 * * * *")

    def test_30m(self):
        self.assertEqual(self._build("30m"), "*/30 * * * *")

    def test_1h(self):
        self.assertEqual(self._build("1h"), "0 * * * *")

    def test_6h(self):
        self.assertEqual(self._build("6h"), "0 */6 * * *")

    def test_1d(self):
        self.assertEqual(self._build("1d"), "0 9 * * *")

    def test_1w(self):
        self.assertEqual(self._build("1w"), "0 9 * * 1")

    def test_unsupported_interval_raises(self):
        with self.assertRaises(ValueError, msg="should reject unknown interval"):
            self._build("2d")

    # ── --every + --at HH:MM ──────────────────────────────────────────────────

    def test_daily_with_at_time(self):
        self.assertEqual(self._build("1d", "14:30"), "30 14 * * *")

    def test_weekly_with_at_time(self):
        self.assertEqual(self._build("1w", "08:00"), "0 8 * * 1")

    def test_hourly_with_at_minute_only(self):
        # Sub-daily: only the minute is applied; hour is discarded
        self.assertEqual(self._build("1h", "00:15"), "15 * * * *")

    def test_sub_daily_at_non_zero_hour_still_applies_minute(self):
        # Hour is ignored for sub-daily, but minute is still applied
        result = self._build("6h", "02:30")
        self.assertEqual(result.split()[0], "30")   # minute = 30
        self.assertEqual(result.split()[1], "*/6")  # hour unchanged

    def test_sub_minute_interval_rejects_at_hhmm(self):
        with self.assertRaises(ValueError):
            self._build("30m", "09:00")

    # ── --every 1w + --at DOW HH:MM ───────────────────────────────────────────

    def test_weekly_with_dow_time(self):
        self.assertEqual(self._build("1w", "Fri 17:00"), "0 17 * * 5")

    def test_weekly_dow_case_insensitive(self):
        self.assertEqual(self._build("1w", "fri 17:00"), "0 17 * * 5")

    def test_weekly_sunday(self):
        self.assertEqual(self._build("1w", "Sun 00:00"), "0 0 * * 0")

    def test_dow_syntax_only_with_1w(self):
        with self.assertRaises(ValueError):
            self._build("1d", "Mon 09:00")

    def test_unknown_dow_raises(self):
        with self.assertRaises(ValueError):
            self._build("1w", "Xyz 09:00")

    # ── One-shot (no --every, --at datetime) ──────────────────────────────────

    def test_one_shot_at_datetime(self):
        self.assertEqual(self._build(at="2026-04-10 15:30"), "30 15 10 4 *")

    def test_one_shot_at_iso_format(self):
        self.assertEqual(self._build(at="2026-04-10T15:30"), "30 15 10 4 *")

    def test_one_shot_at_slash_format(self):
        self.assertEqual(self._build(at="2026/04/10 15:30"), "30 15 10 4 *")

    def test_one_shot_at_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            self._build(at="not-a-date")

    def test_one_shot_at_empty_raises(self):
        with self.assertRaises(ValueError):
            self._build(at="")

    # ── _parse_hhmm edge cases ─────────────────────────────────────────────────

    def test_invalid_hhmm_raises(self):
        from gateway.cli import _parse_hhmm
        with self.assertRaises(ValueError):
            _parse_hhmm("25:00")  # hour out of range

    def test_invalid_hhmm_no_colon_raises(self):
        from gateway.cli import _parse_hhmm
        with self.assertRaises(ValueError):
            _parse_hhmm("0900")

    def test_valid_hhmm(self):
        from gateway.cli import _parse_hhmm
        self.assertEqual(_parse_hhmm("09:05"), (9, 5))
        self.assertEqual(_parse_hhmm("23:59"), (23, 59))
        self.assertEqual(_parse_hhmm("00:00"), (0, 0))

    # ── Boundary cron values ──────────────────────────────────────────────────

    def test_arbitrary_2m_recurring(self):
        """2m (not in _INTERVAL_MAP, but valid 1-59 range) → */2 * * * *."""
        self.assertEqual(self._build("2m"), "*/2 * * * *")

    def test_arbitrary_59m_recurring(self):
        """59m is the upper boundary for sub-hourly intervals → */59 * * * *."""
        self.assertEqual(self._build("59m"), "*/59 * * * *")

    def test_arbitrary_23h_recurring(self):
        """23h is the upper boundary for hourly intervals → 0 */23 * * *."""
        self.assertEqual(self._build("23h"), "0 */23 * * *")

    def test_arbitrary_7h_recurring(self):
        """7h (not in _INTERVAL_MAP) → 0 */7 * * *."""
        self.assertEqual(self._build("7h"), "0 */7 * * *")

    def test_60m_raises(self):
        """60m exceeds the 1-59 minute range → ValueError."""
        with self.assertRaises(ValueError):
            self._build("60m")

    def test_0h_raises(self):
        """0h is below the 1-23 hour range → ValueError."""
        with self.assertRaises(ValueError):
            self._build("0h")

    def test_24h_raises(self):
        """24h exceeds the 1-23 hour range → ValueError."""
        with self.assertRaises(ValueError):
            self._build("24h")

    # ── Daily/weekly --at boundary times ─────────────────────────────────────

    def test_daily_at_midnight(self):
        """1d + 00:00 → '0 0 * * *'."""
        self.assertEqual(self._build("1d", "00:00"), "0 0 * * *")

    def test_daily_at_end_of_day(self):
        """1d + 23:59 → '59 23 * * *'."""
        self.assertEqual(self._build("1d", "23:59"), "59 23 * * *")

    def test_weekly_plain_hhmm_preserves_monday_dow(self):
        """1w + '15:00' (no DOW token) preserves the default DOW=1 (Monday)."""
        result = self._build("1w", "15:00")
        self.assertEqual(result, "0 15 * * 1")

    # ── --at with hourly interval: non-zero hour triggers warning ─────────────

    def test_hourly_at_nonzero_hour_emits_warning(self):
        """1h + '09:00' discards the hour with a warning; minute stays 0."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._build("1h", "09:00")
        self.assertIn("ignored", buf.getvalue())
        self.assertEqual(result, "0 * * * *")

    def test_6h_at_nonzero_hour_only_applies_minute(self):
        """6h + '03:45' discards hour=3, applies only minute=45."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._build("6h", "03:45")
        parts = result.split()
        self.assertEqual(parts[0], "45")   # minute applied
        self.assertEqual(parts[1], "*/6")  # hour unchanged
        self.assertIn("ignored", buf.getvalue())

    # ── One-shot --at past-date emits warning but succeeds ────────────────────

    def test_one_shot_past_date_warns_but_returns_cron(self):
        """A past --at datetime emits a warning but still returns a valid cron."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._build(at="2000-01-01 09:00")
        self.assertIn("past", buf.getvalue().lower())
        self.assertEqual(result, "0 9 1 1 *")

    def test_one_shot_boundary_dec31(self):
        """Boundary one-shot date Dec 31 23:59 → '59 23 31 12 *'."""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._build(at="2099-12-31 23:59")
        self.assertEqual(result, "59 23 31 12 *")


# ── Tests: _parse_one_shot_interval ──────────────────────────────────────────


class TestParseOneShotInterval(unittest.TestCase):
    """Tests for _parse_one_shot_interval (arbitrary Nm/Nh for one-shot reminders)."""

    def _parse(self, s: str):
        from gateway.cli import _parse_one_shot_interval
        return _parse_one_shot_interval(s)

    def test_1m_returns_1(self):
        self.assertEqual(self._parse("1m"), 1)

    def test_7m_returns_7(self):
        self.assertEqual(self._parse("7m"), 7)

    def test_59m_returns_59(self):
        self.assertEqual(self._parse("59m"), 59)

    def test_90m_returns_90(self):
        """Values above 59 are allowed for one-shot: 90m = 90 minutes from now."""
        self.assertEqual(self._parse("90m"), 90)

    def test_2h_returns_120(self):
        """2h → 120 minutes."""
        self.assertEqual(self._parse("2h"), 120)

    def test_1h_returns_60(self):
        self.assertEqual(self._parse("1h"), 60)

    def test_0m_returns_none(self):
        """0m is not a valid positive interval → None (falls through to _build)."""
        self.assertIsNone(self._parse("0m"))

    def test_1d_returns_none(self):
        """1d is not an Nm/Nh expression → None (falls through to _INTERVAL_MAP)."""
        self.assertIsNone(self._parse("1d"))

    def test_1w_returns_none(self):
        """1w is not an Nm/Nh expression → None."""
        self.assertIsNone(self._parse("1w"))

    def test_bad_string_returns_none(self):
        """Non-matching garbage → None."""
        self.assertIsNone(self._parse("bad"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._parse(""))

    def test_case_insensitive_uppercase_M(self):
        """Uppercase M is accepted (input is lowercased before parsing)."""
        self.assertEqual(self._parse("5M"), 5)

    def test_case_insensitive_uppercase_H(self):
        self.assertEqual(self._parse("2H"), 120)

    def test_with_leading_whitespace(self):
        """strip() normalizes surrounding whitespace before parsing."""
        self.assertEqual(self._parse("  5m  "), 5)


# ── Tests: _parse_starting ────────────────────────────────────────────────────


class TestParseStarting(unittest.TestCase):
    """Tests for _parse_starting: smart date parsing for the --starting flag."""

    def _parse(self, s: str, tz_name: str | None = None, now_utc: datetime | None = None):
        from gateway.cli import _parse_starting
        if now_utc is None:
            now_utc = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)  # 2026-04-09 10:00 UTC (Thursday)
        return _parse_starting(s, tz_name, now_utc)

    # ── HH:MM format ──────────────────────────────────────────────────────────

    def test_hhmm_future_today(self):
        """'15:00' when it's 10:00 UTC → today at 15:00, was_past=False."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("15:00", now_utc=now)
        self.assertEqual(result.hour, 15)
        self.assertEqual(result.minute, 0)
        self.assertFalse(result.was_past)
        self.assertIsNone(result.dow)
        # first_run should be on the same day
        self.assertEqual(result.first_run.date(), now.date())

    def test_hhmm_past_advances_to_tomorrow(self):
        """'09:00' when it's 10:00 UTC → tomorrow at 09:00, was_past=True."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("09:00", now_utc=now)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 0)
        self.assertTrue(result.was_past)
        # first_run should be the next day
        from datetime import timedelta
        expected_date = (now + timedelta(days=1)).date()
        self.assertEqual(result.first_run.astimezone(UTC).date(), expected_date)

    def test_hhmm_first_run_is_utc_and_future(self):
        """first_run is always UTC and in the future."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("09:00", now_utc=now)
        self.assertGreater(result.first_run, now)
        self.assertIsNotNone(result.first_run.tzinfo)

    # ── Mon HH:MM format ──────────────────────────────────────────────────────

    def test_dow_next_monday(self):
        """'Mon 09:00' on a Thursday → next Monday."""
        # 2026-04-09 is a Thursday
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("Mon 09:00", now_utc=now)
        self.assertEqual(result.dow, "1")  # cron DOW for Monday
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 0)
        self.assertFalse(result.was_past)
        # Next Monday from Thursday Apr 9 is Apr 13
        self.assertEqual(result.first_run.astimezone(UTC).date().isoformat(), "2026-04-13")

    def test_dow_case_insensitive(self):
        """'fri 17:00' works (lowercase)."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("fri 17:00", now_utc=now)
        self.assertEqual(result.dow, "5")  # Friday

    def test_dow_unknown_raises(self):
        """Unknown DOW raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("Xyz 09:00")

    # ── Apr 15 09:00 format ───────────────────────────────────────────────────

    def test_month_name_future_this_year(self):
        """'Apr 15 09:00' when today is Apr 9 → this year Apr 15."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("Apr 15 09:00", now_utc=now)
        self.assertFalse(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).month, 4)
        self.assertEqual(result.first_run.astimezone(UTC).day, 15)

    def test_month_name_past_advances_one_year(self):
        """'Jan 01 09:00' in April → next year."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("Jan 01 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).year, 2027)

    def test_month_name_case_insensitive(self):
        """'apr 15 09:00' works (lowercase)."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("apr 15 09:00", now_utc=now)
        self.assertEqual(result.first_run.astimezone(UTC).month, 4)

    # ── 04-15 09:00 format ────────────────────────────────────────────────────

    def test_mmdd_future_this_year(self):
        """'04-15 09:00' → this year Apr 15."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("04-15 09:00", now_utc=now)
        self.assertFalse(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).month, 4)
        self.assertEqual(result.first_run.astimezone(UTC).day, 15)

    def test_mmdd_past_advances_one_year(self):
        """'01-01 09:00' in April → next year."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("01-01 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).year, 2027)

    # ── Full datetime format ──────────────────────────────────────────────────

    def test_full_datetime_future(self):
        """'2026-05-01 09:00' → explicit UTC datetime."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("2026-05-01 09:00", now_utc=now)
        self.assertFalse(result.was_past)
        self.assertEqual(result.first_run.astimezone(UTC).year, 2026)
        self.assertEqual(result.first_run.astimezone(UTC).month, 5)
        self.assertEqual(result.first_run.astimezone(UTC).day, 1)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 0)

    def test_full_datetime_past_was_past_true(self):
        """'2000-01-01 09:00' (past) → was_past=True, first_run still that datetime."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("2000-01-01 09:00", now_utc=now)
        self.assertTrue(result.was_past)
        # first_run is the literal datetime (no auto-advance for full explicit datetimes)
        self.assertEqual(result.first_run.astimezone(UTC).year, 2000)

    # ── Timezone handling ─────────────────────────────────────────────────────

    def test_tz_shifts_first_run_to_utc(self):
        """'09:00' with tz='America/New_York' → UTC = 09:00 + offset."""
        from zoneinfo import ZoneInfo
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("09:00", tz_name="America/New_York", now_utc=now)
        # America/New_York is UTC-4 in April (EDT)
        # 09:00 EDT = 13:00 UTC
        utc_hour = result.first_run.astimezone(UTC).hour
        self.assertIn(utc_hour, (13, 14))  # EDT is -4, so 09+4=13; DST edge: 14 is possible

    def test_invalid_tz_falls_back_to_utc(self):
        """Unknown timezone silently falls back to UTC."""
        now = datetime(2026, 4, 9, 10, 0, 0, tzinfo=UTC)
        result = self._parse("15:00", tz_name="Invalid/Zone", now_utc=now)
        self.assertIsNotNone(result.first_run)  # should not raise

    # ── Invalid input ─────────────────────────────────────────────────────────

    def test_invalid_format_raises(self):
        """Completely unrecognized format raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("not-a-date")

    def test_empty_string_raises(self):
        """Empty string raises ValueError."""
        with self.assertRaises(ValueError):
            self._parse("")


if __name__ == "__main__":
    unittest.main()
