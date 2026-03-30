"""Inbound message policy helpers for Rocket.Chat.

Extracted from connector._on_raw_ddp_message() so the connector stays focused
on transport and normalization.  Policy decisions (thread routing, permission
thread targeting) live here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.connector import IncomingMessage

if TYPE_CHECKING:
    from .config import RocketChatConfig


def apply_thread_policy(msg: IncomingMessage, config: "RocketChatConfig") -> None:
    """Apply thread and permission-thread policies to a normalized message.

    Mutates ``msg`` in place:

    1. **Proactive-thread**: if ``reply_in_thread`` is enabled and the message
       is top-level (no ``tmid``), promote it to a new thread anchored at the
       triggering message's ``_id``.

    2. **Permission-thread**: compute where 🔐 permission notifications should
       be posted and store the result in ``msg.extra_context``.

    This function is pure policy — it does not touch the transport layer.
    """
    # Proactive-thread: reply in thread when configured
    if msg.thread_id is None and config.reply_in_thread:
        msg.thread_id = msg.id  # anchor: start a new thread on this message

    # Permission-thread: decide where permission notifications land
    if msg.thread_id is not None:
        permission_thread_id: str | None = msg.thread_id
    elif config.permission_reply_in_thread:
        permission_thread_id = msg.id
    else:
        permission_thread_id = None

    if permission_thread_id is not None:
        msg.extra_context["permission_thread_id"] = permission_thread_id
