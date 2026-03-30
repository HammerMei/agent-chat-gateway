"""Tests for WebSocket call_method fast-fail when disconnected (P1-1).

Covers:
  - call_method returns {} immediately when WebSocket is disconnected
  - is_connected reflects WebSocket state
  - call_method still works normally when connected
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.connectors.rocketchat.websocket import RCWebSocketClient


def _make_client() -> RCWebSocketClient:
    return RCWebSocketClient(
        server_url="http://localhost:3000",
        username="bot",
        password="pass",
    )


class TestCallMethodFastFail(unittest.IsolatedAsyncioTestCase):

    async def test_disconnected_returns_immediately(self):
        """call_method returns {} in <100ms when WebSocket is None (disconnected)."""
        client = _make_client()
        self.assertIsNone(client._ws)

        start = time.monotonic()
        result = await client.call_method("some-method", ["arg1"])
        elapsed = time.monotonic() - start

        self.assertEqual(result, {})
        self.assertLess(elapsed, 0.1)  # must not wait for timeout

    async def test_is_connected_false_when_no_ws(self):
        client = _make_client()
        self.assertFalse(client.is_connected)

    async def test_is_connected_true_when_ws_exists(self):
        client = _make_client()
        client._ws = MagicMock()
        self.assertTrue(client.is_connected)

    async def test_connected_call_method_works(self):
        """call_method sends and waits for result when connected."""
        client = _make_client()
        client._ws = MagicMock()

        sent: list[dict] = []

        async def capture(data_str):
            sent.append(json.loads(data_str))

        client._ws.send = capture

        async def resolve_result():
            await asyncio.sleep(0)
            for mid, fut in list(client._pending_results.items()):
                if not fut.done():
                    fut.set_result({"msg": "result", "id": mid, "result": "ok"})

        task = asyncio.create_task(resolve_result())
        result = await client.call_method("test-method", ["a", "b"])
        await task

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "test-method")
        self.assertEqual(result.get("result"), "ok")


if __name__ == "__main__":
    unittest.main()
