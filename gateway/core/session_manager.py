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
from .lifecycle_sweep import LifecycleSweep
from .permission import PermissionRegistry
from .session_maps import SessionMaps
from .state import StateFilter, parse_state_filter, past_idle_ttl
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
        # The idle sweep exists only where rule-derived watchers do: a static
        # deployment's lifecycle is config.yaml's, and its records carry no
        # frozen rule for the sweep to read anyway (§2.5).
        self._sweep = (
            LifecycleSweep(self._lifecycle)
            if self._watcher_manager is not None
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
        # Snapshotted before the stream opens, and that placement is the whole
        # defence against the race below: after `start_inbound` a live message
        # can recreate a room and advance its watermark past the entire
        # down-window before the replay loop reaches that record. Reading the
        # watermarks here freezes what "missed" means; the replay itself still
        # runs after the stream, so a room's live traffic is never held up by
        # a REST probe.
        down_window = self._snapshot_watermarks()
        await self._connector.start_inbound()
        # Before the replay, deliberately: a record this evaluation recreates
        # owns its own replay (§2.2, replay ownership), so the loop below finds
        # it resident and skips it — and a record it marks idle is still probed
        # below, because messages waiting in a room outrank its idleness.
        await self._evaluate_lifecycle_at_boot()
        await self._replay_persisted_records(down_window)
        if self._sweep is not None:
            # After the replay completes, structurally — §2.5 makes this
            # ordering non-optional once the expiry leg exists (expiry deletes
            # the record recreation reads from, and the replay's whole job is
            # reviving rooms with messages waiting), and the idle leg honors it
            # from day one so step 5 does not have to move this line.
            self._sweep.start()
        return errors

    async def _evaluate_lifecycle_at_boot(self) -> None:
        """Boot runs the same evaluation the sweep runs (§2.5), over the
        records that were *active* at shutdown.

        One function, two callers: the TTL arithmetic is `past_idle_ttl`, the
        same call the sweep makes — a boot rule and a running rule would drift
        on exactly the restart-only path nothing exercises.

        A was-active record (rule-derived, not paused, no `dropped_at`) is
        either **recreated** through `get_or_create` — so replay, sticky
        binding and the paused refusal all apply for free — or, when it is
        already past its idle TTL, **marked idle with a fresh `dropped_at`**
        rather than paying a resume for a room the first sweep would drop
        minutes later. Fresh, never backdated: expiry measures from this
        moment, which is what keeps `active → expired` impossible through an
        outage of any length (§2.5).

        This is also what keeps `list` honest after a restart: a was-active
        record has no `dropped_at` and no processor, which reads as `failed` —
        the default view reporting a healthy fleet as broken. After this pass
        every such record is resident again or honestly idle.
        """
        if self._watcher_manager is None:
            return
        now = datetime.now().astimezone()
        stamped = 0
        for record in list(self._lifecycle.states().values()):
            if (
                not record.rule_name
                or record.paused
                or record.dropped_at
                or not record.room_id
            ):
                continue
            if past_idle_ttl(record, now):
                record.dropped_at = now.isoformat(timespec="seconds")
                stamped += 1
                logger.info(
                    "Watcher '%s' was idle past its TTL across the restart — "
                    "marked idle; its next message wakes it",
                    record.watcher_name,
                )
                continue
            kind = RoomKind(record.room_kind) if record.room_kind else RoomKind.CHANNEL
            try:
                await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=record.room_id,
                        kind=kind,
                        name=record.room_name,
                        participants=tuple(record.participants),
                    ),
                )
            except Exception as e:
                # An abort, not a decision (§2.2): the record keeps reading
                # `failed`, which is the honest state, and the next boot — or
                # the room's next message — retries.
                logger.warning(
                    "Boot recreation failed for watcher '%s' (room %s) — it "
                    "stays failed until its next message or the next start: %s",
                    record.watcher_name, record.room_id, e,
                )
        if stamped:
            self._lifecycle.save_state()

    def _snapshot_watermarks(self) -> dict[str, str]:
        """Each rule-derived record's watermark as of boot, by watcher name.

        Taken after `sync_watchers` — hydration is what puts the records in
        memory — and before the inbound stream opens, so every value is the
        boundary of the interval the daemon was down for, uncontaminated by
        anything that arrives after.
        """
        return {
            ws.watcher_name: ws.last_processed_ts
            for ws in self._lifecycle.states().values()
            if ws.rule_name and ws.last_processed_ts
        }

    async def _replay_persisted_records(self, down_window: dict[str, str]) -> None:
        """Recover messages that arrived while the daemon was down (§2.2).

        The abort guarantee — "watermark unchanged, so redelivery can retry" —
        is only worth something if something actually redelivers, and at
        startup nothing did: both connectors replay from their *reconnect*
        callback, which a process restart never fires. This walks the
        persisted rule-derived records instead, and for each one probes the
        gap between its boot-time watermark and now. An empty gap leaves the
        room idle — that is the lazy model working. A non-empty gap recreates
        the watcher from its own record (sticky, §2.4 — rules are not
        consulted) and replays the window through the connector's normal
        pipeline, where the restored watermark and the id window dedup as
        usual.

        ``down_window`` holds the watermarks as of *before the stream opened*
        (`_snapshot_watermarks`), and every decision here reads it rather than
        the record. A live message arriving between `start_inbound` and this
        loop recreates its room and advances that record's watermark past the
        whole down-window; reading the record then would report no gap and skip
        the room, losing every message from the outage with no log line. The
        snapshot also means a room that is *already resident* by the time the
        loop reaches it is still replayed — being resident is not evidence that
        anyone looked below the boundary.

        Best-effort per record: a room whose probe, recreation or replay fails
        stays idle, and its next live message triggers a recreation — which
        replays from the record's watermark, so the interval is recovered
        rather than merely the room. Boot must not die on one bad room.

        The accepted residual, restated: a room that never produced a record —
        or produced one with no watermark — has nothing to replay from, and a
        message that arrived for it while the daemon was down is gone until
        someone speaks again.
        """
        if self._watcher_manager is None:
            return
        for ws in list(self._lifecycle.states().values()):
            boundary = down_window.get(ws.watcher_name)
            if not boundary:
                # No record at boot, or no watermark to replay from — including
                # every room created live since the stream opened, which has
                # nothing behind it to recover.
                continue
            if ws.paused or not ws.config or not ws.room_id:
                continue
            room = Room(
                id=ws.room_id,
                name=ws.room_name or ws.watcher_name,
                # The record's kind, so a group DM reaches the direct-room
                # history endpoint rather than a channel one.
                type=ws.room_kind or ws.room_type or "channel",
            )
            try:
                missed = await self._connector.probe_missed_since(room, boundary)
            except Exception as e:
                logger.warning(
                    "Startup replay: could not probe room %s for watcher '%s': %s",
                    ws.room_id, ws.watcher_name, e,
                )
                continue
            if not missed:
                continue
            try:
                if self._lifecycle.processor_named(ws.watcher_name) is not None:
                    # Already resident, so a recreation already ran for it — and
                    # a recreation owns its room's replay (§2.2, "replay
                    # ownership"). Nothing to do: replaying here would be a
                    # second pass over the interval the recreation just covered,
                    # and a second pass is not free, because replay hands the
                    # filter its own boundary rather than the live watermark —
                    # so the ts filter suppresses nothing and only the bounded
                    # id window separates the two.
                    #
                    # That a resident room with a record must have come through
                    # a recreation is not an assumption: its record rules out
                    # `_create`, and a rule-derived record is absent from
                    # `watchers:`, so the static path never starts one either.
                    continue
                kind = RoomKind(ws.room_kind) if ws.room_kind else RoomKind.CHANNEL
                # Triggering the recreation is this loop's whole job. It owns no
                # interval of its own; the recreation replays what the room owes.
                await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=ws.room_id,
                        kind=kind,
                        name=ws.room_name,
                        participants=tuple(ws.participants),
                    ),
                )
            except Exception as e:
                logger.warning(
                    "Startup replay failed for watcher '%s' (room %s) — the room "
                    "stays idle; its next message recreates it and replays from "
                    "the record's watermark, so this interval is deferred rather "
                    "than lost: %s",
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
        if self._sweep is not None:
            # Before stop_all, so a pass cannot overlap the shutdown's own
            # teardown of the processors it is judging.
            await self._sweep.stop()
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
        if processor is None and self._watcher_manager is not None:
            # The wake, from the inside (§2.5): an idle room's record is real
            # and its session is kept, so a scheduled job due in it recreates
            # the watcher through the same get_or_create a message would —
            # the sweep's expiry exemption for job-bearing rooms rests on
            # exactly this ("idling one is harmless — the job wakes it"), and
            # without it that sentence was an assumption with no backing.
            # Paused answers None, so pause still outranks a schedule (§4.4).
            record = self._lifecycle.get_watcher_state(watcher_name)
            if record is not None and record.room_id:
                kind = (
                    RoomKind(record.room_kind)
                    if record.room_kind
                    else RoomKind.CHANNEL
                )
                processor = await self._watcher_manager.get_or_create(
                    self._connector_name,
                    RoomRef(
                        id=record.room_id,
                        kind=kind,
                        name=record.room_name,
                        participants=tuple(record.participants),
                    ),
                )
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
