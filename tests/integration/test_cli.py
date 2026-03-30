"""Integration tests for gateway/cli.py.

Exercises the full CLI path: argument parsing → command dispatch → Unix socket
communication → output formatting.  Uses a real Unix socket server running in a
background thread so that ``_send_command_async`` makes an actual network call.

Run with:
    uv run python -m pytest tests/test_cli.py -v
"""

from __future__ import annotations
import pytest

import io
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_main():
    from gateway.cli import main
    return main



pytestmark = pytest.mark.integration

class _MockDaemon:
    """Minimal Unix-socket server that returns canned JSON responses.

    Runs in a background daemon thread so the test's ``asyncio.run()`` call
    (inside ``_send_command_async``) can connect to it synchronously.
    """

    def __init__(self, sock_path: Path, responses: dict):
        self._sock_path = sock_path
        self._responses = responses
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(str(self._sock_path))
        s.listen(10)
        s.settimeout(5.0)
        self._sock = s

        def _serve():
            try:
                while True:
                    try:
                        conn, _ = s.accept()
                    except OSError:
                        return
                    with conn:
                        data = b""
                        while b"\n" not in data:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            data += chunk
                        try:
                            req = json.loads(data.strip())
                        except Exception:
                            conn.sendall(b'{"ok":false,"error":"bad json"}\n')
                            continue
                        cmd = req.get("cmd", "")
                        # Allow a callable for dynamic responses
                        resp = self._responses.get(cmd)
                        if callable(resp):
                            resp = resp(req)
                        elif resp is None:
                            resp = {"ok": False, "error": f"unknown cmd: {cmd}"}
                        conn.sendall(json.dumps(resp).encode() + b"\n")
            except Exception:
                pass

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        self._thread = t
        # Small pause so the socket is ready before the test calls main()
        time.sleep(0.05)

    def stop(self) -> None:
        if self._sock:
            self._sock.close()


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class _CLITestBase(unittest.TestCase):
    """Sets up a temp directory, mock daemon, and argv patching utilities."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock_path = Path(self.tmp) / "control.sock"
        self.pid_file = Path(self.tmp) / "gateway.pid"
        self.log_file = Path(self.tmp) / "gateway.log"
        self._daemon: _MockDaemon | None = None

    def tearDown(self):
        if self._daemon:
            self._daemon.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_daemon(self, responses: dict) -> None:
        self._daemon = _MockDaemon(self.sock_path, responses)
        self._daemon.start()

    def _run(self, args: list[str]) -> tuple[str, str, int]:
        """Run CLI main() with patched argv; return (stdout, stderr, exit_code)."""
        main = _import_main()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code = 0
        with (
            patch("sys.argv", ["acg"] + args),
            patch("gateway.cli.CONTROL_SOCK", self.sock_path),
            patch("gateway.daemon.is_running", return_value=(True, 99999)),
            patch("gateway.daemon.PID_FILE", self.pid_file),
            patch("gateway.daemon.LOG_FILE", self.log_file),
        ):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code


# ---------------------------------------------------------------------------
# Tests: argument parsing edge cases
# ---------------------------------------------------------------------------

class TestCLIArgParsing(unittest.TestCase):
    """Argument parsing: no command → print help + exit 1."""

    def test_no_command_exits_1(self):
        main = _import_main()
        with (
            patch("sys.argv", ["acg"]),
            self.assertRaises(SystemExit) as cm,
        ):
            main()
        self.assertEqual(cm.exception.code, 1)


# ---------------------------------------------------------------------------
# Tests: status command
# ---------------------------------------------------------------------------

class TestCLIStatus(_CLITestBase):
    """status command: outputs running/not-running state."""

    def _write_pid_file(self):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text("99999")

    def test_status_not_running(self):
        """When daemon is not running, print 'not running'."""
        main = _import_main()
        stdout_buf = io.StringIO()
        with (
            patch("sys.argv", ["acg", "status"]),
            patch("gateway.daemon.is_running", return_value=(False, None)),
        ):
            with redirect_stdout(stdout_buf):
                main()
        self.assertIn("not running", stdout_buf.getvalue())

    def test_status_running_shows_pid_and_uptime(self):
        """When daemon is running, print pid, uptime, and watcher count."""
        self._write_pid_file()
        self._start_daemon({"list": {"ok": True, "data": [{"x": 1}, {"x": 2}], "errors": []}})

        stdout, _, code = self._run(["status"])

        self.assertEqual(code, 0)
        self.assertIn("running", stdout)
        self.assertIn("99999", stdout)          # pid shown
        self.assertIn("Watchers: 2", stdout)     # watcher count from list response


# ---------------------------------------------------------------------------
# Tests: list command  ← PRIMARY INTEGRATION TEST
# ---------------------------------------------------------------------------

class TestCLIList(_CLITestBase):
    """list: full integration path through socket, response parsing, formatting."""

    def test_list_normal_path_shows_watchers(self):
        """Normal path: daemon running, watchers returned, output formatted."""
        self._start_daemon({
            "list": {
                "ok": True,
                "data": [
                    {
                        "watcher_name": "support",
                        "room_name": "support-channel",
                        "connector": "rc-prod",
                        "agent_name": "claude",
                        "session_id": "sess-abc123",
                        "active": True,
                        "paused": False,
                    },
                    {
                        "watcher_name": "internal",
                        "room_name": "internal-chat",
                        "connector": "rc-prod",
                        "agent_name": "opencode",
                        "session_id": "sess-def456",
                        "active": True,
                        "paused": True,
                    },
                ],
                "errors": [],
            }
        })

        stdout, stderr, code = self._run(["list"])

        self.assertEqual(code, 0, f"stderr: {stderr}")
        # Both watchers should appear
        self.assertIn("support", stdout)
        self.assertIn("sess-abc123", stdout)
        self.assertIn("internal", stdout)
        self.assertIn("sess-def456", stdout)
        # Paused watcher should show PAUSED status
        self.assertIn("PAUSED", stdout)
        # Active non-paused shows "active"
        self.assertIn("active", stdout)

    def test_list_empty_shows_no_watchers_message(self):
        """When no watchers configured, print the no-watchers message."""
        self._start_daemon({"list": {"ok": True, "data": [], "errors": []}})

        stdout, _, code = self._run(["list"])

        self.assertEqual(code, 0)
        self.assertIn("No configured watchers", stdout)

    def test_list_with_connector_filter(self):
        """--connector flag is forwarded in the command payload."""
        received_cmds: list[dict] = []

        def _capture(req):
            received_cmds.append(req)
            return {"ok": True, "data": [], "errors": []}

        self._start_daemon({"list": _capture})
        self._run(["list", "--connector", "rc-staging"])

        self.assertEqual(len(received_cmds), 1)
        self.assertEqual(received_cmds[0].get("connector"), "rc-staging")

    def test_list_connector_error_exits_nonzero(self):
        """Partial connector failure (errors list) → stderr warning + exit 1."""
        self._start_daemon({
            "list": {
                "ok": True,
                "data": [],
                "errors": [{"connector": "rc-prod", "error": "connection refused"}],
            }
        })

        stdout, stderr, code = self._run(["list"])

        self.assertEqual(code, 1)
        self.assertIn("rc-prod", stderr)


# ---------------------------------------------------------------------------
# Tests: pause / resume / reset commands
# ---------------------------------------------------------------------------

class TestCLIPauseResumeReset(_CLITestBase):
    """pause, resume, reset: success and failure paths."""

    def test_pause_normal_path(self):
        """Successful pause → print confirmation + exit 0."""
        self._start_daemon({"pause": {"ok": True}})
        stdout, _, code = self._run(["pause", "support"])
        self.assertEqual(code, 0)
        self.assertIn("paused", stdout.lower())

    def test_pause_failure_exits_1(self):
        """Failed pause → stderr error + exit 1."""
        self._start_daemon({"pause": {"ok": False, "error": "watcher not found"}})
        _, stderr, code = self._run(["pause", "nonexistent"])
        self.assertEqual(code, 1)
        self.assertIn("watcher not found", stderr)

    def test_resume_normal_path(self):
        """Successful resume → print confirmation + exit 0."""
        self._start_daemon({"resume": {"ok": True}})
        stdout, _, code = self._run(["resume", "support"])
        self.assertEqual(code, 0)
        self.assertIn("resumed", stdout.lower())

    def test_resume_failure_exits_1(self):
        """Failed resume → stderr error + exit 1."""
        self._start_daemon({"resume": {"ok": False, "error": "not paused"}})
        _, stderr, code = self._run(["resume", "support"])
        self.assertEqual(code, 1)

    def test_reset_normal_path(self):
        """Successful reset → print confirmation + exit 0."""
        self._start_daemon({"reset": {"ok": True}})
        stdout, _, code = self._run(["reset", "support"])
        self.assertEqual(code, 0)
        self.assertIn("reset", stdout.lower())

    def test_pause_watcher_name_forwarded(self):
        """watcher_name is forwarded correctly in the socket payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"pause": _capture})
        self._run(["pause", "my-watcher"])
        self.assertEqual(received[0]["watcher_name"], "my-watcher")

    def test_pause_with_connector_filter(self):
        """--connector flag forwarded in pause payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"pause": _capture})
        self._run(["pause", "my-watcher", "--connector", "rc-prod"])
        self.assertEqual(received[0].get("connector"), "rc-prod")


# ---------------------------------------------------------------------------
# Tests: send command
# ---------------------------------------------------------------------------

class TestCLISend(_CLITestBase):
    """send: inline text, --file, validation errors."""

    def test_send_inline_text_normal_path(self):
        """Inline text message dispatched, 'Sent.' printed on success."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"send": _capture})
        stdout, _, code = self._run(["send", "general", "Hello", "world"])

        self.assertEqual(code, 0)
        self.assertIn("Sent.", stdout)
        self.assertEqual(received[0]["text"], "Hello world")
        self.assertEqual(received[0]["room"], "general")

    def test_send_from_file(self):
        """--file reads text from file and sends it."""
        msg_file = Path(self.tmp) / "msg.txt"
        msg_file.write_text("Message from file")

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"send": _capture})
        stdout, _, code = self._run(["send", "general", "--file", str(msg_file)])

        self.assertEqual(code, 0)
        self.assertEqual(received[0]["text"], "Message from file")

    def test_send_file_not_found_exits_1(self):
        """Missing --file → error message + exit 1 (no socket call)."""
        _, stderr, code = self._run(["send", "general", "--file", "/no/such/file.txt"])
        self.assertEqual(code, 1)
        self.assertIn("not found", stderr)

    def test_send_attach_not_found_exits_1(self):
        """Missing --attach → error + exit 1."""
        _, stderr, code = self._run(["send", "general", "hi", "--attach", "/no/file.png"])
        self.assertEqual(code, 1)
        self.assertIn("not found", stderr)

    def test_send_no_message_no_file_no_attach_exits_1(self):
        """Nothing to send → validation error + exit 1."""
        _, stderr, code = self._run(["send", "general"])
        self.assertEqual(code, 1)
        self.assertIn("provide a message", stderr)

    def test_send_inline_and_file_mutual_exclusion(self):
        """Inline text + --file together → error + exit 1."""
        msg_file = Path(self.tmp) / "m.txt"
        msg_file.write_text("x")
        _, stderr, code = self._run(
            ["send", "general", "hello", "--file", str(msg_file)]
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot use both", stderr)

    def test_send_failure_exits_1(self):
        """Daemon returns error → stderr message + exit 1."""
        self._start_daemon({"send": {"ok": False, "error": "room not found"}})
        _, stderr, code = self._run(["send", "unknown-room", "hi"])
        self.assertEqual(code, 1)
        self.assertIn("room not found", stderr)

    def test_send_with_attachment_path_resolved(self):
        """--attach path is resolved to absolute before sending."""
        attach_file = Path(self.tmp) / "img.png"
        attach_file.write_bytes(b"\x89PNG")

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"send": _capture})
        self._run(["send", "general", "caption", "--attach", str(attach_file)])

        self.assertIn("attachment_path", received[0])
        self.assertTrue(Path(received[0]["attachment_path"]).is_absolute())


# ---------------------------------------------------------------------------
# Tests: daemon-not-running path
# ---------------------------------------------------------------------------

class TestCLIDaemonNotRunning(unittest.TestCase):
    """Commands that require the daemon print an error when it's not running."""

    def _run_no_daemon(self, args: list[str]) -> tuple[str, str, int]:
        main = _import_main()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code = 0
        with (
            patch("sys.argv", ["acg"] + args),
            patch("gateway.daemon.is_running", return_value=(False, None)),
        ):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code

    def test_list_when_not_running(self):
        _, stderr, code = self._run_no_daemon(["list"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_pause_when_not_running(self):
        _, stderr, code = self._run_no_daemon(["pause", "foo"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_send_when_not_running(self):
        _, stderr, code = self._run_no_daemon(["send", "general", "hello"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)


if __name__ == "__main__":
    unittest.main()
