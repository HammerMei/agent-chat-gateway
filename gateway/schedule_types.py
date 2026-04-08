"""Scheduling data types: ScheduledJob dataclass and JobStatus enum.

These types are the canonical definitions shared by JobStore, JobScheduler,
the CLI, and the control socket command handlers.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import Enum


class JobStatus(str, Enum):
    """Lifecycle status of a scheduled job.

    ACTIVE    — scheduler fires this job on schedule.
    PAUSED    — user explicitly paused via ``acg schedule pause``; scheduler skips it.
    COMPLETED — all runs exhausted (``run_count >= times > 0``); pending TTL purge.
                Forever jobs (``times == 0``) never transition to COMPLETED automatically.
    """
    ACTIVE    = "active"
    PAUSED    = "paused"
    COMPLETED = "completed"


def _new_job_id() -> str:
    """Generate an 8-char hex job ID, e.g. 'acg-a3f2b1c0'."""
    return f"acg-{secrets.token_hex(4)}"


@dataclass
class ScheduledJob:
    """A single scheduled job persisted in jobs.json.

    Fields
    ------
    id          : Unique job identifier (``acg-<8hex>``).
    watcher     : Watcher name as defined in config.yaml.
    connector   : Connector name the watcher belongs to.
    message     : Text injected directly into the agent session when fired.
    cron        : 5-field POSIX cron expression (e.g. ``"0 9 * * 1-5"``).
    timezone    : IANA timezone name used when interpreting the cron expression
                  (e.g. ``"Asia/Taipei"``, ``"America/New_York"``, ``"UTC"``).
    times       : Maximum number of runs. 0 = run forever.
    run_count   : Number of times the job has been fired successfully.
    status      : Current lifecycle status (see JobStatus).
    created_at  : ISO 8601 UTC timestamp when the job was created.
    next_run    : ISO 8601 UTC timestamp of the next scheduled fire time.
                  None for one-shot jobs that have been completed.
    last_run    : ISO 8601 UTC timestamp of the most recent successful fire.
    completed_at: ISO 8601 UTC timestamp when status transitioned to COMPLETED.
                  None until the job completes.
    """

    id: str = field(default_factory=_new_job_id)
    watcher: str = ""
    connector: str = ""
    message: str = ""
    cron: str = ""
    timezone: str = "UTC"
    times: int = 0                          # 0 = forever
    run_count: int = 0
    status: JobStatus = JobStatus.ACTIVE
    created_at: str = ""                    # ISO 8601 UTC
    next_run: str | None = None             # ISO 8601 UTC
    last_run: str | None = None             # ISO 8601 UTC
    completed_at: str | None = None         # ISO 8601 UTC; set when → COMPLETED

    def is_active(self) -> bool:
        """True if the scheduler should fire this job."""
        return self.status == JobStatus.ACTIVE

    def remaining_runs(self) -> int | None:
        """Remaining runs until completion. None if forever (times == 0)."""
        if self.times == 0:
            return None
        return max(0, self.times - self.run_count)

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            "id": self.id,
            "watcher": self.watcher,
            "connector": self.connector,
            "message": self.message,
            "cron": self.cron,
            "timezone": self.timezone,
            "times": self.times,
            "run_count": self.run_count,
            "status": self.status.value,
            "created_at": self.created_at,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "completed_at": self.completed_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "ScheduledJob":
        """Deserialize from a JSON-compatible dict. Unknown fields are ignored."""
        return ScheduledJob(
            id=data.get("id", _new_job_id()),
            watcher=data.get("watcher", ""),
            connector=data.get("connector", ""),
            message=data.get("message", ""),
            cron=data.get("cron", ""),
            timezone=data.get("timezone", "UTC"),
            times=data.get("times", 0),
            run_count=data.get("run_count", 0),
            status=JobStatus(data.get("status", JobStatus.ACTIVE.value)),
            created_at=data.get("created_at", ""),
            next_run=data.get("next_run"),
            last_run=data.get("last_run"),
            completed_at=data.get("completed_at"),
        )
