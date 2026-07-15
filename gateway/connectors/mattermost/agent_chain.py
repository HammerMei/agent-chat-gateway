"""Agent-chain turn tracking for Mattermost.

TurnStore and the agent-chain constants/helpers are platform-agnostic (see
gateway.core.agent_chain) and shared with Rocket.Chat. This module
re-exports them for a consistent import path alongside the other
connector-local modules.
"""

from __future__ import annotations

from ...core.agent_chain import (  # noqa: F401 — re-export for connector consumers
    AGENT_CHAIN_TERMINATION_TOKEN,
    TurnStore,
    build_agent_chain_context,
)
