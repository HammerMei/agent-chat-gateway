"""Who may start a turn — one answer, both connectors.

Extracted when Rocket.Chat's routing path needed the same check Mattermost's already made.
A second copy of an allow-list is one copy too many: the two would answer differently the
first time one of them learned about a new exemption.

Duck-typed on the connector config rather than importing either connector's config class,
which would make `gateway.core` depend on the packages that depend on it.
"""

from __future__ import annotations


def sender_allowed(config, sender_username: str) -> bool:
    """Whether this sender may start a turn at all — synchronous, no room metadata.

    Design §2.7 step 1 places the sender allow-list among the cheap rejects, above the
    room-state lookup, for a reason that only shows up on the routing path: a sender the
    operator excluded must not be able to cause a watcher and a backend session to exist.

    Agent senders bypass it deliberately — an agent-to-agent chain is authorised by being
    in `agent_usernames`, not by appearing in a human allow-list.
    """
    if not config.filter_sender:
        return True
    return (
        sender_username in config.allow_senders
        or sender_username in config.agent_chain.agent_usernames
    )
