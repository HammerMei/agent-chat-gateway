"""RocketChatConnector: full Connector implementation for Rocket.Chat.

Encapsulates ALL Rocket.Chat-specific knowledge:
  - DDP WebSocket subscription per room (subscribe_room / unsubscribe_room)
  - REST API calls for posting text and uploading files
  - Inbound message filtering (bot-self, allow-list, @mention, timestamp dedup)
  - Inbound message normalization (field extraction, attachment download)
  - Role resolution from allow-list config (RBAC lives here, not in core)
  - Per-room state tracking (room type, last processed timestamp, cache path)

The core library (SessionManager, MessageProcessor) interacts with this
connector only through the Connector ABC defined in gateway.core.connector.
"""

from __future__ import annotations

import collections
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ...agents.response import AgentEvent, AgentResponse
from ...core.adapter_utils import ts_ms_to_iso_local, weekday_abbrev
from ...core.bot_identity import (
    BotIdentity,
    ConnectorIdentityError,
    canonical_origin,
)
from ...core.connector import (
    CapacityCheck,
    Connector,
    IncomingMessage,
    MessageHandler,
    Room,
    RoomCapacity,
)
from ...core.sender_policy import sender_allowed
from ...core.tz_utils import local_iana_timezone as _server_local_timezone
from ...core.watcher_manager import RoomRef
from ...core.watcher_rule import RoomKind
from .agent_chain import TurnStore
from .config import RocketChatConfig
from .mentions import is_room_wide_mention
from .normalize import FilterResult, filter_rc_message, normalize_rc_message
from .outbound import send_media as _send_media
from .outbound import send_text as _send_text
from .policy import apply_thread_policy
from .rest import RocketChatREST, RoomNotFoundError, room_type_for
from .websocket import RCWebSocketClient

logger = logging.getLogger("agent-chat-gateway.connectors.rocketchat")


# ---------------------------------------------------------------------------
# Per-room runtime state (internal to the connector)
# ---------------------------------------------------------------------------


# Sustained size of the per-room seen-id dedup window.  The eviction check
# uses strict-greater-than (len > MAXLEN) so the deque reaches MAXLEN + 1
# momentarily (one synchronous Python statement) before popleft brings it
# back to exactly MAXLEN.  The post-eviction cap is MAXLEN; the peak is
# MAXLEN + 1 but is never visible to other coroutines because no await
# occurs between append and popleft.
_SEEN_IDS_MAXLEN = 200


@dataclass
class _RoomSubscription:
    """Connector-level room state: platform subscription + shared dedup watermark.

    Owned by the connector, not by any individual watcher.
    """

    room: Room
    last_processed_ts: str | None = None
    # Where the outage started, captured before delivery is restored — see
    # `_snapshot_replay_boundaries`. `last_processed_ts` cannot answer that question by
    # the time replay runs, because the first live message through the new subscription
    # has already moved it past the gap.
    replay_boundary: str | None = None
    # Bumped whenever this account is confirmed to have left the room. A replay reads it
    # before its history fetch and again as it dispatches: the fetch and the dispatch loop
    # are long, and a live rejection arriving inside them is exactly the news that the
    # batch must not be delivered.
    membership_epoch: int = 0
    # Bounded FIFO set of recently-seen message _id values.  Used to deduplicate
    # messages that arrive on both the live DDP stream and the reconnect history
    # replay path.  deque provides O(1) append and fast len() checks while the
    # set provides O(1) membership tests.  Both are updated together so they
    # stay in sync; the deque is used only for eviction ordering.
    seen_ids: collections.deque = field(default_factory=lambda: collections.deque())
    seen_ids_set: set = field(default_factory=set)

    def left_the_room(self) -> None:
        """Record that this account is no longer a member. Three things, one call.

        A method for the same reason `remember` is one: the parts have to move together
        and one of them is easy to leave out. They were written separately at the two
        sites that learn this fact — the live `roomParticipant` gate and the REST
        membership check — and the epoch was added to one of them only, so a message in
        flight past the other one restored the watermark it had just cleared.

        * `replay_boundary` — a retained window is a promise to read it later, and after a
          removal nobody is entitled to.
        * `last_processed_ts` — the *fallback* boundary, frozen at the removal because the
          live gate remembers a rejected id without advancing it. Left set, a reconnect
          arriving before the first post-re-add message replays the whole time away.
          Empty means for a re-added room what it means for one seen for the first time.
        * `membership_epoch` — how work already in flight finds out. Clearing the marks
          cannot reach a replay or a dispatch holding its own copy of them.
        """
        self.replay_boundary = None
        # `""`, not `None`, and the difference is what survives a restart. Both are falsy
        # everywhere this value is read, but the lifecycle's save step only copies the
        # connector's watermark when it has an opinion — `None` means "this room had no
        # activity in this run, keep what is on disk", which is right for a quiet room and
        # exactly wrong here. Empty says "cleared on purpose", so the stored record is
        # overwritten and a restart cannot hand the pre-removal mark back.
        self.last_processed_ts = ""
        self.membership_epoch += 1

    def remember(self, msg_id: str) -> None:
        """Record a message id as handled, evicting the oldest past the bound.

        A method because the deque and the set have to move together and the eviction
        is easy to leave out: this existed as two copies, and a third — added for the
        unrouted path — kept the two appends and dropped the eviction, so a busy room
        with no watcher grew both containers without limit. One place to state it.
        """
        if not msg_id:
            return
        self.seen_ids_set.add(msg_id)
        self.seen_ids.append(msg_id)
        if len(self.seen_ids) > _SEEN_IDS_MAXLEN:
            self.seen_ids_set.discard(self.seen_ids.popleft())


@dataclass
class _WatcherRoomContext:
    """Per-watcher subscription membership for a shared room.

    The connector tracks watcher IDs for refcounting — when the last watcher
    for a room is removed, the DDP subscription is torn down.  All per-watcher
    filesystem concerns (working directory, attachment workspace) live in the
    core layer (``WatcherLifecycle`` / ``AttachmentWorkspace``), not here.
    """

    watcher_id: str


# ---------------------------------------------------------------------------
# RocketChatConnector
# ---------------------------------------------------------------------------


class RocketChatConnector(Connector):
    """Connector for Rocket.Chat (REST + DDP/WebSocket).

    Usage::

        config = RocketChatConfig.from_gateway_config(gateway_cfg)
        connector = RocketChatConnector(config)

        connector.register_handler(my_handler)
        await connector.connect()

        room = await connector.resolve_room("general")
        await connector.subscribe_room(
            room,
            watcher_id="abc123",
            working_directory="/path/to/cwd",
        )

        # Required, and last. Subscribing to every room the account can see happens here
        # rather than in connect(), because a message arriving before the watchers exist
        # would be treated as a room nothing tracks — offered for creation while its real
        # watcher was still being built. With no router registered this is a no-op and
        # delivery stays per-room.
        await connector.start_inbound()

        # ... messages arrive, handler is called ...

        await connector.disconnect()
    """

    @property
    def delivery_mode(self):
        """Delivery goes through the RC DDP gateway broker."""
        return "gateway"

    _TEXT_CHUNK_LIMIT = 40_000

    def __init__(self, config: RocketChatConfig) -> None:
        self._config = config
        self._rest = RocketChatREST(config.server_url)
        self._ws = RCWebSocketClient(
            config.server_url, config.username, config.password
        )
        self._handler: MessageHandler | None = None
        self._capacity_check: CapacityCheck | None = None
        self._rooms: dict[str, _RoomSubscription] = {}  # room_id -> subscription
        self._watcher_contexts: dict[
            str, list[_WatcherRoomContext]
        ] = {}  # room_id -> [watcher...]
        self._room_refcount: dict[str, int] = {}  # room_id -> subscriber count
        self._router = None
        # Rooms currently being offered to the router. The routing workers are a pool, so
        # several frames from one untracked room can be in flight at once — and offering a
        # room is slow (a DM needs `im.members` before it can even be classified), which
        # makes the overlap the normal case for a room that has just started talking rather
        # than a rare one. Two offers for one room are two watchers and two sessions for it.
        self._rooms_being_routed: set[str] = set()
        # What `start_inbound` decided, once — **not** whether the stream is live now, and
        # nothing may read it as that. The stream can die under a healthy socket, and a copy
        # of its liveness would go on saying otherwise while a watcher added in that window
        # registered a callback for a room nobody had subscribed to (§6.1, invariant 2).
        # Ask `self._ws.stream_active` for the current fact; this only records that delivery
        # started out reaching every room the account can see, which is why `subscribe_room`
        # became local bookkeeping rather than what makes a room deliver (§5.2).
        self._subscribe_all = False
        # room_id → RoomKind for direct rooms. Never invalidated, and that is a verified
        # property rather than an optimisation: on 8.5.1 every route for adding a member to
        # a type-`d` room is refused on the room's *type*, and `im.create` returns a
        # **different room id** for a different member set — so a group DM is a separate
        # room, never a mutated 1:1, and this cache cannot go stale (§6.4).
        self._dm_kinds: dict[str, tuple[RoomKind, tuple[str, ...]]] = {}
        # Global attachment cache base: {cache_dir_global}/{connector_name}/{room_id}/
        # Namespaced by connector name to avoid collisions across multi-connector deployments.
        self._attachments_cache_base = (
            Path(config.attachments.cache_dir_global).expanduser() / config.name
        )
        # TurnStore for agent-to-agent loop protection; only allocated when agents are configured.
        self._turn_store: TurnStore | None = (
            TurnStore(ttl_seconds=config.agent_chain.ttl_seconds)
            if config.agent_chain.agent_usernames
            else None
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    # Maximum messages fetched per room during a reconnect history replay.
    # Chosen to cover most realistic outage windows (e.g. 200 messages at
    # ~1 msg/s ≈ 3 minutes of traffic).  A warning is emitted when the
    # response fills the limit so operators know replay may be incomplete.
    _REPLAY_HISTORY_COUNT = 200

    async def connect(self) -> None:
        """Login via REST and establish the DDP WebSocket connection."""
        await self._rest.login(self._config.username, self._config.password)
        await self._ws.connect()
        self._ws.register_outage_callback(self._snapshot_replay_boundaries)
        self._ws.register_reconnect_callback(self._on_ws_reconnect)
        await self._ws.start()

        logger.info(
            "RocketChatConnector connected to %s as %s",
            self._config.server_url,
            self._config.username,
        )

    async def start_inbound(self) -> None:
        """Ask for every room the account can see — after the watchers exist.

        Deliberately not in `connect()`. Startup restores watchers between connecting and
        this call, and a message arriving in that window would take the *untracked* path:
        offered to the router, and either dropped or turned into a second attempt to create
        a watcher for a room whose real one was still being built. That is the same
        ordering `start_inbound` exists for on Mattermost — this connector had it backwards
        until review caught it.

        A server that refuses answers `nosub`, and the connector keeps its per-room
        subscriptions: a capability answer, not an error, so an older or differently
        configured server still runs the gateway. Only attempted when a router is
        registered — without one there is nothing to do with a message for an untracked
        room, and asking for them all would pay delivery cost for messages the connector
        immediately drops.
        """
        if self._router is None:
            return
        self._subscribe_all = await self._ws.subscribe_all()
        if self._subscribe_all:
            # Watchers are restored before this call, so every tracked room already has a
            # per-room subscription. Left in place they deliver every message a second
            # time — dedup hides the duplicate handler call, not the queue slot it takes.
            await self._ws.unsubscribe_rooms_keeping_callbacks()
        logger.info(
            "Rocket.Chat delivery: %s",
            "all rooms" if self._subscribe_all else "per room",
        )

    async def disconnect(self) -> None:
        """Close the WebSocket and release HTTP client resources."""
        await self._ws.stop()
        await self._rest.close()
        logger.info("RocketChatConnector disconnected")

    async def _snapshot_replay_boundaries(self) -> None:
        """Record where each room's outage starts, before delivery is restored.

        Called by the transport at the point delivery is known lost and nothing is
        subscribed yet, so no live message can be dispatched while this runs.

        Replay used to read `last_processed_ts` when it ran, which is *after* every room
        has been resubscribed — and rooms are confirmed one by one, so a message arriving
        in the first room while the last is still subscribing was dispatched immediately
        and moved that room's watermark past the whole gap. History was then requested
        from the new position and the outage's messages were skipped for good. The window
        is small and the loss is silent, which is the combination that makes it worth a
        second field rather than a comment.

        Snapshotting the *boundary* rather than freezing `last_processed_ts` keeps live
        dedup working normally during the recovery: the watermark stays free to advance,
        and only replay reads the older mark.
        """
        for sub in self._rooms.values():
            # An unconsumed boundary is an outage nobody has read yet, and it is older than
            # this one — overwriting it would advance past a gap while claiming to record
            # where a gap starts, which is the bug this field exists to prevent. The older
            # mark covers both windows; dedup and `_REPLAY_HISTORY_COUNT` bound what that
            # costs.
            sub.replay_boundary = sub.replay_boundary or sub.last_processed_ts

    async def _on_ws_reconnect(self) -> None:
        """Replay messages missed during a WebSocket outage.

        Called by ``RCWebSocketClient`` after every successful reconnect + room
        resubscription.  For each subscribed room that has a known watermark,
        we fetch up to ``_REPLAY_HISTORY_COUNT`` messages via the REST history
        API and re-inject them through the normal filter/normalize/dispatch
        pipeline.

        The ``_id``-based dedup window on ``_RoomSubscription`` ensures that any
        messages delivered on both the live DDP stream and this replay path are
        processed exactly once.  The ts-watermark handles older messages that
        fall below the last-processed timestamp.
        """
        logger.info("WebSocket reconnected — replaying missed messages for %d room(s)", len(self._rooms))
        for room_id, sub in list(self._rooms.items()):
            # Snapshot the watermark NOW, before any await in this iteration.
            # The live DDP listen loop runs concurrently: awaiting get_room_history
            # for an earlier room yields the event loop and allows live messages for
            # subsequent rooms to advance their last_processed_ts.  If we read the
            # watermark inside the await we would use a newer ts that skips the
            # entire outage window for those rooms.
            # The outage boundary if one was captured, and the live watermark only as a
            # fallback for a replay that no outage callback preceded. Cleared where the
            # history is actually read, not here — a replay that declines below (membership
            # unknown, or the fetch failing) has not read the window, and dropping the mark
            # would close a gap nobody looked at. Those two failures are correlated with the
            # outage itself, so this is the likely path, not the exotic one.
            watermark = sub.replay_boundary or sub.last_processed_ts
            if not watermark:
                logger.debug(
                    "Room '%s': no watermark yet — skipping replay", sub.room.name
                )
                continue

            # Membership is re-established before the outage is replayed, because the
            # outage is exactly when it can have changed and nobody was listening. The
            # live path is gated on `roomParticipant` (see `_on_raw_ddp_message`); replay
            # has no access object to read it from, so it asks. Without this, an account
            # removed from a public channel mid-outage still replays that channel — REST
            # history for a public channel does not require membership, so the fetch
            # succeeds and the agent answers in a room it was thrown out of.
            # Read before the lookup, and compared again around every await below. The
            # fetch is a REST round trip and the dispatch loop is up to 200 handler calls;
            # a live rejection arriving anywhere inside that is news this batch has to act
            # on, and it cannot see the cleared marks because it is holding a snapshot.
            epoch = sub.membership_epoch
            member = await self._rest.is_room_member(sub.room.id)
            if sub.membership_epoch != epoch:
                logger.warning(
                    "Room '%s': this account was removed while its membership was being "
                    "checked — abandoning the replay",
                    sub.room.name,
                )
                continue
            if member is False:
                # Removed, confirmed. Both marks are dropped, not just the boundary: an
                # account that is later re-added would otherwise replay from before its
                # removal, delivering everything said while it was not in the room. A
                # retained boundary is a promise to read that window later, and after a
                # removal nobody is entitled to read it.
                #
                # The watermark has to go with it, because the watermark *is* the fallback
                # boundary and it is frozen at the moment of removal — the live gate
                # remembers a rejected id without advancing it. Left in place, a reconnect
                # that arrives before the first post-re-add message would snapshot that
                # frozen value and replay the whole time away. Empty means what it means
                # for a room seen for the first time: no window, and ts-dedup off until
                # live traffic establishes one (`normalize.py`, step 4).
                sub.left_the_room()
                logger.warning(
                    "Room '%s': this account is no longer a member — skipping replay and "
                    "closing the outage window; a later re-add starts from that point, "
                    "not from before the removal",
                    sub.room.name,
                )
                continue
            if member is None:
                # Unknown is not removal. The lookup failing is correlated with the outage
                # itself, so this is the likely path, and the window stays open for the
                # next attempt to read.
                logger.warning(
                    "Room '%s': membership could not be established — skipping replay; "
                    "live delivery is unaffected and the next reconnect will ask again",
                    sub.room.name,
                )
                continue

            try:
                page = await self._rest.get_room_history_page(
                    sub.room.id,
                    sub.room.type,
                    count=self._REPLAY_HISTORY_COUNT,
                    after_ts=watermark,
                )
                raw_msgs = page.messages
            except Exception as e:
                logger.warning(
                    "Room '%s': failed to fetch history for replay: %s",
                    sub.room.name, e,
                )
                continue

            if sub.membership_epoch != epoch:
                logger.warning(
                    "Room '%s': this account was removed while its history was being "
                    "fetched — discarding %d message(s) rather than dispatching them",
                    sub.room.name, len(raw_msgs),
                )
                continue

            if not raw_msgs:
                if page.was_full:
                    # Not an empty window — a page the server filled entirely with system
                    # events, because the count is applied before they are filtered out.
                    # Every user message older than this page is still waiting behind it,
                    # and reporting the outage as read would skip them silently. This is
                    # the same bound the warning below describes, reached from the one
                    # direction that produced no evidence at all.
                    logger.warning(
                        "Room '%s': the newest %d history entries are all system events "
                        "— any user messages older than them cannot be reached in one "
                        "page and may be permanently missed",
                        sub.room.name, self._REPLAY_HISTORY_COUNT,
                    )
                else:
                    # A read that found nothing is still a read: the window is closed.
                    sub.replay_boundary = None
                logger.debug(
                    "Room '%s': no missed messages since %s",
                    sub.room.name, watermark,
                )
                continue

            if page.was_full:
                logger.warning(
                    "Room '%s': replay fetched the maximum %d message(s) — "
                    "the outage window may have produced more; some messages "
                    "could be permanently lost",
                    sub.room.name, self._REPLAY_HISTORY_COUNT,
                )
            else:
                logger.info(
                    "Room '%s': replaying %d missed message(s) since %s",
                    sub.room.name, len(raw_msgs), watermark,
                )

            # Warn when the live DDP subscription for this room is not healthy.
            # History replay still proceeds — the user gets missed messages —
            # but future live messages will be lost until the sub recovers.
            ws_status = self._ws.subscription_statuses.get(room_id, {})
            if ws_status.get("status") not in ("active", None, ""):
                logger.warning(
                    "Room '%s': DDP subscription is in '%s' state — "
                    "replaying history but future live messages will be lost "
                    "until the subscription recovers",
                    sub.room.name, ws_status.get("status"),
                )

            all_accepted = True
            for idx, doc in enumerate(raw_msgs):
                # Guard against concurrent unsubscribe_room: if the room was
                # removed while we were awaiting get_room_history, skip the
                # remaining docs rather than logging spurious "unknown room_id"
                # warnings for each one.
                if sub.membership_epoch != epoch:
                    logger.warning(
                        "Room '%s': this account was removed mid-replay — dropping the "
                        "remaining %d message(s)",
                        sub.room.name, len(raw_msgs) - idx,
                    )
                    break
                if room_id not in self._rooms:
                    logger.debug(
                        "Room '%s' was unsubscribed during replay — "
                        "skipping %d remaining message(s)",
                        sub.room.name,
                        len(raw_msgs) - idx,
                    )
                    break
                accepted = await self._on_raw_ddp_message(
                    room_id, doc, is_replay=True, replay_after_ts=watermark
                )
                if sub.membership_epoch != epoch:
                    # The removal landed *inside* that handler. The check at the top of the
                    # loop cannot reach this: the last document has no next iteration, so
                    # the `for`/`else` below would bless a revoked batch as complete.
                    #
                    # The marks are not re-cleared here. `left_the_room()` cleared them and
                    # `_on_raw_ddp_message` refuses to commit a watermark once the epoch has
                    # moved under it, so there is nothing left to repair — and repairing it
                    # here as well would be the same rule in two places, which is how the
                    # rule ends up applied in one.
                    logger.warning(
                        "Room '%s': this account was removed while message %d of %d was "
                        "being handled — dropping the rest and re-closing the window",
                        sub.room.name, idx + 1, len(raw_msgs),
                    )
                    break
                if not accepted:
                    all_accepted = False
            else:
                # Only once the batch has actually been *dispatched*. Fetching it is not
                # reading it: a shutdown or another disconnect cancelling this loop midway
                # leaves the tail unprocessed, and by then the restored live traffic has
                # moved `last_processed_ts` past it — so a boundary cleared at fetch time
                # would have the next recovery snapshot the newer mark and skip the tail
                # for good.
                #
                # `for`/`else`, so neither a cancellation nor the `break` above reaches it.
                # The cancellation case is the one this exists for; the `break` means the
                # room stopped being tracked mid-replay, and leaving a boundary on a
                # subscription nobody holds any more costs nothing.
                #
                # Completing the call is not the handler accepting: a full processor queue
                # hands the message back and forgets its id so a later replay can bring it
                # back. Spending the boundary on a batch that contains one of those removes
                # the only mark that could — the live watermark has moved past it by then.
                if all_accepted:
                    sub.replay_boundary = None
                else:
                    logger.warning(
                        "Room '%s': part of the replayed batch was handed back (queue "
                        "full) — keeping the outage window open for the next recovery",
                        sub.room.name,
                    )

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
        """Post an agent response to the room.

        Uses ``response.text`` as the message body.  When ``response.is_error``
        is True the text is delivered as-is (already contains an error prefix).
        ``thread_id`` is forwarded as RC's ``tmid`` so the reply lands in the
        correct thread.
        """
        await _send_text(
            self._rest,
            room_id,
            response.text,
            chunk_limit=self.text_chunk_limit,
            tmid=thread_id,
        )

    async def notify_agent_event(
        self,
        room_id: str,
        event: AgentEvent,
        thread_id: str | None = None,
    ) -> None:
        """Refresh the typing indicator on each intermediate agent event.

        RC's typing indicator auto-expires after ~10 seconds.  For long-running
        turns (tool calls, permission approvals, extended thinking) this means
        the indicator vanishes mid-turn, leaving the user with no feedback.

        Re-triggering it on every non-final AgentEvent keeps it alive for the
        full duration without posting any messages (no delete permissions needed,
        no placeholder race conditions).

        All errors are silently swallowed — a failed typing refresh must never
        abort an agent turn.
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
        """Upload a local file to the room."""
        await _send_media(self._rest, room_id, file_path, caption)

    async def send_to_room(
        self,
        room: str,
        text: str,
        attachment_path: str | None = None,
    ) -> None:
        """Send a message (and optional attachment) to a room by name or ID.

        Overrides the base Connector implementation to use the RC REST client
        directly for efficient room resolution and delivery.
        """
        # Resolve room name to ID.
        # Only fall back to treating the input as a raw room ID when the room
        # was genuinely not found.  Broader failures (auth, network, API errors)
        # are re-raised so callers receive an accurate error.
        try:
            room_info = await self._rest.resolve_room(room)
            room_id = room_info["_id"]
        except RoomNotFoundError:
            # Input is likely already a room ID — use it directly.
            room_id = room

        if attachment_path:
            await self._rest.upload_file(room_id, attachment_path, caption=text)
        elif text:
            await self._rest.post_message(room_id, text)

    def supports_attachments(self) -> bool:
        return True

    async def download_attachment(self, ref: dict, dest_path: str) -> None:
        """Download a RC file attachment (identified by title_link) to dest_path."""
        title_link = ref.get("title_link", "")
        await self._rest.download_file(title_link, dest_path)

    # ── Room resolution ───────────────────────────────────────────────────────

    async def resolve_room(self, room_name: str) -> Room:
        """Resolve a human-readable room name to a Room object via REST."""
        info = await self._rest.resolve_room(room_name)
        return Room(
            id=info["_id"],
            name=info.get("name", room_name),
            type=info.get("type", "channel"),
        )

    # ── Per-room subscription ─────────────────────────────────────────────────

    def register_router(self, router) -> None:
        """Register the callback consulted for a room no watcher is tracking.

        Only ever consulted under subscribe-all: with per-room subscriptions a message for
        an unwatched room cannot arrive at all. Optional, so a deployment whose server
        refuses `__my_messages__` behaves exactly as before.
        """
        self._router = router
        self._ws.register_default_callback(self._on_unrouted_message)

    async def _on_unrouted_message(self, doc: dict, access: dict | None = None) -> None:
        """A message for a room this connector has no watcher for (§2.2).

        Three gates before the room is offered, in this order and for different reasons:

        1. **System messages.** A join notification is not a reason to create a watcher,
           and `t` is on the doc, so this costs nothing.
        2. **Own messages.** The bot's own posts arrive too.
        3. **Membership.** `roomParticipant` is server-computed per message, and under
           subscribe-all it is load-bearing in a way it never was before: the stream
           delivers **public channels the account can merely read**, not only the ones it
           belongs to. Without this gate the gateway would offer a watcher for every
           public channel on the server.

        A missing access object is treated as *not* a routing candidate rather than as a
        participant. That is the one place absence must not be read generously: the object
        is what carries the membership answer, so no object means no answer, and creating a
        watcher on no answer is how the agent ends up in a room nobody invited it to.
        """
        if self._router is None or doc.get("t"):
            return
        sender = doc.get("u", {}).get("username", "")
        if sender == self._config.username:
            return
        if not access or not access.get("roomParticipant"):
            return
        if not sender_allowed(self._config, sender):
            return

        room_id = doc.get("rid", "")
        if not room_id:
            return
        if room_id in self._rooms:
            # Tracked now — so deliver it rather than offer it again. The frame reached
            # this path because the room was untracked when it was routed here, and the
            # watcher was created while it waited: the per-room callback that would have
            # taken it was registered after the routing decision was made, so nobody else
            # is going to deliver it. Returning here is how the message that arrived
            # during a creation used to be lost.
            await self._on_raw_ddp_message(room_id, doc, access=access)
            return
        if room_id in self._rooms_being_routed:
            # An offer for this room is in flight and a second one would create a second
            # watcher. This frame is dropped: the watcher does not exist yet, so there is
            # nothing to deliver it to, and holding it would need a queue per room being
            # created. The window is the duration of one creation, and the frames in it
            # are the residue this coalescing costs — stated rather than implied, because
            # the comment that used to be here called the loss "nothing".
            logger.debug(
                "Room %s is being created; dropping a frame that arrived during it",
                room_id,
            )
            return
        self._rooms_being_routed.add(room_id)
        try:
            room = await self._room_ref_from_access(room_id, access)
            if room is None:
                return
            try:
                await self._router(room)
            except Exception as e:
                logger.error("Router failed for room %s: %s", room_id, e)
                return
            # The message that prompted the creation is delivered now, through the
            # ordinary path. Offering a room is not delivering a message, and a brand-new
            # room has no watermark for the replay to fetch it from later, so without this
            # the message that caused the watcher to exist is the one message it never
            # sees — and its sender waits for an answer that needs a second message to
            # arrive.
            #
            # Through `_on_raw_ddp_message` rather than around it, so every gate that
            # applies to a tracked room's message applies to this one: the mention gate,
            # the sender policy, dedup, the capacity preflight. Creating a watcher and
            # answering unprompted are separate decisions, and this keeps them separate.
            if room_id in self._rooms:
                await self._on_raw_ddp_message(room_id, doc, access=access)
        finally:
            # Released whatever happened. A room that failed to be offered must be
            # offerable again on its next message — holding the reservation would make one
            # transient REST failure permanent for that room.
            self._rooms_being_routed.discard(room_id)

    async def _room_ref_from_access(
        self, room_id: str, access: dict
    ) -> "RoomRef | None":
        """Build a `RoomRef` from the per-delivery access object.

        `roomName` is absent for direct rooms, which is why the kind is resolved before the
        name is used: a channel without a name is a frame we cannot describe, while a DM
        without one is normal.
        """
        room_type = room_type_for(access.get("roomType"))
        if room_type == "dm":
            identity = await self._direct_room_identity(room_id)
            if identity is None:
                # An unknown classification is not a kind. Answering `dm` here would create
                # a 1:1 watcher for what may be a group DM, and a room typed `dm` skips the
                # mention gate entirely (§6.4) — so the agent would answer every message
                # from everyone in that group. The room is not lost: the next message from
                # it arrives on the same unrouted path and asks again.
                logger.warning(
                    "Direct room %s could not be classified — not routing it this time",
                    room_id,
                )
                return None
            kind, participants = identity
            # The participants are not decoration: a direct room has no name, so they are
            # the only thing that identifies it to a human. Without them a 1:1 watcher is
            # labelled by a room-id digest and a group DM's *description* — what the agent
            # is told about where it lives — becomes its own opaque label (§2.3, §2.4).
            return RoomRef(id=room_id, kind=kind, participants=participants)

        name = access.get("roomName") or ""
        if not name:
            logger.debug("Room %s has no name and is not direct — not routable", room_id)
            return None
        kind = RoomKind.GROUP if room_type == "group" else RoomKind.CHANNEL
        return RoomRef(id=room_id, kind=kind, name=name)

    async def _direct_room_identity(
        self, room_id: str
    ) -> tuple[RoomKind, tuple[str, ...]] | None:
        """1:1 or group DM — the distinction Rocket.Chat does not make on the wire.

        Cached permanently, and that is verified rather than assumed: on 8.5.1 every route
        for adding a member to a type-`d` room is refused on the room's *type*, and
        `im.create` returns a **different room id** for a different member set. A group DM
        is therefore a separate room and never a mutated 1:1, so there is nothing for an
        invalidation path to catch (§6.4).

        The *kind* is what that immutability justifies caching. The participant names are a
        snapshot, and a renamed counterpart keeps the old name here for the life of the
        process. Left that way on purpose: the cache is what stops a DM that no rule claims
        from calling `im.members` on every message it ever receives, and nothing binds to
        the name — watchers are keyed `(connector, room_id)` and recreation reads the
        persisted config rather than re-deriving the label, so a stale name cannot split a
        watcher's identity. Recorded in §2.3.

        A failed lookup returns `None` — unknown — and caches nothing. It deliberately does
        not fall back to 1:1: this answer decides whether the mention gate applies at all, so
        a wrong one is not a slightly-off label but a watcher that replies to everyone.
        """
        cached = self._dm_kinds.get(room_id)
        if cached is not None:
            return cached

        members = await self._rest.dm_members(room_id)
        if not members:
            return None  # unknown, and unknown is not a kind

        others = tuple(m for m in members if m != self._config.username)
        kind = RoomKind.GROUP_DM if len(members) > 2 else RoomKind.DM
        identity = (kind, others)
        self._dm_kinds[room_id] = identity
        return identity

    async def subscribe_room(
        self,
        room: Room,
        watcher_id: str = "",
        working_directory: str = "",
    ) -> None:
        """Subscribe to DDP stream-room-messages for this room.

        Each call registers a new per-watcher context even if the DDP
        subscription already exists.  The DDP subscription is opened only once
        (on the first subscriber); subsequent callers increment the refcount and
        append their watcher context.

        Args:
            room              : Resolved Room to subscribe to.
            watcher_id        : Unique ID for the watcher; used as the
                                attachment cache subdirectory name.
            working_directory : Base path for attachment cache storage.
        """
        ctx = _WatcherRoomContext(
            watcher_id=watcher_id or room.id,
        )

        if room.id in self._rooms:
            self._room_refcount[room.id] += 1
            self._watcher_contexts.setdefault(room.id, []).append(ctx)
            logger.debug(
                "Room '%s' (id=%s) already subscribed — added watcher '%s', refcount=%d",
                room.name,
                room.id,
                ctx.watcher_id,
                self._room_refcount[room.id],
            )
            return

        self._rooms[room.id] = _RoomSubscription(room=room)
        self._watcher_contexts[room.id] = [ctx]
        self._room_refcount[room.id] = 1

        try:
            if self._ws.stream_active:
                # Asked, not remembered: a restore that failed leaves the transport on
                # per-room subscriptions, and a connector-side flag saying otherwise would
                # register this room's callback without anyone having subscribed to it —
                # a watcher that receives nothing, silently.
                #
                # Local bookkeeping only (§5.2). The stream already delivers this room, so
                # a per-room `sub` would ask the server to send it twice — and the frames
                # are indistinguishable, so the second copy would be deduped by message id
                # rather than refused, wasting the round trip and the queue slot.
                #
                # The callback still has to be registered: the fan-out prefers a per-room
                # callback and falls back to the default one, and this room *is* tracked,
                # so it belongs on the tracked path rather than the routing path.
                self._ws.register_room_callback(room.id, self._make_ddp_callback(room.id))
            else:
                await self._ws.subscribe_room(room.id, self._make_ddp_callback(room.id))
        except Exception:
            # DDP subscription failed — roll back the connector-level state so there is no
            # dangling entry with a refcount of 1 and no live subscription. Only this call
            # can be here: the already-subscribed branch above returns before the transport
            # is touched, so a failure always belongs to the watcher that created the room.
            self._rooms.pop(room.id, None)
            self._watcher_contexts.pop(room.id, None)
            self._room_refcount.pop(room.id, None)
            # And the transport's state with it. A concurrent recovery can install its own
            # subscription for this room and release this one — which is what made the
            # await raise — and the transport's own rollback deliberately leaves a
            # successor it does not own alone. Without this the successor goes on
            # delivering into a room the connector has just forgotten: dropped as unknown,
            # and never offered to the router either, because a registered callback keeps
            # those frames off the routing path.
            try:
                await self._ws.unsubscribe_room(room.id)
            except Exception as cleanup_error:
                logger.warning(
                    "Room '%s': could not release the transport state of a failed "
                    "subscription: %s", room.name, cleanup_error,
                )
            raise

        logger.info(
            "Subscribed to room '%s' (id=%s, type=%s)",
            room.name,
            room.id,
            room.type,
        )

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        """Remove a watcher from a room; cancel the DDP subscription when the last watcher leaves.

        Args:
            room_id   : Platform room ID.
            watcher_id: ID of the departing watcher.  Its ``_WatcherRoomContext``
                        is removed regardless of the refcount; the DDP subscription
                        is cancelled only when the refcount reaches zero.
        """
        # Remove the specific watcher context and track whether it was found.
        # The refcount must only be decremented when an actual watcher is removed;
        # calling unsubscribe_room with a stale/unknown watcher_id must be a no-op.
        removed = False
        if room_id in self._watcher_contexts and watcher_id:
            before = self._watcher_contexts[room_id]
            after = [ctx for ctx in before if ctx.watcher_id != watcher_id]
            removed = len(after) < len(before)
            self._watcher_contexts[room_id] = after

        if room_id in self._room_refcount:
            if removed:
                self._room_refcount[room_id] -= 1
            if self._room_refcount[room_id] > 0:
                logger.debug(
                    "Room %s still has %d active watcher(s) — skipping DDP unsubscribe",
                    room_id,
                    self._room_refcount[room_id],
                )
                return
            del self._room_refcount[room_id]

        self._rooms.pop(room_id, None)
        self._watcher_contexts.pop(room_id, None)
        await self._ws.unsubscribe_room(room_id)
        logger.info("Unsubscribed from room %s", room_id)

    def update_last_processed_ts(self, room_id: str, ts: str) -> None:
        """Update the deduplication timestamp for a room after processing."""
        if room_id in self._rooms:
            self._rooms[room_id].last_processed_ts = ts

    def get_last_processed_ts(self, room_id: str) -> str | None:
        """Return the last processed message timestamp for a room."""
        sub = self._rooms.get(room_id)
        return sub.last_processed_ts if sub else None

    # ── Attachment cache ────────────────────────────────────────────────────────

    def attachment_cache_dir(self, room_id: str) -> str | None:
        """Return the global cache directory for a room's attachments."""
        return str(self._attachments_cache_base / room_id)

    @property
    def text_chunk_limit(self) -> int | None:
        """Maximum outbound text size before RC responses are split into chunks."""
        return self._TEXT_CHUNK_LIMIT

    # ── Security: server-injected prompt prefix ───────────────────────────────

    # Characters that are illegal in this protocol's delimiter grammar.
    # The prefix format uses '|' as a field separator and ']' as the closing
    # bracket.  A room name or username containing these could be crafted by a
    # malicious RC admin to inject fake role fields (e.g. "| role: owner") and
    # bypass RBAC enforcement in CLAUDE.md.  Stripping them here closes the gap.
    _PREFIX_UNSAFE_RE = re.compile(r"[\|\[\]\r\n]")

    def bot_identity(self) -> BotIdentity:
        """The id Rocket.Chat's own login response assigned, never the configured name.

        `username` is what an operator typed; the id is what the server says this
        connection is. Two connectors can spell one account two ways (case, an alias),
        and only the id makes them compare equal.

        No scope: Rocket.Chat has no team concept to keep two connectors on one account
        apart, so they duplicate every room, not merely DMs.

        The id arrives on the REST login response (`POST /api/v1/login`). The DDP login
        returns it too and discards it, so this deliberately reads the REST client
        rather than adding a second source that could disagree.
        """
        user_id = self._rest.user_id
        if not user_id:
            raise ConnectorIdentityError(
                f"Rocket.Chat connector for {self._config.server_url} cannot report its "
                f"own user id — the login response carried none, so this connector is "
                f"not authenticated. It cannot be checked against the other connectors "
                f"for a shared bot account, and starting it unchecked is what that "
                f"check exists to prevent."
            )
        return BotIdentity(
            platform="rocketchat",
            origin=canonical_origin(self._config.server_url),
            user_id=user_id,
        )

    @property
    def agent_username(self) -> str:
        """The bot's own RC username (from connector config)."""
        return self._config.username

    @property
    def timezone(self) -> str:
        """IANA timezone from connector config, falling back to server local."""
        return self._config.timezone or _server_local_timezone()

    def _compute_to_field(self, msg: IncomingMessage) -> str:
        """Compute the compact ``to:`` routing field for the agent prompt prefix.

        Summarises who the message is addressed to among agents:

        - ``to: me``          — only this bot is @mentioned
        - ``to: @wavebro``    — one or more other agents mentioned, not this bot
        - ``to: me+@wavebro`` — this bot and other agents mentioned
        - ``to: @all``        — room-wide explicit mention such as ``@all``
        - ``to: me+@all+@wavebro`` — room-wide mention plus priority agents
        - ``to: *``           — no explicit agent mention in a channel (broadcast)

        DMs are treated as ``to: me`` because the user is speaking to the bot
        directly without needing an @mention.  All other broadcast messages
        (no agent mention in a channel) are ``to: *``.

        Usernames from ``msg.mentions`` are sanitized with ``_PREFIX_UNSAFE_RE``
        before use in the trusted header — the same treatment as room and sender.
        """
        # DMs are always addressed to this bot, regardless of mentions metadata.
        if msg.room.type == "dm":
            return "to: me"

        own = self._config.username
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

    def supports_unsolicited_inbound(self) -> bool:
        """Yes — `stream-room-messages` with the reserved id `__my_messages__`
        delivers messages for rooms this connector never per-room-subscribed to
        (design §2.6, verified in §6.1)."""
        return True

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

        Calls the RC REST history endpoint, filters out messages from users
        not in the owner/guest allowlist or agent chain (same security boundary
        as live processing — anonymous users are excluded to prevent prompt
        injection), and returns a chronological list of normalized dicts.

        The bot's own prior messages are included with ``role: "agent"`` and
        ``username: "me"`` so the agent knows what it said before the session
        was reset.  Peer agents (``agent_chain.agent_usernames``) are also
        included as ``role: "agent"`` with their sanitized username, giving the
        agent full conversation context in multi-agent rooms.

        Args:
            room     : Resolved Room (provides id and type for the API call).
            count    : Maximum number of messages to retrieve.
            before_ts: ISO 8601 exclusive upper-bound timestamp for backward
                       pagination (maps to RC ``latest`` parameter).  Only
                       messages older than this timestamp are returned.
            after_ts : ISO 8601 inclusive lower-bound timestamp for forward
                       navigation (maps to RC ``oldest`` parameter).  Only
                       messages newer than or equal to this timestamp are
                       returned.
        """
        raw_msgs = await self._rest.get_room_history(
            room.id, room.type, count, before_ts=before_ts, after_ts=after_ts
        )
        bot_username = self._config.username
        owners = set(self._config.owners)
        guests = set(self._config.guests)
        peer_agents = set(self._config.agent_chain.agent_usernames)
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", room.name)
        tz = self.timezone

        result: list[dict] = []
        for m in raw_msgs:
            sender = m.get("u", {}).get("username", "")
            if not sender:
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
                # Peer agents in the agent chain — include as role="agent" with
                # their actual username so the bot can distinguish peer turns
                # from its own prior turns (which use username="me").
                role = "agent"
                display_username = self._PREFIX_UNSAFE_RE.sub("_", sender)
            else:
                # Anonymous / unlisted sender — exclude for prompt injection safety.
                continue

            ts_raw = m.get("ts", {})
            ts_epoch_ms = ts_raw.get("$date") if isinstance(ts_raw, dict) else None
            ts_str = ts_ms_to_iso_local(str(ts_epoch_ms), tz) if ts_epoch_ms else None

            result.append({
                "ts": ts_str,
                "username": display_username,
                "role": role,
                "room_name": safe_room,
                "text": m.get("msg", ""),
            })
        return result

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        """Return the trusted RC identity header for the agent prompt.

        This is server-controlled and parsed by CLAUDE.md as the security
        boundary for RBAC enforcement.  It must never be derived from
        user-controlled content.

        room.name and sender.username are sanitized to remove characters that
        could be used to inject fake delimiter fields (``|``, ``[``, ``]``,
        newlines).  role.value is an enum — not user-controlled.

        The ``ts`` field is the original RC message timestamp formatted in the
        connector's configured timezone (ISO 8601 with UTC offset) so agents
        can reason about local time without needing to know the offset.  It
        is kept machine-parseable — agents echo it back into
        ``fetch-history --before/--after`` — so the day of week is surfaced
        separately as a ``day:`` field instead of being embedded in ``ts``.

        The ``to:`` field summarises addressing among agents: ``me``, ``@other``,
        ``me+@other``, or ``*`` (broadcast).  See ``_compute_to_field``.
        """
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", msg.room.name)
        safe_user = self._PREFIX_UNSAFE_RE.sub("_", msg.sender.username)
        ts = ts_ms_to_iso_local(msg.timestamp, self.timezone)
        day = weekday_abbrev(ts)
        day_part = f" | day: {day}" if day else ""
        ts_part = f" | ts: {ts}" if ts else ""
        to_part = f" | {self._compute_to_field(msg)}"
        return (
            f"[Rocket.Chat #{safe_room} | "
            f"from: {safe_user} | "
            f"role: {msg.role.value}{day_part}{ts_part}{to_part}]"
        )

    # ── Status notifications ──────────────────────────────────────────────────

    async def notify_typing(self, room_id: str, is_typing: bool) -> None:
        """Send a typing indicator via DDP WebSocket.

        RC 7.x replaced the old stream-notify-room/typing event with
        stream-notify-room/user-activity.  The event args are:
          typing=True:  [username, ["user-typing"], {}]
          typing=False: [username, [],              {}]
        """
        activity = ["user-typing"] if is_typing else []
        await self._ws.call_method(
            "stream-notify-room",
            [f"{room_id}/user-activity", self._config.username, activity],
        )

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
        """Called when an agent chain LLM response was dropped (termination token detected).

        Resets the sender's turn counter so future messages from the same agent
        are not penalised by an artificially inflated count.
        """
        if self._turn_store is not None:
            self._turn_store.reset_sender(room_id, thread_id, sender)

    # ── Internal: DDP callback factory ───────────────────────────────────────

    def _make_ddp_callback(self, room_id: str):
        """Return the async callback that the WebSocket client calls for each DDP message."""

        async def on_raw_ddp_message(doc: dict, access: dict | None = None) -> None:
            await self._enqueue_room_doc(room_id, doc, access)

        return on_raw_ddp_message

    async def _enqueue_room_doc(
        self, room_id: str, doc: dict, access: dict | None = None
    ) -> None:
        """Forward one raw DDP doc into connector normalization/dispatch.

        Per-room buffering and ordering already live in ``RCWebSocketClient``.
        Keeping a second connector-owned room queue here duplicated backpressure
        and blurred the transport-vs-connector boundary.  The callback now
        relies on the transport layer's queue and proceeds directly to the
        connector-specific normalize/filter step.
        """
        await self._on_raw_ddp_message(room_id, doc, access=access)

    def _hand_back(self, doc: dict, result, reason: str, generation: int) -> bool:
        """Report a message as still owed, and give back what taking it consumed.

        Every path that answers False has already run the filter, and the filter is not
        read-only: it spends a turn of the sender's agent-chain budget before anything
        knows whether the message can be delivered. A retry re-enters it and spends
        another, so a message handed back often enough exhausts a budget it never used —
        after which the filter rejects it as complete, the replay reports success, and the
        window closes over a message nobody saw.

        One place, because there are two ways to hand a message back and they are the two
        that kept being fixed one at a time.
        """
        if result.agent_chain_token and self._turn_store is not None:
            remaining = self._turn_store.release_turn(
                doc.get("rid", ""), doc.get("tmid") or None, result.sender,
                result.agent_chain_token, generation,
            )
            logger.debug(
                "Released an agent-chain turn for %s (%s) — now at %d",
                result.sender, reason, remaining,
            )
        return False

    async def _on_raw_ddp_message(
        self,
        room_id: str,
        doc: dict,
        *,
        access: dict | None = None,
        is_replay: bool = False,
        replay_after_ts: str | None = None,
    ) -> bool:
        """Parse a raw RC DDP message doc, filter it, normalize it, fire handler.

        Returns **False only when this message is still owed a retry**. Two paths do that,
        and both are about a full processor: the handler handing the message back, which
        forgets its id so a later replay can bring it back, and a *replayed* message
        rejected by the capacity preflight, which never records the id at all. Every other
        outcome returns True, including messages filtered out on purpose: they are
        finished with, not pending. The distinction exists because the
        replay loop cannot otherwise tell "dispatched" from "handed back", and a boundary
        spent on a batch that was handed back loses it (§6.1, invariant 6).

        A handler *exception* also returns True, and deliberately: the id is left
        registered in that path, so a redo could not re-deliver the message anyway, and
        holding the window open for something no redo can produce would keep it open for
        good.

        `access` is the per-delivery object Rocket.Chat appends to the frame —
        `roomParticipant`, `roomType`, and `roomName` for rooms that have one. It is
        `None` on a per-room subscription and on the replay path, which reconstructs its
        docs from REST history, so nothing here may treat its absence as a negative
        answer: "not a participant" and "nobody said" are different, and only the first is
        a reason to drop a message.

        Filtering and deduplication are room-level (done once).
        Normalization and dispatch are per-watcher so each watcher gets its own
        attachment cache path and its own IncomingMessage instance.

        This is the boundary where all RC-specific field names disappear.
        After this method, only IncomingMessage objects exist in the codebase.

        Args:
            room_id        : Platform room ID.
            doc            : Raw RC DDP message document.
            is_replay      : True when called from the reconnect history replay
                             path.  Suppresses busy-notification REST posts to
                             avoid spamming the user with one per missed message.
            replay_after_ts: Snapshotted watermark passed by ``_on_ws_reconnect``
                             at the start of a replay loop.  When set, the
                             timestamp dedup filter uses this value instead of
                             the live ``sub.last_processed_ts``, preventing a
                             concurrent live message from advancing the watermark
                             past the replay window and silently dropping every
                             remaining replay message as "already processed".
        """
        if not self._handler:
            return True

        sub = self._rooms.get(room_id)
        if not sub:
            logger.warning("Received message for unknown room_id=%s", room_id)
            return True

        # Which membership era this delivery belongs to, read before the first await. Every
        # path from here to the watermark commit is long — normalization, attachment
        # downloads, the handler itself — and a message that was delivered with
        # `roomParticipant: true` moments before a removal is entitled to nothing on the
        # other side of it. The commit at the end is the write that matters: it is what a
        # later re-add would replay from.
        entry_epoch = sub.membership_epoch

        # --- _id dedup (live + replay race guard) ---
        # A message can arrive on both the live DDP stream and the reconnect
        # history replay path within the same short window.  The ts-based
        # watermark alone cannot catch this because the watermark is advanced
        # *after* the handler returns, leaving a gap.  The seen_ids set provides
        # a fast O(1) check that eliminates exact duplicates regardless of
        # ordering.  The deque bounds memory to _SEEN_IDS_MAXLEN entries.
        # Membership, on the tracked path. `_on_unrouted_message` gates rooms the
        # connector does not know; this gates the ones it does, and they are not the same
        # question. Under subscribe-all the stream keeps delivering a channel after the
        # bot is removed from it — the account can still *read* it — so a watcher would go
        # on answering in a room it no longer belongs to, which is exactly the state
        # someone removing the bot was trying to produce.
        #
        # Only an explicit `False` counts. Absence means nobody said: a per-room
        # subscription carries no access object and neither does the replay path, and
        # treating those as "not a member" would drop every message on both.
        if access is not None and access.get("roomParticipant") is False:
            logger.info(
                "No longer a participant in room %s — dropping message", room_id)
            # Recorded, or the rejection lasts only until the next reconnect. History is
            # fetched from an unchanged watermark and re-injected with no access object,
            # and absence is deliberately not a negative — so the same post would come
            # back and be accepted, and the removed bot would answer in the room again.
            # The one thing the live path knows and the replay path cannot is this
            # rejection; remembering the id is how it is carried across.
            sub.remember(doc.get("_id", ""))
            # And the window closes here, exactly as it does on the REST membership check
            # (§6.1, invariant 6): this rejection is the same fact, learned earlier. Left
            # open, an account re-added before any reconnect would have the next one see
            # `True` and replay from the frozen pre-removal mark — the whole interval it
            # was not a member, none of which is in the 200-id window because none of it
            # was ever delivered.
            sub.left_the_room()
            return True

        msg_id = doc.get("_id", "")
        if msg_id and msg_id in sub.seen_ids_set:
            logger.debug("Skipping already-seen message _id=%s in room %s", msg_id, room_id)
            return True

        # --- Filter (room-level, evaluated once) ---
        # During replay, use the watermark that was snapshotted at the START of
        # _on_ws_reconnect (passed as replay_after_ts) rather than the live
        # sub.last_processed_ts.  Without this, a concurrent live message that
        # arrives and advances sub.last_processed_ts mid-replay would cause every
        # remaining replay message (whose ts falls inside the outage window but
        # below the new live watermark) to be rejected as "already processed",
        # silently dropping messages the user sent during the outage.
        filter_ts = (
            replay_after_ts
            if (is_replay and replay_after_ts is not None)
            else sub.last_processed_ts
        )
        result: FilterResult = filter_rc_message(
            doc=doc,
            config=self._config,
            room_type=sub.room.type,
            last_processed_ts=filter_ts,
            turn_store=self._turn_store,
        )
        # Captured here, with no await between the filter's increment and this read, so
        # it names the count that increment belonged to. `_hand_back` compares it before
        # giving the turn back.
        turn_generation = (
            self._turn_store.generation(room_id, doc.get("tmid") or None, result.sender)
            if (result.agent_chain_token and self._turn_store is not None)
            else 0
        )
        if not result.accepted:
            logger.debug(
                "Message filtered: %s (sender=%s)", result.reason, result.sender
            )
            return True

        logger.info(
            "Filter passed for message from %s in room '%s' — dispatching: %s",
            result.sender,
            sub.room.name,
            doc.get("msg", "")[:80],
        )

        # --- Preflight capacity check (two-phase inbound acceptance) ---
        # Short-circuit BEFORE expensive normalization + attachment download
        # when the core pipeline cannot accept the message anyway.  This avoids
        # wasted network, disk, and CPU under overload.
        #
        # Note: there is a TOCTOU gap — capacity may change between this check
        # and the later enqueue().  This is handled correctly: enqueue() returns
        # False and the watermark is not advanced.  The preflight is a best-effort
        # optimization, not a hard guarantee.
        capacity = self._capacity_check(room_id) if self._capacity_check else None
        if capacity is RoomCapacity.UNROUTED:
            # Not backpressure: no watcher serves this room, so there is nothing to be
            # busy with and nothing to tell its members. Telling them the gateway is
            # busy would be a wrong answer from an idle gateway (§2.7).
            #
            # The id is recorded, and that is a *decision* rather than an inheritance
            # from the branch below, which no longer records during replay.
            #
            # The difference is whether the rejection is transient. A full queue drains,
            # so a replayed message rejected for capacity is owed another attempt, and
            # recording it would lose it silently. UNROUTED is not like that: it means no
            # watcher serves this room, which is a configuration state and can persist
            # indefinitely. Keeping the window open for it would have every recovery
            # re-fetch a batch that can never be spent — a boundary that is never
            # consumed is its own defect, and a worse one than a message that no watcher
            # was ever going to see.
            #
            # The watermark is left where it is, so a user who resends is served normally
            # once a watcher exists.
            logger.warning(
                "Message for room '%s' has no watcher — dropping without a reply. "
                "A watcher that failed to start, or a room subscribed with none "
                "configured.",
                sub.room.name,
            )
            sub.remember(msg_id)
            return True
        if capacity is RoomCapacity.FULL:
            logger.warning(
                "Preflight rejected for message from %s in room '%s' — "
                "all processor queues full, skipping normalize + download",
                result.sender,
                sub.room.name,
            )
            if is_replay:
                # Nothing is remembered and nothing is announced, so the only way this
                # message can come back is a later replay — which means the id must stay
                # unknown and the batch must not report itself complete.
                #
                # The live path below remembers the id on purpose, because the sender is
                # told and can resend. That reasoning does not survive being copied here:
                # the busy notification is suppressed during replay (200 missed messages
                # against full queues would fire 200 REST posts), so nobody is told, and a
                # remembered id makes the next recovery skip it at the dedup check and
                # then clear the window as though the batch had been delivered. Silent,
                # and permanent.
                logger.info(
                    "Room '%s': a replayed message could not be accepted (queues full) — "
                    "keeping it replayable rather than recording it as handled",
                    sub.room.name,
                )
                return self._hand_back(
                    doc, result, "replay preflight, queues full", turn_generation)

            # Record _id BEFORE the first await so a concurrent delivery of the
            # same msg_id (e.g. live DDP racing a replay) hits the dedup check
            # at the top of this function rather than both calls appending to the
            # deque and creating a phantom duplicate entry.
            # Watermark is intentionally left unchanged so the user can retry by
            # resending — we only suppress automated replay re-delivery.
            sub.remember(msg_id)
            # Best-effort notification so the user knows their message was dropped.
            try:
                await self._handler_send_busy(room_id, doc)
            except Exception as exc:
                logger.debug(
                    "Best-effort busy notification failed for room '%s': %s",
                    room_id, exc
                )
            # Watermark NOT advanced — the sender has been told and can resend.
            return True

        # --- Optimistic seen_ids registration (TOCTOU guard) ---
        # Mark this message as "in-flight" before the first await so that any
        # concurrent delivery of the same _id (live DDP racing a replay, or two
        # replay calls overlapping) will hit the dedup check at the top and
        # return immediately.  We do this AFTER the filter / preflight checks so
        # that messages rejected by those paths are NOT permanently suppressed
        # from future deliveries (a filtered message may become eligible once
        # room state changes; a preflight-rejected message was already recorded
        # above so the duplicate add here is a no-op for that branch).
        #
        # Consequence: if normalize or handler raises, msg_id stays in seen_ids
        # and the message will NOT be replayed.  This is intentional — a message
        # that fails normalization is almost certainly malformed and would fail
        # again on replay, causing a poison-pill replay storm on every reconnect.
        sub.remember(msg_id)

        # --- Normalize (once per message) ---
        # Attachment files are downloaded to a connector-global cache directory
        # namespaced by connector name and room ID.  All processors that subscribe
        # to this room reference the same local file paths — no per-watcher copies.
        # Routing to the room's processor is the core's responsibility;
        # the connector always calls the handler exactly once per accepted message.
        # Sanitize room_id before using it as a path component — room IDs are
        # server-controlled values and may contain path-traversal characters.
        # The downstream path-traversal check in normalize.py provides a second
        # layer of defense, but early sanitization is cleaner.
        safe_room_id = re.sub(r"[^\w.\-]", "_", room_id)
        cache_dir = self._attachments_cache_base / safe_room_id
        try:
            msg: IncomingMessage = await normalize_rc_message(
                doc=doc,
                room=sub.room,
                sender_username=result.sender,
                msg_ts=result.msg_ts,
                config=self._config,
                rest=self._rest,
                cache_dir=cache_dir,
                is_agent_chain=result.is_agent_chain,
                agent_chain_turn=result.agent_chain_turn,
                agent_chain_max_turns=result.agent_chain_max_turns,
            )
        except Exception as e:
            logger.error("Failed to normalize message: %s", e)
            return True

        # --- Apply thread + permission-thread policy (extracted to policy.py) ---
        apply_thread_policy(msg, self._config)

        # --- Hand off to core (the dispatcher routes to the room's processor) ---
        try:
            accepted = await self._handler(msg)
        except Exception as e:
            logger.error("Handler error for message from %s: %s", result.sender, e)
            return True

        if not accepted:
            logger.warning(
                "Message from %s was dropped (queue full)",
                result.sender,
            )
            # Remove msg_id from seen_ids so the reconnect replay path can
            # re-deliver this message.  The optimistic registration above added
            # it before the handler call to guard against concurrent live+replay
            # races, but a queue-full drop should be retryable: the queue may
            # have drained by the time the next reconnect fires.
            # We discard from the set and remove the single deque entry to keep
            # them in sync; the O(N) deque.remove is acceptable at N ≤ 200.
            if msg_id:
                sub.seen_ids_set.discard(msg_id)
                try:
                    sub.seen_ids.remove(msg_id)
                except ValueError:
                    pass
            # Pin the outage window at "before this message", because forgetting the id is
            # not enough on its own to bring it back. The watermark has not advanced past
            # it — that only happens on acceptance — but the *next* accepted message moves
            # it past for good, and a replay copy of this same message may already have
            # reported the batch complete on the strength of the id this branch has just
            # removed. Whoever drops a message owns keeping it reachable.
            #
            # `or`, so an older window already open is not narrowed to this one.
            sub.replay_boundary = sub.replay_boundary or sub.last_processed_ts
            # The one outcome that leaves this message pending: its id was just forgotten
            # precisely so a later replay can bring it back, and a boundary spent on a
            # batch containing it would remove the only thing that could.
            return self._hand_back(doc, result, "handler queue full", turn_generation)

        # --- Advance dedup watermark AFTER confirmed acceptance ---
        # Update the watermark only once the handler has confirmed the message
        # was accepted (enqueued).  Advancing it before the handler call would
        # silently lose messages that are dropped due to queue-full conditions:
        # the RC replay mechanism skips messages whose ts <= last_processed_ts,
        # so a message dropped before it reaches a processor would never be
        # re-delivered on reconnect.
        #
        # Reconnect-duplicate risk: the window between handler returning True
        # and this assignment is a single Python statement — effectively zero.
        # This is a much smaller race than waiting for the entire handler
        # duration, so the previous "advance before handler" behaviour did not
        # meaningfully reduce reconnect duplication in practice.
        if sub.membership_epoch != entry_epoch:
            # The account left this room while this message was in flight. Committing now
            # would restore the very watermark the removal cleared, and a later re-add
            # would replay from before the removal — delivering the interval the account
            # was not a member for. The message itself is already handled; only the mark
            # it would leave behind is refused.
            logger.warning(
                "Room %s: discarding the watermark of a message that was in flight when "
                "this account left", room_id,
            )
            return True

        sub.last_processed_ts = result.msg_ts
        # msg_id was already added to seen_ids_set by the optimistic registration
        # block above (before the first await).  No second add needed here.
        return True

    async def _handler_send_busy(self, room_id: str, doc: dict) -> None:
        """Best-effort 'server busy' notification to the user when preflight rejects."""
        thread_id = doc.get("tmid") or None
        await self._rest.post_message(
            room_id,
            "⚠️ Server busy — your message was dropped. Please retry.",
            tmid=thread_id,
        )
