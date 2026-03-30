"""SessionMaps: shared mutable state between SessionManager, brokers, and processors.

Groups the four session→X maps into a single passable object to reduce
constructor parameter noise across the gateway.  All maps are live references
(dict instances) — mutating them in one component is immediately visible to
all others that hold the same SessionMaps instance.
"""

from __future__ import annotations

from types import MappingProxyType
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connector import Connector


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
        """Register the connector-routing context for a session."""
        self.room[session_id] = room_id
        self.connector[session_id] = connector

    def update_role(self, session_id: str, role: str) -> None:
        """Update the effective role for a session."""
        self.role[session_id] = role

    def update_permission_thread(self, session_id: str, thread_id: str | None) -> None:
        """Update the permission notification thread for a session."""
        self.permission_thread[session_id] = thread_id

    def remove_session(self, session_id: str) -> None:
        """Remove all routing context for a session."""
        self.room.pop(session_id, None)
        self.role.pop(session_id, None)
        self.permission_thread.pop(session_id, None)
        self.connector.pop(session_id, None)
