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
from .runtime_lock import RUNTIME_DIR

if TYPE_CHECKING:
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

    def __init__(self, entries: "list[ConnectorEntry]") -> None:
        self._entries = entries
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
