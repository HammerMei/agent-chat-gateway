"""StateStore: thin wrapper around WatcherState persistence.

Encapsulates load/save logic and watermark pulling from the connector,
keeping SessionManager free of persistence details.
"""

from __future__ import annotations

import logging

from .connector import Connector
from .state import WatcherState, load_state, save_state

logger = logging.getLogger("agent-chat-gateway.core.state_store")


class StateStore:
    """Loads, saves, and manages WatcherState records on disk.

    Pulls live watermarks from the connector before serializing so that
    the persisted timestamps are always up-to-date.
    """

    def __init__(self, state_name: str, connector: Connector) -> None:
        self._state_name = state_name
        self._connector = connector

    def load(self) -> dict[str, WatcherState]:
        """Load persisted state records, keyed by watcher_name."""
        return {ws.watcher_name: ws for ws in load_state(self._state_name)}

    def save(
        self, states: dict[str, WatcherState], *, prune: set[str] | None = None
    ) -> None:
        """Merge ``states`` into the persisted records and write the result.

        **This merges rather than replaces.**  ``save_state`` rewrites the whole
        file, so passing it only the caller's dict silently discards every
        record the caller does not happen to hold — and callers routinely hold a
        subset.  ``sync_watchers`` seeds its map only from *configured* watchers,
        so a watcher skipped because its agent was unavailable (a fail-closed
        guard, not a removal) or one whose start raised would have its session
        id, watermark and paused flag erased.  Both are silent, and the session
        id is unrecoverable.

        Merging inverts that failure mode: the worst case becomes a record that
        outlives its config, which ``config_validate`` already warns about,
        instead of a session that cannot be resumed.

        Removal is therefore explicit.  Pass ``prune`` to drop records
        deliberately — currently only ``sync_watchers``, for watchers no longer
        in ``config.yaml``.

        Read-modify-write is safe here because this method is synchronous: it
        contains no ``await``, so no other coroutine can interleave between the
        read and the write.  Keep it that way.

        Watermark reads are best-effort — if the connector is partially torn
        down (e.g. during shutdown), a failure for one room is logged and
        skipped rather than aborting the save.  Only the live records are
        polled; a record read back from disk has no connector-side room state to
        consult.
        """
        for ws in states.values():
            if ws.room_id:
                try:
                    live_ts = self._connector.get_last_processed_ts(ws.room_id)
                    if live_ts:
                        ws.last_processed_ts = live_ts
                except Exception as e:
                    logger.warning(
                        "Could not read live watermark for room '%s': %s — "
                        "persisting last known value instead",
                        ws.room_id, e,
                    )

        # Start from disk so records this process never touched survive.  A
        # corrupt or missing file loads as empty (see load_state), which
        # degrades to the old replace behaviour rather than raising.
        merged = self.load()
        merged.update(states)

        for name in prune or ():
            if merged.pop(name, None) is not None:
                logger.info("Pruned persisted state for watcher '%s'", name)

        save_state(self._state_name, list(merged.values()))
