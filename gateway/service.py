"""Gateway service: wires multiple Connectors + SessionManagers together.

This module is the top-level orchestration layer:
  - One ConnectorEntry per connector defined in config
  - Each ConnectorEntry has its own SessionManager with isolated state
  - A single unified control socket routes CLI commands to the right manager

Startup order
-------------
1-2. AgentRuntimeManager.start_all() — start all backends and permission brokers
     (ordering handled internally: backends first, then brokers)
3.   run_once()  — connect connectors and resume sessions

daemon.py and cli.py interface is unchanged:
    service = GatewayService(config)
    await service.run()
"""

import asyncio
import logging
import os
from dataclasses import dataclass

from .agents import AgentBackend, GatewayBrokerConfig
from .agents.claude import ClaudeBackend
from .agents.opencode import OpenCodeBackend
from .config import AgentConfig, GatewayConfig
from .connectors import connector_factory
from .control import ControlServer
from .core.config import CoreConfig
from .core.connector import Connector
from .core.expiry_task import run_expiry_task
from .core.job_store import JobStore
from .core.permission import (
    ConnectorPermissionNotifier,
    PermissionBroker,
    PermissionRegistry,
)
from .core.scheduler import JobScheduler
from .core.session_manager import SessionManager
from .core.session_maps import SessionMaps

logger = logging.getLogger("agent-chat-gateway.service")


def _build_agent_backend(agent_cfg: AgentConfig) -> AgentBackend:
    """Instantiate the correct AgentBackend from an AgentConfig."""
    if not agent_cfg.permissions.enabled and agent_cfg.permissions.skip_owner_approval:
        logger.warning(
            "Agent '%s': permissions.skip_owner_approval=true has no effect because "
            "permissions.enabled=false — the permission broker is disabled entirely. "
            "Set permissions.enabled=true to activate skip_owner_approval.",
            agent_cfg.name,
        )
    broker_config = (
        GatewayBrokerConfig(
            owner_allowed_tools=agent_cfg.effective_owner_allowed_tools(),
            guest_allowed_tools=agent_cfg.guest_allowed_tools,
            timeout=agent_cfg.permissions.timeout,
            skip_owner_approval=agent_cfg.permissions.skip_owner_approval,
        )
        if agent_cfg.permissions.enabled
        else None
    )

    if agent_cfg.type == "claude":
        return ClaudeBackend(
            command=agent_cfg.command,
            new_session_args=agent_cfg.new_session_args,
            timeout=agent_cfg.timeout,
            broker_config=broker_config,
        )
    if agent_cfg.type == "opencode":
        # sidecar_env is intentionally hardcoded: the opencode sidecar process
        # always runs as "owner" because it is the gateway's own agent backend.
        # Per-message guest enforcement (tool allow-lists, permission prompts)
        # is handled by the PermissionBroker, not by environment variables.
        return OpenCodeBackend(
            command=agent_cfg.command,
            new_session_args=agent_cfg.new_session_args,
            timeout=agent_cfg.timeout,
            sidecar_env={"ACG_ROLE": "owner"},
            sidecar_cwd=agent_cfg.working_directory or None,
            broker_config=broker_config,
        )
    raise ValueError(
        f"Unknown agent type: {agent_cfg.type!r} (supported: 'claude', 'opencode')"
    )


class AgentRuntimeManager:
    """Manages per-agent backend + permission broker lifecycle.

    Encapsulates the startup ordering constraint (backends first, then brokers)
    and failure tracking so that :class:`GatewayService` never needs to know
    about internal sequencing or which agents are permission-enabled.
    """

    def __init__(self, agents: dict[str, AgentBackend]) -> None:
        self._agents = agents
        self._brokers: dict[str, PermissionBroker] = {}
        self._unavailable: set[str] = set()

    async def start_all(
        self,
        registry: PermissionRegistry,
        notifier: "ConnectorPermissionNotifier",
        maps: SessionMaps,
    ) -> list[str]:
        """Start all agent backends and their permission brokers.

        Ordering is handled internally:
          1. Start backends (e.g. ``opencode serve``).
          2. Start permission brokers — only for backends that succeeded.
             Broker creation uses the backend's resolved URL, so it must follow
             backend startup.

        Returns:
            List of human-readable error strings for any agent that failed.
        """
        errors: list[str] = []
        failed_backends: set[str] = set()

        # Phase 1: start backends
        async def _start_backend(
            name: str, backend: AgentBackend
        ) -> tuple[str, Exception | None]:
            try:
                await backend.start()
                return name, None
            except Exception as e:
                return name, e

        backend_results = await asyncio.gather(
            *[_start_backend(name, backend) for name, backend in self._agents.items()]
        )
        for name, err in backend_results:
            if err is not None:
                msg = f"Agent '{name}': backend failed to start — agent will be unavailable: {err}"
                logger.error(msg)
                errors.append(msg)
                failed_backends.add(name)

        # Phase 2: start permission brokers (skip agents with failed backends)
        failed_broker_agents: set[str] = set()
        for name in failed_backends:
            logger.debug("Agent '%s': skipping broker — backend failed to start", name)

        async def _start_broker(
            name: str, backend: AgentBackend
        ) -> tuple[str, PermissionBroker | None, Exception | None]:
            try:
                broker = backend.create_gateway_broker(
                    registry=registry,
                    notifier=notifier,
                    session_room_map=maps.room_view,
                    session_role_map=maps.role_view,
                    session_permission_thread_map=maps.permission_thread_view,
                )
                if broker is None:
                    return name, None, None
                await broker.start()
                return name, broker, None
            except Exception as e:
                return name, None, e

        broker_results = await asyncio.gather(
            *[
                _start_broker(name, backend)
                for name, backend in self._agents.items()
                if name not in failed_backends
            ]
        )
        for name, broker, err in broker_results:
            if err is None:
                if broker is None:
                    continue
                self._brokers[name] = broker
                logger.info("Agent '%s': permission broker started", name)
            else:
                msg = f"Agent '{name}': permission broker failed to start: {err}"
                logger.error(msg)
                errors.append(msg)
                failed_broker_agents.add(name)

        for name in failed_broker_agents:
            backend = self._agents.get(name)
            if not backend:
                continue
            try:
                await backend.stop()
            except Exception as e:
                logger.error(
                    "Agent '%s': backend stop after broker failure also failed: %s",
                    name,
                    e,
                )

        self._unavailable = failed_backends | failed_broker_agents
        return errors

    async def stop_all(self) -> None:
        """Stop all brokers and backends (reverse of start order)."""
        for name, broker in self._brokers.items():
            try:
                await broker.stop()
            except Exception as e:
                logger.error("Error stopping broker for agent '%s': %s", name, e)
        self._brokers.clear()

        backend_results = await asyncio.gather(
            *[backend.stop() for backend in self._agents.values()],
            return_exceptions=True,
        )
        for (name, _backend), result in zip(
            self._agents.items(), backend_results, strict=False
        ):
            if isinstance(result, Exception):
                logger.error("Error stopping backend for agent '%s': %s", name, result)

    @property
    def unavailable_agents(self) -> set[str]:
        """Agent names that failed to start (backend or broker)."""
        return self._unavailable

    @property
    def has_active_brokers(self) -> bool:
        """True if at least one permission broker was started successfully."""
        return bool(self._brokers)


@dataclass
class ConnectorEntry:
    """A single connector instance paired with its dedicated SessionManager."""

    name: str
    connector: Connector
    session_manager: SessionManager


class GatewayService:
    """Top-level orchestrator: manages one ConnectorEntry per configured connector.

    Each connector gets its own SessionManager with isolated state
    (state.{name}.json).  A single unified control socket routes CLI
    commands to the correct manager by connector name.

    External interface (used by daemon.py) is unchanged:
        service = GatewayService(config)
        await service.run()
    """

    def __init__(self, config: GatewayConfig) -> None:
        core_config = CoreConfig.from_gateway_config(config)

        # Shared permission registry (one per gateway instance)
        self._registry = PermissionRegistry()
        # Shared mutable maps between SessionManagers, brokers, and processors
        self._maps = SessionMaps()
        # Expiry background task handle
        self._expiry_task: asyncio.Task | None = None
        # Scheduler task handle
        self._scheduler_task: asyncio.Task | None = None

        # Build agents — runtime manager handles backend + broker lifecycle
        agents: dict[str, AgentBackend] = {
            name: _build_agent_backend(agent_cfg)
            for name, agent_cfg in config.agents.items()
        }
        self._runtime_manager = AgentRuntimeManager(agents)

        self._entries: list[ConnectorEntry] = []
        for cc in config.connectors:
            connector = connector_factory(cc)
            # Filter watcher configs belonging to this connector
            connector_watchers = [
                wc for wc in config.watchers if wc.connector == cc.name
            ]
            sm = SessionManager(
                connector=connector,
                agents=agents,
                default_agent=config.default_agent,
                config=core_config,
                state_name=cc.name,
                watcher_configs=connector_watchers,
                permission_registry=self._registry,
                session_maps=self._maps,
            )
            self._entries.append(
                ConnectorEntry(name=cc.name, connector=connector, session_manager=sm)
            )

        # Build JobStore + JobScheduler
        self._job_store = JobStore()
        session_managers = {e.name: e.session_manager for e in self._entries}
        self._job_scheduler = JobScheduler(
            store=self._job_store,
            session_managers=session_managers,
            completed_job_ttl_days=config.scheduler.completed_job_ttl_days,
        )

        self._control = ControlServer(
            self._entries,
            job_store=self._job_store,
            default_timezone=config.scheduler.default_timezone,
        )

    async def run(self, startup_fd: int = -1) -> None:
        """Connect all connectors, start unified control socket, block until cancelled.

        Args:
            startup_fd: Write end of the daemon startup handshake pipe.  When >= 0
                the method writes startup results (zero or more ``error:<msg>\\n``
                lines followed by ``ok\\n``) and closes the fd after the full startup
                sequence completes.  Pass -1 (default) to skip signalling — used in
                tests and scripts that call run() directly.
        """
        startup_errors: list[str] = []
        startup_signaled = False

        try:
            # 1-2. Start all agent backends and permission brokers.  The runtime
            #      manager handles ordering (backends first, then brokers) and
            #      failure isolation internally.
            notifier = ConnectorPermissionNotifier(self._maps.connector_view)
            runtime_errors = await self._runtime_manager.start_all(
                registry=self._registry,
                notifier=notifier,
                maps=self._maps,
            )
            startup_errors.extend(runtime_errors)

            # Start the permission expiry background task if any brokers are active.
            if self._runtime_manager.has_active_brokers:
                self._expiry_task = asyncio.create_task(
                    run_expiry_task(self._registry, notifier),
                    name="permission-expiry",
                )

            # 3. run_once() connects each SessionManager without blocking — the daemon
            #    loop below keeps the process alive.  We intentionally avoid sm.run()
            #    so that only the GatewayService owns the control socket.
            sm_error_lists = await asyncio.gather(
                *[
                    e.session_manager.run_once(
                        unavailable_agents=self._runtime_manager.unavailable_agents,
                    )
                    for e in self._entries
                ]
            )
            for errs in sm_error_lists:
                startup_errors.extend(errs)

            # Load persisted jobs and start the job scheduler AFTER connectors are
            # connected and watchers are up.  Starting it before run_once() would
            # cause catch-up messages to be dropped (processors not yet started).
            if getattr(self, "_job_store", None) is not None:
                self._job_store.load()
                self._scheduler_task = asyncio.create_task(
                    self._job_scheduler.run(),
                    name="job-scheduler",
                )

            await self._control.start()
            names = ", ".join(e.name for e in self._entries)
            logger.info("GatewayService running with connector(s): %s", names)

            # Signal startup complete to the parent process (daemon handshake).
            if startup_fd >= 0:
                _write_startup_signal(startup_fd, startup_errors)
                startup_signaled = True

            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
        except Exception as e:
            if startup_fd >= 0 and not startup_signaled:
                # fatal=True: startup failed — do NOT emit "ok" so the parent
                # correctly reports failure and exits 1 instead of "degraded".
                _write_startup_signal(
                    startup_fd,
                    startup_errors + [f"startup failed: {e}"],
                    fatal=True,
                )
                startup_signaled = True
            raise
        finally:
            await self.shutdown()
            # Ensure the parent process never blocks forever on os.read(read_fd)
            # if startup was interrupted mid-flight by CancelledError (which is
            # NOT caught by `except Exception` above).  Without this, SIGTERM
            # arriving before startup_signaled=True leaves the write-fd open and
            # the parent hangs indefinitely waiting for EOF.
            if startup_fd >= 0 and not startup_signaled:
                # fatal=True: startup was cancelled before completing — no "ok".
                _write_startup_signal(
                    startup_fd,
                    startup_errors + ["startup cancelled"],
                    fatal=True,
                )
                startup_signaled = True

    async def shutdown(self) -> None:
        """Graceful shutdown — called by daemon.py on SIGTERM/crash.

        Shutdown order:
          1. Stop the control socket FIRST so no new lifecycle commands
             (pause/resume/reset) can arrive while session managers are
             tearing down.  A command reaching an already-shut-down
             WatcherLifecycle would produce confusing errors.
          2. Cancel the job scheduler BEFORE session managers shut down so
             that an in-progress fire cannot race a draining queue.
          3. Shut down session managers (drain processors, cancel permissions).
          4. Stop agent runtime (brokers, backends).
          5. Cancel the permission expiry task.
        """
        logger.info("GatewayService shutting down")
        # Step 1: close the control socket so no new commands arrive during teardown.
        try:
            await self._control.stop()
        except Exception as e:
            logger.error("Error stopping control server: %s", e)
        # Step 2: cancel the job scheduler before session managers stop.
        # This prevents a scheduler tick from trying to inject into a processor
        # that is in the middle of draining its queue.
        if getattr(self, "_scheduler_task", None):
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Error stopping job scheduler task: %s", e)
            finally:
                self._scheduler_task = None  # type: ignore[assignment]
        # Step 3: shut down session managers.
        sm_results = await asyncio.gather(
            *[e.session_manager.shutdown() for e in self._entries],
            return_exceptions=True,
        )
        for entry, result in zip(self._entries, sm_results, strict=False):
            if isinstance(result, Exception):
                logger.error(
                    "Error shutting down session manager for connector '%s': %s",
                    entry.name,
                    result,
                )
        # Step 4: stop agent runtime (brokers, backends).
        try:
            await self._runtime_manager.stop_all()
        except Exception as e:
            logger.error("Error stopping agent runtime manager: %s", e)
        # Step 5: cancel the permission expiry task.
        if self._expiry_task:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Error stopping permission expiry task: %s", e)
            finally:
                self._expiry_task = None
        logger.info("GatewayService shut down")

    # Control socket has been extracted to gateway.control.ControlServer.
    # Backend + broker lifecycle has been extracted to AgentRuntimeManager.


# ── Module-level helpers ───────────────────────────────────────────────────────


def _write_startup_signal(fd: int, errors: list[str], *, fatal: bool = False) -> None:
    """Write startup result to the daemon handshake pipe and close it.

    Protocol:
      - Zero or more ``error:<message>\\n`` lines for startup failures.
      - A final ``ok\\n`` line IFF startup completed (possibly degraded).

    When ``fatal=True`` the ``ok`` line is intentionally omitted so the
    parent process sees no ``ok`` and correctly reports failure + exits 1.
    Emitting ``ok`` after a fatal error would cause the parent to report
    "degraded startup" even though the daemon has already crashed.

    The parent process reads until EOF, then checks for error lines and the
    presence of the ``ok`` marker.
    """
    try:
        # Sanitize error messages: newlines would split a single message into
        # multiple protocol lines, confusing the parent's line-by-line parser.
        sanitized = [e.replace("\n", " ").replace("\r", " ") for e in errors]
        payload = "".join(f"error:{e}\n" for e in sanitized)
        if not fatal:
            payload += "ok\n"
        os.write(fd, payload.encode())
    except OSError as exc:
        # Log but do not re-raise — the finally block closes the fd, which
        # sends EOF to the parent so it can unblock.  The parent will see no
        # 'ok' line and report failure, which is the right outcome when we
        # cannot write the startup signal.
        import logging as _logging
        _logging.getLogger("agent-chat-gateway.service").warning(
            "Failed to write startup signal to handshake pipe (fd=%d): %s", fd, exc
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
