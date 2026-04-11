"""Control socket server: routes CLI commands to SessionManagers.

Extracted from GatewayService so the top-level orchestrator stays focused on
process-level lifecycle.  This module owns:

  - The Unix domain socket server lifecycle.
  - JSON framing (read request line → dispatch → write response line).
  - Command routing by connector name (aggregate ``list``, explicit targeting,
    ambiguity guard for multi-connector deployments).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from . import runtime_lock
from .core.tz_utils import local_iana_timezone as _server_local_timezone
from .runtime_lock import RUNTIME_DIR

if TYPE_CHECKING:
    from .core.job_store import JobStore
    from .service import ConnectorEntry

logger = logging.getLogger("agent-chat-gateway.control")

CONTROL_SOCK = RUNTIME_DIR / "control.sock"


class ControlServer:
    """Unix socket server for CLI command routing.

    Usage::

        server = ControlServer(entries)
        await server.start()

        # ... gateway runs ...

        await server.stop()
    """

    def __init__(
        self,
        entries: "list[ConnectorEntry]",
        job_store: "JobStore | None" = None,
        default_timezone: str = "",
    ) -> None:
        self._entries = entries
        self._job_store = job_store
        self._default_timezone = default_timezone
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """Bind the Unix domain socket and start accepting connections."""
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        if CONTROL_SOCK.exists():
            # Attempt a quick probe connection before removing the socket file.
            # If the probe succeeds, another live gateway instance is already
            # bound — refuse to start rather than silently hijacking the socket.
            # If the probe fails (ConnectionRefusedError / OSError), the socket
            # is a stale leftover from a previous crash and is safe to remove.
            _probe_writer = None
            try:
                reader, _probe_writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(CONTROL_SOCK)),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                # IMPORTANT: asyncio.TimeoutError is TimeoutError which is a
                # subclass of OSError (Python 3.3+), so this clause MUST come
                # before the OSError clause below — otherwise OSError catches it
                # first and the PID-lock check is never reached.
                #
                # Socket exists but the connection timed out.  This could mean
                # the instance is alive but overloaded.  Check the PID lock
                # before treating the socket as stale — unlinking a live socket
                # would allow two gateway instances to run simultaneously (split-brain).
                pid = runtime_lock.locked_pid()
                if pid is not None:
                    raise RuntimeError(
                        f"Another gateway instance may be running (pid={pid}, "
                        f"socket {CONTROL_SOCK} timed out). "
                        f"Stop it first, or remove the socket manually to force-start."
                    )
                # No live PID owner — the socket is stale despite the timeout.
                CONTROL_SOCK.unlink()
            except (ConnectionRefusedError, OSError):
                # Socket refused or OS error — clearly stale, safe to replace.
                CONTROL_SOCK.unlink()
            else:
                # Probe succeeded — a live gateway instance is already bound.
                # Close the probe writer OUTSIDE the try/except so that any
                # OSError from writer cleanup cannot be caught by the OSError
                # clause above (which would mis-classify the socket as stale
                # and unlink it, allowing two instances to run simultaneously).
                if _probe_writer is not None:
                    try:
                        _probe_writer.close()
                        await _probe_writer.wait_closed()
                    except OSError:
                        pass
                raise RuntimeError(
                    f"Another gateway instance is already running "
                    f"(socket {CONTROL_SOCK} is live). "
                    f"Stop it first, or remove the socket manually to force-start."
                )

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(CONTROL_SOCK)
        )
        # Restrict socket access to the owner only.  asyncio.start_unix_server
        # creates the socket with the process umask (typically 0o666 & ~umask),
        # which may allow group/world read-write.  chmod 0o600 ensures only the
        # owner can connect, providing OS-level access control for all commands
        # (pause, resume, reset, send) routed through this socket.
        try:
            import os as _os
            _os.chmod(str(CONTROL_SOCK), 0o600)
        except OSError as exc:
            logger.warning("Could not set control socket permissions: %s", exc)
        logger.info("Control socket listening at %s", CONTROL_SOCK)

    async def stop(self) -> None:
        """Close the server and remove the socket file."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        CONTROL_SOCK.unlink(missing_ok=True)

    # ── Client handling ───────────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single CLI connection: read JSON request, dispatch, respond."""
        try:
            # Apply a read timeout so a client that connects but never sends data
            # (e.g., killed mid-send) cannot hold the handler open indefinitely.
            data = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not data:
                return
            request = json.loads(data.decode())
            response = await self.dispatch_command(request)
            writer.write(json.dumps(response).encode() + b"\n")
            # Drain with a timeout so a client that receives the response but
            # then dies before consuming it (e.g. SIGKILL mid-read) cannot
            # block this handler indefinitely, leaking a file descriptor and
            # a coroutine slot in the event loop.
            await asyncio.wait_for(writer.drain(), timeout=10.0)
        except Exception as e:
            try:
                writer.write(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
                await asyncio.wait_for(writer.drain(), timeout=10.0)
            except Exception:
                pass
        finally:
            writer.close()
            try:
                # Guard against a crashed client that never acknowledges the
                # close — the default TCP keepalive timeout is ~2 hours, so
                # without this limit the handler coroutine would leak for hours
                # holding an open file descriptor.
                await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass

    # ── Command routing ───────────────────────────────────────────────────────

    async def dispatch_command(self, request: dict) -> dict:
        """Route a CLI command to the appropriate SessionManager.

        Uses the 'connector' field in the request to select the target entry.
        Special case: 'list' without a connector name aggregates watchers
        across ALL connectors, annotating each entry with its connector name.
        Special case: 'reset' without a connector name auto-resolves the entry
        by searching all connectors for the named watcher (watcher names are
        globally unique, enforced at config load time).
        """
        cmd = request.get("cmd")
        connector_name = request.get("connector")

        # list without a specific connector → aggregate across all entries
        if cmd == "list" and not connector_name:
            all_watchers: list = []
            errors: list = []
            for entry in self._entries:
                try:
                    result = await entry.session_manager.dispatch_command(request)
                except Exception as e:
                    # An unexpected exception from one connector must not
                    # abort the entire list — other connectors' watchers are
                    # still valid.  Capture the error with connector attribution
                    # so the caller can see which connector is broken.
                    logger.error(
                        "dispatch_command('list') raised for connector '%s': %s",
                        entry.name,
                        e,
                    )
                    errors.append({"connector": entry.name, "error": str(e)})
                    continue
                if result.get("ok"):
                    all_watchers.extend(result.get("data", []))
                else:
                    errors.append({
                        "connector": entry.name,
                        "error": result.get("error", "unknown error"),
                    })
            return {
                "ok": len(errors) == 0,
                "data": all_watchers,
                **({"errors": errors} if errors else {}),
            }

        # send: route directly to a connector's send_to_room method
        if cmd == "send":
            return await self._handle_send(request, connector_name)

        # schedule-*: managed by JobStore (no connector routing needed)
        if cmd and cmd.startswith("schedule-"):
            return self._handle_schedule(cmd, request)

        # reset: auto-resolve connector from watcher name (watcher names are
        # globally unique across all connectors, so no --connector is needed).
        if cmd == "reset" and not connector_name:
            watcher_name = request.get("watcher_name", "")
            entry = self._find_entry_for_watcher(watcher_name)
            if isinstance(entry, dict):
                return entry  # error response (unknown watcher)
            return await entry.session_manager.dispatch_command(request)

        # All other commands: route to a specific entry
        entry = self._resolve_entry(connector_name)
        if isinstance(entry, dict):
            return entry  # error response

        return await entry.session_manager.dispatch_command(request)

    def _resolve_entry(
        self, connector_name: str | None
    ) -> "ConnectorEntry | dict":
        """Resolve a connector name to a ConnectorEntry, or return an error dict."""
        if connector_name:
            entry = next((e for e in self._entries if e.name == connector_name), None)
            if entry is None:
                return {"ok": False, "error": f"Unknown connector: {connector_name!r}"}
            return entry

        if not self._entries:
            return {"ok": False, "error": "No connectors configured"}
        # Require explicit connector selection when multiple are configured.
        if len(self._entries) > 1:
            names = ", ".join(f"'{e.name}'" for e in self._entries)
            return {
                "ok": False,
                "error": (
                    f"Multiple connectors configured ({names}). "
                    f"Please specify --connector <name>."
                ),
            }
        return self._entries[0]

    def _find_entry_for_watcher(self, watcher_name: str) -> "ConnectorEntry | dict":
        """Find the ConnectorEntry that owns the named watcher.

        Watcher names are globally unique (enforced at config load time), so
        searching all entries by name is unambiguous.  Returns an error dict
        if no entry owns the watcher.
        """
        if not watcher_name:
            return {"ok": False, "error": "Missing 'watcher_name'"}
        for entry in self._entries:
            if entry.session_manager.get_watcher_config(watcher_name) is not None:
                return entry
        return {"ok": False, "error": f"Unknown watcher: {watcher_name!r}"}

    def _handle_schedule(self, cmd: str, request: dict) -> dict:
        """Route schedule-* commands to the JobStore.

        All sub-handlers are synchronous (they call JobStore.save() which is
        synchronous file I/O).  This method is therefore a plain def — not async —
        to make the call-site ``return self._handle_schedule(cmd, request)`` in
        ``dispatch_command`` accurate and avoid the misleading impression that there
        is any I/O awaiting happening here.
        """
        if self._job_store is None:
            return {"ok": False, "error": "Scheduler is not enabled (JobStore not configured)"}

        if cmd == "schedule-create":
            return self._handle_schedule_create(request)
        if cmd == "schedule-list":
            return self._handle_schedule_list(request)
        if cmd == "schedule-delete":
            return self._handle_schedule_delete(request)
        if cmd == "schedule-pause":
            return self._handle_schedule_pause(request)
        if cmd == "schedule-resume":
            return self._handle_schedule_resume(request)
        return {"ok": False, "error": f"Unknown schedule command: {cmd!r}"}

    def _handle_schedule_create(self, request: dict) -> dict:
        from datetime import UTC, datetime

        from .core.scheduler import compute_next_run
        from .schedule_types import JobStatus, ScheduledJob

        watcher = (request.get("watcher") or "").strip()
        message = request.get("message", "")
        cron = request.get("cron", "")
        timezone = request.get("timezone") or self._default_timezone or _server_local_timezone()
        times = request.get("times", 0)

        if not watcher:
            return {"ok": False, "error": "Missing 'watcher' field"}
        if not isinstance(message, str):
            return {"ok": False, "error": "'message' must be a string"}
        if not message:
            return {"ok": False, "error": "Missing 'message' field"}
        if len(message) > 4096:
            return {"ok": False, "error": "'message' must be at most 4096 characters"}
        if not cron:
            return {"ok": False, "error": "Missing 'cron' field"}
        if isinstance(times, bool) or not isinstance(times, int) or times < 0:
            return {"ok": False, "error": "'times' must be a non-negative integer (0 = forever)"}

        # Validate timezone string (M6): reject invalid IANA names at creation time
        # so the job is never stored with a timezone that compute_next_run silently
        # falls back from, emitting a spurious warning on every tick.
        try:
            import zoneinfo as _zi
            _zi.ZoneInfo(timezone)
        except (_zi.ZoneInfoNotFoundError, KeyError):
            return {"ok": False, "error": f"Unknown timezone {timezone!r}. Use an IANA name (e.g. 'America/Los_Angeles', 'UTC')."}
        except Exception as e:
            return {"ok": False, "error": f"Failed to validate timezone {timezone!r}: {e}"}

        # Validate cron expression
        try:
            from croniter import croniter  # type: ignore[import-untyped]
            if not croniter.is_valid(cron):
                return {"ok": False, "error": f"Invalid cron expression: {cron!r}"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to validate cron expression: {e}"}

        now = datetime.now(UTC)
        next_run_override = request.get("next_run")
        if next_run_override is not None:
            # Validate: must be a parseable, timezone-aware ISO 8601 string.
            # An untrusted or malformed value stored verbatim would cause the
            # scheduler to skip the job (ValueError on parse) or fire it
            # immediately on every tick (past timestamp).
            try:
                nr_dt = datetime.fromisoformat(next_run_override)
            except ValueError:
                return {
                    "ok": False,
                    "error": (
                        f"Invalid 'next_run' value {next_run_override!r}: "
                        "must be an ISO 8601 datetime string "
                        "(e.g. '2026-04-10T15:30:00+00:00')"
                    ),
                }
            if nr_dt.tzinfo is None:
                return {
                    "ok": False,
                    "error": (
                        f"'next_run' value {next_run_override!r} is missing timezone info. "
                        "Use UTC offset (e.g. '+00:00') or 'Z'."
                    ),
                }
            # C3: reject past timestamps — a past next_run causes the scheduler
            # to fire the job on the very next tick, bypassing the intended schedule.
            if nr_dt < now:
                return {
                    "ok": False,
                    "error": (
                        f"'next_run' value {next_run_override!r} is in the past. "
                        "Provide a future datetime."
                    ),
                }
            next_run = next_run_override
        else:
            try:
                next_run = compute_next_run(cron, timezone, after=now)
            except Exception as e:
                return {"ok": False, "error": f"Failed to compute next run time: {e}"}

        # Resolve connector from watcher name — the watcher name is the sole
        # identifier; connectors need not be specified by the caller.
        connector = self._find_connector_for_watcher(watcher)
        if not connector:
            available = self._list_all_watcher_names()
            hint = f" Available watchers: {available}" if available else ""
            return {
                "ok": False,
                "error": f"Watcher {watcher!r} not found in any connector.{hint}",
            }

        job = ScheduledJob(
            watcher=watcher,
            connector=connector,
            message=message,
            cron=cron,
            timezone=timezone,
            times=times,
            status=JobStatus.ACTIVE,
            created_at=now.isoformat(),
            next_run=next_run,
        )
        try:
            self._job_store.add(job)
        except Exception as e:
            return {"ok": False, "error": f"Failed to save job: {e}"}
        return {"ok": True, "job_id": job.id, "next_run": next_run}

    def _handle_schedule_list(self, request: dict) -> dict:
        connector = request.get("connector")
        include_completed = request.get("include_completed", False)
        try:
            jobs = self._job_store.list_jobs(connector=connector, include_completed=include_completed)
            return {"ok": True, "jobs": [j.to_dict() for j in jobs]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _handle_schedule_delete(self, request: dict) -> dict:
        job_id = request.get("job_id", "")
        if not job_id:
            return {"ok": False, "error": "Missing 'job_id' field"}
        removed = self._job_store.remove(job_id)
        if not removed:
            return {"ok": False, "error": f"Job {job_id!r} not found"}
        return {"ok": True}

    def _handle_schedule_pause(self, request: dict) -> dict:
        from .schedule_types import JobStatus
        job_id = request.get("job_id", "")
        if not job_id:
            return {"ok": False, "error": "Missing 'job_id' field"}
        job = self._job_store.get(job_id)
        if not job:
            return {"ok": False, "error": f"Job {job_id!r} not found"}
        if job.status == JobStatus.COMPLETED:
            return {"ok": False, "error": f"Job {job_id!r} is already completed"}
        if job.status == JobStatus.PAUSED:
            return {"ok": True}  # idempotent: already paused
        job.status = JobStatus.PAUSED
        try:
            self._job_store.update(job)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def _handle_schedule_resume(self, request: dict) -> dict:
        from datetime import UTC, datetime

        from .core.scheduler import compute_next_run
        from .schedule_types import JobStatus
        job_id = request.get("job_id", "")
        if not job_id:
            return {"ok": False, "error": "Missing 'job_id' field"}
        job = self._job_store.get(job_id)
        if not job:
            return {"ok": False, "error": f"Job {job_id!r} not found"}
        if job.status == JobStatus.COMPLETED:
            return {"ok": False, "error": f"Job {job_id!r} is already completed and cannot be resumed"}
        if job.status == JobStatus.ACTIVE:
            return {"ok": True, "next_run": job.next_run}  # idempotent: already active
        # Compute next_run BEFORE mutating status so that a bad cron expression
        # leaves the job in its current (paused) state rather than half-resuming.
        try:
            next_run = compute_next_run(job.cron, job.timezone, after=datetime.now(UTC))
        except Exception as e:
            return {"ok": False, "error": f"Failed to compute next_run: {e}"}
        job.status = JobStatus.ACTIVE
        job.next_run = next_run
        try:
            self._job_store.update(job)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "next_run": job.next_run}

    def _find_connector_for_watcher(self, watcher_name: str) -> str:
        """Find the connector name for a watcher by searching all entries."""
        for entry in self._entries:
            sm = entry.session_manager
            # Check running watcher state first (watcher is active)
            if sm.get_watcher_state(watcher_name) is not None:
                return entry.name
        # Fallback: check watcher configs (watcher defined but may be paused/stopped)
        for entry in self._entries:
            if entry.session_manager.get_watcher_config(watcher_name) is not None:
                return entry.name
        return ""

    def _list_all_watcher_names(self) -> str:
        """Return a comma-separated string of all configured watcher names."""
        names: list[str] = []
        seen: set[str] = set()
        for entry in self._entries:
            for name in entry.session_manager.get_all_watcher_names():
                if name not in seen:
                    names.append(name)
                    seen.add(name)
        return ", ".join(f"{n!r}" for n in sorted(names))

    async def _handle_send(self, request: dict, connector_name: str | None) -> dict:
        """Handle the 'send' command: route to a connector's send_to_room method."""
        entry = self._resolve_entry(connector_name)
        if isinstance(entry, dict):
            return entry  # error response

        room = request.get("room", "")
        text = request.get("text", "")
        attachment_path = request.get("attachment_path")

        if not room:
            return {"ok": False, "error": "Missing 'room' field in send command"}
        if not text and not attachment_path:
            return {"ok": False, "error": "Nothing to send: provide 'text' or 'attachment_path'"}

        try:
            await entry.connector.send_to_room(room, text, attachment_path=attachment_path)
            return {"ok": True}
        except Exception as e:
            logger.error("send_to_room failed for connector '%s': %s", entry.name, e)
            return {"ok": False, "error": str(e)}
