"""VoiceConnector: HTTP voice gateway for Siri / iOS Shortcuts.

Exposes a minimal HTTP server that accepts plain-text POST requests and returns
plain-text agent replies.  Designed to slot directly into an iOS Shortcut:

    Dictate Text  →  POST /ask  →  Agent  →  Speak Text

Architecture
------------
- Single fixed room (``voice-room``) to match the configured watcher.
- Requests are serialized with an asyncio.Lock — Siri is sequential.
- No extra dependencies: uses asyncio.start_server (stdlib only).
- Replies are bridged from send_text() → asyncio.Queue → HTTP response.

Security
--------
- Optional bearer token (``secret`` config key). Empty = no auth (dev only).
- Voice messages are sent as UserRole.OWNER so the agent can use its full
  tool set. Gate this at the network level (VPN / firewall) for production.
- Bind address defaults to 0.0.0.0 so the iPhone can reach the server over LAN.

Siri UX note
------------
Responses MUST be plain text — no markdown, no emoji — or Siri will read
asterisks and emoji names aloud. Inject ``contexts/voice-context.md`` in the
watcher config to enforce this automatically.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Literal

from ...agents.response import AgentResponse
from ...core.connector import (
    Connector,
    IncomingMessage,
    MessageHandler,
    Room,
    User,
    UserRole,
)
from .config import VoiceConfig

logger = logging.getLogger("agent-chat-gateway.connectors.voice")

_VOICE_ROOM = Room(id="voice-room", name="voice-room", type="channel")
_VOICE_USER = User(id="siri-user", username="siri", display_name="Siri")

# Hard cap on request body to avoid memory issues.
_MAX_BODY_BYTES = 4096

_TIMEOUT_REPLY = (
    "Sorry, the request timed out. Please try again in a moment."
)
_BUSY_REPLY = (
    "The gateway is busy right now. Please try again in a moment."
)


class VoiceConnector(Connector):
    """HTTP voice gateway connector.

    One instance per ``type: voice`` connector entry in config.yaml.
    Starts an asyncio HTTP server on ``connect()`` and stops it on ``disconnect()``.
    """

    delivery_mode: Literal["direct"] = "direct"

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._handler: MessageHandler | None = None
        self._reply_queue: asyncio.Queue[str] = asyncio.Queue()
        self._request_lock = asyncio.Lock()
        self._server: asyncio.Server | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Start the HTTP server and log the listen address."""
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._config.host,
            port=self._config.port,
        )
        addrs = ", ".join(
            str(s.getsockname()) for s in self._server.sockets or []
        )
        auth_status = "token-auth" if self._config.secret else "NO AUTH (dev mode)"
        logger.info(
            "VoiceConnector listening on %s [%s, timeout=%ds]",
            addrs,
            auth_status,
            self._config.timeout,
        )
        if not self._config.secret:
            logger.warning(
                "VoiceConnector has no 'secret' set — any device on the network "
                "can send voice commands to the agent. Set a bearer token in config."
            )

    async def disconnect(self) -> None:
        """Stop the HTTP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("VoiceConnector stopped")

    # ── Inbound ───────────────────────────────────────────────────────────────

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send_text(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None = None,  # noqa: ARG002
    ) -> None:
        """Bridge the agent reply back to the waiting HTTP handler."""
        await self._reply_queue.put(response.text)
        logger.debug("VoiceConnector reply queued (%d chars)", len(response.text))

    # ── Room resolution ───────────────────────────────────────────────────────

    async def resolve_room(self, room_name: str) -> Room:
        return Room(id=room_name, name=room_name, type="channel")

    # ── Security: prompt prefix ───────────────────────────────────────────────

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        return f"[Voice | from: {msg.sender.username} | role: {msg.role.value}]"

    # ── Internal HTTP server ──────────────────────────────────────────────────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Top-level connection handler — enforces an outer read timeout."""
        peer = writer.get_extra_info("peername", "<unknown>")
        logger.debug("VoiceConnector: connection from %s", peer)
        try:
            await asyncio.wait_for(
                self._handle_http(reader, writer),
                timeout=self._config.timeout + 10,
            )
        except asyncio.TimeoutError:
            logger.warning("VoiceConnector: connection from %s timed out", peer)
            _write_response(writer, 504, "Gateway Timeout", b"")
            await writer.drain()
        except Exception as exc:
            logger.error("VoiceConnector: error handling connection from %s: %s", peer, exc)
        finally:
            writer.close()

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Minimal HTTP/1.1 handler for POST /ask."""
        # ── Request line ──────────────────────────────────────────────────────
        try:
            raw_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            _write_response(writer, 408, "Request Timeout", b"")
            return

        parts = raw_line.decode("utf-8", errors="replace").strip().split(maxsplit=2)
        if len(parts) < 2:
            _write_response(writer, 400, "Bad Request", b"malformed request line")
            return
        method, path = parts[0].upper(), parts[1]

        # ── Headers ───────────────────────────────────────────────────────────
        headers: dict[str, str] = {}
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            except asyncio.TimeoutError:
                _write_response(writer, 408, "Request Timeout", b"")
                return
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                name, _, value = decoded.partition(":")
                headers[name.strip().lower()] = value.strip()

        # ── Auth ──────────────────────────────────────────────────────────────
        if self._config.secret:
            auth = headers.get("authorization", "")
            expected = f"Bearer {self._config.secret}"
            if auth != expected:
                logger.warning("VoiceConnector: unauthorized request rejected")
                _write_response(writer, 401, "Unauthorized", b"invalid or missing Bearer token")
                return

        # ── Route ─────────────────────────────────────────────────────────────
        if method != "POST" or path not in ("/ask", "/"):
            _write_response(writer, 404, "Not Found", b"POST /ask only")
            return

        # ── Body ──────────────────────────────────────────────────────────────
        content_length = int(headers.get("content-length", "0"))
        if content_length > _MAX_BODY_BYTES:
            _write_response(writer, 413, "Payload Too Large", b"")
            return

        try:
            raw_body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=5.0
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            _write_response(writer, 400, "Bad Request", b"incomplete body")
            return

        # ── Body parsing — plain text or JSON {"text": "..."} ────────────────
        # iOS Shortcuts "Get Contents of URL" only offers JSON/Form/File body
        # types (no plain-text option), so we accept both:
        #   Content-Type: application/json  →  extract the "text" key
        #   anything else                   →  treat raw body as plain text
        raw_str = raw_body.decode("utf-8", errors="replace").strip()
        content_type = headers.get("content-type", "")
        if "application/json" in content_type:
            import json as _json
            try:
                payload = _json.loads(raw_str)
                text = str(payload.get("text", "")).strip()
            except (_json.JSONDecodeError, AttributeError):
                _write_response(writer, 400, "Bad Request", b"invalid JSON")
                return
        else:
            text = raw_str

        if not text:
            _write_response(writer, 400, "Bad Request", b"empty message")
            return

        logger.info("VoiceConnector: query (%d chars)", len(text))

        # ── Dispatch (serialized) ─────────────────────────────────────────────
        reply = await self._dispatch(text)

        # ── Response ──────────────────────────────────────────────────────────
        encoded = reply.encode("utf-8")
        _write_response(writer, 200, "OK", encoded, content_type="text/plain; charset=utf-8")
        await writer.drain()
        logger.info("VoiceConnector: reply sent (%d chars)", len(reply))

    async def _dispatch(self, text: str) -> str:
        """Inject a voice message, wait for the agent reply, return it.

        The reply queue is drained at the start of each dispatch (inside the lock)
        to discard any stale reply left by a previous request that timed out while
        the agent was still running.  The lock serializes requests, so a stale item
        can only arrive here if the previous turn timed out — draining clears it.

        Residual race: if a stale reply arrives *during* the current wait (i.e. the
        previous agent finishes after we drain but before we get our own reply) we
        would consume it.  This is acceptable for the sequential Siri use-case.
        The robust fix is per-request correlation (unique room IDs); deferred as a
        follow-up for multi-user / concurrent-request support.
        """
        async with self._request_lock:
            # Drain any stale reply from a prior timed-out request.
            while not self._reply_queue.empty():
                stale = self._reply_queue.get_nowait()
                logger.debug("VoiceConnector: discarding stale reply (%d chars)", len(stale))

            if not self._handler:
                logger.warning("VoiceConnector: handler not registered yet")
                return _BUSY_REPLY

            msg = IncomingMessage(
                id=f"voice-{uuid.uuid4().hex[:8]}",
                timestamp="",
                room=_VOICE_ROOM,
                sender=_VOICE_USER,
                role=UserRole.OWNER,
                text=text,
            )

            accepted = await self._handler(msg)
            if not accepted:
                logger.warning("VoiceConnector: message dropped (queue full)")
                return _BUSY_REPLY

            try:
                return await asyncio.wait_for(
                    self._reply_queue.get(), timeout=float(self._config.timeout)
                )
            except asyncio.TimeoutError:
                logger.warning("VoiceConnector: agent reply timed out after %ds", self._config.timeout)
                return _TIMEOUT_REPLY


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    body: bytes,
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    """Write a minimal HTTP/1.1 response."""
    writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode())
    writer.write(f"Content-Type: {content_type}\r\n".encode())
    writer.write(f"Content-Length: {len(body)}\r\n".encode())
    writer.write(b"Connection: close\r\n")
    writer.write(b"\r\n")
    if body:
        writer.write(body)
