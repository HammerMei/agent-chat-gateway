"""Scheduling data types: ScheduledJob dataclass and JobStatus enum.

These types are the canonical definitions shared by JobStore, JobScheduler,
the CLI, and the control socket command handlers.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("agent-chat-gateway.schedule_types")


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
    id               : Unique job identifier (``acg-<8hex>``).
    watcher          : The watcher's derived handle (`<connector>:<room label>`).
                       NOT a config.yaml name — rule-derived watchers are not named
                       in config at all. It is the display and CLI identity, and it
                       is a pure function of (connector, room), so it changes when
                       the room is renamed. `room_id` is the identity.
    connector        : Connector name the watcher belongs to.
    room_id          : The platform room this job targets — the identity half, and
                       the only field here that never changes under the room's feet.
                       A fire resolves through it, so the job survives both a rename
                       (which moves the handle) and an `expire` (which deletes the
                       watcher record the handle used to resolve against).

                       Empty on a job written before schema version 2. Such a job
                       falls back to resolving by handle, exactly as it did before
                       the field existed, and `agent-chat-gateway schedule migrate`
                       fills it in. That is deliberately an operator step rather
                       than a lazy backfill at fire time: a handle only maps to the
                       right room while nobody has renamed it, and the operator is
                       the one who can choose a moment when that is true. A job
                       firing once a year would otherwise wait a year to migrate,
                       through a window in which anything could have happened.
    message          : Text injected directly into the agent session when fired.
    cron             : 5-field POSIX cron expression (e.g. ``"0 9 * * 1-5"``).
    timezone         : IANA timezone name used when interpreting the cron expression
                       (e.g. ``"Asia/Taipei"``, ``"America/New_York"``, ``"UTC"``).
    times            : Maximum number of runs. 0 = run forever.
    run_count        : Number of times the job has been fired successfully.
    status           : Current lifecycle status (see JobStatus).
    created_at       : ISO 8601 UTC timestamp when the job was created.
    next_run         : ISO 8601 UTC timestamp of the next scheduled fire time.
                       None for one-shot jobs that have been completed.
    last_run         : ISO 8601 UTC timestamp of the most recent *successful* fire
                       (injection accepted by the watcher).
    last_attempted_at: ISO 8601 UTC timestamp of the most recent fire attempt,
                       regardless of whether injection succeeded.  Set on every
                       ``_fire_once`` call — even on failure — so that the
                       catch-up anchor on restart reflects "what time we last tried"
                       rather than "what time we last succeeded", preventing replay
                       of fire slots where injection already failed.
                       None when the job has never been attempted.
    completed_at     : ISO 8601 UTC timestamp when status transitioned to COMPLETED.
                       None until the job completes.
    """

    id: str = field(default_factory=_new_job_id)
    watcher: str = ""
    connector: str = ""
    room_id: str = ""
    message: str = ""
    cron: str = ""
    timezone: str = "UTC"
    times: int = 0                          # 0 = forever
    run_count: int = 0
    status: JobStatus = JobStatus.ACTIVE
    created_at: str = ""                    # ISO 8601 UTC
    next_run: str | None = None             # ISO 8601 UTC
    last_run: str | None = None             # ISO 8601 UTC; only set on successful injection
    last_attempted_at: str | None = None    # ISO 8601 UTC; set on every fire attempt (success or failure)
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
            "room_id": self.room_id,
            "message": self.message,
            "cron": self.cron,
            "timezone": self.timezone,
            "times": self.times,
            "run_count": self.run_count,
            "status": self.status.value,
            "created_at": self.created_at,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "last_attempted_at": self.last_attempted_at,
            "completed_at": self.completed_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "ScheduledJob":
        """Deserialize from a JSON-compatible dict. Unknown fields are ignored."""
        job_id = data.get("id")
        if not job_id:
            job_id = _new_job_id()
            logger.warning(
                "ScheduledJob record missing 'id' field — assigned new id %r. "
                "Check jobs.json for manually edited or corrupted entries.",
                job_id,
            )
        return ScheduledJob(
            id=job_id,
            watcher=data.get("watcher", ""),
            connector=data.get("connector", ""),
            # Absent on a schema-1 file. `""` is the honest reading — it means
            # "this job has no recorded room", which is exactly what
            # `schedule migrate` is for — not a value to invent here.
            room_id=data.get("room_id", ""),
            message=data.get("message", ""),
            cron=data.get("cron", ""),
            timezone=data.get("timezone", "UTC"),
            times=data.get("times", 0),
            run_count=data.get("run_count", 0),
            status=JobStatus(data.get("status", JobStatus.ACTIVE.value)),
            created_at=data.get("created_at", ""),
            next_run=data.get("next_run"),
            last_run=data.get("last_run"),
            last_attempted_at=data.get("last_attempted_at"),
            completed_at=data.get("completed_at"),
        )
