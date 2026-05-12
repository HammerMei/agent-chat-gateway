"""Tests for the history handoff feature (Phase 1).

Covers:
  - RocketChatConnector.fetch_room_history(): filtering, normalization, format
  - WatcherLifecycle: history inject on new session, skip on resume
  - ContextInjector: history_context appears before context files in prompt
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _make_config(
    owners: list[str] | None = None,
    guests: list[str] | None = None,
    bot_username: str = "hammer-mei",
    timezone: str = "Asia/Taipei",
    peer_agents: list[str] | None = None,
):
    from gateway.config import AttachmentConfig
    from gateway.connectors.rocketchat.config import AgentChainConfig, RocketChatConfig

    return RocketChatConfig(
        server_url="http://chat.example.com",
        username=bot_username,
        password="pw",
        name="rc",
        owners=owners or ["alice"],
        guests=guests or ["bob"],
        attachments=AttachmentConfig(cache_dir_global="/tmp/rc-cache"),
        timezone=timezone,
        agent_chain=AgentChainConfig(agent_usernames=peer_agents or []),
    )


def _make_connector(
    owners: list[str] | None = None,
    guests: list[str] | None = None,
    bot_username: str = "hammer-mei",
    timezone: str = "Asia/Taipei",
    peer_agents: list[str] | None = None,
):
    from gateway.connectors.rocketchat.connector import RocketChatConnector

    connector = RocketChatConnector.__new__(RocketChatConnector)
    connector._config = _make_config(owners, guests, bot_username, timezone, peer_agents)
    return connector


def _make_room(name: str = "nest", room_type: str = "channel", room_id: str = "ROOM_ID"):
    from gateway.core.connector import Room

    return Room(id=room_id, name=name, type=room_type)


def _rc_msg(
    username: str,
    text: str,
    ts_date: int = 1_746_000_000_000,
    msg_type: str = "",
) -> dict:
    """Build a minimal RC REST message dict."""
    m: dict = {
        "_id": f"msg-{ts_date}",
        "msg": text,
        "ts": {"$date": ts_date},
        "u": {"_id": "uid", "username": username},
    }
    if msg_type:
        m["t"] = msg_type
    return m


# ---------------------------------------------------------------------------
# RocketChatConnector.fetch_room_history — filtering & normalization
# ---------------------------------------------------------------------------


class TestFetchRoomHistory(unittest.IsolatedAsyncioTestCase):
    """Unit tests for RocketChatConnector.fetch_room_history().

    The REST layer is mocked so we test only the filtering and normalization
    logic inside the connector method.
    """

    async def test_owner_message_included_with_correct_role(self):
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "owner message", ts_date=1_746_000_001_000),
        ])
        room = _make_room()
        msgs = await connector.fetch_room_history(room, count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "owner")
        self.assertEqual(msgs[0]["username"], "alice")
        self.assertEqual(msgs[0]["text"], "owner message")

    async def test_guest_message_included_with_correct_role(self):
        connector = _make_connector(guests=["bob"])
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("bob", "guest message"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "guest")
        self.assertEqual(msgs[0]["username"], "bob")

    async def test_bot_own_message_included_as_agent(self):
        """Bot's own prior messages are included with role='agent' and username='me'."""
        connector = _make_connector(bot_username="hammer-mei")
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("hammer-mei", "I said this before"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "agent")
        self.assertEqual(msgs[0]["username"], "me")
        self.assertEqual(msgs[0]["text"], "I said this before")

    async def test_anonymous_message_excluded(self):
        """Users not in owner/guest list must be silently excluded."""
        connector = _make_connector(owners=["alice"], guests=["bob"])
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "ok"),
            _rc_msg("eve", "prompt injection attempt"),  # anonymous
            _rc_msg("bob", "also ok"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 2)
        usernames = {m["username"] for m in msgs}
        self.assertNotIn("eve", usernames)
        self.assertIn("alice", usernames)
        self.assertIn("bob", usernames)

    async def test_all_messages_anonymous_returns_empty(self):
        connector = _make_connector(owners=["alice"], guests=[])
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("eve", "sneaky msg"),
            _rc_msg("mallory", "another sneaky msg"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(msgs, [])

    async def test_room_name_in_output(self):
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "hello"),
        ])
        room = _make_room(name="nest")
        msgs = await connector.fetch_room_history(room, count=10)
        self.assertEqual(msgs[0]["room_name"], "nest")

    async def test_room_name_sanitized(self):
        """Room name with '|' must be sanitized to prevent header injection."""
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "hello"),
        ])
        room = _make_room(name="bad|room")
        msgs = await connector.fetch_room_history(room, count=10)
        self.assertNotIn("|", msgs[0]["room_name"])
        self.assertEqual(msgs[0]["room_name"], "bad_room")

    async def test_username_sanitized(self):
        """Usernames with '|' must be sanitized."""
        connector = _make_connector(owners=["evil|user"])
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("evil|user", "msg"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertNotIn("|", msgs[0]["username"])

    async def test_message_without_sender_skipped(self):
        """Messages with no 'u' field are skipped gracefully."""
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        no_sender = {"_id": "x", "msg": "text", "ts": {"$date": 1000}}
        connector._rest.get_room_history = AsyncMock(return_value=[
            no_sender,
            _rc_msg("alice", "real msg"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "real msg")

    async def test_rest_called_with_correct_args(self):
        connector = _make_connector()
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[])
        room = _make_room(room_id="CUSTOM_ID", room_type="group")
        await connector.fetch_room_history(room, count=42)
        connector._rest.get_room_history.assert_called_once_with(
            "CUSTOM_ID", "group", 42, before_ts=None, after_ts=None
        )

    async def test_before_ts_passed_through_to_rest(self):
        """before_ts is forwarded to get_room_history for backward pagination."""
        connector = _make_connector()
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[])
        room = _make_room(room_id="ROOM", room_type="channel")
        ts = "2026-05-10T10:00:00+08:00"
        await connector.fetch_room_history(room, count=50, before_ts=ts)
        connector._rest.get_room_history.assert_called_once_with(
            "ROOM", "channel", 50, before_ts=ts, after_ts=None
        )

    async def test_after_ts_passed_through_to_rest(self):
        """after_ts is forwarded to get_room_history for forward navigation."""
        connector = _make_connector()
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[])
        room = _make_room(room_id="ROOM", room_type="channel")
        ts = "2026-05-10T19:25:00+08:00"
        await connector.fetch_room_history(room, count=50, after_ts=ts)
        connector._rest.get_room_history.assert_called_once_with(
            "ROOM", "channel", 50, before_ts=None, after_ts=ts
        )

    async def test_timestamp_formatted_as_iso(self):
        """ts field must be a formatted ISO 8601 string, not raw epoch ms."""
        connector = _make_connector(owners=["alice"], timezone="Asia/Taipei")
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "hello", ts_date=1_746_057_600_000),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        ts = msgs[0]["ts"]
        # Must be an ISO string with timezone offset, not a raw int/epoch
        self.assertIsInstance(ts, str)
        self.assertIn("T", ts)   # ISO 8601 date/time separator
        self.assertIn("+", ts)   # timezone offset

    async def test_message_with_no_timestamp_has_ts_none(self):
        """Messages with missing $date produce ts=None (graceful)."""
        connector = _make_connector(owners=["alice"])
        connector._rest = AsyncMock()
        no_ts = {"_id": "x", "msg": "no timestamp", "ts": {}, "u": {"username": "alice"}}
        connector._rest.get_room_history = AsyncMock(return_value=[no_ts])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertIsNone(msgs[0]["ts"])

    async def test_empty_channel_returns_empty_list(self):
        connector = _make_connector()
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[])
        msgs = await connector.fetch_room_history(_make_room(), count=50)
        self.assertEqual(msgs, [])

    async def test_peer_agent_message_included_with_own_username(self):
        """Peer agents (agent_chain.agent_usernames) are included as role='agent'
        with their actual username — distinct from the bot's 'me' self-reference."""
        connector = _make_connector(
            owners=["alice"],
            peer_agents=["wavebro"],
        )
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "owner msg"),
            _rc_msg("wavebro", "peer agent msg"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 2)
        peer_msg = next(m for m in msgs if m["username"] == "wavebro")
        self.assertEqual(peer_msg["role"], "agent")
        self.assertEqual(peer_msg["username"], "wavebro")
        self.assertEqual(peer_msg["text"], "peer agent msg")

    async def test_peer_agent_not_included_when_not_in_agent_chain(self):
        """A user not in owners, guests, or agent_chain must be excluded."""
        connector = _make_connector(
            owners=["alice"],
            guests=[],
            peer_agents=[],  # no peer agents configured
        )
        connector._rest = AsyncMock()
        connector._rest.get_room_history = AsyncMock(return_value=[
            _rc_msg("alice", "ok"),
            _rc_msg("wavebro", "not in any list"),
        ])
        msgs = await connector.fetch_room_history(_make_room(), count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["username"], "alice")


# ---------------------------------------------------------------------------
# ContextInjector — history_context injection order
# ---------------------------------------------------------------------------


class TestContextInjectorWithHistory(unittest.IsolatedAsyncioTestCase):
    """Verify that history_context is injected after the dynamic header
    and before any user-configured context files."""

    async def test_history_context_included_in_prompt(self):
        from gateway.core.config import CoreConfig, WatcherConfig
        from gateway.core.context_injector import ContextInjector
        from gateway.core.state import WatcherState

        config = CoreConfig()
        injector = ContextInjector(config)

        ws = WatcherState(
            watcher_name="w1",
            session_id="sess1",
            room_id="r1",
            context_injected=False,
        )
        wc = WatcherConfig(name="w1", connector="rc", room="#nest", agent="claude")

        agent = AsyncMock()
        agent.send = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))

        history_block = "[SESSION HISTORY]\nfrom: alice | role: owner\nhello"
        await injector.inject(
            ws, "sess1", agent, "claude", "rc", wc,
            history_context=history_block,
        )

        prompt = agent.send.call_args.kwargs["prompt"]
        self.assertIn("SESSION HISTORY", prompt)
        self.assertIn("hello", prompt)
        # Injection order: identity header first, then history, then context files.
        # combined_context.insert(0, history); insert(0, header) → header is first.
        self.assertLess(prompt.index("ACG Session Identity"), prompt.index("SESSION HISTORY"))

    async def test_history_context_none_skips_injection_when_no_files(self):
        """When history_context=None and no context files are configured,
        agent.send must NOT be called (no empty round-trip to the agent)."""
        from gateway.core.config import CoreConfig, WatcherConfig
        from gateway.core.context_injector import ContextInjector
        from gateway.core.state import WatcherState

        config = CoreConfig()
        injector = ContextInjector(config)

        ws = WatcherState(
            watcher_name="w1",
            session_id="sess2",
            room_id="r1",
            context_injected=False,
        )
        wc = WatcherConfig(name="w1", connector="rc", room="#nest", agent="claude")

        agent = AsyncMock()
        agent.send = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))

        await injector.inject(
            ws, "sess2", agent, "claude", "rc", wc,
            history_context=None,
        )

        # No files + no history → nothing to inject → agent.send never called
        agent.send.assert_not_called()
        # But the session must be marked injected to avoid re-entry on every message
        self.assertTrue(ws.context_injected)


# ---------------------------------------------------------------------------
# WatcherLifecycle — history handoff trigger logic
# ---------------------------------------------------------------------------


class TestWatcherLifecycleHistoryHandoff(unittest.IsolatedAsyncioTestCase):
    """Integration-level tests for history handoff in _start_watcher.

    The connector, agent, and injector are all mocked — we only verify
    that fetch_room_history is (or is not) called under the right conditions.
    """

    def _make_lifecycle(self, history_enabled: bool = True, fetch_count: int = 10, verbatim_tail: int = 5):
        """Build a WatcherLifecycle with history handoff configured."""
        from gateway.core.config import CoreConfig, HistoryHandoffConfig, WatcherConfig
        from gateway.core.context_injector import ContextInjector
        from gateway.core.session_maps import SessionMaps
        from gateway.core.watcher_lifecycle import WatcherLifecycle

        wc = WatcherConfig(
            name="w1",
            connector="rc",
            room="#nest",
            agent="claude",
            history_handoff=HistoryHandoffConfig(
                enabled=history_enabled,
                fetch_count=fetch_count,
                verbatim_tail=verbatim_tail,
            ),
        )

        config = CoreConfig()
        connector = AsyncMock()
        connector.agent_username = "hammer-mei"
        connector.resolve_room = AsyncMock(return_value=MagicMock(id="r1", name="nest", type="channel"))
        connector.subscribe_room = AsyncMock()
        connector.fetch_room_history = AsyncMock(return_value=[])
        connector.get_last_processed_ts = MagicMock(return_value=None)
        connector.update_last_processed_ts = MagicMock()
        connector.attachment_cache_dir = MagicMock(return_value=None)

        agent = AsyncMock()
        agent.create_session = AsyncMock(return_value="new-sess-id")
        agent.send = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))
        agent.delete_session = AsyncMock(return_value=True)

        state_store = MagicMock()
        state_store.load = MagicMock(return_value={})
        state_store.save = MagicMock()

        dispatcher = MagicMock()
        dispatcher.add_processor = MagicMock()

        injector = ContextInjector(config)

        maps = SessionMaps()

        lifecycle = WatcherLifecycle(
            connector=connector,
            agents={"claude": agent},
            default_agent="claude",
            config=config,
            watcher_configs=[wc],
            state_store=state_store,
            dispatcher=dispatcher,
            injector=injector,
            permission_registry=None,
            maps=maps,
        )
        # Patch AttachmentWorkspace.setup to avoid filesystem calls
        lifecycle._attachment_workspace = MagicMock()
        lifecycle._attachment_workspace.setup = MagicMock(return_value="/tmp/fake")

        return lifecycle, connector, wc

    async def test_fetch_room_history_called_for_new_session(self):
        """fetch_room_history must be called when a new session is created and enabled=True."""
        lifecycle, connector, _ = self._make_lifecycle(history_enabled=True, fetch_count=20)

        # Patch MessageProcessor.start to avoid consumer loop startup
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle._start_watcher(lifecycle._watcher_configs[0], state=None)

        connector.fetch_room_history.assert_called_once()
        call_args = connector.fetch_room_history.call_args
        # count argument must match fetch_count config
        self.assertEqual(call_args[0][1], 20)

    async def test_fetch_room_history_not_called_when_disabled(self):
        """fetch_room_history must NOT be called when history_handoff.enabled=False."""
        lifecycle, connector, _ = self._make_lifecycle(history_enabled=False)

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle._start_watcher(lifecycle._watcher_configs[0], state=None)

        connector.fetch_room_history.assert_not_called()

    async def test_fetch_room_history_not_called_when_session_reused(self):
        """When a session is reused (not newly created), history must NOT be injected."""
        from gateway.core.state import WatcherState

        lifecycle, connector, wc = self._make_lifecycle(history_enabled=True)
        # Simulate an existing session — _provision_session will return created_new_session=False
        existing_state = WatcherState(
            watcher_name="w1",
            session_id="existing-session-id",
            room_id="r1",
            context_injected=True,
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle._start_watcher(wc, state=existing_state)

        connector.fetch_room_history.assert_not_called()

    async def test_fetch_failure_does_not_block_startup(self):
        """If fetch_room_history raises, the watcher must still start successfully."""
        lifecycle, connector, _ = self._make_lifecycle(history_enabled=True)
        connector.fetch_room_history = AsyncMock(side_effect=RuntimeError("network error"))

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            # Must not raise
            await lifecycle._start_watcher(lifecycle._watcher_configs[0], state=None)

        # Processor was still started despite the fetch failure
        MockProc.return_value.start.assert_called_once()
