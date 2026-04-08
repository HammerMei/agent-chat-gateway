"""JobScheduler: asyncio background task that fires scheduled jobs.

Architecture
-----------
The scheduler runs as a single asyncio task created in GatewayService.run().
It owns two responsibilities per tick (every 60 s):

  1. _purge_expired_completed_jobs() — remove COMPLETED jobs older than TTL.
  2. _fire_due_jobs()               — inject messages for ACTIVE jobs that are due.

On startup, _catch_up_missed() fires all jobs whose next_run has already passed
(user preference: always fire all missed jobs).

Message delivery uses direct injection (JobScheduler → SessionManager.inject_message())
rather than posting to the chat platform.  This bypasses the connector's self-message
filter, which would silently drop any message sent by the bot's own username.

Dependencies
-----------
  croniter>=2.0.0  — cron expression parsing and next_run calculation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

try:
    from croniter import croniter  # type: ignore[import-untyped]
except ImportError as _e:
    raise ImportError(
        "croniter is required for scheduling support. "
        "Install it with: pip install 'croniter>=2.0.0'"
    ) from _e

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore[no-redef]

from ..schedule_types import JobStatus, ScheduledJob
from .job_store import JobStore

if TYPE_CHECKING:
    from .session_manager import SessionManager

logger = logging.getLogger("agent-chat-gateway.core.scheduler")

_TICK_INTERVAL = 60  # seconds between scheduler polls


def compute_next_run(cron: str, timezone: str, after: datetime | None = None) -> str:
    """Return the next fire time (ISO 8601 UTC string) for a cron expression.

    Args:
        cron:     5-field POSIX cron expression, e.g. ``"0 9 * * 1-5"``.
        timezone: IANA timezone name, e.g. ``"Asia/Taipei"``.
        after:    Compute next run strictly after this UTC datetime.
                  Defaults to ``datetime.now(UTC)``.

    Returns:
        ISO 8601 UTC string, e.g. ``"2026-04-09T01:00:00+00:00"``.
    """
    try:
        tz = zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        logger.warning("Unknown timezone %r, falling back to UTC", timezone)
        tz = zoneinfo.ZoneInfo("UTC")

    base = after if after is not None else datetime.now(UTC)
    # croniter expects a naive or tz-aware datetime as start; convert to the job's timezone
    base_local = base.astimezone(tz)

    it = croniter(cron, base_local)
    next_local: datetime = it.get_next(datetime)
    # Defensive: croniter may return a naive datetime in some versions.
    # astimezone(UTC) raises ValueError on naive datetimes, so attach the
    # job's timezone explicitly before converting.
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    next_utc = next_local.astimezone(UTC)
    return next_utc.isoformat()


def compute_all_missed(
    cron: str,
    timezone: str,
    after_utc: datetime,
    before_utc: datetime,
) -> list[datetime]:
    """Return all cron fire times in the half-open interval (after_utc, before_utc].

    Used for catch-up: determines how many times a job should have fired while
    the daemon was down.
    """
    try:
        tz = zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        tz = zoneinfo.ZoneInfo("UTC")

    base_local = after_utc.astimezone(tz)
    it = croniter(cron, base_local)
    times = []
    while True:
        t: datetime = it.get_next(datetime)
        t_utc = t.astimezone(UTC)
        if t_utc > before_utc:
            break
        times.append(t_utc)
    return times


class JobScheduler:
    """Asyncio background task that manages job firing and cleanup.

    Usage::

        scheduler = JobScheduler(store, session_managers, completed_job_ttl_days=7)
        task = asyncio.create_task(scheduler.run(), name="job-scheduler")
        # ... on shutdown:
        task.cancel()
    """

    def __init__(
        self,
        store: JobStore,
        session_managers: "dict[str, SessionManager]",  # connector_name → SessionManager
        completed_job_ttl_days: int = 7,
    ) -> None:
        self._store = store
        self._session_managers = session_managers
        self._ttl_days = completed_job_ttl_days

    async def run(self) -> None:
        """Main scheduler loop.  Runs until cancelled."""
        logger.info("JobScheduler started (tick_interval=%ds, ttl_days=%d)", _TICK_INTERVAL, self._ttl_days)
        try:
            await self._catch_up_missed()
            while True:
                await asyncio.sleep(_TICK_INTERVAL)
                await self._tick()
        except asyncio.CancelledError:
            logger.info("JobScheduler cancelled")
            raise

    # ── Startup catch-up ─────────────────────────────────────────────────────

    async def _catch_up_missed(self) -> None:
        """Fire all jobs that were due while the daemon was down."""
        now = datetime.now(UTC)
        jobs = self._store.list_due()
        if not jobs:
            return
        logger.info("Catching up %d missed job(s) on startup", len(jobs))
        for job in jobs:
            await self._fire_catch_up(job, now)

    async def _fire_catch_up(self, job: ScheduledJob, now: datetime) -> None:
        """Fire a job that was missed during downtime.

        For recurring jobs, counts all missed fire times and fires once per
        missed occurrence (respecting ``times`` limit).  For one-shot jobs,
        fires once and completes.
        """
        # Determine how many times this job should have fired since last_run
        last_run_dt: datetime | None = None
        if job.last_run:
            try:
                last_run_dt = datetime.fromisoformat(job.last_run)
            except ValueError:
                pass

        if last_run_dt is None:
            # Never ran before — try creation time as the start anchor
            if job.created_at:
                try:
                    last_run_dt = datetime.fromisoformat(job.created_at)
                except ValueError:
                    pass
            # If created_at is also missing/malformed, fall back to next_run itself
            # (which is already in the past — see list_due() precondition).
            # Using `now` would produce an empty missed-fires list and silently
            # skip the catch-up, so next_run is a better anchor.
            if last_run_dt is None and job.next_run:
                try:
                    # Anchor one minute before next_run so the job fires exactly once
                    last_run_dt = datetime.fromisoformat(job.next_run) - timedelta(minutes=1)
                except ValueError:
                    pass
            if last_run_dt is None:
                # Last resort: fire once unconditionally
                logger.warning(
                    "Job %s has neither last_run nor created_at — firing once unconditionally",
                    job.id,
                )
                await self._fire_once(job, now)
                return

        # For one-shot jobs (times==1), fire once regardless of how long they were missed
        if job.times == 1 and job.run_count == 0:
            await self._fire_once(job, now)
            return

        # For recurring jobs, enumerate all missed fire times
        missed = compute_all_missed(job.cron, job.timezone, last_run_dt, now)
        if not missed:
            return

        for fire_time in missed:
            if job.status != JobStatus.ACTIVE:
                break  # completed during catch-up
            await self._fire_once(job, fire_time)

    # ── Per-tick ──────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        """One scheduler tick: purge expired + fire due jobs."""
        self._store.remove_expired_completed(self._ttl_days)
        await self._fire_due_jobs()

    async def _fire_due_jobs(self) -> None:
        """Fire all ACTIVE jobs whose next_run has arrived."""
        due = self._store.list_due()
        for job in due:
            # Use the job's nominal scheduled time (next_run) as the fire timestamp
            # so that next_run is computed from the canonical schedule, not from the
            # actual wall-clock time the scheduler polled (which can drift slightly).
            try:
                fire_time = datetime.fromisoformat(job.next_run) if job.next_run else datetime.now(UTC)
            except ValueError:
                fire_time = datetime.now(UTC)
            await self._fire_once(job, fire_time)

    # ── Job execution ─────────────────────────────────────────────────────────

    async def _fire_once(self, job: ScheduledJob, fire_time: datetime) -> None:
        """Fire a single job: inject message, update state, persist."""
        logger.info(
            "Firing scheduled job %s (watcher=%s, run=%d/%s)",
            job.id,
            job.watcher,
            job.run_count + 1,
            str(job.times) if job.times > 0 else "∞",
        )

        success = await self._inject(job)
        if not success:
            logger.warning(
                "Job %s: injection failed (watcher=%s may not be active). "
                "Advancing next_run anyway to avoid repeated retry flood.",
                job.id,
                job.watcher,
            )

        now_str = fire_time.isoformat()
        job.run_count += 1
        job.last_run = now_str

        # Check completion
        if job.times > 0 and job.run_count >= job.times:
            job.status = JobStatus.COMPLETED
            job.next_run = None
            job.completed_at = datetime.now(UTC).isoformat()
            logger.info("Job %s completed all %d run(s)", job.id, job.times)
        else:
            # Compute next fire time from now
            job.next_run = compute_next_run(job.cron, job.timezone, after=fire_time)

        try:
            self._store.update(job)
        except Exception as e:
            logger.error("Failed to persist job %s after fire: %s", job.id, e)

    async def _inject(self, job: ScheduledJob) -> bool:
        """Inject the job message into the target watcher via SessionManager.

        Tries the connector-specific SessionManager first; falls back to
        searching all managers if connector is not specified or not found.
        """
        sm = self._session_managers.get(job.connector)
        if sm is not None:
            try:
                return await sm.inject_message(job.watcher, job.message)
            except Exception as e:
                logger.error("Job %s: inject_message failed on connector %r: %s", job.id, job.connector, e)
                return False

        # Fallback: search all session managers
        for connector_name, manager in self._session_managers.items():
            try:
                result = await manager.inject_message(job.watcher, job.message)
                if result:
                    return True
            except Exception:
                pass

        logger.warning("Job %s: no session manager could deliver message to watcher %r", job.id, job.watcher)
        return False
