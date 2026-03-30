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

    def save(self, states: dict[str, WatcherState]) -> None:
        """Pull live watermarks from connector and persist all state records.

        Watermark reads are best-effort: if the connector is partially torn
        down (e.g. during shutdown), a failure for one room is logged and
        skipped rather than aborting the entire save.
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
        save_state(self._state_name, list(states.values()))
