"""Rocket.Chat Realtime API (DDP over WebSocket) client."""

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import websockets

logger = logging.getLogger("agent-chat-gateway.connectors.rocketchat.ws")


@dataclass
class SubscriptionState:
    """Runtime state for one room subscription."""

    room_id: str
    callback: Callable
    sub_id: str | None = None
    status: str = "pending"  # pending | active | reconnecting | degraded | failed
    last_error: str | None = None
    dropped_messages: int = 0


class RCWebSocketClient:
    """WebSocket client for Rocket.Chat Realtime API (DDP protocol)."""

    def __init__(self, server_url: str, username: str, password: str):
        # Convert http(s) to ws(s)
        ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
        self.ws_url = f"{ws_url}/websocket"
        self.username = username
        self.password = password

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._subscriptions: dict[str, str] = {}  # room_id -> sub_id
        self._callbacks: dict[str, Callable] = {}  # room_id -> async callback
        self._subscription_states: dict[str, SubscriptionState] = {}
        self._pending_results: dict[str, asyncio.Future] = {}  # method_id -> future
        self._pending_subs: dict[
            str, asyncio.Future
        ] = {}  # sub_id -> future (subscription confirmation)
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._running = False
        self._listen_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._callback_tasks: set[asyncio.Task] = set()
        self._callback_sem = asyncio.Semaphore(20)  # bound concurrent callback tasks
        self._room_queues: dict[
            str, asyncio.Queue
        ] = {}  # per-room bounded inbound queues
        self._room_workers: dict[str, asyncio.Task] = {}  # per-room worker tasks
        self._resubscribe_task: asyncio.Task | None = None
        # Set of room IDs currently being unsubscribed.  Checked inside
        # _subscribe_with_confirmation to detect a race where
        # _resubscribe_all_rooms re-registers a room that unsubscribe_room
        # is concurrently removing.
        self._rooms_unsubscribing: set[str] = set()

    async def connect(self) -> None:
        """Connect, perform DDP handshake, and login.

        If the DDP handshake or login fails after the WebSocket is open,
        the socket is closed before re-raising so no connection is leaked.
        """
        logger.info("Connecting to %s", self.ws_url)
        self._ws = await websockets.connect(self.ws_url)
        self._reconnect_delay = 1.0  # Reset on successful connect

        try:
            # DDP connect
            await self._send({"msg": "connect", "version": "1", "support": ["1"]})
            resp = await self._recv()
            if resp.get("msg") != "connected":
                raise RuntimeError(f"DDP handshake failed: {resp}")
            logger.info("DDP connected (session=%s)", resp.get("session"))

            # Login
            login_id = self._new_id()
            await self._send(
                {
                    "msg": "method",
                    "method": "login",
                    "id": login_id,
                    "params": [
                        {
                            "user": {"username": self.username},
                            "password": {
                                "digest": hashlib.sha256(
                                    self.password.encode()
                                ).hexdigest(),
                                "algorithm": "sha-256",
                            },
                        }
                    ],
                }
            )
            login_resp = await self._recv_until_result(login_id)
            if login_resp.get("msg") == "result" and not login_resp.get("error"):
                logger.info("WebSocket login successful for %s", self.username)
            else:
                raise RuntimeError(f"WebSocket login failed: {login_resp}")
        except Exception:
            # Close the open socket so it is not leaked on handshake / login failure.
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            raise

    async def start(self) -> None:
        """Start the listen and ping loops."""
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def stop(self) -> None:
        """Stop listening and close the connection."""
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._resubscribe_task:
            self._resubscribe_task.cancel()
            try:
                await self._resubscribe_task
            except asyncio.CancelledError:
                pass
            self._resubscribe_task = None
        # Cancel room workers explicitly and collect them for the drain gather.
        # Room workers add themselves to _callback_tasks but also register a
        # done-callback that discards them from that set when they complete.
        # If a worker finishes naturally between worker.cancel() and the gather
        # below, the done-callback fires, removes it from _callback_tasks, and
        # the gather would miss it — leaving an unobserved task exception.
        # Grabbing the workers list before cancellation and including it in the
        # gather set ensures all workers are awaited regardless of timing.
        worker_list = list(self._room_workers.values())
        for worker in worker_list:
            worker.cancel()
        # Cancel and drain all in-flight callback tasks (room workers + others).
        # Union ensures tasks that already left _callback_tasks via done-callback
        # are still awaited through worker_list.
        for task in list(self._callback_tasks):
            task.cancel()
        all_tasks = set(worker_list) | self._callback_tasks
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._callback_tasks.clear()
        self._room_queues.clear()
        self._room_workers.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket client stopped")

    async def subscribe_room(
        self,
        room_id: str,
        callback: Callable,
        timeout: float = 10.0,
    ) -> str:
        """Subscribe to stream-room-messages and wait for server confirmation.

        Sends the ``sub`` DDP frame and blocks until the server responds with
        ``ready`` (success) or ``nosub`` (rejection).  This ensures the caller
        knows the subscription is truly active before registering processors
        in the dispatcher.

        Args:
            room_id:  Room to subscribe to.
            callback: Async callback invoked for each incoming message.
            timeout:  Seconds to wait for server confirmation.

        Returns:
            The DDP subscription ID.

        Raises:
            RuntimeError: If the server rejects the subscription (``nosub``).
            asyncio.TimeoutError: If confirmation is not received within timeout.
        """
        return await self._subscribe_with_confirmation(
            room_id=room_id,
            callback=callback,
            timeout=timeout,
            keep_callback_on_failure=False,
        )

    async def _subscribe_with_confirmation(
        self,
        room_id: str,
        callback: Callable,
        timeout: float,
        keep_callback_on_failure: bool,
    ) -> str:
        """Send a room subscription and wait for explicit server confirmation."""
        sub_id = self._new_id()
        state = self._subscription_states.get(room_id)
        if state is None:
            state = SubscriptionState(room_id=room_id, callback=callback)
            self._subscription_states[room_id] = state
        else:
            state.callback = callback

        # Register confirmation future BEFORE sending the sub frame so the
        # _listen_loop can resolve it as soon as the server replies.
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_subs[sub_id] = future

        state.sub_id = sub_id
        state.status = "pending"
        state.last_error = None
        self._subscriptions[room_id] = sub_id
        self._callbacks[room_id] = callback

        try:
            await self._send(
                {
                    "msg": "sub",
                    "id": sub_id,
                    "name": "stream-room-messages",
                    "params": [room_id, False],
                }
            )
            await asyncio.wait_for(future, timeout=timeout)
            # Check for a concurrent unsubscribe_room call that was in-flight
            # while this confirmation arrived.  unsubscribe_room removes the
            # room from _callbacks/_subscriptions and sends a DDP unsub frame,
            # but if it ran while we were awaiting the confirmation future (a
            # cooperative-multitasking yield point), the server acknowledged
            # our sub before processing the unsub.  We must roll back the
            # local state so the room is not left as an active subscription
            # after the caller intended to remove it.
            if room_id in self._rooms_unsubscribing:
                self._subscriptions.pop(room_id, None)
                self._callbacks.pop(room_id, None)
                state.sub_id = None
                state.status = "failed"
                state.last_error = "unsubscribed concurrently during resubscription"
                self._pending_subs.pop(sub_id, None)
                raise RuntimeError(
                    f"Room {room_id} was unsubscribed while resubscription was "
                    "in flight — subscription rolled back"
                )
            state.status = "active"
            state.dropped_messages = 0
        except (asyncio.TimeoutError, RuntimeError) as e:
            # Subscription failed — roll back local state.
            self._subscriptions.pop(room_id, None)
            state.sub_id = None
            state.status = "failed"
            state.last_error = str(e)
            if not keep_callback_on_failure:
                self._callbacks.pop(room_id, None)
                self._subscription_states.pop(room_id, None)
            self._pending_subs.pop(sub_id, None)
            raise
        except Exception as e:
            self._subscriptions.pop(room_id, None)
            state.sub_id = None
            state.status = "failed"
            state.last_error = str(e)
            if not keep_callback_on_failure:
                self._callbacks.pop(room_id, None)
                self._subscription_states.pop(room_id, None)
            self._pending_subs.pop(sub_id, None)
            raise
        finally:
            self._pending_subs.pop(sub_id, None)

        logger.info("Subscribed to room %s (sub_id=%s, confirmed)", room_id, sub_id)
        return sub_id

    async def unsubscribe_room(self, room_id: str) -> None:
        """Unsubscribe from a room and cancel its worker task."""
        # Mark this room as being unsubscribed before mutating any state.
        # _subscribe_with_confirmation checks this set after subscription
        # confirmation arrives; if the room is being unsubscribed concurrently
        # (because _resubscribe_all_rooms captured it in its snapshot before
        # this call), the confirmation will be rolled back rather than
        # re-registering the room as an active subscription.
        self._rooms_unsubscribing.add(room_id)
        try:
            sub_id = self._subscriptions.pop(room_id, None)
            self._callbacks.pop(room_id, None)
            self._subscription_states.pop(room_id, None)
            self._room_queues.pop(room_id, None)
            # Cancel the room's worker task to prevent zombie coroutines.
            worker = self._room_workers.pop(room_id, None)
            if worker and not worker.done():
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            if sub_id and self._ws:
                await self._send({"msg": "unsub", "id": sub_id})
                logger.info("Unsubscribed from room %s", room_id)
        finally:
            self._rooms_unsubscribing.discard(room_id)

    @property
    def is_connected(self) -> bool:
        """True if the WebSocket connection is currently open."""
        return self._ws is not None

    @property
    def subscription_statuses(self) -> dict[str, dict[str, str | None]]:
        """Return a snapshot of room subscription health for diagnostics."""
        return {
            room_id: {
                "sub_id": state.sub_id,
                "status": state.status,
                "last_error": state.last_error,
                "dropped_messages": state.dropped_messages,
            }
            for room_id, state in self._subscription_states.items()
        }

    async def call_method(
        self, method: str, params: list, timeout: float = 5.0
    ) -> dict:
        """Call a DDP method and return the server's result.

        Waits up to ``timeout`` seconds for the result message.  Used for
        side-effect calls like typing notifications where we want to surface
        any server-side errors for debugging.

        Fast-fails when the WebSocket is disconnected to avoid stalling
        callers (e.g. typing indicators) for the full timeout duration.

        Returns the raw result dict (may contain an ``error`` key).
        """
        if not self._ws:
            logger.debug("call_method %r skipped — WebSocket not connected", method)
            return {}
        method_id = self._new_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_results[method_id] = future
        logger.debug("call_method → %r params=%s id=%s", method, params, method_id)
        try:
            await self._send(
                {
                    "msg": "method",
                    "method": method,
                    "id": method_id,
                    "params": params,
                }
            )
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.debug("call_method ← %r result=%s", method, result)
            if result.get("error"):
                logger.debug("call_method %r error: %s", method, result["error"])
            return result
        except asyncio.TimeoutError:
            logger.debug(
                "call_method %r timed out (no result in %.1fs)", method, timeout
            )
            return {}
        finally:
            self._pending_results.pop(method_id, None)

    # -- Internal methods --

    async def _send(self, data: dict) -> None:
        if self._ws:
            await self._ws.send(json.dumps(data))

    async def _recv(self) -> dict:
        if not self._ws:
            raise RuntimeError("Not connected")
        raw = await self._ws.recv()
        return json.loads(raw)

    async def _recv_until_result(self, method_id: str, timeout: float = 15.0) -> dict:
        """Receive messages until we get the result for our method call."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    self._ws.recv(),
                    # Clamp to a small positive minimum: if the deadline has
                    # already passed (due to cooperative yielding between the
                    # while-condition check and here), a zero or negative timeout
                    # has implementation-defined behaviour across Python versions.
                    timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                )
                msg = json.loads(raw)
                if msg.get("msg") == "ping":
                    await self._send({"msg": "pong"})
                    continue
                if msg.get("msg") == "result" and msg.get("id") == method_id:
                    return msg
            except asyncio.TimeoutError:
                break
        raise RuntimeError(f"Timeout waiting for result of method {method_id}")

    async def _listen_loop(self) -> None:
        """Main receive loop. Dispatches room messages to callbacks."""
        while self._running:
            try:
                if not self._ws:
                    await self._reconnect()
                    continue

                raw = await self._ws.recv()
                msg = json.loads(raw)
                msg_type = msg.get("msg")

                if msg_type == "ping":
                    await self._send({"msg": "pong"})
                elif (
                    msg_type == "changed"
                    and msg.get("collection") == "stream-room-messages"
                ):
                    await self._handle_room_message(msg)
                elif msg_type == "result":
                    mid = msg.get("id")
                    if mid in self._pending_results:
                        fut = self._pending_results[mid]
                        if not fut.done():
                            fut.set_result(msg)
                elif msg_type == "ready":
                    # Resolve pending subscription futures for confirmed subs.
                    for confirmed_id in msg.get("subs", []):
                        fut = self._pending_subs.get(confirmed_id)
                        if fut and not fut.done():
                            fut.set_result(True)
                elif msg_type == "nosub":
                    # Reject the pending subscription future with an error.
                    nosub_id = msg.get("id", "")
                    nosub_error = msg.get("error", {}).get(
                        "message", "subscription rejected"
                    )
                    fut = self._pending_subs.get(nosub_id)
                    if fut and not fut.done():
                        fut.set_exception(
                            RuntimeError(
                                f"Subscription rejected by server: {nosub_error}"
                            )
                        )
                    else:
                        logger.warning(
                            "Subscription rejected (no pending future): %s", msg
                        )
                        for state in self._subscription_states.values():
                            if state.sub_id == nosub_id:
                                state.status = "failed"
                                state.last_error = nosub_error
                                break
                else:
                    logger.debug("Unhandled WS message: %s", msg_type)

            except websockets.ConnectionClosed:
                logger.warning("WebSocket connection closed")
                self._ws = None
                await self._reconnect()
            except json.JSONDecodeError as e:
                # Malformed frame — log and continue without reconnecting.
                # Reconnecting here would be spurious: the connection is still
                # healthy; only this one frame was unparseable.
                logger.warning("Received unparseable WebSocket frame (ignored): %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Error in listen loop: %s", e)
                self._ws = None
                await self._reconnect()

    # Maximum messages buffered per room before drops.  This bounds memory
    # usage under burst load instead of creating unbounded tasks.
    _ROOM_QUEUE_DEPTH = 50

    async def _handle_room_message(self, msg: dict) -> None:
        """Extract room message and dispatch to per-room worker queue.

        Instead of creating one asyncio task per inbound message (unbounded),
        messages are placed on a bounded per-room queue consumed by a single
        worker task per room.  This provides:
          - Explicit memory bounding under burst load
          - Guaranteed per-room ordering (single worker per room)
          - Bounded concurrency via the existing semaphore
        """
        try:
            fields = msg.get("fields", {})
            args = fields.get("args", [])
            if not args:
                return

            message_doc = args[0]
            room_id = fields.get("eventName") or message_doc.get("rid")
            if not room_id:
                return

            callback = self._callbacks.get(room_id)
            if not callback:
                return

            # Lazily create per-room queue and worker on first message.
            # Also re-create if the previous worker died (e.g. after reconnect).
            existing_worker = self._room_workers.get(room_id)
            if room_id not in self._room_queues or (
                existing_worker and existing_worker.done()
            ):
                # Clean up dead worker if present
                if existing_worker and existing_worker.done():
                    old_queue = self._room_queues.pop(room_id, None)
                    if old_queue and not old_queue.empty():
                        logger.warning(
                            "Room worker for %s died with %d unprocessed message(s) "
                            "in queue — these messages are lost",
                            room_id,
                            old_queue.qsize(),
                        )
                    self._room_workers.pop(room_id, None)

                q: asyncio.Queue = asyncio.Queue(maxsize=self._ROOM_QUEUE_DEPTH)
                self._room_queues[room_id] = q
                task = asyncio.create_task(
                    self._room_worker(room_id, q),
                    name=f"rc-room-worker-{room_id[:8]}",
                )
                self._room_workers[room_id] = task
                self._callback_tasks.add(task)
                task.add_done_callback(self._callback_tasks.discard)

            try:
                self._room_queues[room_id].put_nowait(message_doc)
            except asyncio.QueueFull:
                state = self._subscription_states.get(room_id)
                if state is None:
                    state = SubscriptionState(room_id=room_id, callback=callback)
                    self._subscription_states[room_id] = state
                state.dropped_messages += 1
                if state.status not in {"failed", "reconnecting"}:
                    state.status = "degraded"
                state.last_error = f"inbound room queue overflow: dropped {state.dropped_messages} message(s)"
                logger.warning(
                    "Inbound queue full for room %s — dropping message (drop_count=%d)",
                    room_id[:8],
                    state.dropped_messages,
                )
        except Exception as e:
            logger.error("Error handling room message: %s", e)

    async def _room_worker(self, room_id: str, queue: asyncio.Queue) -> None:
        """Consume messages for one room sequentially with bounded global concurrency."""
        # Tracks the message currently dequeued but not yet dispatched.  When
        # CancelledError fires between queue.get() and the semaphore acquire,
        # this message would otherwise be silently lost — it is no longer in
        # the queue, and the outer CancelledError drain only counts items still
        # there.  By tracking it explicitly we can include it in the lost count.
        in_flight: object = None
        try:
            while True:
                doc = await queue.get()
                in_flight = doc  # record before semaphore — cancellation safe
                callback = self._callbacks.get(room_id)
                if callback:
                    async with self._callback_sem:
                        # Semaphore acquired; doc is now being dispatched.
                        # Clear in_flight so the outer CancelledError handler
                        # doesn't double-count it (the mid-callback log below
                        # already accounts for it if cancelled here).
                        in_flight = None
                        try:
                            await callback(doc)
                        except asyncio.CancelledError:
                            # Worker was cancelled *while* the callback was in
                            # flight — the current message (doc) is lost.  Log
                            # it so operators can detect message loss during
                            # graceful shutdown.
                            msg_id = doc.get("_id", "<unknown>") if isinstance(doc, dict) else "<unknown>"
                            logger.warning(
                                "Room worker %s cancelled mid-callback — "
                                "message %s is lost (graceful shutdown)",
                                room_id[:8],
                                msg_id,
                            )
                            raise
                        except Exception as e:
                            logger.error(
                                "Callback error for room %s: %s", room_id[:8], e
                            )
                in_flight = None  # fully processed
        except asyncio.CancelledError:
            # Drain remaining queue items before exiting so operators can
            # see exactly how many messages were permanently lost.
            # These messages cannot be re-delivered: the connector already
            # advanced their watermarks (connector.py), and the WebSocket
            # subscription is being torn down.  get_nowait() is used
            # deliberately — await queue.get() would itself be cancelled
            # again because the task is in a cancelled state.
            remaining = 0
            # Count the in-flight message that was dequeued but cancelled
            # before the semaphore could be acquired.
            if in_flight is not None:
                remaining += 1
            while not queue.empty():
                try:
                    queue.get_nowait()
                    remaining += 1
                except asyncio.QueueEmpty:
                    break
            if remaining:
                logger.warning(
                    "Room worker %s cancelled with %d unprocessed message(s) "
                    "still in queue — messages permanently lost (watermarks "
                    "already advanced, replay will not re-deliver)",
                    room_id[:8],
                    remaining,
                )
            raise
        except Exception as e:
            logger.error("Room worker %s died: %s", room_id[:8], e)

    async def _ping_loop(self) -> None:
        """Send periodic pings to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(25)
                if self._ws:
                    await self._send({"msg": "ping"})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff, then re-subscribe all rooms.

        Ordering guarantee: ``connect()`` is awaited to completion before this
        method returns, so the ``_listen_loop`` caller resumes only after the
        full DDP handshake and login are done.  This means ``_recv_until_result``
        (called inside ``connect()``) has exclusive access to the WebSocket
        receive path — the listen loop cannot race with it.  Do not restructure
        this to run ``connect()`` concurrently with the listen loop without
        revisiting that invariant.
        """
        logger.info("Reconnecting in %.1fs...", self._reconnect_delay)
        await asyncio.sleep(self._reconnect_delay)
        self._reconnect_delay = min(
            self._reconnect_delay * 2, self._max_reconnect_delay
        )

        try:
            await self.connect()
            if self._resubscribe_task and not self._resubscribe_task.done():
                self._resubscribe_task.cancel()
                try:
                    await self._resubscribe_task
                except asyncio.CancelledError:
                    pass

            for room_id, callback in list(self._callbacks.items()):
                self._subscriptions.pop(room_id, None)
                state = self._subscription_states.get(room_id)
                if state is None:
                    state = SubscriptionState(room_id=room_id, callback=callback)
                    self._subscription_states[room_id] = state
                else:
                    state.callback = callback
                state.sub_id = None
                state.status = "reconnecting"
                state.last_error = None

            if self._callbacks:
                self._resubscribe_task = asyncio.create_task(
                    self._resubscribe_all_rooms(),
                    name="rc-resubscribe-all",
                )
                self._callback_tasks.add(self._resubscribe_task)
                self._resubscribe_task.add_done_callback(self._callback_tasks.discard)
        except Exception as e:
            logger.error("Reconnect failed: %s", e)
            self._ws = None
        finally:
            # Resolve any futures that are still waiting for subscription
            # confirmation from the old connection.  This must run in a
            # ``finally`` block so it fires even when CancelledError is raised
            # during ``connect()`` (e.g. SIGTERM arriving mid-reconnect).
            #
            # Without this, a caller blocked in _subscribe_with_confirmation()
            # for a room that was mid-subscription when the connection dropped
            # would wait until its asyncio.wait_for timeout (default 30s) before
            # learning the subscription failed — both on normal reconnect AND on
            # task cancellation.  Failing them here with a clear error lets
            # callers surface the problem immediately.
            for sub_id, fut in list(self._pending_subs.items()):
                if not fut.done():
                    fut.set_exception(
                        RuntimeError(
                            "WebSocket connection lost while waiting for "
                            "subscription confirmation — reconnecting"
                        )
                    )
            self._pending_subs.clear()

    async def _resubscribe_all_rooms(self) -> None:
        """Re-confirm all existing room subscriptions after reconnect."""
        try:
            rooms = list(self._callbacks.items())
            results = await asyncio.gather(
                *[
                    self._subscribe_with_confirmation(
                        room_id=room_id,
                        callback=callback,
                        timeout=10.0,
                        keep_callback_on_failure=True,
                    )
                    for room_id, callback in rooms
                ],
                return_exceptions=True,
            )
            success = 0
            failures: list[str] = []
            for (room_id, _callback), result in zip(rooms, results, strict=False):
                if isinstance(result, Exception):
                    state = self._subscription_states.get(room_id)
                    if state:
                        state.status = "failed"
                        state.last_error = str(result)
                    failures.append(f"{room_id}: {result}")
                else:
                    success += 1
            if failures:
                logger.warning(
                    "Reconnect completed with partial subscription recovery: %d succeeded, %d failed (%s)",
                    success,
                    len(failures),
                    "; ".join(failures[:5]),
                )
            elif self._callbacks:
                logger.info("Reconnect re-confirmed %d room subscription(s)", success)
        finally:
            self._resubscribe_task = None

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:12]
