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
import copy
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


_MAX_MISSED_CATCHUP = 500  # cap to prevent OOM on very long downtimes with frequent crons


def compute_all_missed(
    cron: str,
    timezone: str,
    after_utc: datetime,
    before_utc: datetime,
) -> list[datetime]:
    """Return all cron fire times in the half-open interval (after_utc, before_utc].

    Used for catch-up: determines how many times a job should have fired while
    the daemon was down.

    Capped at ``_MAX_MISSED_CATCHUP`` entries to prevent unbounded memory use
    when a frequent cron (e.g. ``* * * * *``) is combined with a long downtime.
    A warning is logged when the cap is hit.
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
        # Defensive: croniter may return naive datetimes on some versions.
        if t.tzinfo is None:
            t = t.replace(tzinfo=tz)
        t_utc = t.astimezone(UTC)
        if t_utc > before_utc:
            break
        times.append(t_utc)
        if len(times) >= _MAX_MISSED_CATCHUP:
            logger.warning(
                "compute_all_missed: capped at %d entries for cron %r "
                "(downtime window %s → %s). Remaining missed fires will be skipped.",
                _MAX_MISSED_CATCHUP,
                cron,
                after_utc.isoformat(),
                before_utc.isoformat(),
            )
            break
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
        except Exception as e:
            # Log immediately so the scheduler death is visible in runtime logs
            # rather than only surfacing when the task future is awaited at shutdown.
            logger.error("JobScheduler terminated unexpectedly: %s", e, exc_info=True)
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
        def _parse_utc(ts: str) -> datetime | None:
            """Parse an ISO 8601 timestamp and ensure it is UTC-aware."""
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    # Hand-edited or legacy value without offset — assume UTC.
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                return None

        # Determine how many times this job should have fired since last_run
        last_run_dt: datetime | None = None
        if job.last_run:
            last_run_dt = _parse_utc(job.last_run)

        if last_run_dt is None:
            # Never ran before — try creation time as the start anchor
            if job.created_at:
                last_run_dt = _parse_utc(job.created_at)
            # If created_at is also missing/malformed, fall back to next_run itself
            # (which is already in the past — see list_due() precondition).
            # Using `now` would produce an empty missed-fires list and silently
            # skip the catch-up, so next_run is a better anchor.
            if last_run_dt is None and job.next_run:
                # Anchor one minute before next_run so the job fires exactly once
                nr = _parse_utc(job.next_run)
                if nr is not None:
                    last_run_dt = nr - timedelta(minutes=1)
            if last_run_dt is None:
                # Last resort: fire once unconditionally
                logger.warning(
                    "Job %s has neither last_run nor created_at — firing once unconditionally",
                    job.id,
                )
                await self._fire_once(job, now)
                return

        # Guard: job is exhausted (run_count >= times) but status is still ACTIVE.
        # This can happen if jobs.json was hand-edited or if a persistence race left
        # the status un-updated.  Do NOT fire — mark as COMPLETED and bail out.
        remaining = job.times - job.run_count if job.times > 0 else None
        if remaining is not None and remaining <= 0:
            logger.warning(
                "Job %s has run_count=%d >= times=%d but status=ACTIVE — "
                "marking COMPLETED without firing",
                job.id, job.run_count, job.times,
            )
            job.status = JobStatus.COMPLETED
            job.next_run = None
            # Always reset completed_at to now — a hand-edited future timestamp
            # would otherwise make the job immune to TTL purge.
            job.completed_at = datetime.now(UTC).isoformat()
            await asyncio.to_thread(self._store.update, job)
            return

        # For jobs with exactly one remaining run, fire once regardless of how long
        # they were missed — calling compute_all_missed on a large downtime window
        # could return many entries, but only one more fire is allowed.
        # Use the job's canonical scheduled time (next_run) as the fire timestamp
        # so that last_run reflects the intended schedule, not the catch-up wall clock.
        if remaining == 1:
            fire_time = now
            if job.next_run:
                try:
                    parsed = datetime.fromisoformat(job.next_run)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    fire_time = parsed
                except ValueError:
                    pass
            await self._fire_once(job, fire_time)
            return

        # For recurring jobs, enumerate all missed fire times
        missed = compute_all_missed(job.cron, job.timezone, last_run_dt, now)
        if not missed:
            return

        for fire_time in missed:
            if job.status != JobStatus.ACTIVE:
                break  # completed during catch-up
            # _fire_once returns the updated copy; re-assign so the next
            # iteration sees the incremented run_count / updated status.
            job = await self._fire_once(job, fire_time)

    # ── Per-tick ──────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        """One scheduler tick: purge expired + fire due jobs."""
        # Offload the purge file-write to a thread so the event loop is not
        # blocked while jobs.json is being written.
        await asyncio.to_thread(self._store.remove_expired_completed, self._ttl_days)
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
                # Guard: fromisoformat returns naive datetimes for legacy / hand-edited
                # values without a UTC offset.  astimezone() on a naive datetime uses
                # the system local timezone, silently producing a wrong next_run.
                if fire_time.tzinfo is None:
                    fire_time = fire_time.replace(tzinfo=UTC)
            except ValueError:
                fire_time = datetime.now(UTC)
            try:
                await self._fire_once(job, fire_time)
            except Exception as e:
                # Per-job isolation: one broken job must not kill the scheduler
                # or prevent other jobs from firing in the same tick.
                logger.error("Unexpected error firing job %s — skipping: %s", job.id, e)

    # ── Job execution ─────────────────────────────────────────────────────────

    async def _fire_once(self, job: ScheduledJob, fire_time: datetime) -> ScheduledJob:
        """Fire a single job: inject message, update state, persist.

        Returns the updated job object (a shallow copy).  Callers that fire the
        same job multiple times (e.g. the catch-up loop) must re-assign their
        local reference to the return value so subsequent iterations see the
        correct ``run_count`` and ``status``.

        ``run_count`` is incremented and ``next_run`` is advanced even when
        injection fails.  This is intentional: silently retrying a failed
        injection on every subsequent tick would flood the queue if the watcher
        stays down for an extended period.  Users can resume the watcher and
        the next scheduled fire will deliver the message normally.

        All field mutations are applied to a shallow copy of the job object so
        that concurrent ``list_jobs()`` / ``list_due()`` callers on the event-
        loop thread never observe a partially mutated state.  ``store.update``
        atomically replaces the reference in the in-memory dict under the lock,
        so readers either see the old state or the fully-updated state.
        """
        # Shallow copy is sufficient: all ScheduledJob fields are immutable
        # scalar types (str, int, enum) so there are no nested mutable objects.
        job = copy.copy(job)

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
            # Compute next fire time.  Guard against a corrupted/empty cron field
            # that slipped past validation (e.g. hand-edited jobs.json).  A bad
            # cron would otherwise propagate an exception that kills the scheduler
            # task for ALL jobs, not just this one.
            try:
                job.next_run = compute_next_run(job.cron, job.timezone, after=fire_time)
            except Exception as e:
                logger.error(
                    "Job %s: failed to compute next_run (cron=%r): %s — pausing job",
                    job.id, job.cron, e,
                )
                job.status = JobStatus.PAUSED
                job.next_run = None

        try:
            # Offload the file-write to a thread pool so the event loop is not
            # blocked while jobs.json is being written.  store.update() replaces
            # _jobs[job.id] under the lock, making the updated copy visible to
            # all subsequent readers atomically.
            await asyncio.to_thread(self._store.update, job)
        except Exception as e:
            logger.error("Failed to persist job %s after fire: %s", job.id, e)

        return job

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
