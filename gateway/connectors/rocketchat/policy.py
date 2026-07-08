"""Inbound message policy helpers for Rocket.Chat.

``apply_thread_policy`` used to live here, but it only touches generic
``IncomingMessage`` fields and duck-types on config attributes shared by every
threading-capable connector, so it now lives in
``gateway.core.thread_policy`` and is shared with other connectors (e.g.
Mattermost).  This module re-exports it so existing imports keep working
unchanged.
"""

from __future__ import annotations

from ...core.thread_policy import (
    apply_thread_policy,  # noqa: F401 — re-export for connector consumers
)
