"""OpenCode HTTP agent backend.

Implements AgentBackend using the opencode HTTP server (``opencode serve``):
  - Session creation:  POST /session  {"directory": "..."}
                       then POST /session/{id}/message  (init prompt)
  - Message sending:   POST /session/{id}/message  {"parts": [{"type": "text", ...}]}

Both calls are **synchronous** — the server blocks until the full agent turn
completes (including all tool calls and permission approvals) before returning.
This is the key advantage over the old ``opencode run`` subprocess approach, which
returned early when a tool was blocked by a pending permission request.

File attachments are not natively supported by the HTTP API.  They are injected
into the prompt text via :func:`~gateway.core.adapter_utils.build_attachment_prompt`
so the agent can access them using the Read tool — the same fallback used by the
Claude CLI backend.

The ``env`` parameter of :meth:`send` is a no-op in HTTP mode.  ``ACG_ROLE``
is set on the ``opencode serve`` process at startup via ``sidecar_env``,
hardcoded to ``"owner"`` in ``GatewayService._build_agent_backend()`` because
the sidecar always runs as the gateway's own backend process.  Per-message
guest enforcement (tool allow-lists, permission prompts) is handled by
:class:`~gateway.core.permission.PermissionBroker`, not by environment variables.

Lifecycle
---------
Call :meth:`start` before :meth:`create_session`.  ``start`` spawns
``opencode serve``, allocates a free port, and waits for the health check to
pass.  :meth:`stop` terminates the process.  Both methods are idempotent.

When used via :class:`~gateway.agents.session.AgentSession`, ``start``/``stop``
are called automatically by the context manager — no manual calls needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import httpx

from ...core.adapter_utils import build_attachment_prompt
from .. import AgentBackend, GatewayBrokerConfig
from ..errors import (
    AgentExecutionError,
    AgentPermissionError,
    AgentRateLimitedError,
    AgentUnavailableError,
)
from ..response import AgentEvent, AgentResponse, TokenUsage

if TYPE_CHECKING:
    from ...core.permission import (
        PermissionBroker,
        PermissionNotifier,
        PermissionRegistry,
    )

logger = logging.getLogger("agent-chat-gateway.agents.opencode")

# Sentinel string placed in the SSE queue by _collect_sse() once the HTTP
# streaming connection is established.  Using a module-level constant (rather
# than a bare string literal) makes isinstance checks unnecessary and guards
# against accidental collision with real SSE line content.
_SSE_READY = "__opencode_sse_ready__"
# Maximum seconds to block on queue.get() — keeps the deadline check responsive.
_SSE_QUEUE_POLL_INTERVAL = 30.0
# Maximum seconds to wait for the SSE connection to be established before
# giving up with AgentUnavailableError.  Always capped to the caller's deadline.
_SSE_CONNECT_TIMEOUT = 15.0

_INIT_PROMPT = (
    "Chat session initialized. "
    "You are a chat assistant standing by to respond to incoming messages. "
    "Do not take any proactive action — simply wait for the first user message."
)

_HEALTH_CHECK_PATH = "/session"
_STARTUP_TIMEOUT = 30  # seconds
# Circuit-breaker threshold: after this many consecutive failed auto-restarts,
# _ensure_live_runtime() fast-fails instead of blocking callers for ~30s each.
_MAX_RESTART_FAILURES = 3


def _classify_http_error(status_code: int, message: str) -> AgentExecutionError:
    """Map opencode HTTP failures to structured backend exceptions."""
    if status_code == 429:
        return AgentRateLimitedError(message)
    if status_code in (401, 403):
        return AgentPermissionError(message)
    if status_code in (502, 503, 504):
        return AgentUnavailableError(message)
    return AgentExecutionError(message)


def _find_free_port() -> int:
    """Bind to port 0 to let the OS allocate a free ephemeral port, then release it.

    There is a brief TOCTOU window between release and the sidecar binding, but
    this is acceptable for local loopback use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpenCodeBackend(AgentBackend):
    """Agent backend that communicates with a running ``opencode serve`` process."""

    def __init__(
        self,
        command: str,
        new_session_args: list[str],
        timeout: int,
        sidecar_env: dict[str, str] | None = None,
        sidecar_cwd: str | None = None,
        broker_config: GatewayBrokerConfig | None = None,
    ) -> None:
        """
        Args:
            command: opencode binary name (e.g. ``"opencode"``). Used to build
                the startup command: ``[command, "serve", "--port", "{port}"]
                + new_session_args``.
            new_session_args: Extra flags forwarded to ``opencode serve`` at startup
                (e.g. ``["--model", "anthropic/claude-sonnet-4-5"]``). Unlike the
                Claude backend, these are **server startup flags**, not per-message args.
            timeout: Default HTTP timeout in seconds for all API calls.
            sidecar_env: Environment variables to inject into the sidecar process.
                Hardcoded to ``{"ACG_ROLE": "owner"}`` by GatewayService because
                the sidecar always runs as the gateway's own agent backend.
                Guest enforcement is handled by the PermissionBroker at the
                per-request level, not via process environment.
            sidecar_cwd: Working directory for the ``opencode serve`` process.
                ``None`` inherits the gateway's cwd. Set this to the project root
                so opencode can find ``.opencode/opencode.json`` and plugins.
            broker_config: Optional permission policy settings for gateway broker
                creation.  ``None`` means permissions are disabled for this agent.
        """
        self._command = command
        self._new_session_args = new_session_args
        self.timeout = timeout
        self._sidecar_env: dict[str, str] = sidecar_env or {}
        self._sidecar_cwd: str | None = sidecar_cwd
        self._broker_config = broker_config
        self._base_url: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_drain: asyncio.Task | None = None
        self._stderr_drain: asyncio.Task | None = None
        # Long-lived HTTP client — reused across health checks, session creation,
        # and message sending to avoid repeated connection setup/teardown overhead.
        self._client: httpx.AsyncClient | None = None
        self._orphan_session_ids: set[str] = set()
        # Serializes concurrent restart attempts so two simultaneous send() calls
        # that both detect a dead sidecar cannot race through _ensure_live_runtime()
        # and spawn duplicate processes.
        self._restart_lock: asyncio.Lock = asyncio.Lock()
        # Tracks whether start() has ever succeeded.  Used by _ensure_live_runtime()
        # to distinguish "never started (caller error)" from "started, then died/failed
        # restart (auto-recovery eligible)".  Cleared on explicit stop() so that a
        # manually stopped backend requires a fresh start() call.
        self._ever_started: bool = False
        # Circuit-breaker for repeated failed auto-restarts.
        # After _MAX_RESTART_FAILURES consecutive failures, _ensure_live_runtime()
        # raises immediately (fast-fail) instead of blocking callers for ~30s each
        # time.  Reset to 0 on a successful restart or explicit stop().
        self._consecutive_restart_failures: int = 0

    @property
    def supports_per_message_env(self) -> bool:
        """OpenCode HTTP mode ignores per-message env — role is set at sidecar startup."""
        return False

    def create_gateway_broker(
        self,
        registry: "PermissionRegistry",
        notifier: "PermissionNotifier",
        session_room_map: dict[str, str],
        session_role_map: dict[str, str],
        session_permission_thread_map: "dict[str, str | None]",
    ) -> "PermissionBroker | None":
        """Return an OpenCodePermissionBroker wired to the shared notification channel.

        Requires ``start()`` to have been called first so ``_base_url`` is set.
        ``AgentRuntimeManager.start_all()`` ensures this ordering internally.
        """
        if self._broker_config is None:
            return None
        if not self._base_url:
            raise RuntimeError(
                "OpenCodeBackend has no base_url — call start() before create_gateway_broker()"
            )
        from .broker import OpenCodePermissionBroker

        return OpenCodePermissionBroker(
            registry=registry,
            notifier=notifier,
            opencode_base_url=self._base_url,
            session_room_map=session_room_map,
            session_role_map=session_role_map,
            session_permission_thread_map=session_permission_thread_map,
            owner_allowed_tools=self._broker_config.owner_allowed_tools,
            guest_allowed_tools=self._broker_config.guest_allowed_tools,
            timeout_seconds=self._broker_config.timeout,
            skip_owner_approval=self._broker_config.skip_owner_approval,
        )

    def create_callable_broker(self, handler, timeout_seconds: int):
        """Return an OpenCodeCallablePermissionBroker for SSE-based permission callbacks.

        Requires the backend to be started (``start()`` must have been called so
        ``_base_url`` is populated) before the broker's SSE listener can connect.
        AgentSession ensures this ordering: ``start()`` is called in ``__aenter__``
        before the broker is created.
        """
        if not self._base_url:
            raise RuntimeError("OpenCodeBackend has no base_url — call start() first")
        from .callable_broker import OpenCodeCallablePermissionBroker

        return OpenCodeCallablePermissionBroker(
            base_url=self._base_url,
            permission_handler=handler,
            timeout_seconds=timeout_seconds,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def _start_inner(self) -> None:
        """Internal startup logic — **must be called with** ``_restart_lock`` **held**.

        Extracted from :meth:`start` so that :meth:`_ensure_live_runtime` can
        restart the sidecar without re-acquiring the lock, which would deadlock
        because ``asyncio.Lock`` is not re-entrant and ``_ensure_live_runtime``
        already holds ``_restart_lock`` when it calls this method.

        Raises:
            RuntimeError: If the process fails to become healthy within
                ``_STARTUP_TIMEOUT`` seconds.
        """
        if self._base_url:
            return  # another caller already finished start() — double-check guard

        port = _find_free_port()
        base_url = f"http://127.0.0.1:{port}"
        cmd = [self._command, "serve", "--port", str(port)] + self._new_session_args
        env = {**os.environ, **self._sidecar_env}

        logger.info(
            "Starting opencode serve: %s (port=%d, cwd=%s)",
            cmd[0],
            port,
            self._sidecar_cwd or "<inherited>",
        )
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            cwd=self._sidecar_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._stdout_drain = asyncio.create_task(
            self._drain_pipe(self._process.stdout, logging.DEBUG, "[opencode stdout]"),
            name="opencode-stdout-drain",
        )
        self._stderr_drain = asyncio.create_task(
            self._drain_pipe(
                self._process.stderr, logging.WARNING, "[opencode stderr]"
            ),
            name="opencode-stderr-drain",
        )

        try:
            await self._wait_for_health(base_url)
        except BaseException:
            # Health check failed — clean up the partially started sidecar so
            # we don't leak processes or background tasks.  After cleanup the
            # backend is in a clean retryable state (same as "never started").
            #
            # BaseException (not Exception) is caught here so that
            # asyncio.CancelledError — raised when the gateway is shutting down
            # while the health poll is in progress — also triggers cleanup.
            # Without this, a SIGTERM during startup leaves the opencode serve
            # subprocess running indefinitely as an orphan.
            await self._cleanup_partial_start()
            raise

        self._base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=self.timeout)
        self._ever_started = True
        logger.info("OpenCode server ready at %s", self._base_url)

    async def start(self) -> None:
        """Spawn ``opencode serve``, allocate a port, and wait for the health check.

        Idempotent — returns immediately if the server is already running.

        Raises:
            RuntimeError: If the process fails to become healthy within
                ``_STARTUP_TIMEOUT`` seconds.
        """
        if self._base_url:
            return  # fast path — already running, no lock needed

        # Double-checked locking: a second concurrent caller must not spawn a
        # second ``opencode serve`` process.  The fast-path check above is
        # intentionally outside the lock for performance; the re-check inside
        # ``_start_inner`` is the authoritative guard.  ``_restart_lock`` is also
        # held by ``_ensure_live_runtime()`` and ``stop()``, so all lifecycle
        # operations are mutually exclusive.
        async with self._restart_lock:
            await self._start_inner()

    async def _cleanup_partial_start(self) -> None:
        """Clean up after a failed start() — kill process, cancel drain tasks, reset state."""
        for task in (self._stdout_drain, self._stderr_drain):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._stdout_drain = None
        self._stderr_drain = None

        if self._process:
            try:
                self._process.kill()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                pass
            self._process = None

        self._base_url = None
        logger.warning("Cleaned up partially started OpenCode sidecar")

    async def _drain_pipe(
        self, stream: asyncio.StreamReader, level: int, prefix: str
    ) -> None:
        """Read lines from a subprocess pipe and forward them to the logger."""
        try:
            async for line in stream:
                logger.log(
                    level, "%s %s", prefix, line.decode(errors="replace").rstrip()
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Pipe drain stopped: %s", e)

    async def stop(self) -> None:
        """Terminate the ``opencode serve`` process.

        Idempotent — returns immediately if already stopped.

        Holds ``_restart_lock`` for the entire shutdown so that a concurrent
        ``_ensure_live_runtime()`` cannot race to restart the sidecar while it
        is being torn down.  Without the lock, the following sequence is possible:

          1. ``_ensure_live_runtime()`` checks ``_base_url is not None`` → live
          2. ``stop()`` sets ``_base_url = None`` and closes the client
          3. ``_ensure_live_runtime()`` uses the now-closed client → crash
        """
        if self._process is None:
            return  # fast path — no lock needed for this trivial check
        async with self._restart_lock:
            if self._process is None:
                return  # double-check: another stop() already finished
            logger.info("Stopping opencode serve (pid=%d)", self._process.pid)
            try:
                # Wrap with a short total timeout so a crashed sidecar (where
                # every DELETE request blocks for the full client timeout) cannot
                # stall gateway shutdown for N × self.timeout seconds.  Orphan
                # cleanup is best-effort — it is acceptable to skip it when the
                # sidecar is unreachable during shutdown.
                await asyncio.wait_for(
                    self._cleanup_orphan_sessions_best_effort(),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Orphan session cleanup timed out after 10s during shutdown "
                    "(%d session(s) may remain on the opencode server)",
                    len(self._orphan_session_ids),
                )
            except Exception as e:
                logger.warning("Failed orphan session cleanup before shutdown: %s", e)
            for task in (self._stdout_drain, self._stderr_drain):
                if task:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            self._stdout_drain = None
            self._stderr_drain = None
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("opencode serve did not exit after 5s — killing")
                    self._process.kill()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.error(
                            "opencode serve did not exit after SIGKILL — "
                            "process may be stuck in uninterruptible kernel wait"
                        )
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error("Error stopping opencode serve: %s", e)
            finally:
                if self._client:
                    await self._client.aclose()
                    self._client = None
                self._base_url = None
                self._process = None
                self._ever_started = False  # explicit stop resets — require new start() call
                self._consecutive_restart_failures = 0  # clear circuit-breaker on explicit stop

    async def _invalidate_dead_runtime(self) -> None:
        """Reset client/runtime state after detecting a dead sidecar process."""
        for task in (self._stdout_drain, self._stderr_drain):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._stdout_drain = None
        self._stderr_drain = None
        if self._client:
            await self._client.aclose()
            self._client = None
        self._base_url = None
        self._process = None

    async def _ensure_live_runtime(self) -> None:
        """Restart the sidecar on demand if a previously started process died.

        Serialized by ``_restart_lock`` to prevent concurrent ``send()`` calls
        from both detecting a dead sidecar and spawning duplicate processes.
        Without the lock, two concurrent coroutines could both pass the
        ``returncode is not None`` check before either completes the restart,
        resulting in two simultaneous ``opencode serve`` processes and two
        conflicting ``_base_url`` assignments.

        NOTE: ``_require_base_url()`` is intentionally called AFTER the restart
        block, not before.  If the previous ``start()`` call failed,
        ``_base_url`` is None.  Calling ``_require_base_url()`` first would
        raise before the restart logic can run, making auto-recovery impossible
        after a failed restart attempt.

        The outer guard covers two cases that both require a restart attempt:
          1. The sidecar process is present but has exited (returncode is not None).
          2. start() was previously called (``_ever_started`` is True) but the
             base_url is gone — this happens when a restart attempt failed and
             ``_cleanup_partial_start()`` cleared both ``_process`` and
             ``_base_url``.  Without this second condition, the backend would be
             permanently stuck after a failed restart.
        """
        needs_restart = (
            (self._process is not None and self._process.returncode is not None)
            or (self._ever_started and self._base_url is None)
        )
        if needs_restart:
            # Circuit-breaker: if consecutive restart attempts have all failed,
            # fast-fail immediately instead of letting each incoming request
            # block for the full _STARTUP_TIMEOUT (~30s).  Cleared on a
            # successful restart or explicit stop().
            if self._consecutive_restart_failures >= _MAX_RESTART_FAILURES:
                raise AgentUnavailableError(
                    f"opencode sidecar is unavailable after "
                    f"{self._consecutive_restart_failures} consecutive failed restart "
                    "attempts — call stop() then start() to reset"
                )
            async with self._restart_lock:
                # Re-check inside the lock: a concurrent coroutine may have
                # already completed the restart by the time we acquire it.
                still_needs_restart = (
                    (self._process is not None and self._process.returncode is not None)
                    or (self._ever_started and self._base_url is None)
                )
                if still_needs_restart:
                    # Re-check circuit-breaker inside lock too (another coroutine
                    # may have incremented the counter while we were waiting).
                    if self._consecutive_restart_failures >= _MAX_RESTART_FAILURES:
                        raise AgentUnavailableError(
                            f"opencode sidecar is unavailable after "
                            f"{self._consecutive_restart_failures} consecutive failed restart "
                            "attempts — call stop() then start() to reset"
                        )
                    exit_code = self._process.returncode if self._process else None
                    logger.warning(
                        "Detected dead/unrecovered opencode sidecar (exit=%s) — restarting before request",
                        exit_code,
                    )
                    await self._invalidate_dead_runtime()
                    try:
                        # Call _start_inner() directly — NOT start() — because
                        # _restart_lock is already held here and asyncio.Lock is
                        # not re-entrant.  Calling start() would deadlock waiting
                        # to acquire the lock it already owns.
                        await self._start_inner()
                        self._consecutive_restart_failures = 0
                    except Exception:
                        self._consecutive_restart_failures += 1
                        logger.error(
                            "opencode sidecar restart failed (attempt %d/%d)",
                            self._consecutive_restart_failures,
                            _MAX_RESTART_FAILURES,
                        )
                        raise
        # Shutdown race guard: a concurrent stop() call may have acquired and
        # released _restart_lock between when we evaluated `needs_restart`
        # (outside the lock) and when we entered the lock body.  In that case
        # `still_needs_restart` evaluated to False (because stop() cleared
        # _ever_started), we skipped the restart block, and now _base_url is
        # still None — not because of a programming error, but because the
        # sidecar was explicitly stopped.  Raise AgentUnavailableError (a
        # recoverable operational condition) instead of the confusing
        # RuntimeError("call start() before ...") from _require_base_url().
        if not self._ever_started and self._base_url is None:
            raise AgentUnavailableError(
                "opencode sidecar has been stopped — call start() to restart"
            )
        self._require_base_url()

    async def _wait_for_health(self, base_url: str) -> None:
        """Poll the health-check endpoint until it responds or the deadline passes."""
        health_url = f"{base_url}{_HEALTH_CHECK_PATH}"
        deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT
        last_exc: Exception | None = None

        async with httpx.AsyncClient(timeout=2.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                # Check if process died before becoming healthy
                if self._process and self._process.returncode is not None:
                    raise RuntimeError(
                        f"opencode serve exited with code {self._process.returncode} "
                        "before becoming healthy"
                    )
                try:
                    resp = await client.get(health_url)
                    # Only 200 is accepted as proof the sidecar is fully ready.
                    # 404/401/405 mean the endpoint exists but the service is not
                    # in a known-good state; treat anything other than 200 as not ready.
                    if resp.status_code == 200:
                        return
                except Exception as exc:
                    last_exc = exc
                await asyncio.sleep(0.5)

        raise RuntimeError(
            f"opencode serve did not become healthy within {_STARTUP_TIMEOUT}s "
            f"(last error: {last_exc})"
        )

    # ── AgentBackend interface ─────────────────────────────────────────────────

    async def create_session(
        self,
        working_directory: str,
        extra_args: list[str] | None = None,
        session_title: str | None = None,
    ) -> str:
        """Create a new opencode session and return the session_id.

        Args:
            working_directory: The working directory for the agent (passed to
                POST /session as ``directory``).
            extra_args: Ignored in HTTP mode (no per-message subprocess to pass
                args to). Logged at DEBUG if provided.
            session_title: Optional session title. Passed as ``title`` in the
                POST /session body — verify field name against live API.
        """
        if extra_args:
            logger.debug(
                "extra_args ignored by HTTP adapter (set on server at startup): %s",
                extra_args,
            )

        await self._ensure_live_runtime()
        url = f"{self._base_url}/session"
        body: dict = {"directory": working_directory}
        if session_title:
            # ⚠️ Verify field name against live POST /session response — "title" assumed.
            body["title"] = session_title

        logger.info("Creating opencode session via HTTP (cwd=%s)", working_directory)
        try:
            resp = await self._get_client().post(url, json=body)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = (
                f"opencode API returned HTTP {exc.response.status_code} "
                f"for POST /session"
            )
            raise _classify_http_error(exc.response.status_code, message) from None
        data = resp.json()

        session_id = data.get("id", "")
        if not session_id:
            raise RuntimeError(f"opencode POST /session returned no session id: {data}")

        # Send init prompt to prime the session (same as old CLI adapter).
        try:
            await self._post_message(session_id, _INIT_PROMPT)
        except Exception:
            cleaned = await self._cleanup_session_best_effort(session_id)
            if not cleaned:
                self._orphan_session_ids.add(session_id)
                logger.warning(
                    "OpenCode session %s could not be cleaned up after init failure; marked orphan",
                    session_id[:16],
                )
            raise

        logger.info("Created opencode session: %s", session_id[:16])
        return session_id

    async def send(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResponse:
        """Send a message to an existing opencode session and return a normalized AgentResponse.

        File attachments are injected into the prompt text via build_attachment_prompt
        (no native HTTP upload equivalent to the CLI's ``-f`` flag).

        The ``env`` kwarg is a no-op: ACG_ROLE and other role vars must be set on the
        opencode server process at startup, not per-message.
        """
        if attachments:
            logger.info(
                "Injecting %d attachment(s) into prompt text (no native HTTP upload): %s",
                len(attachments),
                attachments,
            )
        prompt = build_attachment_prompt(prompt, attachments, working_directory)

        if env:
            logger.debug(
                "env kwarg ignored by HTTP adapter (set on server at startup): %s",
                list(env.keys()),
            )

        await self._ensure_live_runtime()
        raw = await self._post_message(session_id, prompt, timeout=timeout)
        return self._parse_http_response(raw, session_id)

    async def stream(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Stream intermediate agent events for an OpenCode session turn.

        Submits the prompt via ``POST /session/{id}/prompt_async`` (returns
        immediately), then consumes the ``GET /event`` SSE stream, yielding
        :class:`~gateway.agents.response.AgentEvent` objects as content arrives.

        The SSE connection is established **before** the prompt is posted to
        eliminate the race condition where a very fast turn would complete and
        emit ``session.status idle`` before we started listening.

        Yields:
            ``AgentEvent(kind="tool_call")``   — when a tool transitions to
                ``running`` state.
            ``AgentEvent(kind="tool_result")`` — when a tool reaches
                ``completed`` or ``error`` state.
            ``AgentEvent(kind="thinking")``    — on the first non-empty snapshot
                of a ``reasoning`` part.
            ``AgentEvent(kind="final")``       — with the completed
                :class:`~gateway.agents.response.AgentResponse` when the
                session status becomes ``idle``.

        Raises:
            asyncio.TimeoutError: If the turn doesn't complete within ``timeout``
                seconds.
            AgentExecutionError: On ``session.error`` events or SSE parse failures.
            AgentUnavailableError: If the SSE connection cannot be established.
        """
        if attachments:
            logger.info(
                "Injecting %d attachment(s) into prompt text (no native HTTP upload): %s",
                len(attachments),
                attachments,
            )
        prompt = build_attachment_prompt(prompt, attachments, working_directory)

        if env:
            logger.debug(
                "env kwarg ignored by HTTP adapter (set on server at startup): %s",
                list(env.keys()),
            )

        await self._ensure_live_runtime()

        deadline = asyncio.get_running_loop().time() + timeout

        # ── Phase 1: open SSE connection BEFORE posting the prompt ────────────
        # Events are not replayed on reconnect — we must be listening before
        # the prompt is submitted to guarantee we see session.status idle.
        # A background task drains the SSE stream into a queue; the main
        # coroutine reads from the queue so it can also enforce the deadline.
        queue: asyncio.Queue[str | Exception] = asyncio.Queue()

        async def _collect_sse() -> None:
            url = f"{self._base_url}/event"
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
                ) as sse_client:
                    async with sse_client.stream("GET", url) as response:
                        response.raise_for_status()
                        await queue.put(_SSE_READY)
                        async for line in response.aiter_lines():
                            await queue.put(line)
                        # Natural stream close before session.status idle —
                        # notify _parse_sse_events immediately so it doesn't
                        # stall until the next poll-interval timeout fires.
                        await queue.put(
                            EOFError("OpenCode SSE stream closed before session.status idle")
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(exc)

        sse_task = asyncio.create_task(_collect_sse(), name="opencode-stream-sse")
        try:
            # Wait for SSE to connect — capped at _SSE_CONNECT_TIMEOUT but
            # never exceeds the caller's own deadline so short timeouts don't
            # block extra long.
            sse_connect_timeout = min(
                _SSE_CONNECT_TIMEOUT,
                max(0.1, deadline - asyncio.get_running_loop().time()),
            )
            try:
                ready = await asyncio.wait_for(queue.get(), timeout=sse_connect_timeout)
            except asyncio.TimeoutError:
                raise AgentUnavailableError(
                    "OpenCode SSE stream did not connect within "
                    f"{sse_connect_timeout:.2g}s"
                )
            if isinstance(ready, Exception):
                raise AgentUnavailableError(
                    f"OpenCode SSE connection failed: {ready}"
                ) from ready
            assert ready == _SSE_READY, f"Unexpected first SSE queue item: {ready!r}"

            # ── Phase 2: post the prompt (SSE is now listening) ───────────────
            await self._post_message_async(session_id, prompt, timeout=timeout)

            # ── Phase 3: consume SSE events and yield AgentEvents ─────────────
            async for event in self._parse_sse_events(
                session_id, queue, deadline, timeout
            ):
                yield event

        finally:
            sse_task.cancel()
            await asyncio.gather(sse_task, return_exceptions=True)

    async def _post_message_async(
        self, session_id: str, text: str, *, timeout: int
    ) -> None:
        """POST a prompt to the async endpoint — returns immediately (HTTP 202).

        Unlike :meth:`_post_message` (which blocks until the turn completes),
        this method returns as soon as the server acknowledges the request.
        Progress arrives via the ``GET /event`` SSE stream.

        Args:
            session_id: OpenCode session ID.
            text:       Prompt text to send.
            timeout:    Uniform HTTP timeout in seconds passed to httpx (covers
                        connect, read, write, and pool phases).  This is NOT the
                        agent turn deadline — the server returns 202 immediately
                        after queuing the prompt.

        .. note::
            Endpoint path ``/session/{id}/prompt_async`` — verify against a
            live opencode server if the API version changes.
        """
        url = f"{self._base_url}/session/{session_id}/prompt_async"
        body = {"parts": [{"type": "text", "text": text}]}
        try:
            resp = await self._get_client().post(url, json=body, timeout=timeout)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = (
                f"opencode API returned HTTP {exc.response.status_code} "
                f"for POST /session/{session_id[:16]!r}/prompt_async"
            )
            raise _classify_http_error(exc.response.status_code, message) from None
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise AgentUnavailableError(
                f"opencode sidecar unreachable during POST /prompt_async: {exc}"
            ) from exc

    async def _parse_sse_events(
        self,
        session_id: str,
        queue: asyncio.Queue[str | Exception],
        deadline: float,
        timeout: int,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Consume SSE lines from *queue* and yield :class:`AgentEvent` objects.

        Filters to events whose ``properties.sessionID`` matches *session_id*.
        Tracks accumulated text and usage data across the turn; yields a
        ``final`` event when ``session.status`` becomes ``idle``.

        Args:
            session_id: OpenCode session ID to filter events for.
            queue:      Async queue populated by the SSE collector task.
            deadline:   ``loop.time()`` deadline; raises :exc:`asyncio.TimeoutError`
                        if exceeded.
            timeout:    Original timeout in seconds (used in error messages only).
        """
        # Accumulated state for the final AgentResponse
        part_types: dict[str, str] = {}        # partID → part type
        text_part_order: list[str] = []         # ordered text partIDs
        text_accumulator: dict[str, str] = {}   # partID → accumulated delta text
        emitted_thinking: set[str] = set()      # partIDs whose thinking event was yielded
        # De-duplicate repeated message.part.updated events for the same part:
        # OpenCode may stream incremental state updates (e.g. pending→running→
        # completed) so the same part ID can arrive more than once.
        tool_call_emitted: set[str] = set()     # partIDs for which tool_call was yielded
        tool_result_emitted: set[str] = set()   # partIDs for which tool_result was yielded
        seen_step_finish_ids: set[str] = set()  # partIDs whose tokens were accumulated
        total_cost = 0.0
        total_input = total_output = total_reasoning = 0
        total_cache_read = total_cache_write = 0
        num_turns = 0

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"OpenCode session {session_id[:16]!r} did not complete "
                    f"within {timeout}s"
                )

            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=min(remaining, _SSE_QUEUE_POLL_INTERVAL)
                )
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(
                    f"OpenCode session {session_id[:16]!r} did not complete "
                    f"within {timeout}s"
                )

            if isinstance(item, Exception):
                raise AgentUnavailableError(
                    f"OpenCode SSE stream error: {item}"
                ) from item

            line: str = item
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Malformed OpenCode SSE JSON: %r", raw[:200])
                continue

            # SSE lines may be valid JSON but non-dict (null, array, string…).
            if not isinstance(payload, dict):
                logger.debug("Unexpected non-dict OpenCode SSE payload: %r", raw[:200])
                continue

            event_type = payload.get("type", "")
            # Guard against explicit JSON null ("properties": null).
            props = payload.get("properties") or {}

            # Filter to our session only (the SSE stream is global)
            if props.get("sessionID") != session_id:
                continue

            # ── Text / reasoning streaming ─────────────────────────────────
            if event_type == "message.part.delta":
                part_id = props.get("partID", "")
                field = props.get("field", "")
                delta = props.get("delta", "")
                if not (part_id and field == "text" and isinstance(delta, str) and delta):
                    continue
                # Accumulate only confirmed text parts — skip unregistered
                # parts (part_type unknown) to avoid reasoning deltas bleeding
                # in before the corresponding message.part.updated arrives.
                if part_types.get(part_id) == "text":
                    if part_id not in text_accumulator:
                        text_part_order.append(part_id)
                    text_accumulator[part_id] = (
                        text_accumulator.get(part_id, "") + delta
                    )

            # ── Part creation / state transitions ──────────────────────────
            elif event_type == "message.part.updated":
                # Guard against explicit JSON null for "part" or "state".
                part = props.get("part") or {}
                part_id = part.get("id", "")
                part_type = part.get("type", "")
                if not part_id:
                    continue
                part_types[part_id] = part_type

                if part_type == "tool":
                    state = part.get("state") or {}
                    tool_status = state.get("status", "")
                    tool_name = part.get("tool", "")
                    if not isinstance(tool_name, str):
                        tool_name = ""
                    if tool_status == "running" and tool_name:
                        if part_id not in tool_call_emitted:
                            tool_call_emitted.add(part_id)
                            yield AgentEvent(kind="tool_call", text=f"🔧 {tool_name}")
                    elif tool_status in ("completed", "error") and tool_name:
                        if part_id not in tool_result_emitted:
                            tool_result_emitted.add(part_id)
                            yield AgentEvent(kind="tool_result", text=f"✓ {tool_name}")

                elif part_type == "reasoning":
                    # Yield a single thinking event when the reasoning text
                    # first becomes non-empty (it may be updated incrementally,
                    # but we only need one notification per reasoning block).
                    reasoning_text = part.get("text", "")
                    if reasoning_text and part_id not in emitted_thinking:
                        emitted_thinking.add(part_id)
                        yield AgentEvent(
                            kind="thinking",
                            text=f"💭 {reasoning_text[:80]}",
                        )

                elif part_type == "step-finish":
                    # Guard against double-counting: OpenCode may emit multiple
                    # message.part.updated events for the same step-finish part
                    # (e.g. pending → finalized state transitions).
                    if part_id not in seen_step_finish_ids:
                        seen_step_finish_ids.add(part_id)
                        num_turns += 1
                        # Guard against explicit JSON null for both "tokens"
                        # and "cache" sub-fields.
                        tokens = part.get("tokens") or {}
                        total_input += tokens.get("input", 0)
                        total_output += tokens.get("output", 0)
                        total_reasoning += tokens.get("reasoning", 0)
                        cache = tokens.get("cache") or {}
                        total_cache_read += cache.get("read", 0)
                        total_cache_write += cache.get("write", 0)
                        # Guard against explicit JSON null for "cost".
                        total_cost += part.get("cost") or 0.0

            # ── Turn completion ────────────────────────────────────────────
            # NOTE: we expect exactly one session.status idle event per turn.
            # If OpenCode ever emits multiple idle transitions (e.g. for
            # parallel sub-sessions), this return would cut the stream early.
            elif event_type == "session.status":
                # Guard against null and non-dict payloads: the server may
                # send "status": "idle" (a string) or "status": null.
                status = props.get("status") or {}
                if isinstance(status, dict) and status.get("type") == "idle":
                    text = "".join(
                        text_accumulator.get(pid, "") for pid in text_part_order
                    ).strip()
                    # Mirror _parse_http_response: empty text + no step-finish
                    # events signals an error turn (e.g. the model refused or
                    # the request was rejected before any content was produced).
                    has_step_finish = num_turns > 0
                    is_error = not text and not has_step_finish
                    if not text:
                        text = "(empty response)"

                    # Include reasoning tokens in the has_usage check so that
                    # reasoning-only turns (e.g. thinking models that report
                    # reasoning tokens but zero input/output) still get a usage
                    # object rather than silently dropping the token count.
                    has_usage = (total_input + total_output + total_reasoning) > 0
                    usage = (
                        TokenUsage(
                            input_tokens=total_input,
                            output_tokens=total_output,
                            cache_read_tokens=total_cache_read,
                            cache_write_tokens=total_cache_write,
                            reasoning_tokens=total_reasoning,
                        )
                        if has_usage
                        else None
                    )
                    yield AgentEvent(
                        kind="final",
                        response=AgentResponse(
                            text=text,
                            session_id=session_id,
                            usage=usage,
                            cost_usd=total_cost if total_cost > 0 else None,
                            num_turns=num_turns if num_turns > 0 else None,
                            is_error=is_error,
                        ),
                    )
                    return

            # ── Server-side error ──────────────────────────────────────────
            elif event_type == "session.error":
                error = props.get("error", {})
                raise AgentExecutionError(f"OpenCode session error: {error!s}")

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _require_base_url(self) -> None:
        """Raise clearly if the server URL hasn't been set yet."""
        if not self._base_url:
            raise RuntimeError(
                "OpenCodeBackend has no base_url — "
                "call start() before create_session() / send(), "
                "or use AgentSession as a context manager."
            )

    def _get_client(self) -> httpx.AsyncClient:
        """Return the long-lived HTTP client, raising if start() hasn't been called.

        Raises ``AgentUnavailableError`` (not ``RuntimeError``) when the client
        is None so that callers — including ``AgentTurnRunner`` and
        ``ContextInjector`` — can distinguish a shutdown-race condition from a
        permanent programming error.  The most common cause of a None client
        after ``_ensure_live_runtime()`` returns is a concurrent ``stop()``
        call that cleared ``_client`` between the live-runtime check and the
        actual HTTP call.
        """
        if self._client is None:
            raise AgentUnavailableError(
                "opencode sidecar has been stopped — call start() to restart"
            )
        return self._client

    async def _post_message(
        self,
        session_id: str,
        text: str,
        timeout: int | None = None,
    ) -> dict:
        """POST a text message to an existing opencode session.

        Raises RuntimeError (sanitized) on non-2xx responses — the original
        httpx.HTTPStatusError is NOT propagated because it includes the full
        request URL and response body, which may contain internal server details
        that should not be exposed to end users or leaked into RC chat.

        Callers must not silently catch 404 and create a new session — let the
        error propagate so the watcher fails loudly.
        """
        url = f"{self._base_url}/session/{session_id}/message"
        body = {"parts": [{"type": "text", "text": text}]}
        effective_timeout = timeout if timeout is not None else self.timeout

        try:
            resp = await self._get_client().post(
                url, json=body, timeout=effective_timeout
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Sanitize: only expose status code and reason, not the full URL or
            # response body (which may contain internal opencode server details).
            message = (
                f"opencode API returned HTTP {exc.response.status_code} "
                f"for session {session_id[:16]!r}"
            )
            raise _classify_http_error(exc.response.status_code, message) from None

        if not resp.content:
            raise AgentUnavailableError(
                f"opencode API returned empty response body (HTTP {resp.status_code}) "
                f"for session {session_id[:16]!r} — "
                "server may have returned a non-JSON response or crashed mid-request"
            )
        try:
            return resp.json()
        except Exception as exc:
            raise AgentUnavailableError(
                f"opencode API returned non-JSON response (HTTP {resp.status_code}) "
                f"for session {session_id[:16]!r}: {resp.text[:200]!r}"
            ) from exc

    async def _cleanup_session_best_effort(self, session_id: str) -> bool:
        """Try to delete a failed session; return True if cleanup succeeded."""
        if not self._base_url or not session_id or self._client is None:
            return False
        url = f"{self._base_url}/session/{session_id}"
        try:
            resp = await self._get_client().delete(url)
            if resp.status_code in (200, 202, 204, 404):
                logger.info(
                    "Best-effort cleanup for failed opencode session %s returned HTTP %d",
                    session_id[:16],
                    resp.status_code,
                )
                self._orphan_session_ids.discard(session_id)
                return True
            logger.warning(
                "Best-effort cleanup for failed opencode session %s returned unexpected HTTP %d",
                session_id[:16],
                resp.status_code,
            )
        except Exception as e:
            logger.warning(
                "Best-effort cleanup failed for opencode session %s: %s",
                session_id[:16],
                e,
            )
        return False

    async def _cleanup_orphan_sessions_best_effort(self) -> None:
        """Try to clean up any sessions left orphaned by earlier failures."""
        if not self._orphan_session_ids or not self._base_url or self._client is None:
            return
        for session_id in list(self._orphan_session_ids):
            cleaned = await self._cleanup_session_best_effort(session_id)
            if cleaned:
                self._orphan_session_ids.discard(session_id)

    async def delete_session(self, session_id: str) -> bool:
        """Best-effort deletion hook used by watcher startup rollback."""
        return await self._cleanup_session_best_effort(session_id)

    def _parse_http_response(self, data: dict, session_id: str) -> AgentResponse:
        """Parse the synchronous POST /session/{id}/message response body.

        Response shape::

            {
              "info": {"duration": <ms>, ...},
              "parts": [
                {"type": "text", "text": "..."},
                {"type": "step-finish", "tokens": {"input": N, "output": N,
                  "reasoning": N, "cache": {"read": N, "write": N}},
                  "cost": 0.001},
                ...
              ]
            }

        Notes:
          - ``step-finish`` uses a hyphen, not an underscore (unlike the old CLI stream).
          - ``is_error`` is set to True when no text parts are extracted from the
            response (empty or tool-only turns).  This lets ContextInjector detect
            failed injection attempts without relying on the HTTP status code.
          - ``duration_ms`` is read from ``info.duration``; verify field name against
            a live response.
        """
        # Guard against explicit JSON null for top-level fields.
        parts = data.get("parts") or []
        info = data.get("info") or {}

        text_parts: list[str] = []
        total_input = total_output = total_reasoning = 0
        total_cache_read = total_cache_write = 0
        total_cost: float = 0.0
        num_turns: int = 0

        for part in parts:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype == "text":
                t = part.get("text", "")
                if t:
                    text_parts.append(t)
            elif ptype == "step-finish":
                num_turns += 1
                # Guard against explicit JSON null for tokens, cache, and cost.
                tokens = part.get("tokens") or {}
                total_input += tokens.get("input", 0)
                total_output += tokens.get("output", 0)
                total_reasoning += tokens.get("reasoning", 0)
                cache = tokens.get("cache") or {}
                total_cache_read += cache.get("read", 0)
                total_cache_write += cache.get("write", 0)
                total_cost += part.get("cost") or 0.0

        text = "".join(text_parts).strip()
        is_error = False
        if not text:
            # Distinguish tool-only turns (agent ran tools, no text output) from
            # genuine errors (no parts at all, or only unrecognised part types).
            # Tool-only turns are valid — they produce step-finish events but no
            # text blocks.  Marking them as is_error=True would cause ContextInjector
            # to incorrectly count them as failed injection attempts.
            has_tool_steps = any(p.get("type") == "step-finish" for p in parts)
            if has_tool_steps:
                logger.debug(
                    "No text extracted from opencode response but tool steps completed "
                    "(tool-only turn) — not marking as error."
                )
            else:
                logger.warning(
                    "No text extracted from opencode HTTP response. Parts: %s",
                    str(parts)[:500],
                )
                is_error = True
            text = "(empty response)"

        # Include reasoning tokens so reasoning-only turns still produce a
        # usage object (same fix applied to the SSE path).
        has_usage = (total_input + total_output + total_reasoning) > 0
        usage = (
            TokenUsage(
                input_tokens=total_input,
                output_tokens=total_output,
                cache_read_tokens=total_cache_read,
                cache_write_tokens=total_cache_write,
                reasoning_tokens=total_reasoning,
            )
            if has_usage
            else None
        )

        return AgentResponse(
            text=text,
            session_id=session_id,
            usage=usage,
            cost_usd=total_cost if total_cost > 0 else None,
            duration_ms=info.get("duration"),
            num_turns=num_turns if num_turns > 0 else None,
            is_error=is_error,
        )
