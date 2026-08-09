"""Mattermost Realtime API (WebSocket) client.

Structurally simpler than Rocket.Chat's DDP client (gateway/connectors/
rocketchat/websocket.py) because Mattermost has no per-channel subscribe
handshake: one authenticated connection streams `posted` events for every
channel the bot is a member of, across every team it belongs to. There is no
sub/unsub/ready/nosub confirmation dance and therefore no per-room refcounted
subscription state to manage — `register_channel`/`unregister_channel` on
this client are local bookkeeping only (which channels does the dispatcher
care about), not wire-protocol calls.

Payload quirks confirmed empirically against a live Mattermost 11.7.0 server
(not just from docs) before writing this:
  - `data.post` on a `posted` event is a JSON-encoded STRING, not a nested
    object — requires its own `json.loads()`.
  - `data.mentions` is ALSO a JSON-encoded STRING (of a user-id array), not a
    native JSON array — requires its own `json.loads()` too. This one is not
    documented anywhere obvious; found by driving one real mention through a
    live server.
  - The server excludes the author from their own `mentions` list — a
    self-mention produces no `mentions` field at all. Only cross-user
    mentions populate it.
  - No custom application-level ping/pong is needed (unlike RC's DDP, which
    requires periodic `{"msg": "ping"}`): the `websockets` library's
    transport-level ping/pong keeps the connection alive and raises
    ConnectionClosed on a dead peer, which the reconnect loop below handles.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

logger = logging.getLogger("agent-chat-gateway.connectors.mattermost.ws")

# Bound concurrent per-channel worker tasks, same rationale as RC's
# _callback_sem: caps total in-flight handler invocations across all channels.
_CALLBACK_CONCURRENCY = 20
# Per-channel backpressure depth, enforced manually at dispatch time (see
# _dispatch()) rather than via asyncio.Queue(maxsize=...) — same backpressure
# model as RC once the dispatch gate is open, just without the wire-protocol
# subscription it's normally paired with. The queue itself is unbounded so
# that a closed gate (buffering during startup — see _dispatch_gate) can
# never silently drop events; this depth only caps steady-state growth
# against a genuinely stuck handler once delivery has actually started.
_CHANNEL_QUEUE_DEPTH = 50

PostedEventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MattermostWebSocketClient:
    """WebSocket client for the Mattermost Realtime API."""

    def __init__(self, server_url: str, token_provider: Callable[[], str | None]):
        ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
        self.ws_url = f"{ws_url}/api/v4/websocket"
        # Called fresh on every (re)connect so a token refreshed by REST-mode
        # re-login (session expiry) is picked up without reconstructing the
        # client.
        self._token_provider = token_provider

        self._ws: websockets.ClientConnection | None = None
        self._handler: PostedEventHandler | None = None
        self._seq = 1
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._running = False
        self._listen_task: asyncio.Task | None = None
        self._callback_sem = asyncio.Semaphore(_CALLBACK_CONCURRENCY)
        self._callback_tasks: set[asyncio.Task] = set()
        # "Live" queues: bounded at _CHANNEL_QUEUE_DEPTH, normal steady-state
        # backpressure — only ever written to once the dispatch gate is open.
        self._channel_queues: dict[str, asyncio.Queue] = {}
        # Startup backlog queues: unbounded, only ever written to while the
        # dispatch gate is closed. Kept fully separate from the live queue
        # above (PR #80 review, Finding D) so that draining a large startup
        # backlog can never look like steady-state overflow to a live event
        # arriving mid-drain — the two have independent capacity semantics.
        self._channel_backlogs: dict[str, asyncio.Queue] = {}
        self._channel_workers: dict[str, asyncio.Task] = {}
        # Local bookkeeping only — no wire-protocol subscribe exists.  Tracks
        # which channels the dispatcher currently cares about, purely so
        # register/unregister has somewhere to record intent (parity with
        # RC's subscribe_room/unsubscribe_room surface for the connector).
        self._registered_channels: set[str] = set()
        # Optional callback invoked after every successful reconnect.
        # Registered by the connector to replay messages missed during the
        # outage via REST history.
        self._on_reconnect_cb: Callable[[], Any] | None = None
        # Closed by default (docs/design/on-the-fly-watchers.md, "Startup
        # ordering" section, corrected design). The socket connects and
        # _dispatch() queues every event as soon as it arrives — nothing is
        # lost at the transport layer — but each channel's _channel_worker
        # holds off DRAINING anything until this gate opens, so events that
        # arrive while the caller is still finishing startup (e.g.
        # SessionManager.sync_watchers()) are buffered (in
        # _channel_backlogs, see below), not discarded, and are only handed
        # to try_lazy_create()/the dispatcher once local state is actually
        # ready. open_dispatch_gate() opens it permanently.
        self._dispatch_gate = asyncio.Event()

    def register_handler(self, handler: PostedEventHandler) -> None:
        """Register the single callback invoked for every decoded `posted` event."""
        self._handler = handler

    def register_channel(self, channel_id: str) -> None:
        """Record that the dispatcher cares about this channel (no wire call)."""
        self._registered_channels.add(channel_id)

    def unregister_channel(self, channel_id: str) -> None:
        """Stop caring about this channel locally (no wire call)."""
        self._registered_channels.discard(channel_id)

    def set_reconnect_callback(self, cb: Callable[[], Any]) -> None:
        self._on_reconnect_cb = cb

    def open_dispatch_gate(self) -> None:
        """Allow buffered/incoming events to actually be handed to the
        registered handler. Idempotent; once open it stays open for the
        lifetime of this client (there is no matching close — a later
        reconnect is not a fresh startup and must not re-buffer)."""
        self._dispatch_gate.set()

    async def connect(self) -> None:
        """Connect and perform the authentication_challenge handshake.

        If the handshake fails after the socket is open, the socket is
        closed before re-raising so no connection is leaked.
        """
        logger.info("Connecting to %s", self.ws_url)
        self._ws = await websockets.connect(self.ws_url)
        self._reconnect_delay = 1.0  # Reset on successful connect
        try:
            await self._authenticate()
        except Exception:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            raise

    async def _authenticate(self) -> None:
        token = self._token_provider()
        if not token:
            raise RuntimeError("MattermostWebSocketClient: no token available to authenticate")
        await self._send({
            "seq": self._seq,
            "action": "authentication_challenge",
            "data": {"token": token},
        })
        self._seq += 1
        # Mattermost does not reliably ack authentication_challenge with a
        # dedicated event before streaming `hello`/`posted` etc. — success is
        # implied by subsequent events arriving rather than an explicit
        # confirmation, so there is nothing further to await here (confirmed
        # against a live server: the first frames received were `hello` and a
        # generic seq_reply ack, not a distinguishable "auth OK" event).
        logger.info("Sent authentication_challenge")

    async def start(self) -> None:
        """Start the listen loop."""
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def stop(self) -> None:
        """Stop listening and close the connection."""
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        worker_list = list(self._channel_workers.values())
        for worker in worker_list:
            worker.cancel()
        for task in list(self._callback_tasks):
            task.cancel()
        all_tasks = set(worker_list) | self._callback_tasks
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._callback_tasks.clear()
        self._channel_queues.clear()
        self._channel_backlogs.clear()
        self._channel_workers.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket client stopped")

    async def _send(self, msg: dict) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        await self._ws.send(json.dumps(msg))

    async def send_typing(self, channel_id: str, parent_id: str = "") -> None:
        """Send a best-effort typing indicator action."""
        data: dict[str, Any] = {"channel_id": channel_id}
        if parent_id:
            data["parent_id"] = parent_id
        await self._send({"seq": self._seq, "action": "user_typing", "data": data})
        self._seq += 1

    def _decode_posted_event(self, evt: dict[str, Any]) -> dict[str, Any]:
        """Decode a raw `posted` event into {post, mentions, channel_type, ...}.

        Both `data.post` and `data.mentions` are JSON-encoded strings, not
        native JSON objects/arrays (confirmed against a live server — see
        module docstring). Absence of `mentions` (self-mentions, or no
        mentions at all) decodes to an empty list.
        """
        data = evt.get("data", {})
        post = json.loads(data["post"])
        mentions_raw = data.get("mentions")
        mentions: list[str] = json.loads(mentions_raw) if mentions_raw else []
        return {
            "post": post,
            "mentions": mentions,
            "channel_type": data.get("channel_type"),
            "channel_name": data.get("channel_name"),
            "team_id": data.get("team_id"),
        }

    async def _dispatch(self, decoded: dict[str, Any]) -> None:
        """Route a decoded posted-event to the per-channel backlog or live queue.

        Two separate queues per channel (PR #80 review, Finding D), not one:
        - `_channel_backlogs[channel_id]`: unbounded, written to only while
          the dispatch gate is closed. Nothing arriving before
          `open_dispatch_gate()` can ever be dropped for "being too much" —
          this is the startup buffer.
        - `_channel_queues[channel_id]`: bounded at `_CHANNEL_QUEUE_DEPTH`,
          written to only once the gate is open. Normal steady-state
          backpressure, with no dependency on how large the startup backlog
          was or how far `_channel_worker` has gotten through draining it.

        Keeping these fully separate is the fix for Finding D: an earlier
        version used one queue and compared its size against
        `_CHANNEL_QUEUE_DEPTH` once the gate opened — which meant a live
        event arriving while the worker was still draining a >50-item
        startup backlog got treated as steady-state overflow and dropped,
        even though the WebSocket and handler were both healthy. With two
        queues, a live event's fate never depends on backlog size at all.

        One overflow path is now reachable that wasn't distinct before:
        if live traffic itself exceeds `_CHANNEL_QUEUE_DEPTH` while the
        backlog is still draining, live events do drop. That IS genuine
        backpressure (the handler can't keep up with live traffic, same as
        it couldn't in steady state) — not a repeat of Finding D, which was
        about backlog volume alone triggering drops with a healthy handler.
        """
        channel_id = decoded["post"]["channel_id"]
        if channel_id not in self._channel_queues:
            live_queue = asyncio.Queue(maxsize=_CHANNEL_QUEUE_DEPTH)
            backlog_queue = asyncio.Queue()  # unbounded
            self._channel_queues[channel_id] = live_queue
            self._channel_backlogs[channel_id] = backlog_queue
            worker = asyncio.create_task(
                self._channel_worker(channel_id, backlog_queue, live_queue)
            )
            self._channel_workers[channel_id] = worker
            self._callback_tasks.add(worker)
            worker.add_done_callback(self._callback_tasks.discard)

        if not self._dispatch_gate.is_set():
            # Startup buffering — no capacity check, ever. Safe: no other
            # code path writes here once the gate opens (see class docstring
            # in open_dispatch_gate()), so this can't race past the check
            # below once _listen_loop's single-threaded dispatch order is
            # accounted for.
            self._channel_backlogs[channel_id].put_nowait(decoded)
            return

        live_queue = self._channel_queues[channel_id]
        try:
            live_queue.put_nowait(decoded)
        except asyncio.QueueFull:
            logger.warning(
                "Channel %s inbound queue full (depth=%d) — dropping event",
                channel_id, _CHANNEL_QUEUE_DEPTH,
            )

    async def _invoke_handler(self, channel_id: str, decoded: dict[str, Any]) -> None:
        """Run the registered handler for one decoded event, bounded by the
        global concurrency semaphore. Shared by both drain phases below so
        there is exactly one copy of this error-handling behavior."""
        async with self._callback_sem:
            if self._handler is not None:
                try:
                    await self._handler(decoded)
                except Exception:
                    logger.exception(
                        "Unhandled error in posted-event handler for channel %s",
                        channel_id,
                    )

    async def _channel_worker(
        self, channel_id: str, backlog_queue: asyncio.Queue, live_queue: asyncio.Queue
    ) -> None:
        """Process one channel's events in order, bounded by the global semaphore.

        Waits for the dispatch gate, then drains the ENTIRE startup backlog
        first (unbounded, in the order it was buffered) before touching the
        live queue at all — this preserves overall arrival order (nothing
        buffered before the gate opened can be posted-before something that
        arrived after it) and guarantees the backlog is fully gone before
        live-queue capacity checks become meaningful. Once backlog_queue is
        empty, no further writes to it are possible (see _dispatch()), so
        moving on to draining live_queue forever after is safe.
        """
        try:
            await self._dispatch_gate.wait()
            while not backlog_queue.empty():
                decoded = backlog_queue.get_nowait()
                await self._invoke_handler(channel_id, decoded)
            while True:
                decoded = await live_queue.get()
                await self._invoke_handler(channel_id, decoded)
        except asyncio.CancelledError:
            pass

    async def _listen_loop(self) -> None:
        while self._running:
            try:
                if self._ws is None:
                    raise RuntimeError("WebSocket is not connected")
                async for raw in self._ws:
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Received non-JSON WS frame, ignoring")
                        continue
                    if evt.get("event") == "posted":
                        try:
                            decoded = self._decode_posted_event(evt)
                        except (KeyError, json.JSONDecodeError):
                            logger.exception("Failed to decode posted event: %r", evt)
                            continue
                        await self._dispatch(decoded)
                    # Other events (hello, status_change, typing, seq_reply
                    # acks, etc.) are intentionally not acted on here.
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning("WebSocket connection closed: %s", e)
            except Exception:
                logger.exception("Unexpected error in WS listen loop")

            if not self._running:
                return

            await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff, then fire the reconnect callback."""
        while self._running:
            logger.info("Reconnecting in %.1fs...", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            try:
                self._ws = await websockets.connect(self.ws_url)
                await self._authenticate()
                self._reconnect_delay = 1.0
                logger.info("Reconnected to %s", self.ws_url)
                if self._on_reconnect_cb is not None:
                    try:
                        result = self._on_reconnect_cb()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("Error in on_reconnect callback")
                return
            except Exception:
                logger.exception("Reconnect attempt failed")
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )
