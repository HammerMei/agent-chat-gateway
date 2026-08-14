"""Unit tests for MattermostConnector.

Covers:
  - format_prompt_prefix: unsafe-character sanitization, day/ts/to fields
  - _compute_to_field: the addressing vocabulary (me / @agent / me+@agent / @all / *)
  - subscribe_room / unsubscribe_room: local bookkeeping + refcounting (no
    wire-protocol call — MattermostWebSocketClient.register_channel/
    unregister_channel are asserted, not a subscribe confirmation)
  - _on_posted_event: own-message skip by ID, system-message skip, seen-id
    dedup, watermark advance only after handler acceptance, busy-notification
    suppressed during replay, single dispatch under concurrent delivery of
    the same message id (code-review regression test)
  - agent_username fallback (rest.bot_username vs config.username)
  - supports_attachments / supports_history / attachment_cache_dir
  - on_agent_chain_drop resets the turn store
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.agents.response import AgentResponse
from gateway.connectors.mattermost.config import MattermostConfig
from gateway.connectors.mattermost.connector import MattermostConnector
from gateway.core.agent_chain import AgentChainConfig
from gateway.core.connector import IncomingMessage, Room, RoomCapacity, User, UserRole
from gateway.core.watcher_rule import RoomKind


def _config(**overrides) -> MattermostConfig:
    defaults = dict(
        server_url="https://x", team="t", username="hammer.mei", password="pw",
        name="mm-test", owners=["glin"],
    )
    defaults.update(overrides)
    return MattermostConfig(**defaults)


def _make_connector(**config_overrides) -> MattermostConnector:
    connector = MattermostConnector(_config(**config_overrides))
    connector._rest.bot_username = "hammer.mei"
    connector._rest.bot_user_id = "bot-id-1"
    return connector


def _msg(**overrides) -> IncomingMessage:
    defaults = dict(
        id="m1", timestamp="1700000000000",
        room=Room(id="chan1", name="general", type="channel"),
        sender=User(id="u1", username="alice", display_name="alice"),
        role=UserRole.OWNER, text="hi",
    )
    defaults.update(overrides)
    return IncomingMessage(**defaults)


# ── format_prompt_prefix ──────────────────────────────────────────────────────


class TestFormatPromptPrefixSanitization(unittest.TestCase):
    def test_strips_unsafe_characters_from_room_and_user(self):
        connector = _make_connector()
        msg = _msg(
            room=Room(id="c1", name="gen|eral", type="channel"),
            sender=User(id="u1", username="al|ce", display_name="x"),
        )
        prefix = connector.format_prompt_prefix(msg)
        self.assertNotIn("|ce", prefix.split("from:")[1].split("|")[0])
        self.assertIn("gen_eral", prefix)
        self.assertIn("al_ce", prefix)

    def test_strips_brackets_and_newlines(self):
        connector = _make_connector()
        msg = _msg(
            room=Room(id="c1", name="gen]eral\n", type="channel"),
            sender=User(id="u1", username="a[b", display_name="x"),
        )
        prefix = connector.format_prompt_prefix(msg)
        self.assertNotIn("]", prefix.split("|")[0])
        self.assertNotIn("[", prefix.split("from:")[1])


class TestFormatPromptPrefixFields(unittest.TestCase):
    def test_includes_platform_name_and_role(self):
        connector = _make_connector()
        msg = _msg()
        prefix = connector.format_prompt_prefix(msg)
        self.assertTrue(prefix.startswith("[Mattermost #general | from: alice | role: owner"))

    def test_includes_day_and_ts_when_timestamp_parseable(self):
        connector = _make_connector()
        msg = _msg(timestamp="1700000000000")
        prefix = connector.format_prompt_prefix(msg)
        self.assertIn("day:", prefix)
        self.assertIn("ts:", prefix)

    def test_dm_to_field_is_me(self):
        connector = _make_connector()
        msg = _msg(room=Room(id="dm1", name="@alice", type="dm"))
        prefix = connector.format_prompt_prefix(msg)
        self.assertIn("to: me", prefix)


class TestComputeToField(unittest.TestCase):
    def _connector(self):
        return _make_connector(
            agent_chain=AgentChainConfig(agent_usernames=["wavebro"])
        )

    def test_dm_is_always_me(self):
        connector = self._connector()
        msg = _msg(room=Room(id="dm1", name="@alice", type="dm"), mentions=[])
        self.assertEqual(connector._compute_to_field(msg), "to: me")

    def test_no_mention_is_broadcast(self):
        connector = self._connector()
        msg = _msg(mentions=[])
        self.assertEqual(connector._compute_to_field(msg), "to: *")

    def test_bot_mentioned_directly(self):
        connector = self._connector()
        msg = _msg(mentions=["hammer.mei"])
        self.assertEqual(connector._compute_to_field(msg), "to: me")

    def test_other_agent_mentioned_not_bot(self):
        connector = self._connector()
        msg = _msg(mentions=["wavebro"])
        self.assertEqual(connector._compute_to_field(msg), "to: @wavebro")

    def test_bot_and_other_agent_mentioned(self):
        connector = self._connector()
        msg = _msg(mentions=["hammer.mei", "wavebro"])
        self.assertEqual(connector._compute_to_field(msg), "to: me+@wavebro")

    def test_room_wide_mention(self):
        connector = self._connector()
        msg = _msg(mentions=["all"])
        self.assertEqual(connector._compute_to_field(msg), "to: @all")

    def test_non_agent_user_mention_ignored(self):
        connector = self._connector()
        msg = _msg(mentions=["random_human"])
        self.assertEqual(connector._compute_to_field(msg), "to: *")


# ── subscribe_room / unsubscribe_room ─────────────────────────────────────────


class TestSubscribeUnsubscribe(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_registers_local_state_and_channel(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        room = Room(id="chan1", name="general", type="channel")

        await connector.subscribe_room(room, watcher_id="w1")

        self.assertIn("chan1", connector._channels)
        connector._ws.register_channel.assert_called_once_with("chan1")

    async def test_second_watcher_does_not_reregister_channel(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        room = Room(id="chan1", name="general", type="channel")

        await connector.subscribe_room(room, watcher_id="w1")
        await connector.subscribe_room(room, watcher_id="w2")

        connector._ws.register_channel.assert_called_once()  # only on first subscribe
        self.assertEqual(connector._channels["chan1"].watcher_ids, {"w1", "w2"})

    async def test_unsubscribe_keeps_state_while_watchers_remain(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        connector._ws.unregister_channel = MagicMock()
        room = Room(id="chan1", name="general", type="channel")
        await connector.subscribe_room(room, watcher_id="w1")
        await connector.subscribe_room(room, watcher_id="w2")

        await connector.unsubscribe_room("chan1", watcher_id="w1")

        self.assertIn("chan1", connector._channels)
        connector._ws.unregister_channel.assert_not_called()

    async def test_unsubscribe_last_watcher_removes_state(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        connector._ws.unregister_channel = MagicMock()
        room = Room(id="chan1", name="general", type="channel")
        await connector.subscribe_room(room, watcher_id="w1")

        await connector.unsubscribe_room("chan1", watcher_id="w1")

        self.assertNotIn("chan1", connector._channels)
        connector._ws.unregister_channel.assert_called_once_with("chan1")

    async def test_unsubscribe_unknown_channel_is_noop(self):
        connector = _make_connector()
        connector._ws.unregister_channel = MagicMock()
        await connector.unsubscribe_room("nonexistent", watcher_id="w1")
        connector._ws.unregister_channel.assert_not_called()


# ── send_text / send_media ────────────────────────────────────────────────────


class TestSendTextAndMedia(unittest.IsolatedAsyncioTestCase):
    async def test_send_text_forwards_root_id_as_thread_id(self):
        connector = _make_connector()
        connector._rest.post_message = AsyncMock()

        await connector.send_text("chan1", AgentResponse(text="hi", session_id=""), thread_id="root1")

        connector._rest.post_message.assert_called_once_with("chan1", "hi", root_id="root1")

    async def test_send_media_uploads_then_posts_with_file_ids(self):
        connector = _make_connector()
        connector._rest.upload_file = AsyncMock(return_value=["f1", "f2"])
        connector._rest.post_message = AsyncMock()

        await connector.send_media("chan1", "/tmp/f.txt", caption="a file")

        connector._rest.upload_file.assert_called_once_with("chan1", "/tmp/f.txt")
        connector._rest.post_message.assert_called_once_with("chan1", "a file", file_ids=["f1", "f2"])


class TestSendToRoom(unittest.IsolatedAsyncioTestCase):
    """Regression tests for a bug found post-review: send_to_room() (the
    CLI `agent-chat-gateway send <room> --attach ...` path, per
    mm-gateway-context.md) discarded upload_file()'s returned file_ids and
    called post_message() without them — the uploaded file never got linked
    to any post and rendered as an invisible orphan in the channel."""

    async def test_attachment_with_caption_links_file_ids_to_post(self):
        connector = _make_connector()
        connector._rest.resolve_room = AsyncMock(return_value={"id": "chan1", "name": "general", "type": "channel"})
        connector._rest.upload_file = AsyncMock(return_value=["f1"])
        connector._rest.post_message = AsyncMock()

        await connector.send_to_room("general", "a caption", attachment_path="/tmp/f.txt")

        connector._rest.upload_file.assert_called_once_with("chan1", "/tmp/f.txt")
        connector._rest.post_message.assert_called_once_with("chan1", "a caption", file_ids=["f1"])

    async def test_attachment_with_no_caption_still_posts_with_file_ids(self):
        """Previously: an empty caption meant post_message() was never
        called at all, so the uploaded file was never linked to any post."""
        connector = _make_connector()
        connector._rest.resolve_room = AsyncMock(return_value={"id": "chan1", "name": "general", "type": "channel"})
        connector._rest.upload_file = AsyncMock(return_value=["f1"])
        connector._rest.post_message = AsyncMock()

        await connector.send_to_room("general", "", attachment_path="/tmp/f.txt")

        connector._rest.post_message.assert_called_once_with("chan1", "", file_ids=["f1"])

    async def test_text_only_posts_without_file_ids(self):
        connector = _make_connector()
        connector._rest.resolve_room = AsyncMock(return_value={"id": "chan1", "name": "general", "type": "channel"})
        connector._rest.upload_file = AsyncMock()
        connector._rest.post_message = AsyncMock()

        await connector.send_to_room("general", "hello")

        connector._rest.upload_file.assert_not_called()
        connector._rest.post_message.assert_called_once_with("chan1", "hello")

    async def test_room_not_found_falls_back_to_raw_id(self):
        from gateway.connectors.mattermost.rest import RoomNotFoundError

        connector = _make_connector()
        connector._rest.resolve_room = AsyncMock(side_effect=RoomNotFoundError("nope"))
        connector._rest.post_message = AsyncMock()

        await connector.send_to_room("chan-raw-id", "hello")

        connector._rest.post_message.assert_called_once_with("chan-raw-id", "hello")


# ── agent_username fallback ───────────────────────────────────────────────────


class TestAgentUsername(unittest.TestCase):
    def test_falls_back_to_config_username_before_connect(self):
        connector = MattermostConnector(_config(username="hammer.mei", password="pw"))
        self.assertEqual(connector.agent_username, "hammer.mei")

    def test_uses_rest_bot_username_after_connect(self):
        connector = MattermostConnector(
            _config(token="tok", username="", password="", server_url="https://x")
        )
        connector._rest.bot_username = "resolved.bot"
        self.assertEqual(connector.agent_username, "resolved.bot")


# ── capability flags ──────────────────────────────────────────────────────────


class TestCapabilityFlags(unittest.TestCase):
    def test_supports_attachments(self):
        self.assertTrue(_make_connector().supports_attachments())

    def test_supports_history(self):
        self.assertTrue(_make_connector().supports_history())

    def test_delivery_mode_is_gateway(self):
        self.assertEqual(_make_connector().delivery_mode, "gateway")

    def test_attachment_cache_dir_namespaced_by_connector_and_channel(self):
        connector = _make_connector()
        cache_dir = connector.attachment_cache_dir("chan1")
        self.assertIn("mm-test", cache_dir)
        self.assertIn("chan1", cache_dir)


# ── on_agent_chain_drop ───────────────────────────────────────────────────────


class TestOnAgentChainDrop(unittest.TestCase):
    def test_resets_turn_store_when_configured(self):
        connector = _make_connector(
            agent_chain=AgentChainConfig(agent_usernames=["peer"], max_turns=3)
        )
        connector._turn_store.check_and_increment("chan1", None, "peer", max_turns=3)
        self.assertEqual(connector._turn_store.current_turns("chan1", None, "peer"), 1)

        connector.on_agent_chain_drop("chan1", None, "peer")

        self.assertEqual(connector._turn_store.current_turns("chan1", None, "peer"), 0)

    def test_noop_when_no_turn_store(self):
        connector = _make_connector()  # no agent_chain configured -> no TurnStore
        self.assertIsNone(connector._turn_store)
        connector.on_agent_chain_drop("chan1", None, "peer")  # must not raise


# ── _on_posted_event ──────────────────────────────────────────────────────────


class TestOnPostedEvent(unittest.IsolatedAsyncioTestCase):
    async def _connector_with_channel(self, **config_overrides):
        connector = _make_connector(**config_overrides)
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        return connector

    async def test_ignores_unsubscribed_channel(self):
        connector = _make_connector()
        received = []
        connector.register_handler(AsyncMock(side_effect=lambda m: received.append(m) or True))

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "unknown-chan", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
            "mentions": ["bot-id-1"],
        })

        self.assertEqual(received, [])

    async def test_a_system_message_in_an_unknown_channel_is_dropped(self):
        """Written before hoisting the system and own-message checks above the state
        lookup, so the reorder is demonstrably behaviour-preserving rather than
        argued to be.

        Today the state lookup discards this first; afterwards the type check does. The
        observable outcome — nothing dispatched, no username resolved — must not change.
        """
        connector = await self._connector_with_channel()
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)
        connector._rest.resolve_username = AsyncMock(
            side_effect=AssertionError("should not resolve for a system message"))

        await connector._on_posted_event({
            "post": {"channel_id": "unknown-chan", "id": "m1", "type": "system_join_channel",
                     "user_id": "u1", "message": "joined", "create_at": 1},
            "mentions": [], "channel_type": "O", "channel_name": "elsewhere", "team_id": "t1",
        })

        handler.assert_not_awaited()

    async def test_an_own_message_in_an_unknown_channel_is_dropped(self):
        connector = await self._connector_with_channel()
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)
        connector._rest.resolve_username = AsyncMock(
            side_effect=AssertionError("should not resolve own message"))

        await connector._on_posted_event({
            "post": {"channel_id": "unknown-chan", "id": "m1", "type": "",
                     "user_id": connector._rest.bot_user_id, "message": "hi", "create_at": 1},
            "mentions": [], "channel_type": "O", "channel_name": "elsewhere", "team_id": "t1",
        })

        handler.assert_not_awaited()

    async def test_own_message_skipped_before_resolve(self):
        connector = await self._connector_with_channel()
        connector._rest.resolve_username = AsyncMock(side_effect=AssertionError("should not resolve own message"))
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "bot-id-1", "message": "pong", "root_id": "", "type": "", "create_at": 1},
            "mentions": [],
        })

        handler.assert_not_called()

    async def test_system_message_skipped(self):
        connector = await self._connector_with_channel()
        connector._rest.resolve_username = AsyncMock(side_effect=AssertionError("should not resolve system message"))
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "joined", "root_id": "", "type": "system_join_channel", "create_at": 1},
            "mentions": [],
        })

        handler.assert_not_called()

    async def test_accepted_message_dispatched_and_watermark_advanced(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        received = []

        async def handler(msg):
            received.append(msg)
            return True
        connector.register_handler(handler)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 12345},
            "mentions": ["bot-id-1"],
        })

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].text, "hi")
        self.assertEqual(connector.get_last_processed_ts("chan1"), "12345")

    async def test_dropped_message_does_not_advance_watermark(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        handler = AsyncMock(return_value=False)  # queue full
        connector.register_handler(handler)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 12345},
            "mentions": ["bot-id-1"],
        })

        self.assertIsNone(connector.get_last_processed_ts("chan1"))

    async def test_duplicate_message_id_skipped(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        handler = AsyncMock(return_value=True)
        connector.register_handler(handler)

        post = {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 12345}
        await connector._on_posted_event({"post": post, "mentions": ["bot-id-1"]})
        await connector._on_posted_event({"post": post, "mentions": ["bot-id-1"]})

        self.assertEqual(handler.call_count, 1)

    async def test_busy_notification_suppressed_during_replay(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector._rest.post_message = AsyncMock()
        connector.register_handler(AsyncMock(return_value=True))
        connector.register_capacity_check(lambda room_id: RoomCapacity.FULL)

        await connector._on_posted_event(
            {
                "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
                "mentions": ["bot-id-1"],
            },
            is_replay=True,
        )

        connector._rest.post_message.assert_not_called()

    async def test_busy_notification_sent_when_not_replay(self):
        connector = await self._connector_with_channel(owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector._rest.post_message = AsyncMock()
        connector.register_handler(AsyncMock(return_value=True))
        connector.register_capacity_check(lambda room_id: RoomCapacity.FULL)

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
            "mentions": ["bot-id-1"],
        })

        connector._rest.post_message.assert_called_once()

    async def test_concurrent_delivery_of_same_message_dispatches_once(self):
        """Regression test for a race fixed in code review: resolve_username's
        await used to sit between the seen_ids dedup check and registration,
        so two concurrent deliveries of the identical message (e.g. a live WS
        event racing a reconnect-replay of the same post) both passed the
        dedup check and both reached the handler. Registration now happens
        before that await, so a real yield point inside resolve_username
        (forced here via asyncio.sleep) must not let the second call through.
        """
        connector = await self._connector_with_channel(owners=["alice"])

        async def slow_resolve_username(user_id):
            await asyncio.sleep(0.01)  # force a genuine yield point
            return "alice"

        connector._rest.resolve_username = AsyncMock(side_effect=slow_resolve_username)
        received = []

        async def handler(msg):
            received.append(msg)
            return True

        connector.register_handler(handler)
        # Requires a mention to pass filter_mm_message's require_mention gate
        # (default True), so mentions=["bot-id-1"] is needed — but that also
        # makes normalize_mm_message call resolve_username("bot-id-1") once
        # (unrelated: resolving the mention for msg.mentions), separately
        # from resolve_username("u1") for the sender. Only the latter is what
        # the race duplicates, so assert on calls-for-the-sender-id
        # specifically rather than total call_count.
        decoded = {
            "post": {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1},
            "mentions": ["bot-id-1"],
        }

        await asyncio.gather(
            connector._on_posted_event(decoded),
            connector._on_posted_event(decoded, is_replay=True, replay_after_ts=None),
        )

        self.assertEqual(len(received), 1, "message must dispatch exactly once under concurrent delivery")
        sender_resolutions = [
            c for c in connector._rest.resolve_username.call_args_list if c.args == ("u1",)
        ]
        self.assertEqual(
            len(sender_resolutions), 1,
            "sender identity must be resolved exactly once, not once per concurrent delivery",
        )


# ── _on_ws_reconnect (history replay) ─────────────────────────────────────────


class TestOnWsReconnect(unittest.IsolatedAsyncioTestCase):
    async def _connector_with_channel(self, watermark=None, **config_overrides):
        connector = _make_connector(**config_overrides)
        room = Room(id="chan1", name="general", type="channel")
        connector._ws.register_channel = MagicMock()
        await connector.subscribe_room(room, watcher_id="w1")
        if watermark is not None:
            connector.update_last_processed_ts("chan1", watermark)
        return connector

    async def test_skips_channel_with_no_watermark(self):
        connector = await self._connector_with_channel(watermark=None)
        connector._rest.get_room_history = AsyncMock()

        await connector._on_ws_reconnect()

        connector._rest.get_room_history.assert_not_called()

    async def test_replays_missed_messages_via_rest_history(self):
        connector = await self._connector_with_channel(watermark="1000", owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector._rest.get_room_history = AsyncMock(return_value=[
            {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1500},
        ])
        received = []

        async def handler(msg):
            received.append(msg)
            return True

        connector.register_handler(handler)

        await connector._on_ws_reconnect()

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].text, "hi")

    async def test_history_fetched_with_raw_epoch_ms_watermark_not_iso(self):
        """The internal replay watermark is an epoch-ms string (matching
        post['create_at']'s native units) — it must be passed to
        get_room_history() untouched, NOT converted to/from ISO."""
        connector = await self._connector_with_channel(watermark="1234567890")
        connector._rest.get_room_history = AsyncMock(return_value=[])

        await connector._on_ws_reconnect()

        connector._rest.get_room_history.assert_called_once_with(
            "chan1", count=connector._REPLAY_HISTORY_COUNT, after_ts="1234567890"
        )

    async def test_replay_uses_snapshotted_watermark_not_live_one(self):
        """If state.last_processed_ts changes while get_room_history() is
        in flight (e.g. a concurrent live message advances it), the replay
        loop must keep filtering against the watermark it captured at the
        start of the iteration — not the mutated live value — so in-window
        replayed messages aren't wrongly rejected as already-processed."""
        connector = await self._connector_with_channel(watermark="1000", owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")

        async def mutate_watermark_then_return(*args, **kwargs):
            # Simulate a concurrent live message advancing the watermark
            # while this REST call is "in flight".
            connector.update_last_processed_ts("chan1", "9999999999")
            return [
                {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei hi", "root_id": "", "type": "", "create_at": 1500},
            ]

        connector._rest.get_room_history = AsyncMock(side_effect=mutate_watermark_then_return)
        received = []

        async def handler(msg):
            received.append(msg)
            return True

        connector.register_handler(handler)

        await connector._on_ws_reconnect()

        self.assertEqual(
            len(received), 1,
            "message must still be replayed using the snapshotted watermark, "
            "not incorrectly filtered against the mutated live watermark",
        )

    async def test_stops_replaying_channel_unsubscribed_mid_loop(self):
        connector = await self._connector_with_channel(watermark="1000", owners=["alice"])
        connector._rest.resolve_username = AsyncMock(return_value="alice")
        connector._rest.get_room_history = AsyncMock(return_value=[
            {"id": "p1", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei one", "root_id": "", "type": "", "create_at": 1500},
            {"id": "p2", "channel_id": "chan1", "user_id": "u1", "message": "@hammer.mei two", "root_id": "", "type": "", "create_at": 1600},
        ])
        received = []

        async def handler(msg):
            received.append(msg)
            connector._channels.pop("chan1", None)  # simulate unsubscribe mid-replay
            return True

        connector.register_handler(handler)

        await connector._on_ws_reconnect()

        self.assertEqual(len(received), 1)  # second message's replay was skipped


# ── fetch_room_history: ISO <-> epoch-ms boundary ────────────────────────────


class TestFetchRoomHistoryTimestampWiring(unittest.IsolatedAsyncioTestCase):
    async def test_iso_before_after_converted_to_epoch_ms_for_rest_call(self):
        connector = _make_connector()
        connector._rest.get_room_history = AsyncMock(return_value=[])

        await connector.fetch_room_history(
            Room(id="chan1", name="general", type="channel"),
            count=10,
            before_ts="2026-01-01T00:00:00+00:00",
            after_ts="2025-12-31T00:00:00+00:00",
        )

        connector._rest.get_room_history.assert_called_once_with(
            "chan1", 10,
            before_ts="1767225600000",
            after_ts="1767139200000",
        )

    async def test_none_before_after_passed_through_as_none(self):
        connector = _make_connector()
        connector._rest.get_room_history = AsyncMock(return_value=[])

        await connector.fetch_room_history(
            Room(id="chan1", name="general", type="channel"), count=10,
        )

        connector._rest.get_room_history.assert_called_once_with(
            "chan1", 10, before_ts=None, after_ts=None,
        )

    async def test_history_results_mapped_with_role_and_display_username(self):
        connector = _make_connector(owners=["alice"], guests=["bob"])
        connector._rest.get_room_history = AsyncMock(return_value=[
            {"user_id": "u-bot", "create_at": 1000, "message": "pong"},
            {"user_id": "u-alice", "create_at": 2000, "message": "hi"},
            {"user_id": "u-mallory", "create_at": 3000, "message": "spam"},
        ])
        connector._rest.resolve_username = AsyncMock(side_effect=lambda uid: {
            "u-bot": "hammer.mei", "u-alice": "alice", "u-mallory": "mallory",
        }[uid])

        result = await connector.fetch_room_history(
            Room(id="chan1", name="general", type="channel"), count=10,
        )

        usernames = {r["username"] for r in result}
        self.assertIn("me", usernames)     # bot's own message
        self.assertIn("alice", usernames)  # owner
        self.assertNotIn("mallory", usernames)  # unlisted sender excluded


if __name__ == "__main__":
    unittest.main()


class TestRoutingUntrackedChannels(unittest.IsolatedAsyncioTestCase):
    """"Unknown channel" stops being the end of the road (§2.2).

    The connector used to discard an event for a channel no watcher tracked. It now offers
    a `RoomRef` to a router, when one is registered — deciding whether a watcher should
    exist is the core's business, and creating one is a later increment's.
    """

    async def _connector(self, register_router=True):
        connector = _make_connector()
        # The team this connector serves. Left as a MagicMock the gate correctly refuses
        # everything, which is how the first run of these tests failed.
        connector._rest.team_id = "team-1"
        connector._ws.register_channel = MagicMock()
        connector._ws.unregister_channel = MagicMock()
        connector.register_handler(AsyncMock(return_value=True))
        self.offered = []
        if register_router:
            connector.register_router(AsyncMock(side_effect=self.offered.append))
        return connector

    def _event(self, **overrides):
        event = {
            "post": {"channel_id": "chan-new", "id": "m1", "type": "",
                     "user_id": "u1", "message": "hello", "create_at": 1},
            "mentions": [],
            "channel_type": "O",
            "channel_name": "incident-42",
            "channel_display_name": "Incident 42",
            "team_id": "team-1",
        }
        event.update(overrides)
        return event

    async def test_an_untracked_channel_is_offered_to_the_router(self):
        connector = await self._connector()
        await connector._on_posted_event(self._event())

        self.assertEqual(len(self.offered), 1)
        room = self.offered[0]
        self.assertEqual(room.id, "chan-new")
        self.assertEqual(room.kind, RoomKind.CHANNEL)
        self.assertEqual(room.name, "incident-42")

    async def test_with_no_router_registered_nothing_changes(self):
        """What keeps this branch runnable while creation is still driven by static
        config: the connector behaves exactly as it did before."""
        connector = await self._connector(register_router=False)
        await connector._on_posted_event(self._event())
        self.assertEqual(self.offered, [])

    async def test_a_dm_is_described_by_its_display_name(self):
        """`channel_name` is the opaque `<userid>__<userid>` form; the display name is the
        counterpart (§6.2)."""
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(channel_type="D", channel_name="u1__u2",
                        channel_display_name="alice", team_id=""))

        room = self.offered[0]
        self.assertEqual(room.kind, RoomKind.DM)
        self.assertEqual(room.participants, ("alice",))
        self.assertEqual(room.name, "", "a DM has no usable platform name")

    async def test_a_group_dm_carries_its_members(self):
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(channel_type="G", channel_name="1b4c4b32",
                        channel_display_name="glin, probe-bot, alice", team_id=""))

        room = self.offered[0]
        self.assertEqual(room.kind, RoomKind.GROUP_DM)
        self.assertEqual(room.participants, ("glin", "probe-bot", "alice"))

    async def test_another_team_is_not_offered(self):
        """The socket spans every team the account belongs to (§6.3), so a connector
        scoped to one team discards the others itself."""
        connector = await self._connector()
        connector._rest.team_id = "team-1"
        await connector._on_posted_event(self._event(team_id="team-2"))
        self.assertEqual(self.offered, [])

    async def test_a_dm_passes_the_team_gate(self):
        """A DM belongs to no team, so gating on equality would disable DM support by way
        of a team filter — a silent loss."""
        connector = await self._connector()
        connector._rest.team_id = "team-1"
        await connector._on_posted_event(
            self._event(channel_type="D", channel_display_name="alice", team_id=""))
        self.assertEqual(len(self.offered), 1)

    async def test_a_replayed_event_is_not_offered_and_not_swallowed(self):
        """The trap this PR had to avoid.

        The reconnect path synthesizes events from REST history, which carries bare posts:
        `channel_type`, `channel_name` and `team_id` are all None. A team gate reading
        those would drop the entire replay window, and a router asked to judge one would be
        judging None. Replayed posts belong to channels already tracked, so they reach the
        normal path — this only asserts they are never mistaken for a routing candidate.
        """
        connector = await self._connector()
        connector._rest.team_id = "team-1"
        await connector._on_posted_event(
            self._event(channel_type=None, channel_name=None,
                        channel_display_name=None, team_id=None),
            is_replay=True,
        )
        self.assertEqual(self.offered, [])

    async def test_a_system_message_is_never_offered(self):
        """An unknown channel is now a routing candidate, which is exactly why these two
        checks moved above the channel lookup: a join notification is not a reason to
        create a watcher."""
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(post={"channel_id": "chan-new", "id": "m1",
                              "type": "system_join_channel", "user_id": "u1",
                              "message": "joined", "create_at": 1}))
        self.assertEqual(self.offered, [])

    async def test_an_own_message_is_never_offered(self):
        connector = await self._connector()
        await connector._on_posted_event(
            self._event(post={"channel_id": "chan-new", "id": "m1", "type": "",
                              "user_id": connector._rest.bot_user_id,
                              "message": "hi", "create_at": 1}))
        self.assertEqual(self.offered, [])

    async def test_a_router_failure_does_not_break_delivery(self):
        connector = await self._connector()
        connector.register_router(AsyncMock(side_effect=RuntimeError("boom")))
        with self.assertLogs("agent-chat-gateway.connectors.mattermost", "ERROR"):
            await connector._on_posted_event(self._event())


class TestReaping(unittest.IsolatedAsyncioTestCase):
    """Reaping is the room going away; unsubscribing is a watcher releasing it."""

    async def _connector_with_channel(self):
        connector = _make_connector()
        connector._ws.register_channel = MagicMock()
        connector._ws.unregister_channel = MagicMock()
        await connector.subscribe_room(
            Room(id="chan1", name="general", type="channel"), watcher_id="w1")
        await connector.subscribe_room(
            Room(id="chan1", name="general", type="channel"), watcher_id="w2")
        return connector

    async def test_reaping_drops_the_state_even_with_watchers_holding_it(self):
        """`unsubscribe_room` returns early while another watcher holds the room, which is
        the right answer for a release and the wrong one for a room that is gone."""
        connector = await self._connector_with_channel()
        connector.reap_room("chan1")

        self.assertNotIn("chan1", connector._channels)
        connector._ws.unregister_channel.assert_called_once_with("chan1")

    async def test_reaping_an_unknown_channel_is_not_an_error(self):
        """The caller is reacting to an event that may arrive more than once."""
        connector = await self._connector_with_channel()
        connector.reap_room("never-seen")
        connector._ws.unregister_channel.assert_not_called()
        self.assertIn("chan1", connector._channels)


class TestRoomTypeMapping(unittest.TestCase):
    """All four Mattermost channel types, because the previous mapping knew one.

    It recognised `P` and called everything else a channel, which was harmless while only
    the `@username` path could produce a DM — and would stop being harmless the moment an
    id-based lookup existed, since a DM typed as a channel inverts every `type == "dm"`
    gate: the mention requirement would start applying to DMs, which §2.7 records as making
    the agent answer every message from anyone in the room.
    """

    def test_each_type_maps_to_its_own_room_type(self):
        from gateway.connectors.mattermost.rest import room_type_for

        self.assertEqual(room_type_for("O"), "channel")
        self.assertEqual(room_type_for("P"), "group")
        self.assertEqual(room_type_for("D"), "dm")
        self.assertEqual(room_type_for("G"), "group_dm")

    def test_an_unknown_type_falls_back_to_channel(self):
        """The conservative answer: a channel requires a mention where a DM does not, so
        guessing `channel` cannot turn a quiet room into a talkative one."""
        from gateway.connectors.mattermost.rest import room_type_for

        for value in (None, "", "X", "unknown"):
            with self.subTest(value=value):
                self.assertEqual(room_type_for(value), "channel")


class TestResolveRoomById(unittest.IsolatedAsyncioTestCase):
    """Boot and recreation resolve by id, never by a persisted name (§2.3).

    A name freed by a rename can be reused by a different room, so resolving by name would
    bind an existing session to the wrong one.
    """

    def _connector(self, channel):
        connector = _make_connector()
        connector._rest.get_channel = AsyncMock(return_value=channel)
        return connector

    async def test_a_channel_keeps_its_platform_name(self):
        connector = self._connector(
            {"id": "c1", "name": "incident-42", "display_name": "Incident 42",
             "type": "channel"})
        room = await connector.resolve_room_by_id("c1")

        self.assertEqual((room.id, room.name, room.type), ("c1", "incident-42", "channel"))

    async def test_a_dm_uses_its_display_name(self):
        """A DM's platform name is the opaque `<userid>__<userid>` form, and this value
        reaches the prompt prefix and the history header — both of which want a room a
        human recognises."""
        connector = self._connector(
            {"id": "d1", "name": "u1__u2", "display_name": "alice", "type": "dm"})
        room = await connector.resolve_room_by_id("d1")

        self.assertEqual(room.name, "alice")
        self.assertEqual(room.type, "dm")

    async def test_a_group_dm_uses_its_display_name(self):
        connector = self._connector(
            {"id": "g1", "name": "1b4c4b32", "display_name": "glin, alice",
             "type": "group_dm"})
        room = await connector.resolve_room_by_id("g1")

        self.assertEqual(room.name, "glin, alice")
        self.assertEqual(room.type, "group_dm")

    async def test_a_nameless_channel_falls_back_to_its_id(self):
        connector = self._connector(
            {"id": "c9", "name": "", "display_name": "", "type": "channel"})
        room = await connector.resolve_room_by_id("c9")

        self.assertEqual(room.name, "c9")
