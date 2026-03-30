"""Tests for GatewayService startup/shutdown lifecycle hardening."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.service import GatewayService


def _make_service() -> GatewayService:
    service = GatewayService.__new__(GatewayService)
    service._registry = MagicMock()
    service._maps = SimpleNamespace(connector_view={})
    service._expiry_task = None
    service._runtime_manager = MagicMock()
    service._control = MagicMock()
    service._entries = []
    return service


class TestGatewayServiceRun(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_writes_handshake_error_and_closes_fd(self):
        service = _make_service()
        service._runtime_manager.start_all = AsyncMock(return_value=[])
        service._runtime_manager.has_active_brokers = False
        service._runtime_manager.unavailable_agents = set()
        sm = MagicMock()
        sm.run_once = AsyncMock(return_value=[])
        sm.shutdown = AsyncMock()
        service._entries = [SimpleNamespace(name="script", session_manager=sm)]
        service._control.start = AsyncMock(side_effect=RuntimeError("control boom"))
        service._control.stop = AsyncMock()
        service._runtime_manager.stop_all = AsyncMock()

        rfd, wfd = os.pipe()
        try:
            with self.assertRaisesRegex(RuntimeError, "control boom"):
                await service.run(startup_fd=wfd)
            payload = os.read(rfd, 4096).decode()
        finally:
            os.close(rfd)

        self.assertIn("error:startup failed: control boom", payload)
        # Fatal startup failure must NOT emit "ok" — emitting it would cause
        # the parent to report "degraded startup" even though the daemon crashed.
        self.assertNotIn("ok", payload)
        service._runtime_manager.stop_all.assert_awaited_once()
        service._control.stop.assert_awaited_once()


class TestGatewayServiceShutdown(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_continues_after_session_manager_failure(self):
        service = _make_service()
        bad_sm = MagicMock()
        bad_sm.shutdown = AsyncMock(side_effect=RuntimeError("sm failed"))
        good_sm = MagicMock()
        good_sm.shutdown = AsyncMock()
        service._entries = [
            SimpleNamespace(name="bad", session_manager=bad_sm),
            SimpleNamespace(name="good", session_manager=good_sm),
        ]
        service._runtime_manager.stop_all = AsyncMock()
        service._control.stop = AsyncMock()

        expiry_task = AsyncMock()
        expiry_task.cancel = MagicMock()
        service._expiry_task = expiry_task

        await service.shutdown()

        bad_sm.shutdown.assert_awaited_once()
        good_sm.shutdown.assert_awaited_once()
        service._runtime_manager.stop_all.assert_awaited_once()
        expiry_task.cancel.assert_called_once()
        service._control.stop.assert_awaited_once()

    async def test_shutdown_continues_after_runtime_manager_failure(self):
        service = _make_service()
        sm = MagicMock()
        sm.shutdown = AsyncMock()
        service._entries = [SimpleNamespace(name="only", session_manager=sm)]
        service._runtime_manager.stop_all = AsyncMock(
            side_effect=RuntimeError("rt failed")
        )
        service._control.stop = AsyncMock()

        await service.shutdown()

        sm.shutdown.assert_awaited_once()
        service._control.stop.assert_awaited_once()


class TestWriteStartupSignal(unittest.TestCase):
    """_write_startup_signal() — protocol correctness for success vs. fatal paths.

    Bug fixed: previously the function always appended "ok\\n", so a fatal
    startup failure would still cause the parent to report "degraded startup"
    even though the daemon had already crashed.
    """

    def _read_pipe(self, wfd: int, rfd: int) -> str:
        """Write nothing more, close write-end, read everything from read-end."""
        try:
            os.close(wfd)
        except OSError:
            pass
        data = b""
        try:
            while chunk := os.read(rfd, 4096):
                data += chunk
        except OSError:
            pass
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass
        return data.decode()

    # ── Success path ──────────────────────────────────────────────────────────

    def test_success_no_warnings_emits_ok(self):
        """Clean startup (no errors) must emit exactly 'ok\\n'."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, [])
        payload = self._read_pipe(-1, rfd)  # wfd already closed by function
        self.assertEqual(payload.strip(), "ok")
        self.assertNotIn("error:", payload)

    def test_success_with_warnings_emits_errors_and_ok(self):
        """Degraded startup (non-fatal warnings) must emit error lines AND ok."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, ["agent foo unavailable", "watcher bar skipped"])
        payload = self._read_pipe(-1, rfd)
        self.assertIn("error:agent foo unavailable", payload)
        self.assertIn("error:watcher bar skipped", payload)
        self.assertIn("ok", payload)
        # ok must appear AFTER error lines (last line)
        lines = [line for line in payload.splitlines() if line.strip()]
        self.assertEqual(lines[-1], "ok")

    # ── Fatal path ────────────────────────────────────────────────────────────

    def test_fatal_no_ok_emitted(self):
        """Fatal failure must NOT emit 'ok' — parent must see failure."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, ["startup failed: connection refused"], fatal=True)
        payload = self._read_pipe(-1, rfd)
        self.assertIn("error:startup failed: connection refused", payload)
        self.assertNotIn("ok", payload)

    def test_fatal_empty_errors_no_ok(self):
        """fatal=True with no error messages still must not emit 'ok'."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, [], fatal=True)
        payload = self._read_pipe(-1, rfd)
        self.assertNotIn("ok", payload)

    def test_fatal_multiple_errors_no_ok(self):
        """Multiple error lines on fatal path — none of them must be 'ok'."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, ["err1", "err2", "err3"], fatal=True)
        payload = self._read_pipe(-1, rfd)
        for line in payload.splitlines():
            self.assertNotEqual(line.strip(), "ok", f"unexpected 'ok' line: {line!r}")

    # ── Newline sanitization ───────────────────────────────────────────────────

    def test_newlines_in_errors_sanitized(self):
        """Embedded newlines in error messages must not split the protocol lines."""
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        _write_startup_signal(wfd, ["line one\nline two"], fatal=True)
        payload = self._read_pipe(-1, rfd)
        # Must be a single error: line with the newline replaced by space
        error_lines = [line for line in payload.splitlines() if line.startswith("error:")]
        self.assertEqual(len(error_lines), 1)
        self.assertIn("line one line two", error_lines[0])

    # ── OSError on write is logged, not raised (E1) ──────────────────────────

    def test_oserror_on_write_is_logged_not_raised(self):
        """E1: OSError when writing the startup signal must be logged (not raised).

        If the write fails (e.g. closed fd, EPIPE), the function must still
        close the fd so the parent receives EOF and can unblock.  The error
        must appear in the log rather than being silently swallowed.
        """
        from gateway.service import _write_startup_signal

        # Create a pipe, then close the write end before passing it to the
        # function — any write attempt will raise EBADF (bad file descriptor).
        rfd, wfd = os.pipe()
        os.close(wfd)  # pre-close to force OSError on write

        with self.assertLogs("agent-chat-gateway.service", level="WARNING") as log_ctx:
            # Must NOT raise — the OSError must be caught and logged.
            _write_startup_signal(wfd, [])

        # The warning must mention the fd and the OSError
        combined = " ".join(log_ctx.output)
        self.assertIn("startup signal", combined.lower())

        # Clean up the read end
        try:
            os.close(rfd)
        except OSError:
            pass

    def test_oserror_on_write_still_closes_fd(self):
        """After an OSError on write, the fd must be closed so the parent unblocks.

        We verify this by attempting a second close, which must raise EBADF
        (i.e. the fd was already closed by the function's finally block).
        """
        from gateway.service import _write_startup_signal

        rfd, wfd = os.pipe()
        os.close(wfd)  # force OSError on write

        import logging
        # suppress the expected warning so it doesn't pollute test output
        logger = logging.getLogger("agent-chat-gateway.service")
        logger.disabled = True
        try:
            _write_startup_signal(wfd, [])
        finally:
            logger.disabled = False

        # The fd should already be closed; a second close must raise OSError/EBADF
        with self.assertRaises(OSError):
            os.close(wfd)

        try:
            os.close(rfd)
        except OSError:
            pass

    # ── Parent-side interpretation ────────────────────────────────────────────

    def test_parent_sees_failure_when_no_ok(self):
        """_wait_for_startup_signal must exit(1) when no 'ok' line is received.

        This simulates the daemon writing only error lines (fatal=True path)
        and verifies the parent correctly interprets absence of 'ok' as failure.
        """
        from gateway.daemon import _wait_for_startup_signal

        rfd, wfd = os.pipe()
        # Write error-only payload (no ok) then close write end
        os.write(wfd, b"error:startup failed: kaboom\n")
        os.close(wfd)

        with self.assertRaises(SystemExit) as cm:
            _wait_for_startup_signal(rfd)
        self.assertEqual(cm.exception.code, 1)

    def test_parent_sees_degraded_when_errors_and_ok(self):
        """_wait_for_startup_signal must exit(0) with warnings on degraded startup."""
        from gateway.daemon import _wait_for_startup_signal

        rfd, wfd = os.pipe()
        os.write(wfd, b"error:watcher foo missing\nok\n")
        os.close(wfd)

        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(out), redirect_stderr(err):
                _wait_for_startup_signal(rfd)
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("watcher foo missing", err.getvalue())

    def test_parent_sees_success_on_clean_ok(self):
        """_wait_for_startup_signal must exit(0) on clean 'ok\\n' response."""
        from gateway.daemon import _wait_for_startup_signal

        rfd, wfd = os.pipe()
        os.write(wfd, b"ok\n")
        os.close(wfd)

        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(out):
                _wait_for_startup_signal(rfd)
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("successfully", out.getvalue())


class TestServiceRunFatalHandshake(unittest.IsolatedAsyncioTestCase):
    """GatewayService.run() fatal paths must not emit 'ok' to the handshake pipe."""

    def _make_svc(self):
        svc = GatewayService.__new__(GatewayService)
        svc._registry = MagicMock()
        svc._maps = SimpleNamespace(connector_view={})
        svc._expiry_task = None
        svc._runtime_manager = MagicMock()
        svc._runtime_manager.start_all = AsyncMock(return_value=[])
        svc._runtime_manager.has_active_brokers = False
        svc._runtime_manager.unavailable_agents = set()
        svc._runtime_manager.stop_all = AsyncMock()
        svc._control = MagicMock()
        svc._control.stop = AsyncMock()
        svc._entries = []
        return svc

    async def test_exception_during_startup_no_ok_in_pipe(self):
        """RuntimeError during startup must not produce 'ok' in the pipe."""
        svc = self._make_svc()
        svc._control.start = AsyncMock(side_effect=RuntimeError("boom"))

        rfd, wfd = os.pipe()
        try:
            with self.assertRaises(RuntimeError):
                await svc.run(startup_fd=wfd)
            payload = os.read(rfd, 4096).decode()
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass

        self.assertIn("startup failed: boom", payload)
        self.assertNotIn("ok", payload)

    async def test_cancelled_during_startup_no_ok_in_pipe(self):
        """CancelledError during startup (e.g. SIGTERM) must not produce 'ok'."""
        import asyncio

        svc = self._make_svc()

        async def _cancel_on_start():
            raise asyncio.CancelledError()

        svc._control.start = _cancel_on_start

        rfd, wfd = os.pipe()
        try:
            try:
                await svc.run(startup_fd=wfd)
            except (asyncio.CancelledError, Exception):
                pass
            payload = os.read(rfd, 4096).decode()
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass

        self.assertIn("startup cancelled", payload)
        self.assertNotIn("ok", payload)

    async def test_successful_startup_emits_ok(self):
        """Successful startup must still emit 'ok' so the parent exits 0."""
        import asyncio

        svc = self._make_svc()

        # Control.start() succeeds; the run loop is cancelled immediately after
        start_called = asyncio.Event()

        async def _start_ok():
            start_called.set()

        svc._control.start = _start_ok

        rfd, wfd = os.pipe()
        try:
            task = asyncio.create_task(svc.run(startup_fd=wfd))
            await start_called.wait()
            # Give run() time to write the signal
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            payload = os.read(rfd, 4096).decode()
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass

        self.assertIn("ok", payload)


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round7_fixes.py ────────────────────────────────────────


class TestStartupFdOnCancel(unittest.IsolatedAsyncioTestCase):
    """startup_fd must be closed even if CancelledError is raised during startup."""

    async def test_startup_fd_written_on_cancelled_error(self):
        """_write_startup_signal must be called in finally even after CancelledError."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from gateway.service import GatewayService

        svc = GatewayService.__new__(GatewayService)
        svc._entries = []
        svc._control = MagicMock()
        svc._control.start = AsyncMock(side_effect=asyncio.CancelledError())
        svc._control.stop = AsyncMock()
        svc._runtime_manager = MagicMock()
        svc._runtime_manager.start_all = AsyncMock(return_value=[])
        svc._runtime_manager.has_active_brokers = False
        svc._registry = MagicMock()
        svc._maps = MagicMock()
        svc._maps.connector_view = MagicMock()
        svc._expiry_task = None

        write_signal_calls: list = []

        def fake_write_signal(fd, errors, *, fatal=False):
            write_signal_calls.append((fd, errors))

        with (
            patch("gateway.service._write_startup_signal", side_effect=fake_write_signal),
            patch("gateway.service.ConnectorPermissionNotifier"),
        ):
            try:
                await svc.run(startup_fd=5)
            except (asyncio.CancelledError, Exception):
                pass

        fds_written = [fd for fd, _ in write_signal_calls]
        self.assertIn(5, fds_written, "startup_fd must be written/closed in finally on CancelledError")
