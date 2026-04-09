"""JobStore: atomic read/write of scheduled jobs to jobs.json.

The daemon process is the sole writer — all mutations come through the control
socket, ensuring serial access without file-level locking. The CLI sends commands
via the control socket rather than writing directly.

Storage format
--------------
  ~/.agent-chat-gateway/jobs.json

  {
    "version": 1,
    "jobs": [ { ...ScheduledJob fields... }, ... ]
  }

Atomic writes use the same PID-unique temp-file + rename(2) pattern as
``gateway.core.state`` to guarantee no partial writes on crash.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..schedule_types import JobStatus, ScheduledJob

logger = logging.getLogger("agent-chat-gateway.core.job_store")

RUNTIME_DIR = Path.home() / ".agent-chat-gateway"
JOBS_FILE = RUNTIME_DIR / "jobs.json"
_SCHEMA_VERSION = 1


class JobStore:
    """CRUD store for ScheduledJob objects, persisted to jobs.json.

    All write operations are atomic (PID-unique temp file + rename).
    The in-memory list is the single source of truth; the file is written
    after every mutating operation.
    """

    def __init__(self, jobs_file: Path = JOBS_FILE) -> None:
        self._file = jobs_file
        self._jobs: dict[str, ScheduledJob] = {}  # keyed by job.id
        self._loaded = False

    # ── Load / save ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load jobs from disk. Call once at daemon startup."""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        if not self._file.exists():
            logger.info("No jobs file found at %s — starting with empty job list", self._file)
            self._loaded = True
            return
        try:
            data = json.loads(self._file.read_text())
            raw_jobs = data.get("jobs", [])
            self._jobs = {}
            for raw in raw_jobs:
                try:
                    job = ScheduledJob.from_dict(raw)
                    self._jobs[job.id] = job
                except Exception as e:
                    logger.warning("Skipping malformed job entry: %s — %s", raw, e)
            logger.info("Loaded %d scheduled job(s) from %s", len(self._jobs), self._file)
        except Exception as e:
            logger.warning("Failed to load jobs file %s — starting with empty list: %s", self._file, e)
        self._loaded = True

    def save(self) -> None:
        """Atomically write current job list to disk."""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": _SCHEMA_VERSION,
            "jobs": [j.to_dict() for j in self._jobs.values()],
        }
        tmp = self._file.with_name(f"{self._file.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._file)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        logger.debug("Saved %d scheduled job(s) to %s", len(self._jobs), self._file)

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def _assert_loaded(self) -> None:
        """Raise RuntimeError if load() has not been called yet."""
        if not self._loaded:
            raise RuntimeError(
                "JobStore.load() must be called before any CRUD operation. "
                "Did you forget to call load() at daemon startup?"
            )

    def add(self, job: ScheduledJob) -> ScheduledJob:
        """Add a new job and persist. Returns the saved job."""
        self._assert_loaded()
        self._jobs[job.id] = job
        self.save()
        logger.info("Scheduled job created: %s (watcher=%s, cron=%r)", job.id, job.watcher, job.cron)
        return job

    def update(self, job: ScheduledJob) -> None:
        """Update an existing job in place and persist."""
        self._assert_loaded()
        if job.id not in self._jobs:
            raise KeyError(f"Job {job.id!r} not found")
        self._jobs[job.id] = job
        self.save()

    def remove(self, job_id: str) -> bool:
        """Remove a job by ID. Returns True if found and removed."""
        self._assert_loaded()
        if job_id not in self._jobs:
            return False
        del self._jobs[job_id]
        self.save()
        logger.info("Scheduled job deleted: %s", job_id)
        return True

    def remove_expired_completed(self, ttl_days: int) -> int:
        """Remove completed jobs whose completed_at is older than ttl_days.

        If ttl_days == 0, removes all completed jobs immediately.
        Returns the number of jobs removed.
        """
        self._assert_loaded()
        if ttl_days < 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
        to_remove = []
        for job in self._jobs.values():
            if job.status != JobStatus.COMPLETED:
                continue
            if ttl_days == 0:
                to_remove.append(job.id)
            elif job.completed_at:
                try:
                    completed = datetime.fromisoformat(job.completed_at)
                    if completed < cutoff:
                        to_remove.append(job.id)
                except ValueError:
                    # Malformed completed_at — remove it
                    to_remove.append(job.id)
        for jid in to_remove:
            del self._jobs[jid]
        if to_remove:
            self.save()
            logger.info("Purged %d expired completed job(s)", len(to_remove))
        return len(to_remove)

    # ── Queries ───────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> ScheduledJob | None:
        """Return a job by ID, or None if not found."""
        self._assert_loaded()
        return self._jobs.get(job_id)

    def list_jobs(
        self,
        *,
        connector: str | None = None,
        include_completed: bool = False,
    ) -> list[ScheduledJob]:
        """Return jobs, optionally filtered by connector.

        By default only ACTIVE and PAUSED jobs are returned. Pass
        ``include_completed=True`` to also include COMPLETED jobs.
        """
        self._assert_loaded()
        jobs = list(self._jobs.values())
        if not include_completed:
            jobs = [j for j in jobs if j.status != JobStatus.COMPLETED]
        if connector:
            jobs = [j for j in jobs if j.connector == connector]
        return jobs

    def list_due(self) -> list[ScheduledJob]:
        """Return ACTIVE jobs whose next_run is at or before now (UTC)."""
        self._assert_loaded()
        now = datetime.now(UTC)
        due = []
        for job in self._jobs.values():
            if job.status != JobStatus.ACTIVE:
                continue
            if job.next_run is None:
                continue
            try:
                fire_at = datetime.fromisoformat(job.next_run)
                if fire_at <= now:
                    due.append(job)
            except ValueError:
                logger.warning("Job %s has malformed next_run %r — skipping", job.id, job.next_run)
        return due
