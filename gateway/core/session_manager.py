"""SessionManager: thin orchestrator that wires collaborators together.

Delegates all real work to focused collaborators:
  - WatcherLifecycle: start/stop/pause/resume/reset watchers
  - MessageDispatcher: inbound message routing + permission interception
  - InjectedContextBuilder: context file reading + durable agent session delivery
  - StateStore: WatcherState persistence + watermark management
  - SessionMaps: shared session→room/role/connector routing state
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime

from ..agents import AgentBackend
from .config import CoreConfig, WatcherConfig
from .connector import Connector, IncomingMessage, Room, User, UserRole
from .dispatch import MessageDispatcher
from .injected_context_builder import InjectedContextBuilder
from .permission import PermissionRegistry
from .session_maps import SessionMaps
from .state import StateFilter, parse_state_filter
from .state_store import StateStore
from .watcher_lifecycle import WatcherLifecycle
from .watcher_manager import RoomRef, WatcherManager
from .watcher_rule import RoomKind

logger = logging.getLogger("agent-chat-gateway.core.session_manager")


class SessionManager:
    """Thin orchestrator: wires collaborators and manages top-level lifecycle.

    Accepts any Connector implementation — RocketChatConnector, ScriptConnector,
    or future Slack/Discord connectors — without knowing their platform details.

    Usage::

        manager = SessionManager(connector, agents, "assistance", core_config,
                                 watcher_configs=watchers)
        await manager.run()   # blocks until cancelled
    """

    # How many messages the startup replay's gap probe asks for. Only emptiness
    # is read from the answer, but the count must be big enough that a page of
    # filtered-out system messages does not read as "no gap" — the same
    # count-before-filtering trap the reconnect replay documents.
    _REPLAY_PROBE_COUNT = 50

    def __init__(
        self,
        connector: Connector,
        agents: dict[str, AgentBackend],
        default_agent: str,
        config: CoreConfig,
        state_name: str = "default",
        watcher_configs: list[WatcherConfig] | None = None,
        permission_registry: PermissionRegistry | None = None,
        session_maps: SessionMaps | None = None,
        watcher_rules: list | None = None,
    ) -> None:
        self._connector = connector
        # `state_name` is the connector's config name in production (service.py
        # passes `cc.name`), which is also the name rules bind to — the manager
        # and the router closure below both key on it.
        self._connector_name = state_name
        maps = session_maps or SessionMaps()

        # Collaborators
        self._dispatcher = MessageDispatcher(connector, permission_registry)
        self._injector = InjectedContextBuilder(config)
        self._state_store = StateStore(state_name, connector)
        self._lifecycle = WatcherLifecycle(
            connector=connector,
            agents=agents,
            default_agent=default_agent,
            config=config,
            watcher_configs=watcher_configs or [],
            state_store=self._state_store,
            dispatcher=self._dispatcher,
            injector=self._injector,
            permission_registry=permission_registry,
            maps=maps,
        )
        # The creation path (§2.7/§2.8) exists only when rules do. Gated on the
        # rules rather than always-on because registering a router changes what
        # the connector *asks for* — Rocket.Chat switches to subscribe-all — and
        # a static-only deployment must keep its exact delivery behaviour until
        # its operator writes a rule.
        self._watcher_manager = (
            WatcherManager(state_name, connector, self._lifecycle, watcher_rules)
            if watcher_rules
            else None
        )

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, sync watchers, block until cancelled.

        Note: control socket ownership belongs to GatewayService.
        Use run_once() when GatewayService is the orchestrator (normal production use).
        This method is kept for standalone/test use cases that don't use GatewayService.
        """
        await self.run_once()
        logger.info("SessionManager running")
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def run_once(self, unavailable_agents: set[str] | None = None) -> list[str]:
        """Connect and sync watchers WITHOUT blocking forever.

        Args:
            unavailable_agents: Optional set of agent names whose permission
                broker failed to start.  Watchers that use these agents will
                be skipped with an error rather than started without permission
                enforcement.

        Returns:
            List of human-readable error strings for any watchers that failed.
        """
        await self.connect_only()
        errors = await self.sync_only(unavailable_agents=unavailable_agents)
        logger.info("SessionManager ready (run_once)")
        return errors

    async def connect_only(self) -> None:
        """Phase one: authenticate. No subscription, no watcher, no room.

        Split from `sync_only` so an orchestrator holding several connectors can put a
        barrier between the phases — two connectors on one bot account have to be
        refused *before* either subscribes, and each SessionManager owns exactly one
        connector, so nothing at this layer can see the collision (§4.5).

        A single-connector caller has no such barrier to run and should keep using
        `run_once()`.
        """
        self._connector.register_handler(self._dispatcher.dispatch)
        self._connector.register_capacity_check(self._dispatcher.capacity)
        if self._watcher_manager is not None:
            # Before start_inbound(), necessarily: Rocket.Chat's start_inbound
            # attempts subscribe-all only when a router is already registered,
            # so a router registered later would never receive an offer.
            self._connector.register_router(self._route_unclaimed_room)
        await self._connector.connect()

    async def sync_only(self, unavailable_agents: set[str] | None = None) -> list[str]:
        """Phase two: restore watchers, subscribe, then open the inbound stream.

        `start_inbound()` is last on purpose. A connector that delivers everything its
        account can see discards events for rooms it has no state for, so reading before
        the restore turns "not subscribed yet" into "lost" — and the errors returned
        here are per-watcher failures, not a reason to leave the connector deaf, so the
        stream starts regardless of them.
        """
        errors = await self._lifecycle.sync_watchers(unavailable_agents=unavailable_agents)
        await self._connector.start_inbound()
        await self._replay_persisted_records()
        return errors

    async def _replay_persisted_records(self) -> None:
        """Recover messages that arrived while the daemon was down (§2.2).

        The abort guarantee — "watermark unchanged, so redelivery can retry" —
        is only worth something if something actually redelivers, and at
        startup nothing did: both connectors replay from their *reconnect*
        callback, which a process restart never fires. This walks the
        persisted rule-derived records instead, and for each one probes the
        gap between its stored watermark and now. An empty gap leaves the room
        idle — that is the lazy model working. A non-empty gap recreates the
        watcher from its own record (sticky, §2.4 — rules are not consulted)
        and replays the window through the connector's normal pipeline, where
        the restored watermark and the id window dedup as usual.

        Best-effort per record: a room whose probe, recreation or replay fails
        stays idle and is recovered by its next live message. Boot must not
        die on one bad room.

        The accepted residual, restated: a room that never produced a record —
        or produced one with no watermark — has nothing to replay from, and a
        message that arrived for it while the daemon was down is gone until
        someone speaks again.
        """
        if self._watcher_manager is None:
            return
        for ws in list(self._lifecycle.states().values()):
            if not ws.rule_name or ws.paused or not ws.config:
                continue
            if not ws.last_processed_ts or not ws.room_id:
                continue
            if self._lifecycle.processor_named(ws.watcher_name) is not None:
                continue
            room = Room(
                id=ws.room_id,
                name=ws.room_name or ws.watcher_name,
                type=ws.room_type or "channel",
            )
            try:
                missed = await self._connector.fetch_room_history(
                    room, self._REPLAY_PROBE_COUNT, after_ts=ws.last_processed_ts
                )
            except Exception as e:
                logger.warning(
                    "Startup replay: could not probe room %s for watcher '%s': %s",
                    ws.room_id, ws.watcher_name, e,
                )
                continue
            if not missed:
                continue
            try:
                kind = RoomKind(ws.room_kind) if ws.room_kind else RoomKind.CHANNEL
                created = await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=ws.room_id,
                        kind=kind,
                        name=ws.room_name,
                        participants=tuple(ws.participants),
                    ),
                )
                if created is None:
                    continue
                await self._connector.replay_room_since(ws.room_id)
            except Exception as e:
                logger.warning(
                    "Startup replay failed for watcher '%s' (room %s) — the room "
                    "stays idle and its next message recovers it: %s",
                    ws.watcher_name, ws.room_id, e,
                )

    async def _route_unclaimed_room(self, room: RoomRef, trigger) -> None:
        """The router the connectors call for a room no watcher tracks (§2.2).

        The manager answers the whole question — sticky record, rule match,
        create or drop — and the connector delivers the trigger afterwards if
        the room became tracked. Nothing here inspects the trigger beyond
        asking the connector what history bound it implies: the frame is
        platform-shaped, and this layer deliberately is not.
        """
        await self._watcher_manager.get_or_create(
            self._connector_name,
            room,
            history_before_ts=self._connector.trigger_history_bound(trigger),
        )

    async def shutdown(self) -> None:
        """Stop all processors, save state, disconnect connector.

        Ordering is critical: processors must be stopped FIRST so their final
        live watermarks are flushed back into WatcherState before save_state()
        reads them.  Saving before stop_all() would persist stale watermarks
        and cause duplicate message delivery on the next restart.
        """
        logger.info("SessionManager shutting down")
        await self._lifecycle.stop_all()
        self._lifecycle.save_state()
        await self._connector.disconnect()
        logger.info("SessionManager shut down")

    # ── Public query API ──────────────────────────────────────────────────────

    def list_watchers(
        self, state_filter: StateFilter = StateFilter.OPERABLE
    ) -> list[dict]:
        return self._lifecycle.list_watchers(state_filter)

    def get_watcher_state(self, name: str):
        """Return the WatcherState for a watcher, or None if not found."""
        return self._lifecycle.get_watcher_state(name)

    def get_processor(self, name: str):
        """Return the running MessageProcessor for a watcher, or None.

        "Is a processor running" is a different question from "what state is
        this watcher's record in", and `list` answers only the second (§2.8).
        The old row answered both — `active` from the processor, `paused` from
        the record — which is why callers could reach the first one through
        `list`. They cannot now, and should not: under rule-derived watchers a
        row exists for rooms with no processor by design.
        """
        return self._lifecycle.get_processor(name)

    def get_watcher_config(self, name: str):
        """Return the WatcherConfig for a watcher name, or None if not found."""
        return self._lifecycle.get_watcher_config(name)

    def get_all_watcher_names(self) -> list[str]:
        """Return all configured watcher names for this connector."""
        return [wc.name for wc in self._lifecycle._watcher_configs]

    async def pause_watcher(self, name: str) -> None:
        await self._lifecycle.pause_watcher(name)

    async def resume_watcher(self, name: str) -> None:
        await self._lifecycle.resume_watcher(name)

    async def reset_watcher(self, name: str) -> None:
        await self._lifecycle.reset_watcher(name)

    async def inject_message(self, watcher_name: str, text: str) -> bool:
        """Inject a synthetic OWNER-role message directly into a watcher's queue.

        Bypasses the connector layer entirely, avoiding the self-message filter
        that drops messages sent by the bot's own username.  The injected message
        is treated as if it came from a trusted owner, so it is processed without
        permission approval prompts.

        Returns True if the message was accepted into the queue, False otherwise
        (e.g. watcher not running, queue full, or watcher not found).
        """
        processor = self._lifecycle.get_processor(watcher_name)
        if processor is None:
            logger.warning(
                "inject_message: no active processor for watcher %r — "
                "watcher may be paused, stopped, or not configured",
                watcher_name,
            )
            return False

        # Build a minimal Room from persisted state (room_id + room_type)
        state = self._lifecycle.get_watcher_state(watcher_name)
        wc = self._lifecycle.get_watcher_config(watcher_name)
        if state is None:
            logger.warning(
                "inject_message: no persisted state for watcher %r — "
                "room_id will be empty, which may cause the agent response to "
                "be posted to the wrong room or dropped. "
                "Ensure the watcher has been active at least once so its state is persisted.",
                watcher_name,
            )
        room_id = state.room_id if state else ""
        room_name = wc.room if wc else watcher_name
        room_type = state.room_type if state else "channel"

        msg = IncomingMessage(
            id=f"sched-{secrets.token_hex(8)}",
            # Epoch milliseconds (as a string), matching the format RC's own
            # messages carry — RocketChatConnector.format_prompt_prefix() feeds
            # this straight into ts_ms_to_iso_local(), which only parses epoch-ms.
            # A plain ISO string here silently drops ts:/day: from the header,
            # which is exactly the "scheduled stock report" scenario in #53.
            timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
            room=Room(id=room_id, name=room_name, type=room_type),
            sender=User(id="scheduler", username="scheduler", display_name="Scheduler"),
            role=UserRole.OWNER,
            text=text,
            attachments=[],
            warnings=[],
            thread_id=None,
            raw={},
        )
        accepted = await processor.enqueue(msg)
        if not accepted:
            logger.warning(
                "inject_message: message for watcher %r was dropped (queue full or processor stopped)",
                watcher_name,
            )
        return accepted

    async def notify_watcher_room(self, watcher_name: str, text: str) -> bool:
        """Send a notification directly to the watcher's room via the connector.

        Bypasses the watcher queue entirely — used for system notifications
        (e.g. scheduler injection failures) that should reach the room even when
        the watcher is paused or its queue is full.

        Returns True if sent successfully, False on error or missing state.
        """
        from ..agents.response import AgentResponse  # local import avoids circular dependency

        state = self._lifecycle.get_watcher_state(watcher_name)
        if state is None or not state.room_id:
            logger.warning(
                "notify_watcher_room: no room_id for watcher %r — cannot send notification",
                watcher_name,
            )
            return False
        try:
            await self._connector.send_text(state.room_id, AgentResponse(text=text))
            return True
        except Exception as e:
            logger.warning(
                "notify_watcher_room: failed to send notification to watcher %r room: %s",
                watcher_name,
                e,
            )
            return False

    # ── Control command dispatch (called by GatewayService) ───────────────────

    async def dispatch_command(self, request: dict) -> dict:
        cmd = request.get("cmd")

        if cmd == "list":
            # An unparseable filter is an error, not a silent fallback to the
            # default: the caller asked a specific question and cannot tell from
            # the rows that it was answered with a different one.
            try:
                state_filter = parse_state_filter(request.get("states"))
            except (ValueError, TypeError) as e:
                # TypeError too: `parse_state_filter` iterates what it is given,
                # and a hand-written socket client can send `"states": 5`. The
                # CLI always sends a list, so this arm is unreachable from it —
                # but escaping as a TypeError turns a bad request into a
                # per-connector "failed to list watchers", which reads as the
                # daemon being broken rather than the request being wrong.
                return {"ok": False, "error": f"invalid 'states' filter: {e}"}
            return {"ok": True, "data": self.list_watchers(state_filter)}

        elif cmd == "pause":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'pause' command"}
            try:
                await self.pause_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif cmd == "resume":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'resume' command"}
            try:
                await self.resume_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif cmd == "reset":
            name = request.get("watcher_name", "")
            if not name:
                return {"ok": False, "error": "Missing 'watcher_name' for 'reset' command"}
            try:
                await self.reset_watcher(name)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        else:
            return {"ok": False, "error": f"Unknown command: {cmd}"}
