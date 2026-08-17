"""The lifecycle sweep (§2.5): the timer that notices a room has gone quiet.

Two legs, one visit per record per pass: `active → idle` via
`WatcherLifecycle.drop_idle`, and `idle → expired` — the destructive one —
via `WatcherLifecycle.expire_idle`.

Three rules, all owner rulings recorded in §2.5:

* **A sweep advances a watcher by at most one state.** Deriving the final
  state from the timestamps and acting on it would take a watcher that was
  busy right up to a shutdown from `active` straight to `expired` in one pass.
  Structurally enforced: each record is visited once per pass and the leg is
  chosen by its state *at the visit* — a record this pass idles has already
  had its visit, and the expiry origin is `dropped_at`, which that idle just
  stamped to this pass's own instant.
* **Paused is never reclaimed by a timer** (§4.4) — not idled, not expired.
* **The sweep reads what the record carries** — the frozen rule, never current
  `config.yaml`. `past_idle_ttl`/`past_expire_ttl` (state.py) own that
  arithmetic; boot calls the same idle function (one function, two callers).

And one exemption: **a room with a pending scheduled job is exempt from
expiry, not from idling.** Idling it is harmless — the job's injection wakes
it through `get_or_create` — but expiry deletes the record the recreation
reads from, leaving the job pointing at nothing.

Mechanically: `run_once` is the whole sweep and the only thing tests need —
they inject `now` and never sleep. The free-running loop is a thin shell over
it, because a free-running asyncio loop is where the #110 hang lesson lived.
The first pass runs one interval after `start()`, and `start()` is called
after the startup replay completes — ordering that §2.5 makes non-optional for
the expiry leg, honored structurally from the first day the loop exists.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from .state import past_expire_ttl, past_idle_ttl

if TYPE_CHECKING:
    from .watcher_lifecycle import WatcherLifecycle

logger = logging.getLogger("agent-chat-gateway.core.lifecycle_sweep")

# TTLs are whole days, so hourly resolution is two orders of magnitude finer
# than anything it measures.
_SWEEP_INTERVAL_SECONDS = 3600.0


def _local_now() -> datetime:
    return datetime.now().astimezone()


class LifecycleSweep:
    """Periodic evaluation of every record's lifecycle clocks.

    The decision authority is deliberately split: this class decides *which
    records look due* (cheap reads, no lock), and `drop_idle` re-checks every
    condition under the per-watcher lock before acting — between the look and
    the lock an enqueue can advance the clock, an operator can pause, a turn
    can start.
    """

    def __init__(
        self,
        lifecycle: "WatcherLifecycle",
        *,
        now: Callable[[], datetime] | None = None,
        interval_seconds: float = _SWEEP_INTERVAL_SECONDS,
        pending_jobs: Callable[[str], bool] | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._now = now or _local_now
        self._interval = interval_seconds
        # Answers "does this watcher have a pending scheduled job" — the expiry
        # exemption's oracle, injected because the job store lives with the
        # scheduler, not the lifecycle. None means no scheduler: nothing to
        # exempt.
        self._pending_jobs = pending_jobs
        self._task: asyncio.Task | None = None

    async def run_once(self) -> list[str]:
        """One pass over every record; returns the watchers it transitioned
        (idled or expired — each at most one step, per §2.5).

        One `now` for the whole pass — `drop_idle` stamps `dropped_at` from the
        same instant the TTLs were judged against, so a pass is a single
        moment, not a smear across its own awaits.
        """
        now = self._now()
        transitioned: list[str] = []
        # A snapshot, because the dict mutates under the awaits below — a
        # concurrent creation registers records, a wake re-registers processors.
        for record in list(self._lifecycle.states().values()):
            if record.paused:
                # §4.4: never reclaimed by a timer — not idled, not expired.
                continue
            if record.dropped_at:
                # The expiry leg — the destructive one. The origin is
                # `dropped_at`: a record this pass idled had its visit already,
                # and even a re-read would find a stamp aged zero.
                if not past_expire_ttl(record, now):
                    continue
                if self._pending_jobs is not None and self._pending_jobs(
                        record.watcher_name):
                    # Exempt from expiry, never from idling: the job's
                    # injection wakes an idle room, but it cannot wake a
                    # deleted record.
                    logger.debug(
                        "Watcher '%s' is past its expiry TTL but has a pending "
                        "scheduled job — not expiring", record.watcher_name,
                    )
                    continue
                if await self._lifecycle.expire_idle(record.watcher_name, now=now):
                    transitioned.append(record.watcher_name)
                continue
            # The idle leg.
            if not past_idle_ttl(record, now):
                continue
            # drop_idle answers False for everything this loop cannot cheaply
            # see — not resident (boot owns failed records), a turn in flight,
            # an approval an operator is reading — and re-checks the TTL under
            # the lock.
            if await self._lifecycle.drop_idle(record.watcher_name, now=now):
                transitioned.append(record.watcher_name)
        if transitioned:
            logger.info("Lifecycle sweep transitioned %d watcher(s): %s",
                        len(transitioned), ", ".join(sorted(transitioned)))
        return transitioned

    def start(self) -> None:
        """Start the free-running loop. Call after the startup replay (§2.5)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="lifecycle-sweep")

    async def stop(self) -> None:
        """Stop the loop. Called before `stop_all`, so a pass cannot overlap
        the shutdown's own teardown of the processors it is judging."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        # Sleep first: start() runs right after the startup replay, and a
        # zeroth pass at that moment would judge records against clocks the
        # replay's recreations are still stamping.
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.run_once()
            except Exception:
                # The sweep must outlive one bad pass — a transient connector
                # error during a drop is not a reason to stop noticing idle
                # rooms forever.
                logger.exception("Lifecycle sweep pass failed; next pass in %.0fs",
                                 self._interval)
