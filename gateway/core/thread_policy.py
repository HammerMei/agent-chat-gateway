"""Inbound message thread/permission-thread policy — shared across connectors.

Extracted from the Rocket.Chat connector's ``_on_raw_ddp_message()`` so the
connector stays focused on transport and normalization.  The logic only
touches generic ``IncomingMessage`` fields and duck-types on two config
attributes (``reply_in_thread``, ``permission_reply_in_thread``), so it is
platform-agnostic and shared by every connector that supports threading
(e.g. Rocket.Chat's ``tmid``, Mattermost's ``root_id`` — both map onto the
same generic ``msg.thread_id`` concept before this function ever runs).

``gateway.connectors.rocketchat.policy`` re-exports ``apply_thread_policy``
for backward compatibility.
"""

from __future__ import annotations

from typing import Protocol

from .connector import IncomingMessage


class ThreadPolicyConfig(Protocol):
    """Structural type for any connector config used by apply_thread_policy."""

    reply_in_thread: bool
    permission_reply_in_thread: bool


def apply_thread_policy(msg: IncomingMessage, config: ThreadPolicyConfig) -> None:
    """Apply thread and permission-thread policies to a normalized message.

    Mutates ``msg`` in place:

    1. **Proactive-thread**: if ``reply_in_thread`` is enabled and the message
       is top-level (no thread id), promote it to a new thread anchored at the
       triggering message's id.

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
