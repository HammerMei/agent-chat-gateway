"""Tests for RCWebSocketClient subscription confirmation (P0-2).

Covers:
  - subscribe_room waits for 'ready' confirmation
  - subscribe_room raises on 'nosub' rejection
  - subscribe_room raises on timeout
  - local state rolled back on failure
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.connectors.rocketchat.websocket import RCWebSocketClient


def _make_client() -> RCWebSocketClient:
    return RCWebSocketClient(
        server_url="http://localhost:3000",
        username="bot",
        password="pass",
    )


class TestSubscriptionConfirmation(unittest.IsolatedAsyncioTestCase):

    async def test_subscribe_succeeds_on_ready(self):
        """subscribe_room returns sub_id when server sends 'ready'."""
        client = _make_client()
        client._ws = MagicMock()
        client._ws.send = AsyncMock()

        # Capture the sub_id so we can simulate a ready response
        sent_frames: list[dict] = []

        async def capture_send(data_str):
            sent_frames.append(json.loads(data_str))

        client._ws.send = capture_send

        async def simulate_ready():
            # Wait for the sub frame to be sent
            while not sent_frames:
                await asyncio.sleep(0)
            sub_id = sent_frames[0]["id"]
            # Simulate server 'ready' response
            fut = client._pending_subs.get(sub_id)
            if fut and not fut.done():
                fut.set_result(True)

        task = asyncio.create_task(simulate_ready())
        sub_id = await client.subscribe_room("room_1", AsyncMock())
        await task

        self.assertIsNotNone(sub_id)
        self.assertEqual(client._subscriptions.get("room_1"), sub_id)

    async def test_subscribe_raises_on_nosub(self):
        """subscribe_room raises RuntimeError when server sends 'nosub'."""
        client = _make_client()
        client._ws = MagicMock()

        sent_frames: list[dict] = []

        async def capture_send(data_str):
            sent_frames.append(json.loads(data_str))

        client._ws.send = capture_send

        async def simulate_nosub():
            while not sent_frames:
                await asyncio.sleep(0)
            sub_id = sent_frames[0]["id"]
            fut = client._pending_subs.get(sub_id)
            if fut and not fut.done():
                fut.set_exception(RuntimeError("Subscription rejected by server: room not found"))

        task = asyncio.create_task(simulate_nosub())
        with self.assertRaises(RuntimeError) as ctx:
            await client.subscribe_room("room_bad", AsyncMock())
        await task

        self.assertIn("rejected", str(ctx.exception).lower())
        # Local state must be rolled back
        self.assertNotIn("room_bad", client._subscriptions)
        self.assertNotIn("room_bad", client._callbacks)

    async def test_subscribe_raises_on_timeout(self):
        """subscribe_room raises TimeoutError when server doesn't respond."""
        client = _make_client()
        client._ws = MagicMock()
        client._ws.send = AsyncMock()

        with self.assertRaises(asyncio.TimeoutError):
            await client.subscribe_room("room_slow", AsyncMock(), timeout=0.05)

        # Local state must be rolled back
        self.assertNotIn("room_slow", client._subscriptions)
        self.assertNotIn("room_slow", client._callbacks)

    async def test_pending_subs_cleaned_up_on_success(self):
        """_pending_subs entry is removed after successful confirmation."""
        client = _make_client()
        client._ws = MagicMock()
        client._ws.send = AsyncMock()

        async def auto_confirm():
            await asyncio.sleep(0)
            for sub_id, fut in list(client._pending_subs.items()):
                if not fut.done():
                    fut.set_result(True)

        task = asyncio.create_task(auto_confirm())
        await client.subscribe_room("room_1", AsyncMock())
        await task

        self.assertEqual(len(client._pending_subs), 0)


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round16_fixes.py ───────────────────────────────────────


class TestUnsubscribeRacePrevented(unittest.IsolatedAsyncioTestCase):
    """unsubscribe_room must not leave a room re-registered after concurrent resubscription."""

    def _make_ws(self):
        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._ws = MagicMock()
        ws._running = True
        ws._callbacks = {}
        ws._subscriptions = {}
        ws._subscription_states = {}
        ws._pending_results = {}
        ws._pending_subs = {}
        ws._reconnect_delay = 1.0
        ws._max_reconnect_delay = 30.0
        ws._callback_tasks = set()
        ws._callback_sem = asyncio.Semaphore(10)
        ws._room_queues = {}
        ws._room_workers = {}
        ws._resubscribe_task = None
        ws._rooms_unsubscribing = set()
        return ws

    async def test_rooms_unsubscribing_marker_is_set_during_unsubscribe(self):
        """unsubscribe_room must mark the room in _rooms_unsubscribing before awaiting."""
        ws = self._make_ws()
        room_id = "ROOM001"
        marker_seen = False

        original_send = AsyncMock()

        async def capture_marker(msg):
            nonlocal marker_seen
            if msg.get("msg") == "unsub":
                marker_seen = room_id in ws._rooms_unsubscribing
            await original_send(msg)

        ws._send = capture_marker
        ws._subscriptions[room_id] = "sub-old"
        ws._callbacks[room_id] = AsyncMock()
        ws._subscription_states[room_id] = MagicMock()

        await ws.unsubscribe_room(room_id)

        self.assertTrue(marker_seen, "_rooms_unsubscribing did not contain the room during send")

    async def test_rooms_unsubscribing_marker_cleared_after_unsubscribe(self):
        """unsubscribe_room must remove the room from _rooms_unsubscribing when done."""
        ws = self._make_ws()
        room_id = "ROOM002"
        ws._send = AsyncMock()

        await ws.unsubscribe_room(room_id)

        self.assertNotIn(room_id, ws._rooms_unsubscribing)

    async def test_subscribe_with_confirmation_rolls_back_if_room_being_unsubscribed(self):
        """_subscribe_with_confirmation must roll back if room is in _rooms_unsubscribing."""
        from gateway.connectors.rocketchat.websocket import SubscriptionState

        ws = self._make_ws()
        room_id = "ROOM003"
        callback = AsyncMock()

        state = SubscriptionState(room_id=room_id, callback=callback)
        ws._subscription_states[room_id] = state

        ws._rooms_unsubscribing.add(room_id)

        async def mock_send(msg):
            if msg.get("msg") == "sub":
                sub_id = msg.get("id")
                if sub_id in ws._pending_subs:
                    fut = ws._pending_subs[sub_id]
                    fut.set_result(True)

        ws._send = mock_send

        with self.assertRaises(RuntimeError) as cm:
            await ws._subscribe_with_confirmation(
                room_id=room_id,
                callback=callback,
                timeout=5.0,
                keep_callback_on_failure=True,
            )

        self.assertIn("unsubscribed", str(cm.exception).lower())
        self.assertNotIn(room_id, ws._callbacks)
        self.assertNotIn(room_id, ws._subscriptions)

    async def test_rooms_unsubscribing_marker_cleared_even_on_exception(self):
        """_rooms_unsubscribing must be cleared even when an exception occurs."""
        ws = self._make_ws()
        room_id = "ROOM004"

        async def failing_send(msg):
            if msg.get("msg") == "unsub":
                raise RuntimeError("WebSocket closed")

        ws._send = failing_send
        ws._subscriptions[room_id] = "sub-old"
        ws._callbacks[room_id] = AsyncMock()

        with self.assertRaises(RuntimeError):
            await ws.unsubscribe_room(room_id)

        self.assertNotIn(room_id, ws._rooms_unsubscribing)
