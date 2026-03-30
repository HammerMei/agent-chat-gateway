"""WatcherLifecycle: manages watcher start/stop/pause/resume/reset.

Extracted from SessionManager to keep watcher management logic focused.
Owns the _processors and _states dicts, delegates to MessageDispatcher,
ContextInjector, and StateStore for their respective concerns.
"""

from __future__ import annotations

import asyncio
import logging

from ..agents import AgentBackend
from .adapter_utils import ts_gt as _ts_gt
from .attachment_workspace import AttachmentWorkspace
from .config import CoreConfig, WatcherConfig
from .connector import Connector
from .context_injector import ContextInjector
from .dispatch import MessageDispatcher
from .message_processor import MessageProcessor
from .permission import PermissionRegistry
from .session_maps import SessionMaps
from .state import WatcherState
from .state_store import StateStore

logger = logging.getLogger("agent-chat-gateway.core.watcher_lifecycle")


class WatcherLifecycle:
    """Manages watcher start/stop/pause/resume/reset and related bookkeeping.

    Collaborators:
        - StateStore: persistence
        - MessageDispatcher: room→processor index
        - ContextInjector: context file injection
        - SessionMaps: shared session routing state
    """

    def __init__(
        self,
        connector: Connector,
        agents: dict[str, AgentBackend],
        default_agent: str,
        config: CoreConfig,
        watcher_configs: list[WatcherConfig],
        state_store: StateStore,
        dispatcher: MessageDispatcher,
        injector: ContextInjector,
        permission_registry: PermissionRegistry | None,
        maps: SessionMaps,
    ) -> None:
        self._connector = connector
        self._agents = agents
        self._default_agent = default_agent
        self._config = config
        self._watcher_configs = watcher_configs
        self._state_store = state_store
        self._dispatcher = dispatcher
        self._injector = injector
        self._permission_registry = permission_registry
        self._maps = maps
        self._attachment_workspace = AttachmentWorkspace(connector)
        self._blocked_agents: set[str] = set()

        self._processors: dict[str, MessageProcessor] = {}
        self._states: dict[str, WatcherState] = {}
        # Per-watcher mutex: prevents concurrent pause/resume/reset commands for
        # the same watcher from racing through _stop_processor / _start_watcher.
        # The control socket can serve multiple simultaneous clients, so two
        # commands targeting the same watcher could otherwise interleave and
        # corrupt _processors / _states.
        self._watcher_locks: dict[str, asyncio.Lock] = {}

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync_watchers(
        self, unavailable_agents: set[str] | None = None
    ) -> list[str]:
        """Start processors for all active (non-paused) watchers defined in config.

        Args:
            unavailable_agents: Optional set of agent names that are unavailable
                (backend failed to start, or permission broker failed to start).
                Any watcher whose resolved agent is in this set is skipped rather
                than started without broker enforcement — starting without a broker
                would silently bypass tool-call permission checks.

        Returns:
            List of human-readable error strings for any watchers that failed.
        """
        errors: list[str] = []
        persisted = self._state_store.load()
        blocked_agents = unavailable_agents or set()
        # Only update _blocked_agents when the caller explicitly provides the
        # set of unavailable agents.  Passing None (the default) means "no
        # information about agent availability" — NOT "all agents are available".
        # Unconditionally overwriting with an empty set when None is passed would
        # silently disarm the fail-closed _ensure_agent_available guard in
        # resume_watcher / reset_watcher, allowing watchers to start without
        # their permission brokers if sync_watchers is ever called a second time
        # without an availability check (e.g., a hot-reload path).
        if unavailable_agents is not None:
            self._blocked_agents = set(blocked_agents)

        for wc in self._watcher_configs:
            state = persisted.get(wc.name)
            if state and state.paused:
                logger.info("Watcher '%s' is paused — skipping startup", wc.name)
                self._states[wc.name] = state
                continue

            # Fail-closed: refuse to start a watcher whose agent's permission
            # broker could not be initialized.  A watcher that starts without
            # its broker would process messages with no tool-call enforcement.
            agent_name = self._resolve_agent_name(wc.agent)
            if agent_name in blocked_agents:
                msg = (
                    f"Watcher '{wc.name}' (room '{wc.room}'): skipped — "
                    f"agent '{agent_name}' is unavailable "
                    f"(backend or permission broker failed to start)"
                )
                logger.error(msg)
                errors.append(msg)
                continue

            try:
                # Hold the per-watcher lock for the entire _start_watcher call
                # so that a concurrent pause/resume/reset command (arriving via
                # the control socket after the socket is opened) cannot interleave
                # with the subscribe window.  Without the lock, a pause_watcher()
                # call arriving while _start_watcher is blocked at
                # subscribe_room() would find the processor already in
                # _processors, call stop() on it (sets state="stopped"), and then
                # _start_watcher would resume to call processor.start() — leaving
                # the processor in "stopped" state with a running consumer task,
                # silently dropping every subsequent message.
                # All other _start_watcher callers (resume_watcher, reset_watcher)
                # already hold this lock, so this makes the invariant uniform.
                async with self._get_watcher_lock(wc.name):
                    await self._start_watcher(wc, state)
            except Exception as e:
                msg = f"Watcher '{wc.name}' (room '{wc.room}'): failed to start: {e}"
                logger.error(msg)
                errors.append(msg)

        # Note: state entries for watchers removed from config are not actively
        # deleted from the persisted file here.  The next save() call (line below)
        # only persists self._states, which only contains watchers that were
        # started or are paused in this run — removed watchers are implicitly
        # dropped from the next save.  Log at debug level to avoid misleading
        # "pruning" messages when no actual deletion is performed yet.
        config_names = {wc.name for wc in self._watcher_configs}
        for name in list(persisted):
            if name not in config_names:
                logger.debug(
                    "Watcher '%s' not in current config — will be omitted from next state save",
                    name,
                )

        self._state_store.save(self._states)
        return errors

    def _get_watcher_lock(self, name: str) -> asyncio.Lock:
        """Return (creating if needed) the per-watcher mutex for lifecycle ops."""
        if name not in self._watcher_locks:
            self._watcher_locks[name] = asyncio.Lock()
        return self._watcher_locks[name]

    # ── Lifecycle controls ────────────────────────────────────────────────────

    async def pause_watcher(self, name: str) -> None:
        """Pause a watcher: stop processing messages but preserve state."""
        self._find_watcher_config(name)
        async with self._get_watcher_lock(name):
            state = self._states.get(name)
            if state and state.paused:
                logger.info("Watcher '%s' is already paused", name)
                return
            try:
                await self._stop_processor(name, save=False)
            except Exception as e:
                # Best-effort teardown: _stop_processor already removed the processor
                # from _processors even when it raises (e.g. network error during
                # DDP unsubscribe).  Log the error but continue — marking the watcher
                # paused is still correct since it is no longer processing messages.
                logger.warning(
                    "Watcher '%s': error during stop phase of pause (proceeding with pause): %s",
                    name,
                    e,
                )
            if state:
                state.paused = True
            else:
                self._states[name] = WatcherState(
                    watcher_name=name,
                    session_id="",
                    room_id="",
                    paused=True,
                )
            self._state_store.save(self._states)
            logger.info("Watcher '%s' paused", name)

    async def resume_watcher(self, name: str) -> None:
        """Resume a paused watcher."""
        wc = self._find_watcher_config(name)
        self._ensure_agent_available(wc)
        async with self._get_watcher_lock(name):
            state = self._states.get(name)
            if name in self._processors:
                logger.info("Watcher '%s' is already running", name)
                # Clear paused flag and persist — the watcher is already running
                # so no restart is needed, but the flag must be updated.
                if state:
                    state.paused = False
                self._state_store.save(self._states)
                return
            try:
                await self._start_watcher(wc, state)
            except Exception as e:
                logger.error("Failed to resume watcher '%s': %s", name, e)
                raise
            # Only clear paused flag AFTER successful start — if _start_watcher() raises,
            # the watcher is still stopped and the paused flag should remain True in memory
            # so the next restart (or manual retry) correctly reflects the watcher's state.
            if state:
                state.paused = False
            self._state_store.save(self._states)
            logger.info("Watcher '%s' resumed", name)

    async def reset_watcher(self, name: str) -> None:
        """Reset a watcher: clear session and restart with fresh state."""
        wc = self._find_watcher_config(name)
        self._ensure_agent_available(wc)
        async with self._get_watcher_lock(name):
            try:
                await self._stop_processor(name, save=False)
            except Exception as e:
                # Best-effort teardown: log the error but continue with the restart.
                # A failure here (e.g. network error while sending DDP unsub) should
                # not prevent the user from recovering the watcher via reset.
                logger.warning(
                    "Watcher '%s': error during stop phase of reset (proceeding with restart): %s",
                    name,
                    e,
                )

            state = self._states.get(name)
            # Clear injection retry state BEFORE resetting context_injected so
            # the new startup attempt begins with a fresh failure counter.
            # Without this, a watcher that reached ``failed_degraded`` would
            # immediately re-enter that state after reset (the old failure_count
            # is still at ``_MAX_INJECT_ATTEMPTS``, so one more failure tips it
            # over again).
            # NOTE: computed OUTSIDE the `if state:` guard — a pinned wc.session_id
            # must be reset even when state is None (watcher failed before any
            # state was persisted).
            old_session_id = wc.session_id or (state.session_id if state else "")
            if old_session_id:
                self._injector.reset_session(old_session_id)
            if state:
                if not wc.session_id:
                    state.session_id = ""
                state.context_injected = False
                state.paused = False

            try:
                await self._start_watcher(wc, state)
            except Exception as e:
                logger.error("Failed to restart watcher '%s' after reset: %s", name, e)
                raise
            self._state_store.save(self._states)
            logger.info("Watcher '%s' reset", name)

    def list_watchers(self) -> list[dict]:
        """Return info for all configured watchers, including runtime status."""
        result = []
        for wc in self._watcher_configs:
            state = self._states.get(wc.name)
            processor = self._processors.get(wc.name)
            effective_session = wc.session_id or (state.session_id if state else "")
            result.append(
                {
                    "watcher_name": wc.name,
                    "room_name": wc.room,
                    "connector": wc.connector,
                    "agent_name": wc.agent,
                    "session_id": effective_session,
                    "paused": state.paused if state else False,
                    "active": processor is not None,
                    "context_injection_state": (
                        self._injector.status_for(effective_session).state
                        if effective_session
                        else "not_started"
                    ),
                }
            )
        return result

    def get_watcher_state(self, name: str):
        """Return the WatcherState for a watcher, or None if not found."""
        return self._states.get(name)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def stop_all(self) -> None:
        """Stop all processors (called during shutdown)."""
        for name in list(self._processors):
            try:
                await self._stop_processor(name, save=False)
            except Exception as e:
                logger.error("Error stopping watcher '%s' during shutdown: %s", name, e)

    def save_state(self) -> None:
        """Persist current state (called before shutdown)."""
        self._state_store.save(self._states)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _start_watcher(
        self,
        wc: WatcherConfig,
        state: WatcherState | None,
    ) -> None:
        """Start a single watcher: resolve room, ensure session, start processor.

        Phases:
          1. Resolve agent and room.
          2. Provision session (reuse or create).
          3. Build state and register session maps.
          4. Inject context files.
          5. Prepare attachment workspace.
          6. Create MessageProcessor (not yet started).
          7. Subscribe to connector (with rollback on failure).
          8. Register processor with dispatcher (deferred until subscribe succeeds).
          9. Activate processor (start consumer loop + online notification).
         10. Restore dedup watermark.
        """
        agent_name = self._resolve_agent_name(wc.agent)
        agent = self._agents[agent_name]
        agent_cfg = self._config.agent_config(agent_name)

        # 1. Resolve room
        room = await self._connector.resolve_room(wc.room)

        # 2. Provision session
        session_id, created_new_session = await self._provision_session(
            wc, state, agent, agent_cfg
        )

        # 3. Build state and register maps
        ws = WatcherState(
            watcher_name=wc.name,
            session_id=wc.session_id or session_id,
            room_id=room.id,
            room_type=room.type,
            context_injected=state.context_injected if state else False,
            paused=False,
            last_processed_ts=state.last_processed_ts if state else "",
        )
        self._states[wc.name] = ws
        self._maps.bind_session(session_id, room.id, self._connector)

        # 4. Inject context (rollback maps on failure)
        try:
            await self._injector.inject(
                ws, session_id, agent, agent_name, wc.connector, wc
            )
        except Exception:
            self._states.pop(wc.name, None)
            self._maps.remove_session(session_id)
            await self._cleanup_startup_session_best_effort(
                agent, session_id, created_new_session, wc.name
            )
            raise

        # 5. Prepare attachment workspace
        # setup() contains multiple synchronous blocking filesystem calls
        # (mkdir, is_symlink, resolve, unlink, symlink_to, exists).  Running
        # them on the event loop would block all other coroutines during disk
        # I/O; asyncio.to_thread() offloads the whole operation to a thread
        # pool worker to keep the loop responsive.
        try:
            attachment_local_base = await asyncio.to_thread(
                self._attachment_workspace.setup,
                wc.name,
                room.id,
                agent_cfg.working_directory,
            )
        except Exception:
            # setup() failed (e.g., filesystem error, permission denied).  Roll
            # back the state and maps entries added in steps 3–4 so that a later
            # resume/restart sees a clean slate rather than a partially-built
            # WatcherState (with context_injected=True) and a dangling session
            # binding that would cause context re-injection to be skipped on the
            # next attempt.
            self._states.pop(wc.name, None)
            self._maps.remove_session(session_id)
            await self._cleanup_startup_session_best_effort(
                agent, session_id, created_new_session, wc.name
            )
            raise

        # 6. Create processor (not started yet — activation deferred to step 9
        # so that the online notification is not emitted before subscribe succeeds).
        processor = MessageProcessor(
            session_id=session_id,
            room=room,
            working_directory=agent_cfg.working_directory,
            watcher_id=wc.name,
            connector=self._connector,
            agent=agent,
            agent_name=agent_name,
            config=self._config,
            permission_registry=self._permission_registry,
            session_role_map=self._maps.role,
            session_permission_thread_map=self._maps.permission_thread,
            session_maps=self._maps,
            context_injector=self._injector,
            watcher_state=ws,
            watcher_config=wc,
            connector_name=wc.connector,
            online_notification=wc.online_notification,
            offline_notification=wc.offline_notification,
            attachment_local_base=attachment_local_base,
        )
        self._processors[wc.name] = processor

        # 7. Subscribe (rollback everything on failure)
        try:
            await self._connector.subscribe_room(
                room,
                watcher_id=wc.name,
                working_directory=agent_cfg.working_directory,
            )
        except Exception:
            self._processors.pop(wc.name, None)
            # Keep ws in _states (do NOT pop) so that the context_injected flag
            # and session_id are preserved for the next _start_watcher call.
            cleaned = await self._cleanup_startup_session_best_effort(
                agent, session_id, created_new_session, wc.name
            )
            if cleaned and created_new_session and not wc.session_id:
                ws.session_id = ""
                # The session that received context injection was destroyed, so
                # the next _start_watcher will create a brand-new session that
                # has never seen the context.  Reset the flag so injection is
                # re-attempted for the new session — without this, the new
                # session inherits context_injected=True from the old ws and
                # the agent silently operates without its system context.
                ws.context_injected = False
            self._states[wc.name] = ws
            self._maps.remove_session(session_id)
            raise

        # 8. Register with dispatcher — only after subscribe succeeds.
        self._dispatcher.add_processor(room.id, processor)

        # 9. Activate processor — starts the consumer loop and emits the
        # online notification.  Deferred to here so users never see "online"
        # for a watcher whose room subscription failed.
        processor.start()

        # 10. Restore watermark.
        # Only advance the room-level watermark; never move it backwards.
        # This matters when multiple watchers share the same room: a watcher that
        # was paused or reset may have an older persisted timestamp than a sibling
        # watcher that has been running and advancing the shared room watermark.
        # Writing an older value back would cause duplicate message delivery for
        # all watchers on that room after the next reconnect.
        if ws.last_processed_ts:
            current_ts = self._connector.get_last_processed_ts(room.id)
            if not current_ts or _ts_gt(ws.last_processed_ts, current_ts):
                self._connector.update_last_processed_ts(room.id, ws.last_processed_ts)

        logger.info(
            "Started watcher '%s' for room '%s' using agent '%s' (session %s)",
            wc.name,
            wc.room,
            agent_name,
            session_id[:8],
        )

    async def _provision_session(
        self,
        wc: WatcherConfig,
        state: WatcherState | None,
        agent: AgentBackend,
        agent_cfg,
    ) -> tuple[str, bool]:
        """Determine the session ID: reuse from config/state, or create a new one.

        Priority:
          1. Explicit ``wc.session_id`` from config (pinned session).
          2. Persisted ``state.session_id`` from a previous run.
          3. Create a new session via the agent backend.
        """
        if wc.session_id:
            return wc.session_id, False
        if state and state.session_id:
            return state.session_id, False
        session_title = (
            f"{agent_cfg.session_prefix}:{wc.room}"
            if agent_cfg.session_prefix
            else None
        )
        session_id = await agent.create_session(
            agent_cfg.working_directory,
            extra_args=None,
            session_title=session_title,
        )
        logger.info("Watcher '%s': created new session %s", wc.name, session_id[:8])
        return session_id, True

    async def _cleanup_startup_session_best_effort(
        self,
        agent: AgentBackend,
        session_id: str,
        created_new_session: bool,
        watcher_name: str,
    ) -> bool:
        """Delete a freshly created session when watcher startup later fails."""
        if not created_new_session or not session_id:
            return False
        try:
            cleaned = await agent.delete_session(session_id)
            if cleaned:
                logger.info(
                    "Watcher '%s': cleaned up startup session %s after failure",
                    watcher_name,
                    session_id[:8],
                )
                return True
            logger.warning(
                "Watcher '%s': could not confirm cleanup of startup session %s",
                watcher_name,
                session_id[:8],
            )
            return False
        except Exception as e:
            logger.warning(
                "Watcher '%s': startup session cleanup failed for %s: %s",
                watcher_name,
                session_id[:8],
                e,
            )
            return False

    async def _stop_processor(self, name: str, save: bool) -> None:
        """Stop a processor and clean up all mappings.

        Order is critical for correctness:
          1. Remove from dispatcher — new inbound messages stop being routed here.
          2. Unsubscribe from connector — DDP stops delivering messages to this room.
          3. Stop the processor — drains any already-queued messages, then shuts down.
          4. Capture live watermark — after the queue is drained so the timestamp
             reflects the last message the processor *actually* handled.
          5. Clean session maps.
        """
        processor = self._processors.pop(name, None)
        state = self._states.get(name)
        wc = next((w for w in self._watcher_configs if w.name == name), None)
        errors: list[str] = []

        # Step 1: Remove from dispatcher so no new messages are routed to this processor.
        if processor and state and state.room_id:
            self._dispatcher.remove_processor(state.room_id, processor)

        # Step 2: Unsubscribe from the connector (stop DDP delivery for this room).
        if state and state.room_id:
            try:
                await self._connector.unsubscribe_room(state.room_id, watcher_id=name)
            except Exception as e:
                errors.append(f"unsubscribe failed: {e}")
                logger.error(
                    "Watcher '%s': unsubscribe failed for room '%s': %s",
                    name,
                    state.room_id,
                    e,
                )
        elif state:
            logger.debug(
                "Watcher '%s' has no room_id in state — skipping unsubscribe", name
            )

        # Step 3: Stop the processor (drain the queue; _stopping=True rejects late arrivals).
        if processor:
            try:
                await processor.stop()
            except Exception as e:
                errors.append(f"processor stop failed: {e}")
                logger.error("Watcher '%s': processor stop failed: %s", name, e)

        # Step 4: Capture the live watermark after the queue has been fully drained.
        if state and state.room_id:
            live_ts = self._connector.get_last_processed_ts(state.room_id)
            if live_ts:
                state.last_processed_ts = live_ts

        # Step 5: Clean up session maps.
        # Convention: empty string "" in session_id means "no session" (auto-create
        # mode, not yet assigned).  The falsy guard below skips cleanup in that case.
        if state:
            effective_session = (
                wc.session_id if wc and wc.session_id else state.session_id
            )
            if effective_session:
                if self._permission_registry:
                    self._permission_registry.cancel_session(effective_session)
                self._maps.remove_session(effective_session)

        if save:
            self._state_store.save(self._states)
        logger.info("Stopped processor for watcher '%s'", name)
        if errors:
            raise RuntimeError(
                f"Watcher '{name}' stop completed with errors: {'; '.join(errors)}"
            )

    def _find_watcher_config(self, name: str) -> WatcherConfig:
        for wc in self._watcher_configs:
            if wc.name == name:
                return wc
        raise RuntimeError(
            f"Watcher '{name}' not found in config. "
            f"Available: {[wc.name for wc in self._watcher_configs]}"
        )

    def _ensure_agent_available(self, wc: WatcherConfig) -> None:
        """Fail closed if a watcher's resolved agent is currently unavailable."""
        agent_name = self._resolve_agent_name(wc.agent)
        if agent_name in self._blocked_agents:
            raise RuntimeError(
                f"Watcher '{wc.name}' cannot start because agent '{agent_name}' is unavailable"
            )

    def _resolve_agent_name(self, name: str | None) -> str:
        if name and name in self._agents:
            return name
        if name and name not in self._agents:
            logger.warning(
                "Agent '%s' not found in config, using default '%s'",
                name,
                self._default_agent,
            )
        return self._default_agent

    # Attachment symlink management has been extracted to
    # gateway.core.attachment_workspace.AttachmentWorkspace.
