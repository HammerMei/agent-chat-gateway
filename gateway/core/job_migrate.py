"""Operator-run migrations for `jobs.json`.

`agent-chat-gateway schedule migrate` runs these. Deliberately not automatic,
and not lazy at fire time: the 1→2 step reads each job's watcher HANDLE to find
its room, and a handle only names the right room while nobody has renamed it.
The operator is the one who can choose a moment when that holds — right after an
upgrade, before touching room names. A job firing once a year would otherwise
wait a year to migrate, through a window in which anything could have happened
(owner, 2026-09-01).

**Two properties, and both are needed.** Version awareness decides WHICH steps
run, so a deployment jumping 1 → 3 gets both of them and one jumping 2 → 3 gets
only the second. Idempotence means each step is safe to run again anyway, so a
wrong version guess cannot corrupt anything. Either alone is insufficient:
without the version, a 1 → 3 upgrade cannot know it owes two steps; without
idempotence, an interrupted run leaves the file in a state no version describes.

**Nothing is guessed.** A job whose room cannot be resolved is reported and left
exactly as it was. The command's output is the record of what happened, which is
the whole advantage over doing this invisibly at fire time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ..schedule_types import ScheduledJob
from .job_store import _SCHEMA_VERSION, JobStore

logger = logging.getLogger("agent-chat-gateway.core.job_migrate")


@dataclass
class JobOutcome:
    """What happened to one job, in the operator's terms."""

    job_id: str
    watcher: str
    changed: bool
    detail: str


@dataclass
class MigrationReport:
    from_version: int
    to_version: int
    steps: list[str] = field(default_factory=list)
    outcomes: list[JobOutcome] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return sum(1 for o in self.outcomes if o.changed)

    @property
    def unresolved(self) -> list[JobOutcome]:
        return [o for o in self.outcomes if not o.changed]

    def to_dict(self) -> dict:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "steps": list(self.steps),
            "changed": self.changed,
            "outcomes": [
                {"job_id": o.job_id, "watcher": o.watcher,
                 "changed": o.changed, "detail": o.detail}
                for o in self.outcomes
            ],
        }


def split_handle(watcher: str) -> tuple[str, str]:
    """`<connector>:<room label>` → the two halves.

    The FIRST `:` is the boundary: a connector name may not contain one (config
    load refuses it) and the label encoder percent-encodes it out of room and
    user names, so this cannot be fooled by a room called `dm:alice`
    (`watcher_manager.watcher_label`).
    """
    connector, sep, label = watcher.partition(":")
    return (connector, label) if sep else ("", watcher)


def room_name_for_label(label: str) -> str | None:
    """The name to ask a connector to resolve, or `None` if the label is not one.

    * a channel label IS the room name;
    * `dm:alice` asks for `@alice`, the spelling `resolve_room` documents;
    * `gdm:<digest>` is a digest of the room id, not a name — nothing can resolve
      it, and inventing something would be a guess. Reported instead.
    """
    if label.startswith("gdm:"):
        return None
    if label.startswith("dm:"):
        counterpart = label[len("dm:"):]
        return f"@{counterpart}" if counterpart else None
    return label or None


async def _resolve_room_id(entry, job: ScheduledJob) -> tuple[str, str]:
    """`(room_id, detail)` for one job — `("", why not)` when it cannot be found.

    Two sources, strongest first:

    1. **The live record for this handle.** Authoritative: it holds the room id
       the watcher is actually bound to.
    2. **Asking the connector to resolve the label as a name.** Correct exactly
       while the handle still names the room it named when the job was created,
       which is the assumption the operator upholds by running this before
       renaming anything.
    """
    record = entry.session_manager.get_watcher_state(job.watcher)
    if record is not None and record.room_id:
        return record.room_id, "from its watcher's record"

    _connector_name, label = split_handle(job.watcher)
    room_name = room_name_for_label(label)
    if room_name is None:
        return "", (
            "a group DM's label is a digest of its room id, not a name, and this "
            "watcher has no record to read the id from — delete this job and "
            "recreate it against the current watcher"
        )
    try:
        room = await entry.connector.resolve_room(room_name)
    except Exception as exc:
        return "", f"the connector could not resolve {room_name!r}: {exc}"
    if room is None or not getattr(room, "id", ""):
        return "", f"the connector knows no room named {room_name!r}"
    return room.id, f"resolved {room_name!r}"


async def _migrate_1_to_2(store: JobStore, entries) -> list[JobOutcome]:
    """Record each job's room id.

    Idempotent: a job that already has one is skipped, so a re-run touches
    nothing. `connector` is filled in at the same time when it is missing —
    `_get_sm_for_watcher` needs it, and a job with neither field cannot be
    routed to a manager at all.
    """
    by_name = {e.name: e for e in entries}
    outcomes: list[JobOutcome] = []

    # `list_jobs()` already excludes COMPLETED, which is the same set — a job
    # that has finished has nothing left to fire, so nothing to migrate.
    for job in store.list_jobs():
        if job.room_id:
            outcomes.append(JobOutcome(
                job.id, job.watcher, False, "already has a room id"))
            continue

        connector_name, _label = split_handle(job.watcher)
        entry = by_name.get(job.connector) or by_name.get(connector_name)
        if entry is None:
            outcomes.append(JobOutcome(
                job.id, job.watcher, False,
                f"no configured connector named {job.connector or connector_name!r}",
            ))
            continue

        room_id, detail = await _resolve_room_id(entry, job)
        if not room_id:
            outcomes.append(JobOutcome(job.id, job.watcher, False, detail))
            continue

        job.room_id = room_id
        if not job.connector:
            job.connector = entry.name
        store.update(job)
        outcomes.append(JobOutcome(
            job.id, job.watcher, True, f"room {room_id} ({detail})"))

    return outcomes


# (from_version, to_version, description, step)
_MIGRATIONS: list[tuple[int, int, str, Callable]] = [
    (1, 2, "record each job's room id so it survives a rename or an expire",
     _migrate_1_to_2),
]


async def migrate(store: JobStore, entries) -> MigrationReport:
    """Run every migration this file still owes, in order.

    Raises `ValueError` when the file is NEWER than this code: there is nothing
    safe to do, and saving would drop the fields this version does not know.
    """
    from_version = store.file_version
    if from_version > _SCHEMA_VERSION:
        raise ValueError(
            f"jobs.json declares schema version {from_version}, but this ACG "
            f"understands {_SCHEMA_VERSION}. It was written by a newer version — "
            f"upgrade ACG rather than migrating down."
        )

    report = MigrationReport(from_version=from_version, to_version=_SCHEMA_VERSION)
    if from_version == _SCHEMA_VERSION:
        return report

    for start, end, description, step in _MIGRATIONS:
        if start < from_version:
            continue  # already applied by an earlier upgrade
        report.steps.append(f"{start} → {end}: {description}")
        report.outcomes.extend(await step(store, entries))

    # Stamped last, and only after every step has run: an interrupted migration
    # leaves the version where it was, so a re-run repeats the steps rather than
    # skipping them — which is safe precisely because each one is idempotent.
    store.stamp_version(_SCHEMA_VERSION)
    logger.info(
        "jobs.json migrated %d → %d (%d job(s) changed)",
        from_version, _SCHEMA_VERSION, report.changed,
    )
    return report
