"""Daemon lifecycle: fork, PID file, signal handling."""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .config import GatewayConfig
from .runtime_lock import LOCK_FILE as PID_FILE  # noqa: F401 — re-exported for cli.py
from .runtime_lock import RUNTIME_DIR, locked_pid
from .runtime_lock import acquire as _lock_acquire
from .runtime_lock import release as _lock_release
from .service import GatewayService

logger = logging.getLogger("agent-chat-gateway.daemon")

# RUNTIME_DIR is imported from runtime_lock — single source of truth.
# PID_FILE imported from runtime_lock (shared with control.py to break circular import)
LOG_FILE = RUNTIME_DIR / "gateway.log"


def _setup_logging() -> None:
    """Configure file-based logging for the daemon."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        ],
    )


def is_running() -> tuple[bool, int | None]:
    """Check if daemon is running. Returns (is_running, pid)."""
    pid = locked_pid()
    if pid is None:
        # Stale or absent lock file — clean up so future checks don't read it.
        # Use _lock_release() (not PID_FILE.unlink()) so the call goes through
        # runtime_lock.LOCK_FILE, which is patchable in tests.
        _lock_release()
        return False, None
    return True, pid


def _wait_for_startup_signal(read_fd: int) -> None:
    """Block reading from the startup pipe until the daemon closes it.

    Protocol written by the daemon:
      - Zero or more ``error:<message>\\n`` lines for non-fatal startup failures.
      - A final ``ok\\n`` line to confirm the startup sequence completed.

    If the pipe closes without an ``ok`` line the daemon crashed before completing
    startup (e.g. config error, unhandled exception).  In that case we report
    failure and exit 1.

    This function never returns — it always calls sys.exit().
    """
    data = b""
    try:
        while chunk := os.read(read_fd, 4096):
            data += chunk
    except OSError:
        pass
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass

    lines = data.decode(errors="replace").splitlines()
    errors = [line[len("error:"):].strip() for line in lines if line.startswith("error:")]
    has_ok = any(line.strip() == "ok" for line in lines)

    if not has_ok:
        print(
            f"Gateway failed to start — check logs at {LOG_FILE}",
            file=sys.stderr,
        )
        sys.exit(1)

    if errors:
        print("Gateway started with warnings:", file=sys.stderr)
        for e in errors:
            print(f"  ⚠ {e}", file=sys.stderr)
        print(
            f"Some components may be degraded. Check {LOG_FILE} for details.",
            file=sys.stderr,
        )
        print("Gateway started (degraded).")
        sys.exit(0)

    print("Gateway started successfully.")
    sys.exit(0)


def start_daemon(config_path: str) -> None:
    """Daemonize and run the gateway service.

    Blocks the parent process until the daemon has completed its full startup
    sequence (sidecars healthy, brokers running, all watchers started).  Prints
    a clear success/failure message and exits with the appropriate code.
    """
    running, pid = is_running()
    if running:
        print(f"Gateway already running (pid={pid})")
        sys.exit(1)

    # Resolve paths before forking
    config_path = str(Path(config_path).resolve())
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    # Startup handshake pipe:
    #   read_fd  — parent blocks here until daemon signals startup complete
    #   write_fd — daemon writes "error:<msg>\n"* + "ok\n" after startup, then closes
    read_fd, write_fd = os.pipe()

    # ── First fork ────────────────────────────────────────────────────────────
    pid = os.fork()
    if pid > 0:
        # Parent: close the write end and wait for the daemon to report startup.
        os.close(write_fd)
        _wait_for_startup_signal(read_fd)  # blocks; never returns

    # ── First child ───────────────────────────────────────────────────────────
    os.setsid()

    # ── Second fork (prevent controlling terminal reacquisition) ─────────────
    pid = os.fork()
    if pid > 0:
        # Intermediate process: close both ends and exit.
        # Must close write_fd explicitly so the parent only unblocks when the
        # actual daemon (second child) closes its copy.
        os.close(write_fd)
        os.close(read_fd)
        sys.exit(0)

    # ── Daemon process (second child) ─────────────────────────────────────────
    # Close the read end — daemon only writes.
    os.close(read_fd)

    # Redirect stdio → /dev/null for stdin, log file for stdout/stderr
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = open(os.devnull, "r")
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    devnull.close()  # FD duplicated into stdin; close the original to avoid leak
    log_fd = open(str(LOG_FILE), "a")
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())
    log_fd.close()  # FD duplicated into stdout/stderr; close the original to avoid leak

    # Setup logging FIRST — must happen before any operation that could fail,
    # so errors are captured in the log file AND the parent receives a proper
    # error message instead of blocking forever on the startup pipe.
    _setup_logging()
    logger.info("Daemon started (pid=%d)", os.getpid())

    # Acquire the runtime lock (writes PID file atomically).
    # Must run AFTER _setup_logging() so any failure is both logged and
    # signalled to the parent before the process exits.
    try:
        _lock_acquire()
    except Exception as e:
        logger.error("Failed to acquire runtime lock: %s", e)
        try:
            os.write(write_fd, f"error:Failed to acquire runtime lock: {e}\n".encode())
            os.close(write_fd)
        except OSError:
            pass
        sys.exit(1)

    # Load config — failure is fatal; signal the parent before exiting
    try:
        config = GatewayConfig.from_file(config_path)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        try:
            os.write(write_fd, f"error:Config load failed: {e}\n".encode())
        except OSError:
            pass
        try:
            os.close(write_fd)
        except OSError:
            pass
        _cleanup()
        sys.exit(1)

    # Run the service — startup_fd is passed through so service.run() signals
    # the parent once the full startup sequence completes.
    service = GatewayService(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task = loop.create_task(service.run(startup_fd=write_fd))

    # Handle SIGTERM for graceful shutdown
    def handle_sigterm(signum, frame):
        logger.info("Received SIGTERM, shutting down...")
        main_task.cancel()

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        # service.run() catches CancelledError internally and calls shutdown()
        # in its own finally block, so no explicit shutdown call is needed here.
        # Calling service.shutdown() again would risk a double-shutdown.
        pass
    except Exception as e:
        logger.error("Service crashed: %s", e)
        # Signal failure to parent if startup hasn't completed yet (write_fd still open)
        try:
            os.write(write_fd, f"error:Service crashed during startup: {e}\n".encode())
            os.close(write_fd)
        except OSError:
            pass  # already closed — startup signal was already sent
    finally:
        _cleanup()
        loop.close()
        logger.info("Daemon exited")


def stop_daemon() -> None:
    """Stop the running daemon."""
    running, pid = is_running()
    if not running:
        print("Gateway is not running")
        return

    print(f"Stopping gateway (pid={pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait for the gateway to complete its graceful shutdown before resorting
        # to SIGKILL.  The shutdown sequence includes:
        #   - stopping session managers (processor drain: up to 30s per watcher)
        #   - stopping agent backends (opencode: SIGTERM wait 5s + SIGKILL wait 5s)
        # Together these can take up to ~12s in a simple idle case and longer
        # when messages are in-flight.  The original 6s window (30 × 0.2s) was
        # far too short and caused SIGKILL to fire before the gateway could kill
        # its opencode child process, leaving it as an orphan — which is why
        # "acg restart" produced two running opencode processes.
        for _ in range(150):  # 150 × 0.2s = 30s grace period
            try:
                os.kill(pid, 0)
                time.sleep(0.2)
            except ProcessLookupError:
                break
        else:
            # Force kill if still alive after 30s grace period
            print("Force killing...")
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    # Only release the lock if it still belongs to the process we killed.
    # A rapid restart (e.g. by a process supervisor) could have already written
    # its own PID to the file before we get here.  Releasing an alien PID file
    # would cause the new daemon's next is_running() check to return False,
    # allowing yet another instance to start — producing split-brain.
    current_pid = locked_pid()
    if current_pid == pid:
        _lock_release()

    print("Gateway stopped")


def _cleanup() -> None:
    """Release the runtime lock (removes PID file)."""
    _lock_release()
