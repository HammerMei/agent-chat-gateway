"""Integration tests for gateway/cli.py.

Exercises the full CLI path: argument parsing → command dispatch → Unix socket
communication → output formatting.  Uses a real Unix socket server running in a
background thread so that ``_send_command_async`` makes an actual network call.

Run with:
    uv run python -m pytest tests/test_cli.py -v
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

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


class TestCLIInstructions(_CLITestBase):
    """instructions: print bundled docs without contacting the daemon."""

    def test_instructions_scheduling_prints_scheduling_doc(self):
        stdout, stderr, code = self._run(["instructions", "scheduling"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("# ACG Scheduling Commands", stdout)
        self.assertIn("agent-chat-gateway schedule create", stdout)

    def test_instructions_fetch_history_prints_fetch_history_doc(self):
        stdout, stderr, code = self._run(["instructions", "fetch-history"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("# fetch-history", stdout)
        self.assertIn("agent-chat-gateway fetch-history", stdout)


# ---------------------------------------------------------------------------
# Tests: config (no subcommand) — launches the interactive config TUI
# ---------------------------------------------------------------------------

class TestCLIConfigLaunchesTUI(_CLITestBase):
    """'config' with no subcommand launches gateway.configtool.run_app.

    _run() redirects stdout/stderr to io.StringIO, which is never a TTY, so
    every case here exercises run_app's own TTY guard — the same guard a
    real piped/non-interactive invocation would hit. A test that needs to
    verify the *arguments* run_app receives patches run_app itself rather
    than trying to actually launch a full-screen Textual app in a test.
    """

    def test_no_subcommand_hits_tty_guard_and_exits_one(self):
        stdout, stderr, code = self._run(["config"])
        self.assertEqual(code, 1)
        self.assertIn("requires an interactive terminal", stderr)

    def test_no_subcommand_does_not_print_old_usage_message(self):
        """Regression: before this change, no-subcommand printed a plain
        usage string and exited 1 — it must now attempt to launch the TUI
        (and hit the TTY guard under test) instead."""
        stdout, stderr, code = self._run(["config"])
        self.assertNotIn("Usage: agent-chat-gateway config", stdout + stderr)

    def test_config_and_lint_flags_are_forwarded_to_run_app(self):
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 0
            self._run(["config", "--config", "/tmp/example-config.yaml", "--lint"])
        mock_run_app.assert_called_once_with("/tmp/example-config.yaml", lint=True)

    def test_lint_defaults_to_false(self):
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 0
            self._run(["config", "--config", "/tmp/example-config.yaml"])
        mock_run_app.assert_called_once_with("/tmp/example-config.yaml", lint=False)

    def test_default_config_path_used_when_omitted(self):
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 0
            self._run(["config"])
        from gateway.cli import DEFAULT_CONFIG
        mock_run_app.assert_called_once_with(DEFAULT_CONFIG, lint=False)

    def test_exit_code_propagates_from_run_app(self):
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 1
            _, _, code = self._run(["config"])
        self.assertEqual(code, 1)

    def test_validate_subcommand_still_dispatches_normally_not_to_tui(self):
        """Non-regression: 'config validate' must never fall through to
        run_app — the two dispatch paths must stay mutually exclusive."""
        with patch("gateway.configtool.run_app") as mock_run_app:
            cfg_path = Path(self.tmp) / "config.yaml"
            cfg_path.write_text("connectors: []\nagents: {}\n")
            with patch("gateway.core.state.RUNTIME_DIR", Path(self.tmp) / "runtime"):
                self._run(["config", "validate", "--config", str(cfg_path)])
        mock_run_app.assert_not_called()

    def test_lint_before_subcommand_does_not_leak_into_validate(self):
        """Regression: --lint used to share a dest with config_validate_p's
        own --lint, so argparse's subparser dispatch silently overwrote it —
        'config --lint validate' parsed to lint=False for validate_config
        even though the flag was given. Now the two are independent, scoped
        attributes (lint_for_tui vs. validate's own lint) — placing --lint
        before the subcommand must not affect the subcommand's own value."""
        with patch("gateway.config_validate.validate_config") as mock_validate:
            mock_validate.return_value.ok = True
            mock_validate.return_value.errors = []
            mock_validate.return_value.warnings = []
            mock_validate.return_value.lint_findings = []
            mock_validate.return_value.entry_count = 0
            mock_validate.return_value.watcher_count = 0
            self._run(["config", "--lint", "validate", "--config", "/tmp/x.yaml"])
        mock_validate.assert_called_once_with("/tmp/x.yaml", lint=False)

    def test_lint_before_subcommand_sets_tui_lint_when_no_subcommand_given(self):
        """The parent --lint (scoped to launching the TUI) still works
        correctly on its own, independent of the child's own --lint."""
        with patch("gateway.configtool.run_app") as mock_run_app:
            mock_run_app.return_value = 0
            self._run(["config", "--lint", "--config", "/tmp/x.yaml"])
        mock_run_app.assert_called_once_with("/tmp/x.yaml", lint=True)


# ---------------------------------------------------------------------------
# Tests: config validate command
# ---------------------------------------------------------------------------

class TestCLIConfigValidate(_CLITestBase):
    """config validate: validate config.yaml without contacting the daemon.

    gateway.core.state.RUNTIME_DIR is patched to a per-test temp dir in every
    case — otherwise the state-orphan check would read this machine's real
    ~/.agent-chat-gateway/state.*.json files and make the test non-hermetic.
    """

    def setUp(self):
        super().setUp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()
        self.runtime_dir = Path(self.tmp) / "runtime"

    def _write(self, yaml_text: str) -> str:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return str(path)

    def _run_validate(self, extra_args: list[str] | None = None, config_path: str | None = None):
        args = ["config", "validate", "--config", config_path] + (extra_args or [])
        with patch("gateway.core.state.RUNTIME_DIR", self.runtime_dir):
            return self._run(args)

    def test_valid_config_exits_zero(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("✓", stdout)
        self.assertIn("1 watcher(s)", stdout)

    def test_missing_working_directory_exits_one(self):
        cfg_path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
            watchers:
              - name: w1
                room: general
        """)
        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 1)
        self.assertIn("working_directory is required", stderr)

    def test_empty_rocketchat_credentials_flagged_as_errors(self):
        """from_connector_config silently defaults server.url/username/password
        to "" — config_validate.py must catch what from_file alone does not."""
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 1)
        self.assertIn("server.url is empty", stderr)
        self.assertIn("server.username is empty", stderr)
        self.assertIn("server.password is empty", stderr)

    def test_lint_flags_redundant_default(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
                timeout: 360
            watchers:
              - name: w1
                room: general
        """)
        stdout, stderr, code = self._run_validate(["--lint"], config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("agents.default.timeout", stdout)
        self.assertIn("restates the built-in default", stdout)

    def test_lint_with_no_findings_says_so(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        stdout, stderr, code = self._run_validate(["--lint"], config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("no redundant defaults found", stdout)

    def test_rooms_expansion_reflected_in_summary(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - connector: rc
                rooms: [general, dev]
        """)
        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("2 watcher(s)", stdout)
        self.assertIn("expanded from 1 entries", stdout)

    def test_state_orphan_produces_warning(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.rc.json").write_text(json.dumps({
            "watchers": [{"watcher_name": "stale-watcher", "session_id": "x", "room_id": "y"}]
        }))

        stdout, stderr, code = self._run_validate(config_path=cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("stale-watcher", stdout)
        self.assertIn("dropped on next start", stdout)


class TestCLIConfigMigrateEnv(_CLITestBase):
    """config migrate-env: standalone entry point for the same one-time
    migration gateway/daemon.py's start_daemon() runs automatically."""

    def setUp(self):
        super().setUp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()

    def _write(self, yaml_text: str) -> str:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return str(path)

    def _run_migrate(self, config_path: str):
        return self._run(["config", "migrate-env", "--config", config_path])

    def test_no_env_file_reports_nothing_to_do(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        stdout, stderr, code = self._run_migrate(cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("Nothing to migrate", stdout)

    def test_migrates_and_reports_the_reference_count(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: "${{RC_PASSWORD}}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        (Path(self.tmp) / ".env").write_text("RC_PASSWORD=hunter2\n")

        stdout, stderr, code = self._run_migrate(cfg_path)

        self.assertEqual(code, 0)
        self.assertIn("Migrated 1 secret reference(s)", stdout)
        self.assertFalse((Path(self.tmp) / ".env").exists())
        raw = yaml.safe_load(Path(cfg_path).read_text())
        self.assertEqual(raw["connectors"][0]["server"]["password"], "hunter2")

    def test_unresolvable_reference_exits_nonzero(self):
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: "${{MISSING_VAR}}"}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)
        (Path(self.tmp) / ".env").write_text("UNRELATED=1\n")

        stdout, stderr, code = self._run_migrate(cfg_path)

        self.assertEqual(code, 1)
        self.assertIn("Migration failed", stderr)
        self.assertTrue((Path(self.tmp) / ".env").exists())

    def test_plain_oserror_is_caught_cleanly_not_a_raw_traceback(self):
        """Code-review finding: the original except clause only caught
        (ValueError, FileNotFoundError) — a plain OSError (e.g. a
        PermissionError from env_path.rename()) would have crashed with an
        unhandled traceback instead of the clean '✗ Migration failed' message."""
        cfg_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watchers:
              - name: w1
                room: general
        """)

        with patch(
            "gateway.config_migrate.migrate_env_to_config",
            side_effect=OSError("disk full"),
        ):
            stdout, stderr, code = self._run_migrate(cfg_path)

        self.assertEqual(code, 1)
        self.assertIn("Migration failed", stderr)
        self.assertIn("disk full", stderr)


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
