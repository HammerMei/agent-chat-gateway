"""SessionMaps: shared mutable state between SessionManager, brokers, and processors.

Groups the four session→X maps into a single passable object to reduce
constructor parameter noise across the gateway.  All maps are live references
(dict instances) — mutating them in one component is immediately visible to
all others that hold the same SessionMaps instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connector import Connector


class SessionAlreadyBoundError(RuntimeError):
    """A second room tried to claim a session that is already serving one (§4.1)."""


@dataclass
class SessionMaps:
    """Shared live-reference maps between SessionManagers, brokers, and processors.

    All fields are mutable dicts.  Components that need to read or write
    session routing state share the same SessionMaps instance.

    Attributes:
        room: session_id → room_id (where to post notifications)
        role: session_id → "owner"|"guest" (for policy enforcement)
        permission_thread: session_id → RC thread ID or None (where to post 🔐 notifications)
        connector: session_id → Connector (which RC server to use)
    """

    room: dict[str, str] = field(default_factory=dict)
    # session_id → (room_id, connector). The reverse index that makes "one session, one
    # room" checkable: `room` above is single-valued, so it cannot detect a second
    # binding, only absorb it. Private because it is an invariant, not routing state —
    # nothing outside should read or write it.
    _bound: "dict[str, tuple[str, Connector]]" = field(default_factory=dict)
    role: dict[str, str] = field(default_factory=dict)
    permission_thread: dict[str, str | None] = field(default_factory=dict)
    connector: "dict[str, Connector]" = field(default_factory=dict)

    @property
    def room_view(self):
        """Read-only live view of session → room routing."""
        return MappingProxyType(self.room)

    @property
    def role_view(self):
        """Read-only live view of session → role routing."""
        return MappingProxyType(self.role)

    @property
    def permission_thread_view(self):
        """Read-only live view of session → permission thread routing."""
        return MappingProxyType(self.permission_thread)

    @property
    def connector_view(self):
        """Read-only live view of session → connector routing."""
        return MappingProxyType(self.connector)

    def get_room(self, session_id: str) -> str:
        return self.room.get(session_id, "")

    def get_role(self, session_id: str, default: str = "guest") -> str:
        return self.role.get(session_id, default)

    def has_role(self, session_id: str) -> bool:
        return session_id in self.role

    def get_permission_thread(self, session_id: str) -> str | None:
        return self.permission_thread.get(session_id)

    def get_connector(self, session_id: str) -> "Connector | None":
        return self.connector.get(session_id)

    def bind_session(
        self,
        session_id: str,
        room_id: str,
        connector: "Connector",
    ) -> None:
        """Register the connector-routing context for a session. One session, one room.

        **A reused session is a cross-room data leak** (§4.1). A session carries the room
        in three places — the durable identity header re-supplied every turn, the
        transcript with that room's history and every prior `[#room | from: …]` prefix,
        and this map, which decides where a permission prompt for its tool calls
        appears. Binding a second room to it does not split the session; it points all of
        that at the newer room.

        The write used to be unconditional, so the second binding overwrote the first
        with no error and no log — a healthy watcher quietly losing its routing. It now
        fails closed, and *before* touching anything, so a refused binding leaves the
        incumbent exactly as it was. That matters more than it sounds: the rollback in
        `_start_watcher` calls `remove_session` keyed by session id alone, so a late
        refusal would have had the loser tearing down the winner's routing.

        **Keyed by `session_id` alone, and the connector is part of what must match.**
        An earlier version keyed the reservation on `(backend_identity, session_id)`, on
        the reasoning that ids are only unique within the store that issued them — but
        every map here, and every consumer of them, is keyed by the bare `session_id`.
        Permitting two bindings the maps cannot represent just moved the silent overwrite
        one level down. Two backends emitting one id string is vanishingly unlikely and
        recoverable (clear that record's session_id); a corrupted routing table is
        neither.

        The connector is compared as well as the room: two connectors can resolve
        different watched rooms to the same platform room id, and the second bind would
        otherwise pass the room check while overwriting `connector[session_id]` — sending
        that session's permission prompts to the wrong server.

        Rebinding the same room on the same connector is allowed: a watcher restarting
        re-binds what it already held, and refusing that would make a reset unrecoverable.
        """
        bound = self._bound.get(session_id)
        if bound is not None:
            bound_room, bound_connector = bound
            if bound_room != room_id or bound_connector is not connector:
                where = (
                    f"room '{bound_room}'"
                    if bound_room != room_id
                    else f"room '{bound_room}' on another connector"
                )
                raise SessionAlreadyBoundError(
                    f"Session {session_id[:8]} is already bound to {where} and cannot "
                    f"also serve room '{room_id}'. A session carries its room in its "
                    f"transcript, its identity header and its permission routing, so "
                    f"reusing one across rooms leaks each room's conversation into the "
                    f"other. This normally means a hand-edited or corrupted state file: "
                    f"give the second watcher its own session by clearing that record's "
                    f"session_id."
                )
        self._bound[session_id] = (room_id, connector)
        self.room[session_id] = room_id
        self.connector[session_id] = connector

    def update_role(self, session_id: str, role: str) -> None:
        """Update the effective role for a session."""
        self.role[session_id] = role

    def update_permission_thread(self, session_id: str, thread_id: str | None) -> None:
        """Update the permission notification thread for a session."""
        self.permission_thread[session_id] = thread_id

    def remove_session(self, session_id: str) -> None:
        """Remove all routing context for a session.

        Clears the uniqueness reservation too, or a watcher could never rebind after a
        reset — the check would refuse it against its own stale entry.
        """
        self._bound.pop(session_id, None)
        self.room.pop(session_id, None)
        self.role.pop(session_id, None)
        self.permission_thread.pop(session_id, None)
        self.connector.pop(session_id, None)
