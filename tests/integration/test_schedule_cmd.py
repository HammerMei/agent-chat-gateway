"""Integration tests for the 'schedule' CLI subcommands.

Exercises the full CLI path for scheduling:
    argument parsing → command dispatch → Unix socket → mock daemon response

The mock daemon (``_MockDaemon``) is borrowed from test_cli.py and speaks the
same framing protocol: one JSON line in, one JSON line out.  Each test class
targets a specific schedule subcommand; the daemon returns canned JSON matching
what the real GatewayService handlers would return.

Run with:
    uv run python -m pytest tests/integration/test_schedule_cmd.py -v
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers (copied from test_cli.py pattern)
# ---------------------------------------------------------------------------

def _import_main():
    from gateway.cli import main
    return main


class _MockDaemon:
    """Minimal Unix-socket server that returns canned JSON responses.

    Runs in a background daemon thread so the test's ``asyncio.run()`` call
    (inside ``_send_command_async``) can connect to it synchronously.

    ``responses`` maps command strings to either a dict (returned as-is) or a
    callable ``(request_dict) -> response_dict`` for dynamic assertions.
    """

    def __init__(self, sock_path: Path, responses: dict):
        self._sock_path = sock_path
        self._responses = responses
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Remove stale socket file so re-bind (e.g. in round-trip tests) succeeds.
        self._sock_path.unlink(missing_ok=True)
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
        # Unlink the socket file so a subsequent start() can bind to the same path.
        self._sock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class _ScheduleCLITestBase(unittest.TestCase):
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
# Tests: schedule create
# ---------------------------------------------------------------------------

class TestScheduleCreate(_ScheduleCLITestBase):
    """schedule create: argument parsing, cron computation, daemon round-trip."""

    def test_create_basic_every_1h(self):
        """'schedule create' with --every 1h → job created, ID and next_run printed."""
        self._start_daemon({
            "schedule-create": {
                "ok": True,
                "job_id": "acg-aabbccdd",
                "next_run": "2026-04-09T10:00:00+00:00",
            }
        })

        stdout, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Please generate the daily summary",
            "--every", "1h",
        ])

        self.assertEqual(code, 0, f"stderr: {stderr}")
        self.assertIn("acg-aabbccdd", stdout)
        self.assertIn("2026-04-09T10:00:00", stdout)

    def test_create_with_times_stores_correct_values(self):
        """--every 1h --times 3 → daemon receives cron='0 * * * *' and times=3."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-11223344", "next_run": "2026-04-09T11:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        stdout, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Summarize today",
            "--every", "1h",
            "--times", "3",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(len(received), 1)
        req = received[0]
        self.assertEqual(req["cron"], "0 * * * *")   # --every 1h default cron
        self.assertEqual(req["times"], 3)
        self.assertEqual(req["watcher"], "e2e-dm")
        self.assertEqual(req["message"], "Summarize today")

    def test_create_watcher_and_message_forwarded(self):
        """Watcher name and message text are forwarded verbatim to the daemon."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-deadbeef", "next_run": "2026-04-09T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        self._run([
            "schedule", "create", "e2e-claude-channel",
            "Run the weekly report",
            "--every", "1w",
        ])

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["watcher"], "e2e-claude-channel")
        self.assertEqual(received[0]["message"], "Run the weekly report")

    def test_create_one_shot_at_future_date(self):
        """--at 'YYYY-MM-DD HH:MM' with no --every → one-shot job, times=1 enforced."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-0f0f0f0f", "next_run": "2099-01-01T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        stdout, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Happy new year!",
            "--at", "2099-01-01 09:00",
        ])

        self.assertEqual(code, 0, f"stderr: {stderr}")
        # One-shot: times must be 1 (enforced by CLI when --every is absent)
        self.assertEqual(received[0]["times"], 1)
        # Cron should be "0 9 1 1 *" — day=1, month=1, minute=0, hour=9
        self.assertEqual(received[0]["cron"], "0 9 1 1 *")
        self.assertIn("acg-0f0f0f0f", stdout)

    def test_create_with_timezone(self):
        """--tz is forwarded in the command payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-tz000001", "next_run": "2026-04-09T01:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        self._run([
            "schedule", "create", "e2e-dm",
            "Daily standup",
            "--every", "1d",
            "--tz", "Asia/Taipei",
        ])

        self.assertEqual(received[0].get("timezone"), "Asia/Taipei")

    def test_create_with_connector_filter(self):
        """--connector is forwarded in the command payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "job_id": "acg-c0000001", "next_run": "2026-04-09T09:00:00+00:00"}

        self._start_daemon({"schedule-create": _capture})

        self._run([
            "schedule", "create", "e2e-dm",
            "Hello",
            "--every", "1h",
            "--connector", "rc-e2e",
        ])

        self.assertEqual(received[0].get("connector"), "rc-e2e")

    def test_create_invalid_watcher_daemon_returns_error(self):
        """Daemon returning error (e.g. unknown watcher) → stderr error + exit 1."""
        self._start_daemon({
            "schedule-create": {
                "ok": False,
                "error": "watcher 'no-such-watcher' not found",
            }
        })

        _, stderr, code = self._run([
            "schedule", "create", "no-such-watcher",
            "Does not matter",
            "--every", "1h",
        ])

        self.assertEqual(code, 1)
        self.assertIn("no-such-watcher", stderr)

    def test_create_no_every_no_at_exits_1(self):
        """Neither --every nor --at → validation error + exit 1 (no socket call)."""
        # No daemon needed — argument validation fires before the socket call.
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Something",
        ])
        self.assertEqual(code, 1)
        self.assertIn("--every", stderr)

    def test_create_invalid_interval_exits_1(self):
        """Unsupported --every value → validation error + exit 1."""
        _, stderr, code = self._run([
            "schedule", "create", "e2e-dm",
            "Something",
            "--every", "7d",  # not a valid interval key
        ])
        self.assertEqual(code, 1)
        self.assertIn("Unsupported interval", stderr)

    def test_create_cron_mapping(self):
        """Each supported --every value produces the correct default cron expression.

        Uses unittest.subTest so each interval is reported independently.
        (Note: @pytest.mark.parametrize cannot inject arguments into unittest.TestCase
        methods — use subTest instead.)
        """
        cases = [
            ("1m",  "* * * * *"),
            ("30m", "*/30 * * * *"),
            ("1h",  "0 * * * *"),
            ("6h",  "0 */6 * * *"),
            ("1d",  "0 9 * * *"),
            ("1w",  "0 9 * * 1"),
        ]
        for interval, expected_cron in cases:
            with self.subTest(interval=interval):
                received: list[dict] = []

                def _capture(req, _recv=received):
                    _recv.append(req)
                    return {
                        "ok": True,
                        "job_id": "acg-cccc0001",
                        "next_run": "2026-04-09T09:00:00+00:00",
                    }

                self._start_daemon({"schedule-create": _capture})

                _, _, code = self._run([
                    "schedule", "create", "e2e-dm",
                    "test cron mapping",
                    "--every", interval,
                ])

                # Some intervals may produce a warning on stderr for sub-hourly --at
                # combinations; focus only on the cron value sent to the daemon.
                self.assertEqual(code, 0)
                self.assertEqual(
                    len(received), 1, f"Daemon not called for interval {interval!r}"
                )
                self.assertEqual(
                    received[0]["cron"],
                    expected_cron,
                    f"Wrong cron for --every {interval}: "
                    f"expected {expected_cron!r}, got {received[0]['cron']!r}",
                )

                if self._daemon:
                    self._daemon.stop()
                    self._daemon = None

    def test_create_one_shot_relative_uses_exact_datetime_cron(self):
        """--every 5m --times 1 → one-shot cron (MM HH DD MM *), NOT */5 * * * *.

        Regression test for the "fires too early" bug:
        */5 * * * * fires at the next :05/:10/… boundary (could be 0–5 min away),
        not exactly 5 minutes from now.  The fix computes now + 5m and generates a
        specific datetime cron so the job fires at the correct time.

        Also verifies that arbitrary non-cron-aligned intervals (7m, 23m, 90m) are
        accepted for one-shot jobs.
        """
        import re
        from datetime import UTC, datetime, timedelta

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "job_id": "acg-oneshot01",
                "next_run": "2026-04-09T07:47:00+00:00",
            }

        self._start_daemon({"schedule-create": _capture})

        before = datetime.now(UTC)
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Remind me in 5 minutes",
            "--every", "5m",
            "--times", "1",
        ])
        after = datetime.now(UTC)

        self.assertEqual(code, 0)
        self.assertEqual(len(received), 1)

        cron = received[0]["cron"]

        # Must NOT be the periodic pattern
        self.assertNotEqual(
            cron,
            "*/5 * * * *",
            "One-shot job must not use periodic cron */5 * * * *",
        )

        # Must be a 5-field specific datetime cron: M H D Mo *
        m = re.fullmatch(r"(\d+) (\d+) (\d+) (\d+) \*", cron)
        self.assertIsNotNone(m, f"Expected datetime cron 'M H D Mo *', got: {cron!r}")

        # Parse and verify the fire time is approximately now + 5 minutes.
        # Cron has 1-minute granularity (seconds are truncated), so we allow
        # a ±1-minute window: fire must be in [now+4m, now+6m].
        minute, hour, day, month = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        year = before.year
        fire_dt = datetime(year, month, day, hour, minute, tzinfo=UTC)
        window_low = before + timedelta(minutes=4)
        window_high = after + timedelta(minutes=6)
        self.assertGreaterEqual(
            fire_dt, window_low,
            f"Fire time {fire_dt} is more than 1m too early (expected >= {window_low})",
        )
        self.assertLessEqual(
            fire_dt, window_high,
            f"Fire time {fire_dt} is more than 1m too late (expected <= {window_high})",
        )

        # Timezone must be UTC — cron coordinates are UTC and the daemon must
        # not apply a local-timezone offset (regression: UTC-7 was shifting the
        # fire time 7 hours forward instead of N minutes).
        self.assertEqual(
            received[0].get("timezone"),
            "UTC",
            "One-shot relative reminders must always send timezone=UTC",
        )

    def test_create_one_shot_arbitrary_interval_7m(self):
        """--every 7m --times 1 is accepted and produces a specific datetime cron.

        7m is not in _INTERVAL_MAP (not cron-aligned), but is valid for one-shot
        jobs since we compute now+7m directly.
        """
        import re
        from datetime import UTC, datetime, timedelta

        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "job_id": "acg-7m000001",
                "next_run": "2026-04-09T07:49:00+00:00",
            }

        self._start_daemon({"schedule-create": _capture})

        before = datetime.now(UTC)
        _, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Remind me in 7 minutes",
            "--every", "7m",
            "--times", "1",
        ])
        after = datetime.now(UTC)

        self.assertEqual(code, 0, "7m one-shot job should be accepted")
        self.assertEqual(len(received), 1)

        cron = received[0]["cron"]
        m = re.fullmatch(r"(\d+) (\d+) (\d+) (\d+) \*", cron)
        self.assertIsNotNone(m, f"Expected specific datetime cron, got: {cron!r}")

        minute, hour, day, month = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        fire_dt = datetime(before.year, month, day, hour, minute, tzinfo=UTC)
        self.assertGreaterEqual(fire_dt, before + timedelta(minutes=6))
        self.assertLessEqual(fire_dt, after + timedelta(minutes=8))

        # Timezone must be UTC to prevent local-tz double-apply.
        self.assertEqual(received[0].get("timezone"), "UTC")

    def test_create_recurring_out_of_range_interval_rejected(self):
        """Out-of-range intervals are rejected for recurring jobs."""
        # 60m is out of range (1–59 only); 0m is also invalid.
        for bad_interval in ("60m", "0m", "24h", "bad"):
            with self.subTest(interval=bad_interval):
                _, stderr, code = self._run([
                    "schedule", "create", "e2e-dm",
                    "Invalid interval",
                    "--every", bad_interval,
                ])
                self.assertEqual(code, 1, f"--every {bad_interval} should fail")
                self.assertTrue(
                    len(stderr) > 0,
                    f"Expected an error message for --every {bad_interval}",
                )


# ---------------------------------------------------------------------------
# Tests: schedule list
# ---------------------------------------------------------------------------

class TestScheduleList(_ScheduleCLITestBase):
    """schedule list: tabular output, empty state, --all flag."""

    def _sample_active_job(self, job_id: str = "acg-11111111", watcher: str = "e2e-dm") -> dict:
        return {
            "id": job_id,
            "watcher": watcher,
            "connector": "rc-e2e",
            "message": "Daily summary",
            "cron": "0 9 * * *",
            "timezone": "UTC",
            "times": 0,
            "run_count": 2,
            "status": "active",
            "created_at": "2026-04-08T00:00:00+00:00",
            "next_run": "2026-04-09T09:00:00+00:00",
            "last_run": "2026-04-08T09:00:00+00:00",
            "completed_at": None,
        }

    def _sample_completed_job(self, job_id: str = "acg-22222222") -> dict:
        return {
            "id": job_id,
            "watcher": "e2e-dm",
            "connector": "rc-e2e",
            "message": "One-shot task",
            "cron": "0 9 8 4 *",
            "timezone": "UTC",
            "times": 1,
            "run_count": 1,
            "status": "completed",
            "created_at": "2026-04-08T00:00:00+00:00",
            "next_run": None,
            "last_run": "2026-04-08T09:00:00+00:00",
            "completed_at": "2026-04-08T09:00:05+00:00",
        }

    def test_list_shows_active_jobs(self):
        """Normal list path: active jobs appear in tabular output."""
        self._start_daemon({
            "schedule-list": {
                "ok": True,
                "jobs": [self._sample_active_job()],
            }
        })

        stdout, _, code = self._run(["schedule", "list"])

        self.assertEqual(code, 0)
        self.assertIn("acg-11111111", stdout)
        self.assertIn("e2e-dm", stdout)
        self.assertIn("active", stdout)

    def test_list_empty_shows_no_tasks_message(self):
        """When no jobs exist, print the 'no scheduled tasks' message."""
        self._start_daemon({
            "schedule-list": {"ok": True, "jobs": []}
        })

        stdout, _, code = self._run(["schedule", "list"])

        self.assertEqual(code, 0)
        self.assertIn("No scheduled tasks", stdout)

    def test_list_all_includes_completed_jobs(self):
        """--all flag → include_completed=True forwarded; completed jobs shown."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {
                "ok": True,
                "jobs": [
                    self._sample_active_job(),
                    self._sample_completed_job(),
                ],
            }

        self._start_daemon({"schedule-list": _capture})

        stdout, _, code = self._run(["schedule", "list", "--all"])

        self.assertEqual(code, 0)
        self.assertTrue(received[0].get("include_completed"), "include_completed not set in request")
        self.assertIn("acg-22222222", stdout)
        self.assertIn("completed", stdout)

    def test_list_default_omits_completed_jobs(self):
        """Without --all, include_completed defaults to False."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "jobs": []}

        self._start_daemon({"schedule-list": _capture})
        self._run(["schedule", "list"])

        self.assertFalse(received[0].get("include_completed"), "include_completed should be False by default")

    def test_list_connector_filter_forwarded(self):
        """--connector flag is forwarded in the schedule-list payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "jobs": []}

        self._start_daemon({"schedule-list": _capture})
        self._run(["schedule", "list", "--connector", "rc-e2e"])

        self.assertEqual(received[0].get("connector"), "rc-e2e")

    def test_list_failure_exits_1(self):
        """Daemon error on schedule-list → stderr + exit 1."""
        self._start_daemon({
            "schedule-list": {"ok": False, "error": "internal error"}
        })

        _, stderr, code = self._run(["schedule", "list"])

        self.assertEqual(code, 1)
        self.assertIn("internal error", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule pause
# ---------------------------------------------------------------------------

class TestSchedulePause(_ScheduleCLITestBase):
    """schedule pause: success, failure, payload forwarding."""

    def test_pause_normal_path(self):
        """Successful pause → confirmation printed + exit 0."""
        self._start_daemon({"schedule-pause": {"ok": True}})

        stdout, _, code = self._run(["schedule", "pause", "acg-aabbccdd"])

        self.assertEqual(code, 0)
        self.assertIn("acg-aabbccdd", stdout)
        self.assertIn("paused", stdout.lower())

    def test_pause_job_id_forwarded(self):
        """job_id is forwarded correctly in the socket payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"schedule-pause": _capture})
        self._run(["schedule", "pause", "acg-deadbeef"])

        self.assertEqual(received[0]["job_id"], "acg-deadbeef")

    def test_pause_failure_exits_1(self):
        """Daemon error (job not found) → stderr + exit 1."""
        self._start_daemon({
            "schedule-pause": {"ok": False, "error": "job 'acg-nope' not found"}
        })

        _, stderr, code = self._run(["schedule", "pause", "acg-nope"])

        self.assertEqual(code, 1)
        self.assertIn("acg-nope", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule resume
# ---------------------------------------------------------------------------

class TestScheduleResume(_ScheduleCLITestBase):
    """schedule resume: success, failure, next_run shown."""

    def test_resume_normal_path(self):
        """Successful resume → confirmation + next_run printed + exit 0."""
        self._start_daemon({
            "schedule-resume": {
                "ok": True,
                "next_run": "2026-04-09T10:00:00+00:00",
            }
        })

        stdout, _, code = self._run(["schedule", "resume", "acg-aabbccdd"])

        self.assertEqual(code, 0)
        self.assertIn("acg-aabbccdd", stdout)
        self.assertIn("resumed", stdout.lower())
        self.assertIn("2026-04-09T10:00:00", stdout)

    def test_resume_job_id_forwarded(self):
        """job_id is forwarded correctly in the socket payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True, "next_run": "2026-04-09T10:00:00+00:00"}

        self._start_daemon({"schedule-resume": _capture})
        self._run(["schedule", "resume", "acg-deadbeef"])

        self.assertEqual(received[0]["job_id"], "acg-deadbeef")

    def test_resume_failure_exits_1(self):
        """Daemon error on resume → stderr + exit 1."""
        self._start_daemon({
            "schedule-resume": {"ok": False, "error": "job 'acg-nope' not found"}
        })

        _, stderr, code = self._run(["schedule", "resume", "acg-nope"])

        self.assertEqual(code, 1)
        self.assertIn("acg-nope", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule delete
# ---------------------------------------------------------------------------

class TestScheduleDelete(_ScheduleCLITestBase):
    """schedule delete: success, failure, payload forwarding."""

    def test_delete_normal_path(self):
        """Successful delete → confirmation printed + exit 0."""
        self._start_daemon({"schedule-delete": {"ok": True}})

        stdout, _, code = self._run(["schedule", "delete", "acg-aabbccdd"])

        self.assertEqual(code, 0)
        self.assertIn("acg-aabbccdd", stdout)
        self.assertIn("deleted", stdout.lower())

    def test_delete_job_id_forwarded(self):
        """job_id is forwarded correctly in the socket payload."""
        received: list[dict] = []

        def _capture(req):
            received.append(req)
            return {"ok": True}

        self._start_daemon({"schedule-delete": _capture})
        self._run(["schedule", "delete", "acg-deadbeef"])

        self.assertEqual(received[0]["job_id"], "acg-deadbeef")

    def test_delete_failure_exits_1(self):
        """Daemon error (job not found) → stderr + exit 1."""
        self._start_daemon({
            "schedule-delete": {"ok": False, "error": "job 'acg-gone' not found"}
        })

        _, stderr, code = self._run(["schedule", "delete", "acg-gone"])

        self.assertEqual(code, 1)
        self.assertIn("acg-gone", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule create → list round-trip (multi-step)
# ---------------------------------------------------------------------------

class TestScheduleRoundTrip(_ScheduleCLITestBase):
    """Multi-step scenario: create → list → pause → resume → delete."""

    def test_create_then_list_shows_job(self):
        """Create a job then list — the created job_id appears in list output."""
        created_id = "acg-55667788"
        created_job = {
            "id": created_id,
            "watcher": "e2e-dm",
            "connector": "rc-e2e",
            "message": "Summarize my week",
            "cron": "0 9 * * 1",
            "timezone": "UTC",
            "times": 0,
            "run_count": 0,
            "status": "active",
            "created_at": "2026-04-08T00:00:00+00:00",
            "next_run": "2026-04-14T09:00:00+00:00",
            "last_run": None,
            "completed_at": None,
        }

        # Step 1: create
        self._start_daemon({
            "schedule-create": {
                "ok": True,
                "job_id": created_id,
                "next_run": "2026-04-14T09:00:00+00:00",
            }
        })

        stdout, _, code = self._run([
            "schedule", "create", "e2e-dm",
            "Summarize my week",
            "--every", "1w",
        ])
        self.assertEqual(code, 0)
        self.assertIn(created_id, stdout)

        # Step 2: list shows the new job
        if self._daemon:
            self._daemon.stop()
        self._start_daemon({
            "schedule-list": {"ok": True, "jobs": [created_job]}
        })

        stdout, _, code = self._run(["schedule", "list"])
        self.assertEqual(code, 0)
        self.assertIn(created_id, stdout)

    def test_pause_then_resume_status_transitions(self):
        """Pause then resume: status transitions active → paused → active."""
        job_id = "acg-99aabbcc"

        # Pause
        self._start_daemon({"schedule-pause": {"ok": True}})
        stdout, _, code = self._run(["schedule", "pause", job_id])
        self.assertEqual(code, 0)
        self.assertIn("paused", stdout.lower())

        # Resume
        if self._daemon:
            self._daemon.stop()
        self._start_daemon({
            "schedule-resume": {
                "ok": True,
                "next_run": "2026-04-09T10:00:00+00:00",
            }
        })
        stdout, _, code = self._run(["schedule", "resume", job_id])
        self.assertEqual(code, 0)
        self.assertIn("resumed", stdout.lower())

    def test_delete_then_list_shows_empty(self):
        """Delete a job then list — no jobs remain."""
        job_id = "acg-ddddeeeef"

        # Delete
        self._start_daemon({"schedule-delete": {"ok": True}})
        stdout, _, code = self._run(["schedule", "delete", job_id])
        self.assertEqual(code, 0)

        # List — empty
        if self._daemon:
            self._daemon.stop()
        self._start_daemon({"schedule-list": {"ok": True, "jobs": []}})

        stdout, _, code = self._run(["schedule", "list"])
        self.assertEqual(code, 0)
        self.assertIn("No scheduled tasks", stdout)


# ---------------------------------------------------------------------------
# Tests: daemon-not-running path for schedule subcommands
# ---------------------------------------------------------------------------

class TestScheduleCLIDaemonNotRunning(unittest.TestCase):
    """schedule subcommands print an error when the daemon isn't running."""

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

    def test_schedule_list_when_not_running(self):
        """schedule list when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon(["schedule", "list"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_schedule_create_when_not_running(self):
        """schedule create when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon([
            "schedule", "create", "e2e-dm", "hello", "--every", "1h"
        ])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_schedule_pause_when_not_running(self):
        """schedule pause when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon(["schedule", "pause", "acg-aabbccdd"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_schedule_resume_when_not_running(self):
        """schedule resume when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon(["schedule", "resume", "acg-aabbccdd"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)

    def test_schedule_delete_when_not_running(self):
        """schedule delete when daemon is not running → 'not running' error + exit 1."""
        _, stderr, code = self._run_no_daemon(["schedule", "delete", "acg-aabbccdd"])
        self.assertEqual(code, 1)
        self.assertIn("not running", stderr)


# ---------------------------------------------------------------------------
# Tests: schedule subcommand routing
# ---------------------------------------------------------------------------

class TestScheduleSubcommandRouting(unittest.TestCase):
    """Verify that 'schedule' with no subcommand exits 1 and shows usage."""

    def test_schedule_no_subcommand_exits_1(self):
        """acg schedule with no subcommand → usage message + exit 1."""
        main = _import_main()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code = 0
        with (
            patch("sys.argv", ["acg", "schedule"]),
            patch("gateway.daemon.is_running", return_value=(True, 99999)),
        ):
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        self.assertEqual(exit_code, 1)
        combined = stdout_buf.getvalue() + stderr_buf.getvalue()
        self.assertIn("schedule", combined.lower())


if __name__ == "__main__":
    unittest.main()
