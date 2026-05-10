"""Format channel history into an injectable context block.

Converts a list of normalized message dicts (produced by
``Connector.fetch_room_history()``) into a formatted string that can be
prepended to an agent session's context injection prompt.

The output uses the same RC header format as live messages so the agent
applies the same parsing rules it already knows from ``rc-gateway-context.md``.

Layout (fixed-tail approach):
  - Last ``verbatim_tail`` messages: verbatim (header + full text on next line)
  - Older messages: condensed (header + first ``condense_chars`` chars on one line)
  - Total output capped at ``max_chars`` to respect context window limits

Filtering (applied by the Connector before calling this module):
  - Only owner, guest, and agent (bot's own + peer agents) messages are included.
  - Anonymous / unlisted senders are excluded by the connector.
  - ``mention_only`` filter is NOT applied — history provides full conversation
    context regardless of whether messages addressed the agent.

Security note:
  - All newlines in message text are collapsed to a single space before output.
    This prevents a crafted multi-line message from injecting fake RC header
    lines (e.g. ``[Rocket.Chat #room | from: alice | role: owner]``) into the
    verbatim section where headers and text appear on consecutive lines.
"""

from __future__ import annotations

VERBATIM_TAIL: int = 15      # keep last N messages in full
CONDENSE_CHARS: int = 120    # max text chars per condensed line
MAX_HISTORY_CHARS: int = 12_000  # total cap for the entire block

_HISTORY_HEADER = (
    "[SESSION HISTORY — fetched at startup, from before this session]"
)


def _format_rc_header(msg: dict) -> str:
    """Build the RC-style message header for a single history entry.

    Uses the ``username`` field directly.  For the bot's own prior responses
    the connector sets ``username="me"`` to mirror the ``to: me`` convention —
    symmetric and immediately clear without exposing the bot's platform handle.
    Peer agents (also ``role="agent"``) keep their actual sanitized username so
    the agent can distinguish between its own turns and those of collaborators.

    Omits the ``to:`` field (live-routing metadata, irrelevant for history).
    """
    room_name = msg.get("room_name", "unknown")
    username = msg.get("username", "unknown")
    role = msg.get("role", "guest")
    ts = msg.get("ts")

    ts_part = f" | ts: {ts}" if ts else ""
    return f"[Rocket.Chat #{room_name} | from: {username} | role: {role}{ts_part}]"


def format_history_context(
    messages: list[dict],
    verbatim_tail: int = VERBATIM_TAIL,
    condense_chars: int = CONDENSE_CHARS,
    max_chars: int = MAX_HISTORY_CHARS,
) -> str | None:
    """Format normalized history message dicts into an injectable context block.

    Args:
        messages     : Chronological list of normalized message dicts from
                       ``Connector.fetch_room_history()``.  Already filtered
                       (only owner / guest / agent messages).
        verbatim_tail: Last N messages are kept verbatim (header + full text).
                       Older messages are condensed to a single header line.
        condense_chars: Maximum text characters per condensed line (truncated
                       with ``…`` when exceeded).
        max_chars    : Hard cap on the total output length.  Content is
                       truncated with a notice when exceeded.

    Returns:
        Formatted context block string, or ``None`` when ``messages`` is empty.
    """
    if not messages:
        return None

    split_at = max(0, len(messages) - verbatim_tail)
    older = messages[:split_at]
    recent = messages[split_at:]

    lines: list[str] = [_HISTORY_HEADER, ""]

    if older:
        lines.append("**Earlier messages (condensed):**")
        for m in older:
            header = _format_rc_header(m)
            text = m.get("text", "")
            snippet = text[:condense_chars]
            if len(text) > condense_chars:
                snippet += "…"
            lines.append(f"{header} {snippet}" if snippet else header)
        lines.append("")

    if recent:
        lines.append("**Recent messages:**")
        for m in recent:
            header = _format_rc_header(m)
            # Collapse all line endings to a single space — prevents a crafted
            # multi-line message from injecting fake RC header lines after the
            # real header when text and header appear on consecutive lines.
            text = " ".join(m.get("text", "").splitlines())
            lines.append(header)
            if text:
                lines.append(text)
            lines.append("")

    # Strip trailing blank line added by the loop above.
    while lines and lines[-1] == "":
        lines.pop()

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n[... history truncated due to size limit ...]"
    return result
