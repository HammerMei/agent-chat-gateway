"""SessionManager: thin orchestrator that wires collaborators together.

Delegates all real work to focused collaborators:
  - WatcherLifecycle: start/stop/pause/resume/reset watchers
  - MessageDispatcher: inbound message routing + permission interception
  - ContextInjector: context file reading + agent session injection
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
from .context_injector import ContextInjector
from .dispatch import MessageDispatcher
from .permission import PermissionRegistry
from .session_maps import SessionMaps
from .state_store import StateStore
from .watcher_lifecycle import WatcherLifecycle

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
    ) -> None:
        self._connector = connector
        maps = session_maps or SessionMaps()

        # Collaborators
        self._dispatcher = MessageDispatcher(connector, permission_registry)
        self._injector = ContextInjector(config)
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
        self._connector.register_handler(self._dispatcher.dispatch)
        self._connector.register_capacity_check(self._dispatcher.has_capacity)
        await self._connector.connect()
        errors = await self._lifecycle.sync_watchers(unavailable_agents=unavailable_agents)
        logger.info("SessionManager ready (run_once)")
        return errors

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

    def list_watchers(self) -> list[dict]:
        return self._lifecycle.list_watchers()

    def get_watcher_state(self, name: str):
        """Return the WatcherState for a watcher, or None if not found."""
        return self._lifecycle.get_watcher_state(name)

    def get_watcher_config(self, name: str):
        """Return the WatcherConfig for a watcher name, or None if not found."""
        return self._lifecycle.get_watcher_config(name)

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
        room_id = state.room_id if state else ""
        room_name = wc.room if wc else watcher_name
        room_type = state.room_type if state else "channel"

        msg = IncomingMessage(
            id=f"sched-{secrets.token_hex(8)}",
            timestamp=datetime.now(UTC).isoformat(),
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

    # ── Control command dispatch (called by GatewayService) ───────────────────

    async def dispatch_command(self, request: dict) -> dict:
        cmd = request.get("cmd")

        if cmd == "list":
            return {"ok": True, "data": self.list_watchers()}

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
