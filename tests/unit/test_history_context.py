"""Unit tests for gateway.core.history_context.

Pure function tests — no I/O, no network, no agent calls.
"""

import pytest

from gateway.core.history_context import (
    CONDENSE_CHARS,
    MAX_HISTORY_CHARS,
    VERBATIM_TAIL,
    _format_rc_header,
    format_history_context,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _msg(
    username: str = "alice",
    role: str = "owner",
    text: str = "hello",
    ts: str | None = "2026-05-10T14:32:00+08:00",
    room_name: str = "nest",
) -> dict:
    return {"username": username, "role": role, "text": text, "ts": ts, "room_name": room_name}


def _msgs(n: int, **kw) -> list[dict]:
    return [_msg(text=f"msg {i}", **kw) for i in range(n)]


# ---------------------------------------------------------------------------
# _format_rc_header
# ---------------------------------------------------------------------------


class TestFormatRcHeader:
    def test_owner_message(self):
        m = _msg(username="alice", role="owner", ts="2026-05-10T14:32:00+08:00")
        header = _format_rc_header(m)
        assert header == "[Rocket.Chat #nest | from: alice | role: owner | ts: 2026-05-10T14:32:00+08:00]"

    def test_guest_message(self):
        m = _msg(username="bob", role="guest", ts="2026-05-10T14:33:00+08:00")
        header = _format_rc_header(m)
        assert "from: bob" in header
        assert "role: guest" in header

    def test_agent_message_uses_from_me(self):
        """Bot's own prior messages should use 'from: me', not the bot's username."""
        m = _msg(username="hammer-mei", role="agent", ts="2026-05-10T14:34:00+08:00")
        header = _format_rc_header(m)
        assert "from: me" in header
        assert "role: agent" in header
        # Must NOT expose the bot's username in the from field
        assert "hammer-mei" not in header

    def test_no_timestamp(self):
        """ts=None should omit the ts field entirely."""
        m = _msg(ts=None)
        header = _format_rc_header(m)
        assert "ts:" not in header
        assert "from: alice" in header

    def test_no_to_field(self):
        """History headers must not include a 'to:' routing field."""
        m = _msg()
        assert "to:" not in _format_rc_header(m)


# ---------------------------------------------------------------------------
# format_history_context
# ---------------------------------------------------------------------------


class TestFormatHistoryContext:
    def test_empty_returns_none(self):
        assert format_history_context([]) is None

    def test_single_message_is_verbatim(self):
        msgs = [_msg(text="hello world")]
        result = format_history_context(msgs, verbatim_tail=15)
        assert result is not None
        assert "hello world" in result
        # With only 1 message (≤ verbatim_tail), no condensed section
        assert "Earlier messages" not in result
        assert "Recent messages" in result

    def test_all_verbatim_when_fewer_than_tail(self):
        msgs = _msgs(5)
        result = format_history_context(msgs, verbatim_tail=15)
        assert "Earlier messages" not in result
        assert "Recent messages" in result
        # All 5 messages should appear verbatim
        for i in range(5):
            assert f"msg {i}" in result

    def test_split_older_and_recent(self):
        msgs = _msgs(20)
        result = format_history_context(msgs, verbatim_tail=5)
        assert result is not None
        assert "Earlier messages (condensed)" in result
        assert "Recent messages" in result
        # Last 5 messages verbatim
        for i in range(15, 20):
            assert f"msg {i}" in result
        # First 15 condensed (appear on same line as header)
        assert "msg 0" in result

    def test_condensed_text_truncated_at_limit(self):
        long_text = "x" * 200
        msgs = _msgs(2) + [_msg(text=long_text)] + _msgs(1)
        result = format_history_context(msgs, verbatim_tail=1, condense_chars=50)
        assert result is not None
        # The long text should be truncated with ellipsis
        assert "x" * 50 in result
        assert "…" in result
        # Full long text must NOT appear
        assert "x" * 200 not in result

    def test_header_is_always_present(self):
        result = format_history_context([_msg()])
        assert "SESSION HISTORY" in result

    def test_max_chars_truncation(self):
        msgs = [_msg(text="y" * 200) for _ in range(100)]
        result = format_history_context(msgs, max_chars=500)
        assert result is not None
        assert len(result) <= 500 + len("\n\n[... history truncated due to size limit ...]")
        assert "truncated" in result

    def test_agent_message_uses_from_me(self):
        msgs = [_msg(username="me", role="agent", text="I replied with this")]
        result = format_history_context(msgs, verbatim_tail=15)
        assert "from: me" in result
        assert "role: agent" in result
        assert "I replied with this" in result

    def test_mixed_roles(self):
        msgs = [
            _msg(username="alice", role="owner", text="owner msg"),
            _msg(username="me", role="agent", text="agent reply"),
            _msg(username="bob", role="guest", text="guest msg"),
        ]
        result = format_history_context(msgs, verbatim_tail=15)
        assert "role: owner" in result
        assert "role: agent" in result
        assert "role: guest" in result
        assert "from: me" in result   # agent uses 'me'
        assert "from: alice" in result
        assert "from: bob" in result

    def test_message_without_text_produces_header_only(self):
        msgs = [_msg(text="")]
        result = format_history_context(msgs, verbatim_tail=15)
        assert result is not None
        # Header is present; no crash for empty text
        assert "Rocket.Chat" in result

    def test_no_trailing_blank_lines(self):
        """Output should not end with blank lines."""
        result = format_history_context(_msgs(3))
        assert result is not None
        assert not result.endswith("\n\n")
        assert not result.endswith("\n")

    def test_exactly_verbatim_tail_count(self):
        """When len(messages) == verbatim_tail, no condensed section."""
        msgs = _msgs(15)
        result = format_history_context(msgs, verbatim_tail=15)
        assert "Earlier messages" not in result
        assert "Recent messages" in result

    def test_one_more_than_tail(self):
        """When len(messages) == verbatim_tail + 1, one condensed line."""
        msgs = _msgs(16)
        result = format_history_context(msgs, verbatim_tail=15)
        assert "Earlier messages (condensed)" in result
        assert "Recent messages" in result


# ---------------------------------------------------------------------------
# Default constant sanity checks
# ---------------------------------------------------------------------------


def test_defaults_are_reasonable():
    assert VERBATIM_TAIL == 15
    assert CONDENSE_CHARS >= 80
    assert MAX_HISTORY_CHARS >= 8_000
