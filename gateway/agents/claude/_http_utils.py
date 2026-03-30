"""Shared HTTP/1.1 utilities for Claude permission broker hook servers.

Both ``ClaudePermissionBroker`` and ``CallablePermissionBroker`` run a
lightweight HTTP/1.1 server to intercept Claude's ``PreToolUse`` hook
requests.  This module provides the common request-reading and
response-building helpers so the protocol logic lives in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

_MAX_HTTP_BODY = 10 * 1024 * 1024  # 10 MB


async def read_http_body(reader: asyncio.StreamReader) -> str:
    """Read an HTTP/1.1 request and return the decoded body string.

    Raises ``ValueError`` if the Content-Length exceeds ``_MAX_HTTP_BODY``.
    Raises ``ConnectionError`` if any individual read takes longer than 30 seconds
    (e.g. subprocess hang or mid-handshake crash).
    """
    _READ_TIMEOUT = 30.0
    try:
        # Read request line
        request_line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT)
        if not request_line:
            raise ConnectionError("Empty request")

        # Read headers
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT)
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                key, value = line.decode().split(":", 1)
                headers[key.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length", "0"))
        if content_length > _MAX_HTTP_BODY:
            raise ValueError(
                f"Request body too large: {content_length} bytes "
                f"(limit {_MAX_HTTP_BODY} bytes)"
            )
        if content_length > 0:
            body_bytes = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=_READ_TIMEOUT
            )
            return body_bytes.decode()
        return ""
    except asyncio.TimeoutError:
        raise ConnectionError("Hook server read timed out")


def build_http_response(body: str, status: int = 200, status_text: str = "OK") -> bytes:
    """Build a complete HTTP/1.1 response with correct byte-level Content-Length."""
    body_bytes = body.encode()
    header = (
        f"HTTP/1.1 {status} {status_text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    return header.encode() + body_bytes


def build_error_response(error_message: str) -> bytes:
    """Build a block-decision error response."""
    body = json.dumps({"decision": "block", "reason": error_message})
    return build_http_response(body)
