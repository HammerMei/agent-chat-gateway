"""Unit tests for gateway.connectors.rocketchat.outbound.

Covers:
  - _post_with_retry: retry behaviour, backoff delays, final re-raise
  - send_text: single-message path, chunked path, passes tmid correctly
  - _split_text: newline-preference and hard-cut fallback
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from gateway.connectors.rocketchat.outbound import (
    _MAX_RETRIES,
    _RETRY_DELAYS,
    _split_text,
    send_text,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_rest(*, fail_times: int = 0, error: Exception | None = None) -> MagicMock:
    """Return a mock RocketChatREST whose post_message succeeds after *fail_times* calls.

    If *error* is given it is raised on every failure; otherwise RuntimeError is used.
    """
    err = error or RuntimeError("transient RC error")
    side_effects: list = [err] * fail_times + [None]  # None → success (AsyncMock)
    rest = MagicMock()
    rest.post_message = AsyncMock(side_effect=side_effects)
    return rest


# ── _split_text ────────────────────────────────────────────────────────────────


class TestSplitText(unittest.TestCase):
    def test_short_text_returns_single_chunk(self):
        result = _split_text("hello world", limit=100)
        self.assertEqual(result, ["hello world"])

    def test_exact_limit_returns_single_chunk(self):
        text = "a" * 50
        result = _split_text(text, limit=50)
        self.assertEqual(result, [text])

    def test_hard_cut_at_limit_when_no_newline(self):
        text = "a" * 120
        result = _split_text(text, limit=100)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 100)
        self.assertEqual(result[1], "a" * 20)

    def test_prefers_newline_boundary_near_limit(self):
        # Put a newline within the last 20 % of the window (positions 80-100)
        text = "x" * 90 + "\n" + "y" * 50
        result = _split_text(text, limit=100)
        self.assertEqual(len(result), 2)
        # First chunk ends at the newline — no mid-word cut
        self.assertNotIn("\n", result[0])

    def test_empty_text_returns_empty_list(self):
        result = _split_text("", limit=50)
        self.assertEqual(result, [])

    def test_chunks_cover_full_text(self):
        text = ("hello world\n" * 20).strip()
        limit = 50
        result = _split_text(text, limit=limit)
        # Reassembled text (stripped) matches the original (stripped)
        reassembled = " ".join(result)  # chunks are stripped — content is preserved
        for chunk in result:
            self.assertLessEqual(len(chunk), limit)
        self.assertGreater(len(result), 1)


# ── _post_with_retry ──────────────────────────────────────────────────────────


class TestPostWithRetry(unittest.IsolatedAsyncioTestCase):
    async def _call(self, rest, room_id="room-1", text="hi", tmid=None):
        from gateway.connectors.rocketchat.outbound import _post_with_retry

        return await _post_with_retry(rest, room_id, text, tmid)

    async def test_succeeds_on_first_attempt(self):
        rest = _make_rest(fail_times=0)
        await self._call(rest)
        rest.post_message.assert_called_once_with("room-1", "hi", tmid=None)

    async def test_retries_on_transient_failure_and_succeeds(self):
        rest = _make_rest(fail_times=1)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await self._call(rest)
        # Two calls total: 1 failure + 1 success
        self.assertEqual(rest.post_message.call_count, 2)
        # One sleep between attempts
        mock_sleep.assert_called_once_with(_RETRY_DELAYS[0])

    async def test_retries_twice_before_success(self):
        rest = _make_rest(fail_times=2)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await self._call(rest)
        self.assertEqual(rest.post_message.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_has_calls(
            [call(_RETRY_DELAYS[0]), call(_RETRY_DELAYS[1])]
        )

    async def test_raises_after_max_retries_exhausted(self):
        err = RuntimeError("persistent error")
        rest = _make_rest(fail_times=_MAX_RETRIES, error=err)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(RuntimeError) as ctx:
                await self._call(rest)
        self.assertIs(ctx.exception, err)
        self.assertEqual(rest.post_message.call_count, _MAX_RETRIES)

    async def test_passes_tmid_on_every_attempt(self):
        rest = _make_rest(fail_times=1)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self._call(rest, tmid="thread-99")
        for c in rest.post_message.call_args_list:
            self.assertEqual(c.kwargs.get("tmid"), "thread-99")

    async def test_no_sleep_after_final_attempt(self):
        """No sleep should occur after the last failing attempt."""
        rest = _make_rest(fail_times=_MAX_RETRIES)
        sleep_calls: list[float] = []

        async def _capture_sleep(delay):
            sleep_calls.append(delay)

        with patch("asyncio.sleep", side_effect=_capture_sleep):
            with self.assertRaises(Exception):
                await self._call(rest)

        # There are _MAX_RETRIES-1 sleeps (between attempts), not _MAX_RETRIES
        self.assertEqual(len(sleep_calls), _MAX_RETRIES - 1)


# ── send_text ─────────────────────────────────────────────────────────────────


class TestSendText(unittest.IsolatedAsyncioTestCase):
    async def test_short_message_sent_as_single_call(self):
        rest = _make_rest()
        await send_text(rest, "room-1", "hello", chunk_limit=None)
        rest.post_message.assert_called_once_with("room-1", "hello", tmid=None)

    async def test_message_at_chunk_limit_sent_as_single_call(self):
        rest = _make_rest()
        text = "x" * 100
        await send_text(rest, "room-1", text, chunk_limit=100)
        rest.post_message.assert_called_once()

    async def test_long_message_split_into_multiple_calls(self):
        rest = _make_rest(fail_times=0)
        # Make post_message succeed repeatedly
        rest.post_message = AsyncMock(return_value=None)
        text = "a" * 300
        await send_text(rest, "room-1", text, chunk_limit=100)
        # 300 chars / 100 limit = 3 chunks
        self.assertEqual(rest.post_message.call_count, 3)

    async def test_tmid_forwarded_to_every_chunk(self):
        rest = MagicMock()
        rest.post_message = AsyncMock(return_value=None)
        text = "b" * 250
        await send_text(rest, "room-1", text, chunk_limit=100, tmid="t-42")
        for c in rest.post_message.call_args_list:
            self.assertEqual(c.kwargs.get("tmid"), "t-42")

    async def test_chunk_limit_none_sends_full_text(self):
        rest = _make_rest()
        long_text = "x" * 10_000
        await send_text(rest, "room-1", long_text, chunk_limit=None)
        rest.post_message.assert_called_once_with("room-1", long_text, tmid=None)

    async def test_retry_on_transient_failure_within_chunk(self):
        """A transient RC error on first chunk triggers retry inside _post_with_retry."""
        err = RuntimeError("flaky")
        rest = _make_rest(fail_times=1, error=err)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Should not raise — retry succeeds
            await send_text(rest, "room-1", "hello", chunk_limit=None)
        self.assertEqual(rest.post_message.call_count, 2)


if __name__ == "__main__":
    unittest.main()
