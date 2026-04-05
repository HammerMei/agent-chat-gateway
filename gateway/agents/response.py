"""AgentResponse: normalized response dataclass returned by all AgentBackend.send() calls.

Every backend parses its own JSON stream format and populates this structure.
Fields not available from a particular backend are left as None / 0.

Usage::

    response = await backend.send(session_id, prompt, cwd, timeout)

    print(response.text)              # plain-text reply
    print(str(response))              # same — __str__ delegates to .text
    print(response.usage.input_tokens if response.usage else "n/a")
    print(f"${response.cost_usd:.4f}" if response.cost_usd else "cost unknown")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    pass  # AgentResponse referenced below after its definition


@dataclass
class TokenUsage:
    """Normalized token usage across Claude and opencode backends.

    Claude fields     → input_tokens, output_tokens,
                        cache_read_tokens, cache_write_tokens
    opencode fields   → input_tokens, output_tokens,
                        cache_read_tokens, cache_write_tokens, reasoning_tokens
                        (accumulated across all step_finish events)
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0      # Claude: cache_read_input_tokens
                                    # opencode: sum of tokens.cache.read
    cache_write_tokens: int = 0     # Claude: cache_creation_input_tokens
                                    # opencode: sum of tokens.cache.write
    reasoning_tokens: int = 0       # opencode only (tokens.reasoning); 0 for Claude

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class AgentResponse:
    """Normalized response returned by AgentBackend.send() and AgentSession.send().

    ``text`` is the primary field — the agent's plain-text reply.
    ``__str__`` delegates to ``text`` so existing code that treats the return
    value as a string continues to work without modification.

    All other fields are populated on a best-effort basis; backends that do not
    expose a field leave it as ``None`` / ``False``.

    Attributes:
        text        : The agent's plain-text reply (always populated).
        session_id  : The session ID used for this turn — useful for confirming
                      or updating persisted state.
        usage       : Token counts for this turn.  ``None`` if the backend does
                      not expose usage data.
        cost_usd    : Total cost of this turn in USD.  ``None`` if unavailable.
                      Note: opencode reports cost per step; reliability varies.
        duration_ms : Wall-clock duration of the entire agent turn in ms.
                      Claude: from the ``result`` event.
                      opencode (blocking path): derived from ``info.duration``
                      in the HTTP response body.
                      opencode (SSE/streaming path): not populated — the SSE
                      stream does not carry timing metadata.
                      ``None`` if not available.
        num_turns   : Number of agentic loop iterations (tool-use round trips).
                      Claude: from the ``result`` event.
                      opencode: count of ``step_finish`` events.
        is_error    : True when the agent reported an error condition, or when
                      the gateway synthesised an error response (timeout, etc.).
    """

    text: str
    session_id: str | None = None
    usage: TokenUsage | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    is_error: bool = False

    def __str__(self) -> str:
        """Return the plain-text reply, so AgentResponse is a drop-in for str."""
        return self.text

    def __repr__(self) -> str:
        cost = f"${self.cost_usd:.4f}" if self.cost_usd is not None else "n/a"
        tokens = self.usage.total_tokens if self.usage else "n/a"
        return (
            f"AgentResponse(text={self.text[:40]!r}{'...' if len(self.text) > 40 else ''}, "
            f"tokens={tokens}, cost={cost}, turns={self.num_turns}, "
            f"is_error={self.is_error})"
        )


@dataclass
class AgentEvent:
    """An intermediate or final event emitted by :meth:`AgentBackend.stream`.

    Intermediate events (``kind != "final"``) carry a human-readable ``text``
    label suitable for display as a live status update in the chat room, e.g.
    ``"🔧 Bash"`` or ``"💭 thinking..."``.

    The final event (``kind == "final"``) carries the complete
    :class:`AgentResponse` in ``response``.  ``text`` is empty for final events
    — consumers should read ``response.text`` instead.

    Attributes:
        kind     : Event category.
                   ``"tool_call"``   — agent is invoking a tool.
                   ``"tool_result"`` — tool returned a result.
                   ``"thinking"``    — extended-thinking step in progress.
                   ``"final"``       — turn complete; ``response`` is populated.
        text     : Human-readable status label for intermediate events.
        response : Populated only when ``kind == "final"``.
    """

    kind: Literal["tool_call", "tool_result", "thinking", "final"]
    text: str = ""
    response: AgentResponse | None = None
