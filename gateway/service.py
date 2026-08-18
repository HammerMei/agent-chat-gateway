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

from .agents import AgentBackend, GatewayBrokerConfig, check_backend_signatures
from .agents.claude import ClaudeBackend
from .agents.opencode import OpenCodeBackend
from .config import AgentConfig, GatewayConfig
from .connectors import connector_factory
from .control import ControlServer
from .core.bot_identity import (
    ConnectorIdentity,
    DmClaim,
    DuplicateBotIdentityError,
    dm_claims,
    find_identity_conflicts,
    fold_record_dm_claims,
)
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
from .core.state import check_session_uniqueness, check_state_formats, load_state

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
            guest_allowed_tools=agent_cfg.effective_guest_allowed_tools(),
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
        # Preflight the persisted state BEFORE building anything. A state file this
        # build cannot read holds real sessions, and every path that would notice it
        # later is per-connector — so a file belonging to a connector no longer in
        # config.yaml would never be opened, and the daemon would start successfully
        # while abandoning every session in it. Raising here is the whole point of the
        # version marker: an unreadable file must stop the boot, not be discovered as
        # an absence. See gateway/core/state.py.
        check_state_formats()
        # Before anything is built: a state file binding one session to two rooms is a
        # cross-room leak waiting for both watchers to start (§4.1). The runtime check
        # in `bind_session` catches it too, but only once one of them is already
        # answering, and which one wins would depend on start order.
        check_session_uniqueness()

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
        # Fail here rather than at the first watcher start: a backend implementing the
        # pre-rename signature would otherwise raise a bare TypeError deep in the
        # lifecycle, which rolls the startup back and says nothing about what to change.
        check_backend_signatures(agents)

        self._runtime_manager = AgentRuntimeManager(agents)

        # What each connector claims of its account's direct messages — a rule opting
        # in takes the whole stream, a static `@someone` watcher takes one channel.
        # Read once here rather than holding the whole config: the identity barrier
        # needs only this, and config is immutable after load. A DM has no team, so it
        # is the one thing the Mattermost different-teams exception cannot keep apart
        # (§4.5).
        self._dm_claims: dict[str, DmClaim] = dm_claims(config.watcher_rules)
        self._entries: list[ConnectorEntry] = []
        for cc in config.connectors:
            connector = connector_factory(cc)
            sm = SessionManager(
                connector=connector,
                agents=agents,
                default_agent=config.default_agent,
                config=core_config,
                state_name=cc.name,
                permission_registry=self._registry,
                session_maps=self._maps,
                # Rules give the manager runtime effect (§2.8). Filtered like the
                # watcher configs: a rule binds to one connector by name, and the
                # manager keys its matches on that same name.
                watcher_rules=[
                    r for r in config.watcher_rules if r.connector == cc.name
                ],
                # The expiry exemption's oracle (§2.5): a room with a pending
                # scheduled job must not have its record deleted — the job's
                # injection can wake an idle room, never a reclaimed one. The
                # closure binds this connector's name (jobs are unique per
                # connector, not globally) and reads the store lazily — it is
                # loaded later in startup, and the sweep's first pass is an
                # hour away. Fails EXEMPT: if the store cannot answer, keeping
                # a record one more pass beats deleting one a job points at.
                pending_jobs=(
                    lambda name, _cn=cc.name: self._has_pending_jobs(_cn, name)
                ),
                # The membership-remove handler's job cancellation (§2.7):
                # same closure shape and same key as the oracle above.
                cancel_jobs=(
                    lambda name, _cn=cc.name: self._cancel_jobs_for(_cn, name)
                ),
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
        )

    def _has_pending_jobs(self, connector_name: str, watcher_name: str) -> bool:
        """Whether any non-completed scheduled job targets this watcher.

        The expiry exemption's oracle (§2.5). ACTIVE **and PAUSED** jobs both
        exempt: deleting a record under a paused job orphans it the moment an
        operator resumes it. Jobs key by watcher name and connector — names
        are unique only per connector, so both halves matter.

        Fails EXEMPT: a store that cannot answer (not yet loaded, corrupt
        file) keeps the record one more pass, which costs a state-file entry;
        answering False would delete a record a job points at, permanently.
        """
        try:
            return any(
                j.watcher == watcher_name
                for j in self._job_store.list_jobs(connector=connector_name)
            )
        except Exception as e:
            logger.warning(
                "Could not read the job store for the expiry exemption "
                "(keeping watcher '%s' one more pass): %s", watcher_name, e,
            )
            return True

    def _cancel_jobs_for(self, connector_name: str, watcher_name: str) -> None:
        """Cancel every scheduled job targeting a reclaimed watcher (§2.7).

        Fired by the membership-remove handler after `reclaim_room` succeeds:
        the room can never receive another message, so a job left in the store
        would fire forever at nothing. Each cancellation is logged as an audit
        line with the reason — a job disappearing from `schedule list` must be
        explainable. Best-effort like the oracle: a store that cannot answer
        leaves the jobs, and each fires audibly against the missing room
        rather than being silently lost here.
        """
        try:
            doomed = [
                j for j in self._job_store.list_jobs(connector=connector_name)
                if j.watcher == watcher_name
            ]
            for job in doomed:
                self._job_store.remove(job.id)
                logger.warning(
                    "AUDIT: cancelled scheduled job %s (watcher '%s', "
                    "connector '%s') — the bot was removed from the room, so "
                    "the job could never deliver", job.id, watcher_name,
                    connector_name,
                )
        except Exception as e:
            logger.warning(
                "Could not cancel scheduled jobs for reclaimed watcher '%s': %s",
                watcher_name, e,
            )

    async def _settle(
        self,
        coros: list,
        *,
        phase: str,
        startup_errors: list[str],
    ) -> None:
        """Await every coroutine, then raise the first failure — never before.

        `return_exceptions=True` is required, and the reason is a race this code was
        bitten by: without it the FIRST failure (a bad URL failing DNS almost instantly)
        propagates immediately WITHOUT cancelling the still-in-flight calls for the
        other connectors (a real login plus handshake is much slower). Those keep
        running as orphaned tasks, while the caller's `except Exception` routes into
        `shutdown()` -> `session_manager.shutdown()` -> `save_state()` for EVERY entry —
        including one whose startup had not finished populating its watcher states,
        overwriting that connector's state file with a partial dict and wiping real
        session ids for a connector that was never part of the failure.

        Awaiting every result before deciding closes that race. Extracted because
        startup now settles twice, and two copies of this reasoning would be one edit
        away from one of them losing it.
        """
        results = await asyncio.gather(*coros, return_exceptions=True)
        first_exception: BaseException | None = None
        for entry, result in zip(self._entries, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "SessionManager for connector '%s' failed to %s during startup: %s",
                    entry.name,
                    phase,
                    result,
                )
                startup_errors.append(
                    f"Connector '{entry.name}' failed to {phase}: {result}"
                )
                if first_exception is None:
                    first_exception = result
            elif isinstance(result, list):
                startup_errors.extend(result)
        if first_exception is not None:
            raise first_exception

    def _check_bot_identities(self) -> None:
        """Refuse to go further if two connectors are one bot account (§4.5).

        Runs between authentication and subscription: earlier there is no identity to
        read, later the damage is already possible. A connector that cannot answer
        raises `ConnectorIdentityError` out of here, which is the fail-closed half.

        Rejects the whole startup rather than disabling the offending connector: which
        one is "offending" depends only on config order, and this project's other
        preflights (`check_state_formats`, `check_backend_signatures`) refuse loudly
        rather than degrade quietly — a daemon that silently drops a connector looks
        healthy while half its rooms go unanswered.
        """
        identities: list[ConnectorIdentity] = []
        for e in self._entries:
            identity = e.connector.bot_identity()
            if identity is None:
                continue  # declares no shared account to collide over
            claim = self._dm_claims.get(e.name, DmClaim())
            # Widened by the connector's persisted DM records (Codex round
            # 6): sticky binding keeps a record answering its room after its
            # rule is deleted, so a rule-only claim misses exactly the
            # records that outlive their rules — and two connectors sharing
            # an account would both answer one private conversation. Read
            # best-effort: an unreadable file was already refused loudly by
            # check_state_formats, so nothing here needs a second refusal.
            try:
                claim = fold_record_dm_claims(claim, load_state(e.name))
            except Exception:
                logger.debug(
                    "Could not fold persisted DM claims for connector '%s' — "
                    "using the rule-derived claim alone", e.name, exc_info=True,
                )
            identities.append(
                ConnectorIdentity(
                    connector_name=e.name,
                    identity=identity,
                    dms=claim,
                )
            )
        conflicts = find_identity_conflicts(identities)
        if conflicts:
            raise DuplicateBotIdentityError("\n".join(conflicts))

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
            #
            # return_exceptions=True is required here — without it, the FIRST
            # SessionManager.run_once() to raise (e.g. a newly added connector
            # with a bad URL failing DNS/connect almost instantly) makes
            # gather() propagate immediately WITHOUT cancelling the other,
            # still-in-flight run_once() calls (a real RC/Mattermost login +
            # DDP handshake is much slower). Those keep running as orphaned
            # background tasks. The `except Exception` below used to route
            # straight into `shutdown()` -> `session_manager.shutdown()` ->
            # `save_state()` for EVERY entry, including the one whose
            # run_once() hadn't finished populating its watcher states yet —
            # unconditionally overwriting that connector's state.<name>.json
            # with an empty/partial dict and silently wiping out real
            # session IDs for a connector that was never actually part of
            # the failure. Awaiting every result here (success or exception)
            # before deciding what to do next closes that race: by the time
            # any exception is re-raised, no run_once() call is still
            # in-flight, so shutdown() can no longer race one.
            #
            # The two phases are separated by an identity barrier rather than merged:
            # two connectors on one bot account must be refused before EITHER
            # subscribes (§4.5), and a SessionManager owns exactly one connector, so
            # only this loop can see the collision. Fanning `run_once()` out would let
            # connector A finish subscribing while B was still logging in, and the
            # check would then run after the duplicate had started answering.
            await self._settle(
                [e.session_manager.connect_only() for e in self._entries],
                phase="connect",
                startup_errors=startup_errors,
            )
            self._check_bot_identities()
            await self._settle(
                [
                    e.session_manager.sync_only(
                        unavailable_agents=self._runtime_manager.unavailable_agents,
                    )
                    for e in self._entries
                ],
                phase="start",
                startup_errors=startup_errors,
            )

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


def sanitize_pipe_message(message: str) -> str:
    """Strip embedded newlines from a message before writing it into the
    daemon startup handshake pipe's line-oriented `info:`/`error:`/`ok`
    protocol — an embedded newline would split one message into multiple,
    unparseable protocol lines. Shared by `_write_startup_signal()` below
    AND `gateway/daemon.py`'s own pipe writes (lock-acquire/config-
    migration/config-load/service-crash failures) — code-review finding:
    those two files used to each keep an independent inline copy of this
    exact sanitization, applied inconsistently (only 2 of daemon.py's 5
    write sites), so this is now the one place it's defined."""
    return message.replace("\n", " ").replace("\r", " ")


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
        sanitized = [sanitize_pipe_message(e) for e in errors]
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
