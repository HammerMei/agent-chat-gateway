"""The frames a room accumulates while its first message is being resolved.

One copy of the buffer rule for both connectors (§2.2, §2.7 step 3). While a
room's routing episode is open — classification, rule match, creation — later
frames for that room must go *somewhere*: dropping them was the old single-
flight's answer, and it deterministically lost every message that arrived
during a creation. They are buffered here instead, bounded, deduplicated by
message id, and drained in arrival order once the episode ends.

The dedup set matters more than it looks (§2.2 outcome 6): a brand-new room's
subscription starts with an empty seen-ids window and an empty watermark, so a
duplicate that rides the buffer would be *delivered twice* after creation —
this set is the only guard that exists during the episode.

The retry helper lives here for the same reason the buffer does: both
connectors run the same bounded-backoff rule around a resolution stage, and a
rule stated twice will be applied once.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable

# One visible answer for "messages are arriving faster than the room can be
# created" (§2.2 outcome 5, §2.7 step 7). Shared so the operator reads one
# message regardless of which side of the cap fired.
STARTING_UP_NOTICE = (
    "⏳ Starting up — this room's agent is still being set up. "
    "Recent messages may need to be resent."
)


class PendingRoute:
    """Bookkeeping for one open routing episode.

    `add` answers one of three verdicts, and the caller acts on the verdict
    rather than inspecting state:

    * ``"buffered"`` — the frame waits for the episode's outcome.
    * ``"duplicate"`` — the same message id is already held; the copy is
      discarded and the reservation is not disturbed (§2.2 outcome 6).
    * ``"full"`` — the bounded buffer is at capacity; the frame is dropped and
      the caller owes the room a visible notice (§2.2 outcome 5).

    A frame with no id cannot be deduplicated and is buffered as-is — losing a
    real message over a missing id would be the worse trade.
    """

    def __init__(self, capacity: int) -> None:
        self._frames: deque[Any] = deque()
        self._seen: set[str] = set()
        self._capacity = capacity
        # One "starting up" notice per episode, however many frames overflow —
        # the room is told once, not once per message.
        self.notice_posted = False

    def add(self, frame_id: str, frame: Any) -> str:
        if frame_id and frame_id in self._seen:
            return "duplicate"
        if len(self._frames) >= self._capacity:
            return "full"
        if frame_id:
            self._seen.add(frame_id)
        self._frames.append(frame)
        return "buffered"

    def drain(self) -> list[Any]:
        """Every buffered frame in arrival order, emptying the buffer."""
        frames = list(self._frames)
        self._frames.clear()
        return frames

    def __len__(self) -> int:
        return len(self._frames)


async def route_attempts(
    fn: Callable[[], Awaitable[None]],
    *,
    retry_on: type[BaseException],
    delays: tuple[float, ...],
    logger: logging.Logger,
    label: str,
) -> bool:
    """Run one resolution stage with bounded backoff; True means it completed.

    False means the stage failed every attempt and the room **parks**: the
    episode ends, its buffer is dropped, and the room is offered again by its
    next message.

    What happens to the dropped interval depends on whether the room has a
    persisted record, and the mechanism is worth naming because an earlier
    version of this docstring claimed the recovery without one:

    * **A room with a record** recovers it. Its watermark still sits below the
      dropped frames, and the recreation that its next message triggers
      replays from that watermark before delivering anything new
      (`WatcherManager._recreate`). Nothing else would: the reconnect replay
      iterates *tracked* rooms, and a parked room is untracked.
    * **A first-ever room** does not. There is no watermark to replay from,
      which is the accepted residual §2.2 records — making it recoverable
      would cost a disk write per inbound message, including for every message
      the rules are about to discard.

    Only ``retry_on`` is retried; anything else propagates. How much that
    promise is worth depends on what the caller passes, and the two stages
    differ deliberately:

    * **Classification** names ``ClassificationUnavailable``, so a bug in the
      resolver surfaces as a bug rather than spending three backoffs
      pretending to be weather.
    * **The offer** names ``Exception``, because the manager deliberately lets
      every failed start propagate (§2.2 outcome 4) and there is no typed
      family covering "the backend was down". The cost is that a genuine bug
      in creation is retried before it parks — visible in the log either way.
      The *final* conditions that used to be caught by that breadth are
      handled at the call site instead, which is where they are recognisable.
    """
    for attempt in range(len(delays) + 1):
        try:
            await fn()
            return True
        except retry_on as e:
            if attempt >= len(delays):
                logger.warning(
                    "%s failed %d times — parking until the room's next message "
                    "(last error: %s)",
                    label, attempt + 1, e,
                )
                return False
            logger.debug(
                "%s failed (attempt %d: %s) — retrying in %.1fs",
                label, attempt + 1, e, delays[attempt],
            )
            await asyncio.sleep(delays[attempt])
    return False  # unreachable; the loop always returns
