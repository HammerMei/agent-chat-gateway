"""Unit tests for MattermostWebSocketClient.

Covers the platform-specific quirks confirmed against a live Mattermost
11.7.0 server during implementation:
  - data.post is a JSON-encoded STRING requiring its own json.loads()
  - data.mentions is ALSO a JSON-encoded STRING (of a user-id array), not a
    native array — a second undocumented quirk found via live testing
  - Absence of mentions (self-mentions, or none) decodes to an empty list
  - No per-channel wire-protocol subscribe: register_channel/unregister_channel
    are local bookkeeping only (verified by asserting no _send call)
  - Per-channel ordering: events for the same channel_id route to the same
    worker/queue
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from gateway.connectors.mattermost.websocket import MattermostWebSocketClient


def _make_ws(token: str | None = "tok") -> MattermostWebSocketClient:
    return MattermostWebSocketClient("https://mm.example.com", token_provider=lambda: token)


def _raw_posted_event(post: dict, mentions: list[str] | None = None) -> dict:
    data = {"post": json.dumps(post)}
    if mentions is not None:
        data["mentions"] = json.dumps(mentions)
    return {"event": "posted", "data": data}


# ── Construction / URL derivation ────────────────────────────────────────────


class TestConstruction(unittest.TestCase):
    def test_https_converted_to_wss(self):
        ws = _make_ws()
        self.assertEqual(ws.ws_url, "wss://mm.example.com/api/v4/websocket")

    def test_http_converted_to_ws(self):
        ws = MattermostWebSocketClient("http://mm.local", token_provider=lambda: "t")
        self.assertEqual(ws.ws_url, "ws://mm.local/api/v4/websocket")


# ── _decode_posted_event ──────────────────────────────────────────────────────


class TestDecodePostedEvent(unittest.TestCase):
    def test_double_decodes_post(self):
        ws = _make_ws()
        post = {"id": "p1", "channel_id": "c1", "message": "hi"}
        evt = _raw_posted_event(post, mentions=["u1"])

        decoded = ws._decode_posted_event(evt)

        self.assertEqual(decoded["post"], post)
        self.assertEqual(decoded["mentions"], ["u1"])

    def test_missing_mentions_decodes_to_empty_list(self):
        """Self-mentions produce no `mentions` field at all — confirmed live."""
        ws = _make_ws()
        post = {"id": "p1", "channel_id": "c1", "message": "hi"}
        evt = _raw_posted_event(post, mentions=None)

        decoded = ws._decode_posted_event(evt)

        self.assertEqual(decoded["mentions"], [])

    def test_mentions_is_json_string_not_native_array(self):
        """Regression guard for the second (undocumented) double-decode quirk:
        a raw native-array `mentions` value must NOT be passed through as-is —
        the real server always sends it JSON-encoded."""
        ws = _make_ws()
        post = {"id": "p1", "channel_id": "c1", "message": "hi"}
        evt = {"event": "posted", "data": {"post": json.dumps(post), "mentions": json.dumps(["u1", "u2"])}}

        decoded = ws._decode_posted_event(evt)

        self.assertEqual(decoded["mentions"], ["u1", "u2"])

    def test_carries_channel_metadata(self):
        ws = _make_ws()
        post = {"id": "p1", "channel_id": "c1", "message": "hi"}
        evt = _raw_posted_event(post)
        evt["data"]["channel_type"] = "O"
        evt["data"]["channel_name"] = "town-square"
        evt["data"]["team_id"] = "team-1"

        decoded = ws._decode_posted_event(evt)

        self.assertEqual(decoded["channel_type"], "O")
        self.assertEqual(decoded["channel_name"], "town-square")
        self.assertEqual(decoded["team_id"], "team-1")


# ── register_channel / unregister_channel (no wire protocol) ────────────────


class TestChannelBookkeeping(unittest.IsolatedAsyncioTestCase):
    async def test_register_channel_is_local_only(self):
        ws = _make_ws()
        ws._send = AsyncMock()

        ws.register_channel("chan1")

        self.assertIn("chan1", ws._registered_channels)
        ws._send.assert_not_called()

    async def test_unregister_channel_is_local_only(self):
        ws = _make_ws()
        ws._send = AsyncMock()
        ws.register_channel("chan1")

        ws.unregister_channel("chan1")

        self.assertNotIn("chan1", ws._registered_channels)
        ws._send.assert_not_called()


# ── _dispatch / per-channel ordering ──────────────────────────────────────────


class TestDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_routes_to_handler(self):
        ws = _make_ws()
        received = []

        async def handler(decoded):
            received.append(decoded)

        ws.register_handler(handler)
        decoded = {"post": {"channel_id": "chan1", "message": "hi"}, "mentions": []}

        await ws._dispatch(decoded)
        await asyncio.sleep(0.05)  # let the worker task run

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], decoded)

        await ws.stop()

    async def test_same_channel_events_share_one_worker(self):
        ws = _make_ws()
        order = []

        async def handler(decoded):
            order.append(decoded["post"]["id"])

        ws.register_handler(handler)
        for i in range(5):
            await ws._dispatch({"post": {"channel_id": "chan1", "id": f"p{i}"}, "mentions": []})
        await asyncio.sleep(0.05)

        self.assertEqual(order, [f"p{i}" for i in range(5)])
        self.assertEqual(len(ws._channel_workers), 1)  # one worker for the one channel

        await ws.stop()

    async def test_different_channels_get_different_workers(self):
        ws = _make_ws()

        async def handler(decoded):
            pass

        ws.register_handler(handler)
        await ws._dispatch({"post": {"channel_id": "chan1", "id": "p1"}, "mentions": []})
        await ws._dispatch({"post": {"channel_id": "chan2", "id": "p2"}, "mentions": []})
        await asyncio.sleep(0.05)

        self.assertEqual(len(ws._channel_workers), 2)

        await ws.stop()

    async def test_handler_exception_does_not_crash_worker(self):
        ws = _make_ws()
        calls = []

        async def handler(decoded):
            calls.append(decoded["post"]["id"])
            if decoded["post"]["id"] == "p0":
                raise RuntimeError("boom")

        ws.register_handler(handler)
        await ws._dispatch({"post": {"channel_id": "chan1", "id": "p0"}, "mentions": []})
        await ws._dispatch({"post": {"channel_id": "chan1", "id": "p1"}, "mentions": []})
        await asyncio.sleep(0.05)

        self.assertEqual(calls, ["p0", "p1"])  # worker survives the exception

        await ws.stop()


# ── send_typing ───────────────────────────────────────────────────────────────


class TestSendTyping(unittest.IsolatedAsyncioTestCase):
    async def test_sends_user_typing_action(self):
        ws = _make_ws()
        ws._send = AsyncMock()

        await ws.send_typing("chan1")

        ws._send.assert_called_once()
        sent = ws._send.call_args[0][0]
        self.assertEqual(sent["action"], "user_typing")
        self.assertEqual(sent["data"]["channel_id"], "chan1")
        self.assertNotIn("parent_id", sent["data"])

    async def test_sends_parent_id_when_given(self):
        ws = _make_ws()
        ws._send = AsyncMock()

        await ws.send_typing("chan1", parent_id="root1")

        sent = ws._send.call_args[0][0]
        self.assertEqual(sent["data"]["parent_id"], "root1")


# ── _authenticate ─────────────────────────────────────────────────────────────


class TestAuthenticate(unittest.IsolatedAsyncioTestCase):
    async def test_sends_authentication_challenge_with_current_token(self):
        ws = _make_ws(token="fresh-token")
        ws._send = AsyncMock()

        await ws._authenticate()

        sent = ws._send.call_args[0][0]
        self.assertEqual(sent["action"], "authentication_challenge")
        self.assertEqual(sent["data"]["token"], "fresh-token")

    async def test_no_token_raises(self):
        ws = _make_ws(token=None)
        ws._send = AsyncMock()

        with self.assertRaises(RuntimeError):
            await ws._authenticate()


if __name__ == "__main__":
    unittest.main()
