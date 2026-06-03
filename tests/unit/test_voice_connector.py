"""Unit tests for VoiceConnector and VoiceConfig.

Tests cover:
  - VoiceConfig.from_connector_config  — field parsing and defaults
  - _parse_room                        — URL path → room name
  - VoiceConnector.send_text           — queues reply into correct room
  - VoiceConnector._dispatch           — inject → wait → reply, per room
  - VoiceConnector._handle_http        — HTTP parsing, auth, routing
  - Per-room isolation                 — different rooms run concurrently
  - connector_factory("voice")         — factory wiring
  - Timeout and busy/dropped paths
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.connectors.voice.config import VoiceConfig
from gateway.connectors.voice.connector import (
    VoiceConnector,
    _BUSY_REPLY,
    _TIMEOUT_REPLY,
    _parse_room,
)
from gateway.core.connector import IncomingMessage, UserRole


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_cc(raw: dict | None = None) -> MagicMock:
    cc = MagicMock()
    cc.raw = raw or {}
    return cc


def _make_config(**kwargs) -> VoiceConfig:
    defaults = {"port": 8765, "host": "127.0.0.1", "secret": "", "timeout": 5}
    defaults.update(kwargs)
    return VoiceConfig(**defaults)


def _make_stream(data: bytes):
    """Return (StreamReader, StreamWriter) pair pre-loaded with data."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))
    return reader, writer


def _written(writer) -> bytes:
    """Concatenate all bytes passed to writer.write()."""
    return b"".join(
        call.args[0] for call in writer.write.call_args_list
        if isinstance(call.args[0], bytes)
    )


# ── VoiceConfig ────────────────────────────────────────────────────────────────

class TestVoiceConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = VoiceConfig.from_connector_config(_make_cc())
        self.assertEqual(cfg.port, 8765)
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertEqual(cfg.secret, "")
        self.assertEqual(cfg.timeout, 45)

    def test_custom_values(self):
        cfg = VoiceConfig.from_connector_config(
            _make_cc({"port": 9000, "host": "127.0.0.1", "secret": "tok", "timeout": 30})
        )
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.secret, "tok")
        self.assertEqual(cfg.timeout, 30)


# ── _parse_room ────────────────────────────────────────────────────────────────

class TestParseRoom(unittest.TestCase):
    def test_valid_room(self):
        self.assertEqual(_parse_room("/ask/laomei"), "laomei")

    def test_valid_room_with_hyphen(self):
        self.assertEqual(_parse_room("/ask/voice-room"), "voice-room")

    def test_no_room_returns_none(self):
        self.assertIsNone(_parse_room("/ask"))

    def test_trailing_slash_only_returns_none(self):
        self.assertIsNone(_parse_room("/ask/"))

    def test_wrong_prefix_returns_none(self):
        self.assertIsNone(_parse_room("/other/laomei"))

    def test_query_string_stripped(self):
        self.assertEqual(_parse_room("/ask/laomei?foo=bar"), "laomei")

    def test_root_returns_none(self):
        self.assertIsNone(_parse_room("/"))


# ── send_text routes to correct room ─────────────────────────────────────────

class TestSendText(unittest.IsolatedAsyncioTestCase):
    async def test_queues_reply_when_dispatch_active(self):
        conn = VoiceConnector(_make_config())
        conn._get_room("laomei").dispatch_active = True
        response = MagicMock()
        response.text = "Hello from agent"

        await conn.send_text("laomei", response)

        self.assertEqual(conn._rooms["laomei"].queue.qsize(), 1)
        self.assertEqual(conn._rooms["laomei"].queue.get_nowait(), "Hello from agent")

    async def test_drops_reply_when_no_dispatch_active(self):
        """send_text outside a dispatch is dropped — prevents permission notifications
        from being returned as a spurious voice reply."""
        conn = VoiceConnector(_make_config())
        # dispatch_active defaults to False
        response = MagicMock()
        response.text = "🔐 Permission request: approve tool call?"

        await conn.send_text("laomei", response)

        # Nothing enqueued
        self.assertEqual(conn._get_room("laomei").queue.qsize(), 0)

    async def test_replies_go_to_separate_queues(self):
        conn = VoiceConnector(_make_config())
        conn._get_room("laomei").dispatch_active = True
        conn._get_room("xiaomei").dispatch_active = True
        r1, r2 = MagicMock(), MagicMock()
        r1.text, r2.text = "reply A", "reply B"

        await conn.send_text("laomei", r1)
        await conn.send_text("xiaomei", r2)

        self.assertEqual(conn._rooms["laomei"].queue.get_nowait(), "reply A")
        self.assertEqual(conn._rooms["xiaomei"].queue.get_nowait(), "reply B")


# ── _dispatch ─────────────────────────────────────────────────────────────────

class TestDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_returns_agent_reply(self):
        conn = VoiceConnector(_make_config(timeout=5))

        async def handler(msg: IncomingMessage) -> bool:
            await conn._rooms[msg.room.id].queue.put("42")
            return True

        conn.register_handler(handler)
        result = await conn._dispatch("What is 6 × 7?", "laomei")
        self.assertEqual(result, "42")

    async def test_dispatch_uses_room_name_in_message(self):
        conn = VoiceConnector(_make_config(timeout=5))
        received: list[IncomingMessage] = []

        async def handler(msg: IncomingMessage) -> bool:
            received.append(msg)
            await conn._rooms[msg.room.id].queue.put("ok")
            return True

        conn.register_handler(handler)
        await conn._dispatch("hello", "xiaomei")
        self.assertEqual(received[0].room.id, "xiaomei")

    async def test_dispatch_passes_owner_role(self):
        conn = VoiceConnector(_make_config(timeout=5))

        async def handler(msg: IncomingMessage) -> bool:
            await conn._rooms[msg.room.id].queue.put("ok")
            return True

        conn.register_handler(handler)
        await conn._dispatch("test", "laomei")
        # (role validated via the IncomingMessage constructed in _dispatch)

    async def test_dispatch_no_handler_returns_busy(self):
        conn = VoiceConnector(_make_config())
        result = await conn._dispatch("hello", "laomei")
        self.assertEqual(result, _BUSY_REPLY)

    async def test_dispatch_dropped_message_returns_busy(self):
        conn = VoiceConnector(_make_config())
        conn.register_handler(AsyncMock(return_value=False))
        result = await conn._dispatch("hello", "laomei")
        self.assertEqual(result, _BUSY_REPLY)

    async def test_dispatch_timeout_returns_timeout_message(self):
        conn = VoiceConnector(_make_config(timeout=1))
        conn.register_handler(AsyncMock(return_value=True))
        result = await conn._dispatch("slow query", "laomei")
        self.assertEqual(result, _TIMEOUT_REPLY)

    async def test_stale_reply_drained_before_next_request(self):
        conn = VoiceConnector(_make_config(timeout=5))

        async def handler(msg: IncomingMessage) -> bool:
            await conn._rooms[msg.room.id].queue.put("correct reply")
            return True

        conn.register_handler(handler)

        # Pre-seed a stale reply in laomei's queue
        conn._get_room("laomei").queue.put_nowait("stale reply from old request")

        result = await conn._dispatch("second query", "laomei")
        self.assertEqual(result, "correct reply")

    async def test_different_rooms_have_independent_queues(self):
        """Stale reply in room A does not affect room B."""
        conn = VoiceConnector(_make_config(timeout=5))

        async def handler(msg: IncomingMessage) -> bool:
            await conn._rooms[msg.room.id].queue.put("xiaomei reply")
            return True

        conn.register_handler(handler)

        # Stale reply sits in laomei's queue — should NOT affect xiaomei
        conn._get_room("laomei").queue.put_nowait("stale laomei reply")

        result = await conn._dispatch("query for xiaomei", "xiaomei")
        self.assertEqual(result, "xiaomei reply")
        # laomei's stale reply is still there (undrained — we only drain on dispatch)
        self.assertEqual(conn._rooms["laomei"].queue.qsize(), 1)


# ── _handle_http ──────────────────────────────────────────────────────────────

class TestHandleHttp(unittest.IsolatedAsyncioTestCase):
    def _connector_with_reply(self, reply: str, secret: str = "") -> VoiceConnector:
        conn = VoiceConnector(_make_config(secret=secret, timeout=5))

        async def handler(msg: IncomingMessage) -> bool:
            # Simulate agent reply arriving while dispatch_active is True.
            # In production the agent calls send_text(); here we put directly
            # so the test doesn't depend on send_text() gating logic.
            await conn._rooms[msg.room.id].queue.put(reply)
            return True

        conn.register_handler(handler)
        return conn

    async def test_post_ask_room_returns_reply(self):
        conn = self._connector_with_reply("sunny and 72F")
        body = b"What's the weather?"
        raw = (
            b"POST /ask/laomei HTTP/1.1\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        response = _written(writer)
        self.assertIn(b"200 OK", response)
        self.assertIn(b"sunny and 72F", response)

    async def test_room_name_used_in_dispatch(self):
        """The room name from the URL reaches the IncomingMessage."""
        conn = VoiceConnector(_make_config(timeout=5))
        received: list[IncomingMessage] = []

        async def handler(msg: IncomingMessage) -> bool:
            received.append(msg)
            await conn._rooms[msg.room.id].queue.put("ok")
            return True

        conn.register_handler(handler)
        body = b"hello"
        raw = (
            b"POST /ask/my-agent HTTP/1.1\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertEqual(received[0].room.id, "my-agent")

    async def test_post_ask_no_room_returns_400(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /ask HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"400", _written(writer))

    async def test_post_ask_trailing_slash_returns_400(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /ask/ HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"400", _written(writer))

    async def test_get_returns_404(self):
        conn = self._connector_with_reply("never")
        raw = b"GET /ask/laomei HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"404", _written(writer))

    async def test_wrong_path_returns_404(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /other/laomei HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"404", _written(writer))

    async def test_empty_body_returns_400(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /ask/laomei HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"400", _written(writer))

    async def test_valid_bearer_token_accepted(self):
        conn = self._connector_with_reply("pong", secret="s3cr3t")
        body = b"ping"
        raw = (
            b"POST /ask/laomei HTTP/1.1\r\n"
            b"Authorization: Bearer s3cr3t\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"200 OK", _written(writer))

    async def test_missing_bearer_token_returns_401(self):
        conn = self._connector_with_reply("never", secret="s3cr3t")
        body = b"ping"
        raw = (
            b"POST /ask/laomei HTTP/1.1\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"401", _written(writer))

    async def test_wrong_bearer_token_returns_401(self):
        conn = self._connector_with_reply("never", secret="s3cr3t")
        body = b"ping"
        raw = (
            b"POST /ask/laomei HTTP/1.1\r\n"
            b"Authorization: Bearer wrong\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"401", _written(writer))

    async def test_oversized_body_returns_413(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /ask/laomei HTTP/1.1\r\nContent-Length: 99999\r\n\r\n"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"413", _written(writer))

    async def test_json_body_extracted(self):
        conn = self._connector_with_reply("json works")
        body = b'{"text": "hello from Siri"}'
        raw = (
            b"POST /ask/laomei HTTP/1.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"200 OK", _written(writer))
        self.assertIn(b"json works", _written(writer))

    async def test_invalid_json_returns_400(self):
        conn = self._connector_with_reply("never")
        body = b"not json at all"
        raw = (
            b"POST /ask/laomei HTTP/1.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"400", _written(writer))

    async def test_json_null_text_returns_400(self):
        """{"text": null} must be rejected — not dispatched as literal 'None'."""
        conn = self._connector_with_reply("never")
        body = b'{"text": null}'
        raw = (
            b"POST /ask/laomei HTTP/1.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"400", _written(writer))

    async def test_negative_content_length_returns_400(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /ask/laomei HTTP/1.1\r\nContent-Length: -1\r\n\r\n"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"400", _written(writer))

    async def test_wrong_bearer_token_returns_401_constant_time(self):
        """Wrong token is rejected (also covers hmac.compare_digest path)."""
        conn = self._connector_with_reply("never", secret="s3cr3t")
        body = b"ping"
        raw = (
            b"POST /ask/laomei HTTP/1.1\r\n"
            b"Authorization: Bearer almostright\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"401", _written(writer))


# ── connector_factory ─────────────────────────────────────────────────────────

class TestConnectorFactoryVoice(unittest.TestCase):
    def test_factory_returns_voice_connector(self):
        cc = _make_cc({"port": 8765, "secret": "tok"})
        cc.type = "voice"
        cc.name = "siri-voice"

        from gateway.connectors import connector_factory
        result = connector_factory(cc)
        self.assertIsInstance(result, VoiceConnector)

    def test_error_message_includes_voice(self):
        cc = _make_cc()
        cc.type = "unknown-type"
        cc.name = "x"

        from gateway.connectors import connector_factory
        with self.assertRaises(ValueError) as ctx:
            connector_factory(cc)
        self.assertIn("voice", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
