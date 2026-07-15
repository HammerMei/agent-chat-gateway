"""Agent-chain turn tracking for controlled agent-to-agent communication.

TurnStore and the agent-chain constants/helpers used to live here, but they
are purely platform-agnostic (keyed on generic room_id/thread_id/sender
strings, no Rocket.Chat-specific behavior) and are now shared with other
connectors (e.g. Mattermost).  They live in ``gateway.core.agent_chain``;
this module re-exports them so existing imports of
``gateway.connectors.rocketchat.agent_chain`` keep working unchanged.
"""

from __future__ import annotations

from ...core.agent_chain import (  # noqa: F401 — re-export for connector consumers
    AGENT_CHAIN_TERMINATION_TOKEN,
    TurnStore,
    build_agent_chain_context,
)
