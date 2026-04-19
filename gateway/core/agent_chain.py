"""Agent-chain constants shared between the core and connector layers.

Both ``gateway.core.agent_turn_runner`` and
``gateway.connectors.rocketchat.agent_chain`` import from here so that the
token is defined exactly once and neither layer has to import from the other.
"""

from __future__ import annotations

# Sentinel the LLM outputs to self-terminate an agent chain turn.
# ACG detects this via exact match (response.text.strip() == TOKEN).
AGENT_CHAIN_TERMINATION_TOKEN = "<end-of-agent-chain>"


def build_agent_chain_context(turn: int, max_turns: int) -> str:
    """Build the toll-call prompt suffix injected when processing an agent-chain message.

    turn:      1-based current turn number (already incremented).
    max_turns: configured budget ceiling.
    """
    lines = [
        f"\n---\n[Agent chain: turn {turn}/{max_turns}]"
    ]
    if turn == max_turns - 1:
        lines.append(
            "\u26a0\ufe0f  Your next response will be your last turn in this agent chain."
        )
    elif turn >= max_turns:
        lines.append(
            "\u26a0\ufe0f  This is your final turn in this agent chain. "
            "Please wrap up gracefully.\n"
            "If the task is not yet complete, you may use the scheduler tool "
            "to schedule a follow-up message and continue with a fresh turn budget."
        )
    if turn < max_turns:
        lines.append(
            f"If you have nothing meaningful to add, respond with ONLY: "
            f"{AGENT_CHAIN_TERMINATION_TOKEN}"
        )
    return "\n".join(lines)
