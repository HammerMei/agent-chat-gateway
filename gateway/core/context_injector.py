"""ContextInjector: reads context files and sends them to agent sessions.

Extracted from SessionManager to keep context injection logic focused
and independently testable.  Uses asyncio.to_thread for non-blocking
file I/O.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from ..agents import AgentBackend
from .config import CoreConfig, WatcherConfig
from .state import WatcherState

logger = logging.getLogger("agent-chat-gateway.core.context_injector")

_MAX_FILE_SIZE = 256 * 1024  # 256 KB per file
_MAX_CONTEXT_SIZE = 512 * 1024  # 512 KB total
_MAX_INJECT_ATTEMPTS = 3  # Give up after this many agent-error responses


@dataclass
class InjectionStatus:
    state: str = "not_started"  # not_started | pending | failed_retryable | failed_degraded | injected
    failure_count: int = 0
    last_error: str | None = None


class ContextInjector:
    """Injects gateway context into agent sessions once per session lifetime.

    Reads the concatenated list of context files from all three layers
    (connector → agent → watcher) and sends the combined content as a
    silent prompt.  Marks ``state.context_injected = True`` on success.

    Skips silently if context is already injected for the session.
    Raises on hard errors (e.g. missing file) — caller must handle.

    If the agent returns an error response, the failure counter is
    incremented but ``context_injected`` is left as ``False`` so that
    injection can be retried.  After ``_MAX_INJECT_ATTEMPTS`` consecutive
    failures the injector gives up (marks context_injected=True) to prevent
    unbounded retries.

    Retry behavior:
      - Initial injection is attempted during watcher startup.
      - If that fails with an agent error response, ``MessageProcessor`` may
        retry on later messages while the status remains retryable/pending.
      - After ``_MAX_INJECT_ATTEMPTS`` consecutive failures the injector marks
        the session degraded to avoid unbounded retry loops.
    """

    def __init__(self, config: CoreConfig) -> None:
        self._config = config
        # In-memory injection status keyed by session_id. Not persisted: a gateway
        # restart resets the counters, giving the agent a fresh chance.
        self._inject_status: dict[str, InjectionStatus] = {}

    def status_for(self, session_id: str) -> InjectionStatus:
        """Return the current injection status for a session."""
        return self._inject_status.get(session_id, InjectionStatus())

    def reset_session(self, session_id: str) -> None:
        """Clear injection state for a session, resetting retry counters.

        Called by watcher reset so that the next injection attempt starts with
        a fresh failure counter.  Without this, a ``failed_degraded`` session
        that is reset would immediately re-enter ``failed_degraded`` on the
        very first retry because the old failure_count is still at or above
        ``_MAX_INJECT_ATTEMPTS``.
        """
        self._inject_status.pop(session_id, None)

    async def inject(
        self,
        ws: WatcherState,
        session_id: str,
        agent: AgentBackend,
        agent_name: str,
        connector_name: str,
        wc: WatcherConfig,
    ) -> None:
        """Inject context into the agent session if not already done."""
        if ws.context_injected:
            self._inject_status[session_id] = InjectionStatus(state="injected")
            return

        status = self._inject_status.setdefault(session_id, InjectionStatus())

        # Guard against concurrent inject() calls for the same session_id.
        # Two processors sharing a sticky session ID (wc.session_id) can both
        # pass the ws.context_injected=False check above and both call inject().
        # Since asyncio is cooperative, a second call can only arrive while this
        # coroutine is suspended at an `await` (e.g., asyncio.to_thread for file
        # I/O).  If status is already "pending" (another call is in-flight), bail
        # out — the in-progress call will set ws.context_injected=True on success,
        # which the next message check will see.
        if status.state == "pending":
            return

        agent_cfg = self._config.agent_config(agent_name)
        files = self._config.context_inject_files_for(
            connector_name, agent_name, wc.context_inject_files
        )
        if not files:
            # No context files configured for this watcher — nothing to inject.
            # Mark as injected immediately to prevent re-entry on every subsequent
            # message (otherwise _ensure_context_injected() would call inject()
            # on every message, finding no files each time and looping forever).
            ws.context_injected = True
            self._inject_status[session_id] = InjectionStatus(state="injected")
            return
        status.state = "pending"

        try:
            combined_context = []
            for path_str in files:
                p = Path(path_str)
                exists = await asyncio.to_thread(p.exists)
                if not exists:
                    raise FileNotFoundError(
                        f"context_inject_files entry not found for watcher '{wc.name}': {p}"
                    )
                # Pre-check via stat to skip obviously oversized files early and
                # avoid reading them into memory unnecessarily.  However, the stat
                # result can race with the actual read (TOCTOU), so we re-check the
                # content length after reading — this is the authoritative limit.
                file_stat = await asyncio.to_thread(p.stat)
                if file_stat.st_size > _MAX_FILE_SIZE:
                    logger.warning(
                        "Context file %s exceeds %d KB limit (%d KB) — skipping",
                        p.name,
                        _MAX_FILE_SIZE // 1024,
                        file_stat.st_size // 1024,
                    )
                    continue
                content = await asyncio.to_thread(p.read_text, encoding="utf-8")
                # Re-check after read to close TOCTOU window: a file could grow
                # between the stat() and read_text() calls (e.g., a log file being
                # appended to).  This is the authoritative size gate.
                if len(content.encode()) > _MAX_FILE_SIZE:
                    logger.warning(
                        "Context file %s exceeded %d KB limit after read — skipping",
                        p.name,
                        _MAX_FILE_SIZE // 1024,
                    )
                    continue
                combined_context.append(content)

            if not combined_context:
                # All files were present but exceeded the per-file size limit and were
                # skipped.  There is nothing meaningful to inject — treat this as an
                # immediate success to avoid sending an empty prompt to the agent and
                # potentially burning retry attempts on a content-less round-trip.
                logger.warning(
                    "Context injection for watcher '%s' (session %s): "
                    "all %d configured file(s) exceeded the %d KB size limit — "
                    "skipping injection.",
                    wc.name,
                    session_id[:8],
                    len(files),
                    _MAX_FILE_SIZE // 1024,
                )
                ws.context_injected = True
                self._inject_status[session_id] = InjectionStatus(state="injected")
                return

            # Prepend a small dynamic header so the agent knows its own identity.
            # This is added only when there is real file content to inject (i.e. we
            # skip it for the "all files oversized" path above to avoid a pointless
            # agent round-trip for just metadata).
            dynamic_header = (
                f"## ACG Session Identity\n"
                f"- **Watcher name:** `{wc.name}`\n"
                f"- **Connector:** `{connector_name}`\n"
            )
            combined_context.insert(0, dynamic_header)

            full_context = "\n\n".join(combined_context)
            encoded = full_context.encode()
            if len(encoded) > _MAX_CONTEXT_SIZE:
                logger.warning(
                    "Total context size exceeds %d KB limit — truncating",
                    _MAX_CONTEXT_SIZE // 1024,
                )
                full_context = encoded[:_MAX_CONTEXT_SIZE].decode(errors="ignore")
                full_context += "\n\n[... context truncated due to size limit ...]"
            response = await agent.send(
                session_id=session_id,
                prompt=full_context,
                working_directory=agent_cfg.working_directory,
                timeout=agent_cfg.timeout,
            )
        except Exception:
            # Unexpected error (e.g. PermissionError, OSError during file I/O, or
            # an agent send failure that raises rather than returning an error
            # response).  Reset status so the next inject() call can retry —
            # without this, the concurrent-inject guard (status == "pending") would
            # permanently block all future retries for this session.
            #
            # Note: failure_count is intentionally NOT reset here.  failure_count
            # is only incremented by the is_error branch below, so an unexpected
            # exception (e.g. FileNotFoundError) does not count as an inject attempt.
            # Once the root cause is resolved (e.g. file restored), the next
            # inject() call will succeed without being penalised for the prior
            # exception failures.
            if status.state == "pending":
                status.state = "not_started"
            raise

        if response.is_error:
            status.failure_count += 1
            status.last_error = response.text[:200]
            if status.failure_count >= _MAX_INJECT_ATTEMPTS:
                status.state = "failed_degraded"
                logger.error(
                    "Context injection failed %d times for watcher '%s' (session %s) — "
                    "marking degraded.  Last error: %s",
                    status.failure_count,
                    wc.name,
                    session_id[:8],
                    response.text[:200],
                )
            else:
                status.state = "failed_retryable"
                logger.warning(
                    "Context injection failed for watcher '%s' (session %s) "
                    "[attempt %d/%d]: %s",
                    wc.name,
                    session_id[:8],
                    status.failure_count,
                    _MAX_INJECT_ATTEMPTS,
                    response.text[:200],
                )
            return
        # Success — clear any failure counter and mark as injected.
        self._inject_status[session_id] = InjectionStatus(state="injected")
        ws.context_injected = True
        logger.info(
            "Context injected for watcher '%s' (session %s, %d file(s))",
            wc.name,
            session_id[:8],
            len(files),
        )
