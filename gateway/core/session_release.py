"""One line for every discarded session (#143).

Whatever path lets go of a watcher's session — idle expiry, reclamation after
the bot is removed from a room, the operator's `reset` or `expire`, a
static-era record pruned at boot, a record no rule covers any more, a state
file for a connector that is no longer configured, a stored id abandoned at
provisioning because the backend identity or room changed — logs exactly one
`AUDIT` line through here, carrying the FULL session id. The rest of the log uses the
first eight characters; this is the line an operator greps for when a
conversation has to be found again.

Whether the backend still has the session depends on the backend: reclamation
asks `delete_session`, which OpenCode honours and Claude does not support, so
the id recovers a conversation only where the backend kept it (see #146).
"""

from __future__ import annotations

import logging


def log_session_released(
    log: logging.Logger,
    *,
    connector: str,
    room_id: str,
    watcher: str,
    agent: str,
    session_id: str,
    reason: str,
) -> None:
    """Log the one AUDIT line for a session being let go of."""
    log.warning(
        "AUDIT: session released — connector=%s room=%s watcher=%s agent=%s "
        "session=%s reason=%s",
        connector, room_id, watcher, agent or "-", session_id or "-", reason,
    )
