"""Inbound message policy helpers for Mattermost.

``apply_thread_policy`` is platform-agnostic (see gateway.core.thread_policy)
and lives in core; Mattermost's ``root_id`` maps onto the same generic
``msg.thread_id`` concept Rocket.Chat's ``tmid`` does, so no
Mattermost-specific logic is needed here. This module re-exports it for a
consistent import path alongside the other connector-local modules.
"""

from __future__ import annotations

from ...core.thread_policy import (
    apply_thread_policy,  # noqa: F401 — re-export for connector consumers
)
