"""Unit tests for VoiceConnector and VoiceConfig.

Tests cover:
  - VoiceConfig.from_connector_config  — field parsing and defaults
  - VoiceConnector.send_text           — queues reply
  - VoiceConnector._dispatch           — inject → wait → reply
  - VoiceConnector._handle_http        — HTTP parsing, auth, routing
  - connector_factory("voice")         — factory wiring
  - Timeout and busy/dropped paths
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.connectors.voice.config import VoiceConfig
from gateway.connectors.voice.connector import (
    VoiceConnector,
    _BUSY_REPLY,
    _TIMEOUT_REPLY,
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


# ── send_text ─────────────────────────────────────────────────────────────────

class TestSendText(unittest.IsolatedAsyncioTestCase):
    async def test_queues_reply(self):
        conn = VoiceConnector(_make_config())
        response = MagicMock()
        response.text = "Hello from agent"

        await conn.send_text("voice-room", response)

        self.assertEqual(conn._reply_queue.qsize(), 1)
        self.assertEqual(conn._reply_queue.get_nowait(), "Hello from agent")


# ── _dispatch ─────────────────────────────────────────────────────────────────

class TestDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_returns_agent_reply(self):
        conn = VoiceConnector(_make_config(timeout=5))

        async def handler(msg: IncomingMessage) -> bool:
            # Simulate agent replying synchronously
            await conn._reply_queue.put("42")
            return True

        conn.register_handler(handler)
        result = await conn._dispatch("What is 6 × 7?")
        self.assertEqual(result, "42")

    async def test_dispatch_no_handler_returns_busy(self):
        conn = VoiceConnector(_make_config())
        result = await conn._dispatch("hello")
        self.assertEqual(result, _BUSY_REPLY)

    async def test_dispatch_dropped_message_returns_busy(self):
        conn = VoiceConnector(_make_config())
        handler = AsyncMock(return_value=False)
        conn.register_handler(handler)
        result = await conn._dispatch("hello")
        self.assertEqual(result, _BUSY_REPLY)

    async def test_dispatch_timeout_returns_timeout_message(self):
        conn = VoiceConnector(_make_config(timeout=1))
        # Handler accepts but never enqueues a reply
        conn.register_handler(AsyncMock(return_value=True))
        result = await conn._dispatch("slow query")
        self.assertEqual(result, _TIMEOUT_REPLY)

    async def test_stale_reply_drained_before_next_request(self):
        """A reply that arrived after a timeout is discarded before the next request.

        Scenario: a stale reply is pre-seeded in the queue (simulating a prior timed-out
        request whose agent finished late). The next dispatch must drain it first and
        return the fresh reply from its own handler invocation.
        """
        conn = VoiceConnector(_make_config(timeout=5))

        async def handler(msg: IncomingMessage) -> bool:
            # Always enqueue the correct reply for this request
            await conn._reply_queue.put("correct reply")
            return True

        conn.register_handler(handler)

        # Pre-seed a stale reply (simulates late agent response from a prior timeout)
        await conn._reply_queue.put("stale reply from old request")

        # _dispatch must drain "stale reply" first, then receive "correct reply"
        result = await conn._dispatch("second query")
        self.assertEqual(result, "correct reply")

    async def test_dispatch_passes_owner_role(self):
        conn = VoiceConnector(_make_config(timeout=5))
        received: list[IncomingMessage] = []

        async def handler(msg: IncomingMessage) -> bool:
            received.append(msg)
            await conn._reply_queue.put("ok")
            return True

        conn.register_handler(handler)
        await conn._dispatch("test")
        self.assertEqual(received[0].role, UserRole.OWNER)
        self.assertEqual(received[0].room.id, "voice-room")


# ── _handle_http ──────────────────────────────────────────────────────────────

class TestHandleHttp(unittest.IsolatedAsyncioTestCase):
    def _connector_with_reply(self, reply: str, secret: str = "") -> VoiceConnector:
        conn = VoiceConnector(_make_config(secret=secret, timeout=5))

        async def handler(msg: IncomingMessage) -> bool:
            await conn._reply_queue.put(reply)
            return True

        conn.register_handler(handler)
        return conn

    async def test_post_ask_returns_reply(self):
        conn = self._connector_with_reply("sunny and 72F")
        body = b"What's the weather?"
        raw = (
            b"POST /ask HTTP/1.1\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)

        response = _written(writer)
        self.assertIn(b"200 OK", response)
        self.assertIn(b"sunny and 72F", response)

    async def test_post_root_also_works(self):
        conn = self._connector_with_reply("ok")
        body = b"hello"
        raw = (
            b"POST / HTTP/1.1\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"200 OK", _written(writer))

    async def test_get_returns_404(self):
        conn = self._connector_with_reply("never")
        raw = b"GET /ask HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"404", _written(writer))

    async def test_unknown_path_returns_404(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /other HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"404", _written(writer))

    async def test_empty_body_returns_400(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /ask HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"400", _written(writer))

    async def test_valid_bearer_token_accepted(self):
        conn = self._connector_with_reply("pong", secret="s3cr3t")
        body = b"ping"
        raw = (
            b"POST /ask HTTP/1.1\r\n"
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
            b"POST /ask HTTP/1.1\r\n"
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
            b"POST /ask HTTP/1.1\r\n"
            b"Authorization: Bearer wrong\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"401", _written(writer))

    async def test_json_body_extracted(self):
        conn = self._connector_with_reply("json works")
        body = b'{"text": "hello from Siri"}'
        raw = (
            b"POST /ask HTTP/1.1\r\n"
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
            b"POST /ask HTTP/1.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n" + body
        )
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"400", _written(writer))

    async def test_oversized_body_returns_413(self):
        conn = self._connector_with_reply("never")
        raw = b"POST /ask HTTP/1.1\r\nContent-Length: 99999\r\n\r\n"
        reader, writer = _make_stream(raw)
        await conn._handle_http(reader, writer)
        self.assertIn(b"413", _written(writer))


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
