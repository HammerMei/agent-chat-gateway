"""Pure prompt construction — no side effects, no I/O.

Extracted from MessageProcessor._process() to make prompt logic independently
testable and to keep the processor focused on queue orchestration.
"""

from __future__ import annotations


def build_prompt(text: str, prefix: str | None, warnings: list[str] | None = None) -> str:
    """Build the final agent prompt from cleaned text, platform prefix, and warnings.

    Args:
        text: Already cleaned (mention-stripped) message text from the Connector.
        prefix: Platform-injected prompt prefix (RBAC boundary). Empty string
                or ``None`` when the connector does not inject a prefix.
        warnings: Attachment download warnings to append (too large, timed out, etc.).

    Returns:
        Assembled prompt string ready to pass to ``AgentBackend.send()``.
    """
    prompt = f"{prefix} {text}".strip() if prefix else text
    if warnings:
        prompt = prompt + "\n" + "\n".join(warnings)
    return prompt
