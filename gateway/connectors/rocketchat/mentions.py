"""Rocket.Chat mention helpers shared by normalize and prompt routing."""

from __future__ import annotations

ROOM_WIDE_MENTIONS = frozenset({"all"})


def is_room_wide_mention(username: str) -> bool:
    """Return True when a Rocket.Chat mention targets the whole room."""
    return username in ROOM_WIDE_MENTIONS
