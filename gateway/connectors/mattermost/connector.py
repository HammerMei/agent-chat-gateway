"""MattermostConnector: full Connector implementation for Mattermost.

Encapsulates ALL Mattermost-specific knowledge:
  - REST API calls (auth, posting, file upload/download, room/team/user resolution)
  - WebSocket event stream (no per-channel subscribe — see websocket.py)
  - Inbound message filtering (bot-self, allow-list, @mention, timestamp dedup)
  - Inbound message normalization (field extraction, attachment download)
  - Role resolution from allow-list config (RBAC lives here, not in core)
  - Per-channel state tracking (last processed timestamp, dedup window)

The core library (SessionManager, MessageProcessor) interacts with this
connector only through the Connector ABC defined in gateway.core.connector.

Structural note vs RocketChatConnector: Mattermost's WebSocket streams
`posted` events for every channel the bot is a member of, with no
per-channel subscribe/unsubscribe wire protocol. subscribe_room /
unsubscribe_room here are therefore local bookkeeping only (which channels
does the dispatcher currently care about) — see websocket.py's module
docstring for the confirmed payload-shape details behind this design.
"""

from __future__ import annotations

import collections
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...agents.response import AgentEvent, AgentResponse
from ...core.adapter_utils import ts_ms_to_iso_local, weekday_abbrev
from ...core.connector import (
    Connector,
    IncomingMessage,
    MessageHandler,
    Room,
)
from ...core.tz_utils import local_iana_timezone as _server_local_timezone
from .agent_chain import TurnStore
from .config import MattermostConfig
from .mentions import is_room_wide_mention
from .normalize import FilterResult, filter_mm_message, normalize_mm_message, text_mentions_bot
from .outbound import send_media as _send_media
from .outbound import send_text as _send_text
from .policy import apply_thread_policy
from .rest import MattermostREST, RoomNotFoundError, iso_to_epoch_ms_str
from .websocket import MattermostWebSocketClient

logger = logging.getLogger("agent-chat-gateway.connectors.mattermost")


# ---------------------------------------------------------------------------
# Per-channel runtime state (internal to the connector)
# ---------------------------------------------------------------------------


# Same rationale as RC's _SEEN_IDS_MAXLEN — bounds the live+replay dedup window.
_SEEN_IDS_MAXLEN = 200


@dataclass
class _ChannelState:
    """Connector-level channel state: dedup watermark + local subscriber tracking.

    Unlike RC's _RoomSubscription, there is no wire-protocol subscription to
    track — the WebSocket already streams every channel the bot belongs to.
    This just tracks which channels the dispatcher currently cares about and
    the per-channel dedup watermark/seen-id window.
    """

    room: Room
    last_processed_ts: str | None = None
    seen_ids: collections.deque = field(default_factory=lambda: collections.deque())
    seen_ids_set: set = field(default_factory=set)
    watcher_ids: set = field(default_factory=set)


class MattermostConnector(Connector):
    """Connector for Mattermost (REST v4 + WebSocket).

    Usage::

        config = MattermostConfig.from_connector_config(cc)
        connector = MattermostConnector(config)

        connector.register_handler(my_handler)
        await connector.connect()

        room = await connector.resolve_room("town-square")
        await connector.subscribe_room(room, watcher_id="abc123")

        # ... messages arrive, handler is called ...

        await connector.disconnect()
    """

    @property
    def delivery_mode(self):
        """Persistent WebSocket-driven delivery, same transport model as RC."""
        return "gateway"

    # Mattermost's default per-post character limit (ServiceSettings.MaxPostSize)
    # is 16383 — leave a safety margin below that.
    _TEXT_CHUNK_LIMIT = 16_000

    def __init__(self, config: MattermostConfig) -> None:
        self._config = config
        self._rest = MattermostREST(
            config.server_url,
            token=config.token,
            username=config.username,
            password=config.password,
        )
        self._ws = MattermostWebSocketClient(
            config.server_url, token_provider=lambda: self._rest._token
        )
        self._handler: MessageHandler | None = None
        self._capacity_check: Callable[[str], bool] | None = None
        self._channels: dict[str, _ChannelState] = {}  # channel_id -> state
        self._attachments_cache_base = (
            Path(config.attachments.cache_dir_global).expanduser() / config.name
        )
        self._turn_store: TurnStore | None = (
            TurnStore(ttl_seconds=config.agent_chain.ttl_seconds)
            if config.agent_chain.agent_usernames
            else None
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Authenticate, resolve identity + team, and open the WebSocket."""
        await self._rest.authenticate()
        # Mandatory regardless of auth mode: PAT mode has no login response to
        # pull an identity from, and the own-message filter needs bot_user_id.
        await self._rest.get_me()
        await self._rest.resolve_team(self._config.team)

        self._ws.register_handler(self._on_posted_event)
        self._ws.set_reconnect_callback(self._on_ws_reconnect)
        await self._ws.connect()
        await self._ws.start()
        logger.info(
            "MattermostConnector connected to %s as %s (team=%s)",
            self._config.server_url,
            self._rest.bot_username,
            self._config.team,
        )

    async def disconnect(self) -> None:
        """Close the WebSocket and release HTTP client resources."""
        await self._ws.stop()
        await self._rest.close()
        logger.info("MattermostConnector disconnected")

    # Maximum messages fetched per channel during a reconnect history replay —
    # same rationale and value as RC's _REPLAY_HISTORY_COUNT.
    _REPLAY_HISTORY_COUNT = 200

    async def _on_ws_reconnect(self) -> None:
        """Replay messages missed during a WebSocket outage.

        Mirrors RocketChatConnector._on_ws_reconnect: for each tracked
        channel with a known watermark, fetch missed messages via REST
        history and re-inject them through the normal filter/normalize/
        dispatch pipeline (_on_posted_event, with is_replay=True).

        Mention detection differs from live dispatch: Mattermost's REST
        history API returns bare Post objects with no mention data at all
        (unlike the WS event's sibling `mentions` field) — see
        normalize.text_mentions_bot for the text-based fallback this uses
        instead, and its documented limitation (only detects mentions of
        the bot itself, not other agents in the same message).
        """
        logger.info(
            "WebSocket reconnected — replaying missed messages for %d channel(s)",
            len(self._channels),
        )
        for channel_id, state in list(self._channels.items()):
            # Snapshot the watermark NOW, before any await in this iteration —
            # same race rationale as RC: a concurrent live message must not be
            # allowed to advance the watermark mid-replay and cause the rest
            # of this channel's replay window to be skipped as "already
            # processed".
            watermark = state.last_processed_ts
            if not watermark:
                logger.debug("Channel '%s': no watermark yet — skipping replay", state.room.name)
                continue
            try:
                raw_msgs = await self._rest.get_room_history(
                    channel_id, count=self._REPLAY_HISTORY_COUNT, after_ts=watermark
                )
            except Exception as e:
                logger.warning("Channel '%s': failed to fetch history for replay: %s", state.room.name, e)
                continue

            if not raw_msgs:
                logger.debug("Channel '%s': no missed messages since %s", state.room.name, watermark)
                continue

            if len(raw_msgs) == self._REPLAY_HISTORY_COUNT:
                logger.warning(
                    "Channel '%s': replay fetched the maximum %d message(s) — "
                    "the outage window may have produced more; some messages "
                    "could be permanently lost",
                    state.room.name, self._REPLAY_HISTORY_COUNT,
                )
            else:
                logger.info(
                    "Channel '%s': replaying %d missed message(s) since %s",
                    state.room.name, len(raw_msgs), watermark,
                )

            for idx, post in enumerate(raw_msgs):
                if channel_id not in self._channels:
                    logger.debug(
                        "Channel '%s' was unsubscribed during replay — skipping %d remaining message(s)",
                        state.room.name, len(raw_msgs) - idx,
                    )
                    break
                decoded = self._synthesize_decoded_for_replay(post)
                await self._on_posted_event(decoded, is_replay=True, replay_after_ts=watermark)

    def _synthesize_decoded_for_replay(self, post: dict) -> dict:
        """Build a decoded-event dict for a REST-history post (replay path only).

        REST history posts have no `mentions` sibling field (see
        text_mentions_bot's docstring) — approximated here as [bot_user_id]
        when the bot's username appears in the text, so the existing
        bot_user_id-in-mentions check in filter_mm_message works unchanged
        for both live and replayed messages.
        """
        mentions: list[str] = []
        bot_username = self._rest.bot_username or ""
        if text_mentions_bot(post.get("message", ""), bot_username) and self._rest.bot_user_id:
            mentions = [self._rest.bot_user_id]
        return {"post": post, "mentions": mentions, "channel_type": None, "channel_name": None, "team_id": None}

    # ── Inbound ──────────────────────────────────────────────────────────────

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def register_capacity_check(self, check) -> None:
        self._capacity_check = check

    # ── Outbound ─────────────────────────────────────────────────────────────

    async def send_text(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None = None,
    ) -> None:
        """Post an agent response to the channel.

        ``thread_id`` is forwarded as Mattermost's ``root_id`` so the reply
        lands in the correct thread.
        """
        await _send_text(
            self._rest,
            room_id,
            response.text,
            chunk_limit=self.text_chunk_limit,
            root_id=thread_id,
        )

    async def notify_agent_event(
        self,
        room_id: str,
        event: AgentEvent,
        thread_id: str | None = None,
    ) -> None:
        """Refresh the typing indicator on each intermediate agent event.

        Same rationale as RC: keeps a live indicator visible for long-running
        turns (tool calls, permission approvals) instead of it silently
        expiring mid-turn.  Errors are swallowed — a failed typing refresh
        must never abort an agent turn.
        """
        if event.kind == "final":
            return
        try:
            await self.notify_typing(room_id, True)
        except Exception as exc:
            logger.debug(
                "Failed to refresh typing indicator for room %s: %s", room_id, exc
            )

    async def send_media(self, room_id: str, file_path: str, caption: str = "") -> None:
        """Upload a local file to the channel."""
        await _send_media(self._rest, room_id, file_path, caption)

    async def send_to_room(
        self,
        room: str,
        text: str,
        attachment_path: str | None = None,
    ) -> None:
        """Send a message (and optional attachment) to a room by name or ID.

        Overrides the base Connector implementation for efficient direct
        REST resolution + delivery, same rationale as RC's override.
        """
        try:
            room_info = await self._rest.resolve_room(room)
            room_id = room_info["id"]
        except RoomNotFoundError:
            # Input is likely already a channel ID — use it directly.
            room_id = room

        if attachment_path:
            file_ids = await self._rest.upload_file(room_id, attachment_path)
            await self._rest.post_message(room_id, text, file_ids=file_ids)
        elif text:
            await self._rest.post_message(room_id, text)

    # ── Room resolution ───────────────────────────────────────────────────────

    async def resolve_room(self, room_name: str) -> Room:
        """Resolve a human-readable channel name to a Room object via REST."""
        info = await self._rest.resolve_room(room_name)
        return Room(
            id=info["id"],
            name=info.get("name", room_name),
            type=info.get("type", "channel"),
        )

    # ── Per-channel local bookkeeping (no wire protocol — see websocket.py) ────

    async def subscribe_room(
        self,
        room: Room,
        watcher_id: str = "",
        working_directory: str = "",
    ) -> None:
        """Start caring about inbound events for this channel.

        No wire-protocol call: the WebSocket already streams every channel
        the bot is a member of. This just registers local dispatch state —
        events for channels with no _ChannelState entry are ignored by
        _on_posted_event.
        """
        wid = watcher_id or room.id
        state = self._channels.get(room.id)
        if state is None:
            state = _ChannelState(room=room)
            self._channels[room.id] = state
            self._ws.register_channel(room.id)
        state.watcher_ids.add(wid)
        logger.info(
            "Now tracking channel '%s' (id=%s, type=%s) for watcher '%s'",
            room.name, room.id, room.type, wid,
        )

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        """Stop caring about this channel once its last watcher leaves."""
        state = self._channels.get(room_id)
        if state is None:
            return
        if watcher_id:
            state.watcher_ids.discard(watcher_id)
        if state.watcher_ids:
            logger.debug(
                "Channel %s still has %d active watcher(s) — keeping local state",
                room_id, len(state.watcher_ids),
            )
            return
        self._channels.pop(room_id, None)
        self._ws.unregister_channel(room_id)
        logger.info("Stopped tracking channel %s", room_id)

    def update_last_processed_ts(self, room_id: str, ts: str) -> None:
        state = self._channels.get(room_id)
        if state:
            state.last_processed_ts = ts

    def get_last_processed_ts(self, room_id: str) -> str | None:
        state = self._channels.get(room_id)
        return state.last_processed_ts if state else None

    # ── Attachment cache ────────────────────────────────────────────────────────

    def _cache_dir_for(self, channel_id: str) -> Path:
        safe_channel_id = re.sub(r"[^\w.\-]", "_", channel_id)
        return self._attachments_cache_base / safe_channel_id

    @property
    def text_chunk_limit(self) -> int | None:
        return self._TEXT_CHUNK_LIMIT

    # ── Security: server-injected prompt prefix ───────────────────────────────

    # Same rationale as RC's _PREFIX_UNSAFE_RE: a channel/user display name
    # containing these characters could inject fake delimiter fields into the
    # trusted header and bypass RBAC enforcement in CLAUDE.md.
    _PREFIX_UNSAFE_RE = re.compile(r"[\|\[\]\r\n]")

    @property
    def agent_username(self) -> str:
        """The bot's own Mattermost username.

        Resolved via get_me() during connect() — falls back to the
        configured username (login mode) if called before connect(), which
        is empty in token mode until connect() completes.
        """
        return self._rest.bot_username or self._config.username

    @property
    def timezone(self) -> str:
        return self._config.timezone or _server_local_timezone()

    def _compute_to_field(self, msg: IncomingMessage) -> str:
        """Compute the compact ``to:`` routing field — same semantics as RC's.

        See RocketChatConnector._compute_to_field for the full field
        vocabulary (to: me / @agent / me+@agent / @all / *).
        """
        if msg.room.type == "dm":
            return "to: me"

        own = self.agent_username
        agent_names = set(self._config.agent_chain.agent_usernames)
        mentioned = set(msg.mentions)

        own_mentioned = own in mentioned
        all_mentioned = any(is_room_wide_mention(u) for u in mentioned)
        other_agents = [
            self._PREFIX_UNSAFE_RE.sub("_", u)
            for u in msg.mentions
            if u != own and not is_room_wide_mention(u) and u in agent_names
        ]

        if not own_mentioned and not all_mentioned and not other_agents:
            return "to: *"

        parts = []
        if own_mentioned:
            parts.append("me")
        if all_mentioned:
            parts.append("@all")
        parts.extend(f"@{u}" for u in other_agents)
        return "to: " + "+".join(parts)

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        """Return the trusted Mattermost identity header for the agent prompt.

        Matches RC's actual (richer-than-CLAUDE.md-documented) format, for
        feature parity with RC's day/ts/to fields:
            [Mattermost #<channel> | from: <user> | role: <role> |
             day: <Mon-Sun> | ts: <ISO8601> | to: <addressing>]
        """
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", msg.room.name)
        safe_user = self._PREFIX_UNSAFE_RE.sub("_", msg.sender.username)
        ts = ts_ms_to_iso_local(msg.timestamp, self.timezone)
        day = weekday_abbrev(ts)
        day_part = f" | day: {day}" if day else ""
        ts_part = f" | ts: {ts}" if ts else ""
        to_part = f" | {self._compute_to_field(msg)}"
        return (
            f"[Mattermost #{safe_room} | "
            f"from: {safe_user} | "
            f"role: {msg.role.value}{day_part}{ts_part}{to_part}]"
        )

    # ── Status notifications ──────────────────────────────────────────────────

    async def notify_typing(self, room_id: str, is_typing: bool) -> None:
        """Send a typing indicator via the WebSocket user_typing action.

        Mattermost auto-clears typing indicators client-side after a few
        seconds, so is_typing=False is a no-op (nothing to explicitly clear).
        """
        if is_typing:
            try:
                await self._ws.send_typing(room_id)
            except Exception as e:
                logger.debug("Failed to send typing indicator: %s", e)

    async def notify_online(self, room_id: str, text: str) -> None:
        try:
            await self._rest.post_message(room_id, text)
        except Exception as e:
            logger.warning("Failed to post online notification: %s", e)

    async def notify_offline(self, room_id: str, text: str) -> None:
        try:
            await self._rest.post_message(room_id, text)
        except Exception as e:
            logger.warning("Failed to post offline notification: %s", e)

    def on_agent_chain_drop(self, room_id: str, thread_id: str | None, sender: str) -> None:
        """Reset the sender's turn counter after an agent-chain termination drop."""
        if self._turn_store is not None:
            self._turn_store.reset_sender(room_id, thread_id, sender)

    # ── Attachment support ────────────────────────────────────────────────────

    def supports_attachments(self) -> bool:
        return True

    async def download_attachment(self, ref: dict, dest_path: str) -> None:
        """Download a Mattermost file attachment (identified by file_id) to dest_path."""
        file_id = ref.get("file_id", "")
        await self._rest.download_file(file_id, dest_path)

    def attachment_cache_dir(self, room_id: str) -> str | None:
        """Return the global cache directory for a channel's attachments."""
        return str(self._cache_dir_for(room_id))

    # ── History ──────────────────────────────────────────────────────────────

    def supports_history(self) -> bool:
        return True

    async def fetch_room_history(
        self,
        room: Room,
        count: int,
        before_ts: str | None = None,
        after_ts: str | None = None,
    ) -> list[dict]:
        """Fetch recent channel history as normalized, filtered message dicts.

        Same contract and security boundary as RocketChatConnector's
        fetch_room_history: excludes messages from senders not in the
        owner/guest allowlist or agent chain (anonymous users are excluded
        to prevent prompt injection). The bot's own prior messages are
        included with role="agent"/username="me"; peer agents are included
        with role="agent" and their real (sanitized) username.

        Note: before_ts/after_ts are ISO 8601 strings per the Connector ABC
        contract — converted here to the epoch-ms strings
        MattermostREST.get_room_history expects natively (see that method's
        docstring), and applied as a best-effort client-side filter, not
        exact server-side pagination.
        """
        raw_msgs = await self._rest.get_room_history(
            room.id,
            count,
            before_ts=iso_to_epoch_ms_str(before_ts) if before_ts else None,
            after_ts=iso_to_epoch_ms_str(after_ts) if after_ts else None,
        )
        bot_username = self.agent_username
        owners = set(self._config.owners)
        guests = set(self._config.guests)
        peer_agents = set(self._config.agent_chain.agent_usernames)
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", room.name)
        tz = self.timezone

        result: list[dict] = []
        for m in raw_msgs:
            sender_id = m.get("user_id", "")
            if not sender_id:
                continue
            try:
                sender = await self._rest.resolve_username(sender_id)
            except Exception as e:
                logger.warning("Failed to resolve sender for history message: %s", e)
                continue

            if sender == bot_username:
                role = "agent"
                display_username = "me"
            elif sender in owners:
                role = "owner"
                display_username = self._PREFIX_UNSAFE_RE.sub("_", sender)
            elif sender in guests:
                role = "guest"
                display_username = self._PREFIX_UNSAFE_RE.sub("_", sender)
            elif sender in peer_agents:
                role = "agent"
                display_username = self._PREFIX_UNSAFE_RE.sub("_", sender)
            else:
                # Anonymous / unlisted sender — exclude for prompt injection safety.
                continue

            ts_str = ts_ms_to_iso_local(str(m.get("create_at", "")), tz)
            result.append({
                "ts": ts_str,
                "username": display_username,
                "role": role,
                "room_name": safe_room,
                "text": m.get("message", ""),
            })
        return result

    # ── Internal: posted-event dispatch ──────────────────────────────────────

    async def _on_posted_event(
        self,
        decoded: dict,
        *,
        is_replay: bool = False,
        replay_after_ts: str | None = None,
    ) -> None:
        """Filter, normalize, and dispatch one decoded WS posted-event.

        Mirrors RocketChatConnector._on_raw_ddp_message's pipeline, adapted
        for Mattermost's ID-based identity and lack of a wire subscription:
        events for channels with no local _ChannelState (i.e. no watcher has
        called subscribe_room for them) are ignored even though the socket
        delivers them, since the bot may belong to channels ACG isn't
        watching.

        Args:
            is_replay      : True when called from _on_ws_reconnect's history
                              replay path. Suppresses the "server busy" REST
                              notification to avoid spamming the user with one
                              per missed message.
            replay_after_ts: Watermark snapshotted at the start of
                              _on_ws_reconnect's replay loop for this channel.
                              When set, used for the dedup comparison instead
                              of the live state.last_processed_ts, so a
                              concurrent live message can't advance the
                              watermark mid-replay and cause the rest of the
                              replay window to be skipped as already-processed.

        seen_ids registration timing (code-review fix): registered
        immediately after the own-message/system-message checks, BEFORE the
        resolve_username() await below — not after filter_mm_message, as RC
        does. RC can register after filtering because its DDP doc already
        carries the sender's username inline, so nothing awaits between the
        dedup check and registration. Mattermost identifies senders by ID, so
        resolving a username is an unavoidable await sitting between the two
        — leaving it unregistered until after filtering re-opened the exact
        live-vs-replay duplicate-dispatch race the seen_ids window exists to
        close (confirmed via code review: two concurrent calls for the same
        message both passed the dedup check and both dispatched to the
        handler). The tradeoff versus RC's placement: a message that gets
        filtered out (e.g. sender not in the allow-list) is now marked seen
        and won't be re-evaluated on a later replay — acceptable since
        filtering is deterministic given the same message and config, unlike
        RC's rationale for delaying registration (avoiding permanent
        suppression of a message that might become eligible later).
        """
        if not self._handler:
            return

        post = decoded["post"]
        channel_id = post.get("channel_id", "")
        state = self._channels.get(channel_id)
        if not state:
            return  # Not a channel any watcher is tracking — ignore.

        msg_id = post.get("id", "")
        if msg_id and msg_id in state.seen_ids_set:
            logger.debug("Skipping already-seen message id=%s in channel %s", msg_id, channel_id)
            return

        # System messages carry no useful sender identity to resolve — and
        # filter_mm_message rejects them anyway — so skip the async username
        # resolution entirely for them.
        if post.get("type"):
            return

        sender_id = post.get("user_id", "")
        if sender_id == self._rest.bot_user_id:
            return  # Own message — skip before spending a resolve_username call.

        # Register BEFORE the first await (resolve_username) — see the
        # seen_ids registration timing note in the docstring above.
        if msg_id:
            self._remember_seen(state, msg_id)

        try:
            sender_username = await self._rest.resolve_username(sender_id)
        except Exception as e:
            logger.error("Failed to resolve sender username for id=%s: %s", sender_id, e)
            return

        filter_ts = (
            replay_after_ts
            if (is_replay and replay_after_ts is not None)
            else state.last_processed_ts
        )
        result: FilterResult = filter_mm_message(
            post=post,
            mentions=decoded["mentions"],
            sender_username=sender_username,
            config=self._config,
            room_type=state.room.type,
            last_processed_ts=filter_ts,
            bot_user_id=self._rest.bot_user_id or "",
            turn_store=self._turn_store,
        )
        if not result.accepted:
            logger.debug("Message filtered: %s (sender=%s)", result.reason, result.sender)
            return

        logger.info(
            "Filter passed for message from %s in channel '%s' — dispatching: %s",
            result.sender, state.room.name, post.get("message", "")[:80],
        )

        if self._capacity_check and not self._capacity_check(channel_id):
            logger.warning(
                "Preflight rejected for message from %s in channel '%s' — "
                "all processor queues full, skipping normalize + download",
                result.sender, state.room.name,
            )
            # msg_id was already registered before the resolve_username await
            # above — no second registration needed here.
            if not is_replay:
                try:
                    await self._rest.post_message(
                        channel_id,
                        "⚠️ Server busy — your message was dropped. Please retry.",
                        root_id=post.get("root_id") or None,
                    )
                except Exception as exc:
                    logger.debug("Best-effort busy notification failed: %s", exc)
            return

        try:
            msg: IncomingMessage = await normalize_mm_message(
                post=post,
                mentions=decoded["mentions"],
                room=state.room,
                sender_username=result.sender,
                sender_id=sender_id,
                msg_ts=result.msg_ts,
                config=self._config,
                rest=self._rest,
                cache_dir=self._cache_dir_for(channel_id),
                is_agent_chain=result.is_agent_chain,
                agent_chain_turn=result.agent_chain_turn,
                agent_chain_max_turns=result.agent_chain_max_turns,
            )
        except Exception as e:
            logger.error("Failed to normalize message: %s", e)
            return

        apply_thread_policy(msg, self._config)

        try:
            accepted = await self._handler(msg)
        except Exception as e:
            logger.error("Handler error for message from %s: %s", result.sender, e)
            return

        if not accepted:
            logger.warning("Message from %s was dropped (queue full)", result.sender)
            if msg_id:
                state.seen_ids_set.discard(msg_id)
                try:
                    state.seen_ids.remove(msg_id)
                except ValueError:
                    pass
            return

        state.last_processed_ts = result.msg_ts

    @staticmethod
    def _remember_seen(state: _ChannelState, msg_id: str) -> None:
        state.seen_ids_set.add(msg_id)
        state.seen_ids.append(msg_id)
        if len(state.seen_ids) > _SEEN_IDS_MAXLEN:
            evicted = state.seen_ids.popleft()
            state.seen_ids_set.discard(evicted)
