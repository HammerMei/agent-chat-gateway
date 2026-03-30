"""CLI entry point for agent-chat-gateway."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

RUNTIME_DIR = Path.home() / ".agent-chat-gateway"
CONTROL_SOCK = RUNTIME_DIR / "control.sock"

# Default config: check ACG_CONFIG env var first, then ~/.agent-chat-gateway/config.yaml.
DEFAULT_CONFIG = os.environ.get(
    "ACG_CONFIG",
    str(Path.home() / ".agent-chat-gateway" / "config.yaml"),
)


def main():
    parser = argparse.ArgumentParser(
        prog="agent-chat-gateway",
        description="Standalone service bridging Rocket.Chat rooms to agent sessions",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # start
    start_p = sub.add_parser("start", help="Start the gateway service")
    start_p.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $ACG_CONFIG or ~/.agent-chat-gateway/config.yaml)",
    )

    # stop
    sub.add_parser("stop", help="Stop the gateway service")

    # restart
    restart_p = sub.add_parser("restart", help="Restart the gateway service")
    restart_p.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: $ACG_CONFIG or ~/.agent-chat-gateway/config.yaml)",
    )

    # status
    sub.add_parser("status", help="Show gateway status")

    # list
    list_p = sub.add_parser("list", help="List all watchers")
    list_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="Filter by connector name (default: show watchers across all connectors)",
    )

    # pause
    pause_p = sub.add_parser("pause", help="Pause a watcher (stops processing messages)")
    pause_p.add_argument("watcher_name", help="Watcher name as defined in config.yaml")
    pause_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="Connector the watcher belongs to (default: first configured connector)",
    )

    # resume
    resume_p = sub.add_parser("resume", help="Resume a paused watcher")
    resume_p.add_argument("watcher_name", help="Watcher name as defined in config.yaml")
    resume_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="Connector the watcher belongs to (default: first configured connector)",
    )

    # reset
    reset_p = sub.add_parser(
        "reset",
        help="Reset a watcher: clear runtime state and start a fresh session",
    )
    reset_p.add_argument("watcher_name", help="Watcher name as defined in config.yaml")
    reset_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="Connector the watcher belongs to (default: first configured connector)",
    )

    # onboard
    onboard_p = sub.add_parser(
        "onboard",
        help="Interactive setup wizard (run after install or to update config)",
    )
    onboard_p.add_argument(
        "--repo-path",
        default=None,
        help="Path to the ACG repo (stored in install metadata)",
    )

    # upgrade
    sub.add_parser("upgrade", help="Upgrade ACG to the latest version")

    # send
    send_p = sub.add_parser("send", help="Send a message to a room")
    send_p.add_argument("room", help="Room name or room ID")
    send_p.add_argument(
        "message",
        nargs="*",
        help='Inline message text (joined with spaces); use "-" to read from stdin',
    )
    send_p.add_argument("--file", default=None, metavar="PATH", help="Read message text from a file")
    send_p.add_argument(
        "--attach", default=None, metavar="PATH",
        help="Upload a file attachment (message/--file/stdin become optional caption)",
    )
    send_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="Connector to send through (default: first configured connector)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "start":
        from .daemon import start_daemon
        start_daemon(args.config)

    elif args.command == "stop":
        from .daemon import stop_daemon
        stop_daemon()

    elif args.command == "restart":
        from .daemon import start_daemon, stop_daemon
        stop_daemon()
        start_daemon(args.config)

    elif args.command == "status":
        from .daemon import LOG_FILE, PID_FILE, is_running
        running, pid = is_running()
        if running:
            # Get uptime
            import time
            pid_mtime = PID_FILE.stat().st_mtime
            uptime_secs = int(time.time() - pid_mtime)
            hours, remainder = divmod(uptime_secs, 3600)
            minutes, secs = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {secs}s"

            print(f"Gateway:  running (pid={pid})")
            print(f"Uptime:   {uptime_str}")
            print(f"PID file: {PID_FILE}")
            print(f"Log file: {LOG_FILE}")

            # Get watcher count from daemon
            try:
                result = _send_command({"cmd": "list"})
                if result["ok"]:
                    count = len(result.get("data", []))
                    print(f"Watchers: {count}")
            except SystemExit:
                print("Watchers: (unable to query)")
        else:
            print("Gateway:  not running")

    elif args.command == "list":
        cmd_data = {"cmd": "list"}
        if args.connector is not None:
            cmd_data["connector"] = args.connector
        result = _send_command(cmd_data)
        watchers = result.get("data", [])
        connector_errors = result.get("errors", [])
        if watchers:
            for w in watchers:
                status = "PAUSED" if w.get("paused") else ("active" if w.get("active") else "inactive")
                agent_label = f"[{w.get('agent_name', '?')}]"
                connector_label = f"({w.get('connector', '?')})"
                print(
                    f"{w['watcher_name']}: {connector_label} {w['room_name']} "
                    f"{agent_label} session={w.get('session_id', '(none)')} [{status}]"
                )
        elif not connector_errors:
            print("No configured watchers")
        # Surface per-connector failures (partial failure case)
        for ce in connector_errors:
            print(
                f"Warning: connector '{ce['connector']}' failed to list watchers: {ce['error']}",
                file=sys.stderr,
            )
        if not result["ok"] and not connector_errors:
            # Hard failure (e.g. unknown connector specified by --connector)
            print(f"Error: {result.get('error')}", file=sys.stderr)
            sys.exit(1)
        if connector_errors:
            sys.exit(1)

    elif args.command == "pause":
        cmd_data = {"cmd": "pause", "watcher_name": args.watcher_name}
        if args.connector is not None:
            cmd_data["connector"] = args.connector
        result = _send_command(cmd_data)
        if result["ok"]:
            print(f"Watcher '{args.watcher_name}' paused")
        else:
            print(f"Error: {result.get('error')}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "resume":
        cmd_data = {"cmd": "resume", "watcher_name": args.watcher_name}
        if args.connector is not None:
            cmd_data["connector"] = args.connector
        result = _send_command(cmd_data)
        if result["ok"]:
            print(f"Watcher '{args.watcher_name}' resumed")
        else:
            print(f"Error: {result.get('error')}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "reset":
        cmd_data = {"cmd": "reset", "watcher_name": args.watcher_name}
        if args.connector is not None:
            cmd_data["connector"] = args.connector
        result = _send_command(cmd_data)
        if result["ok"]:
            print(f"Watcher '{args.watcher_name}' reset")
        else:
            print(f"Error: {result.get('error')}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "onboard":
        from .onboard import run_onboard
        run_onboard(repo_path=Path(args.repo_path) if args.repo_path else None)

    elif args.command == "upgrade":
        from .upgrade import run_upgrade
        run_upgrade()

    elif args.command == "send":
        _run_send(args)


def _run_send(args) -> None:
    """Handle the 'send' subcommand: post a message or upload a file via the control socket.

    Routes through the running daemon's control socket so the send goes through
    the connector abstraction layer instead of coupling directly to a specific
    platform's REST client.
    """
    from pathlib import Path as _Path

    has_inline = bool(args.message)
    is_stdin = has_inline and args.message == ["-"]
    has_file = bool(args.file)

    # Validate mutual exclusions
    if has_inline and not is_stdin and has_file:
        print("Error: cannot use both inline message and --file", file=sys.stderr)
        sys.exit(1)
    if is_stdin and has_file:
        print("Error: cannot use both '-' (stdin) and --file", file=sys.stderr)
        sys.exit(1)
    if not has_inline and not has_file and not args.attach:
        print("Error: provide a message, --file PATH, or --attach PATH", file=sys.stderr)
        sys.exit(1)

    # Validate paths exist before making any network calls
    if has_file and not _Path(args.file).exists():
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    if args.attach and not _Path(args.attach).exists():
        print(f"Error: attachment not found: {args.attach}", file=sys.stderr)
        sys.exit(1)

    # Read message text
    text = ""
    if is_stdin:
        text = sys.stdin.read()
    elif has_file:
        text = _Path(args.file).read_text()
    elif has_inline:
        text = " ".join(args.message)

    # Build command payload and send through the control socket
    cmd_data: dict = {
        "cmd": "send",
        "room": args.room,
        "text": text,
    }
    if args.attach:
        # Resolve to absolute path so the daemon can find the file
        cmd_data["attachment_path"] = str(_Path(args.attach).resolve())
    if args.connector is not None:
        cmd_data["connector"] = args.connector

    result = _send_command(cmd_data)
    if result["ok"]:
        print("Sent.")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _send_command(request: dict) -> dict:
    """Send a command to the running daemon via Unix domain socket."""
    from .daemon import is_running

    running, pid = is_running()
    if not running:
        print("Error: Gateway is not running. Start it with: agent-chat-gateway start", file=sys.stderr)
        sys.exit(1)

    if not CONTROL_SOCK.exists():
        print("Error: Control socket not found. The daemon may still be starting.", file=sys.stderr)
        sys.exit(1)

    return asyncio.run(_send_command_async(request))


async def _send_command_async(request: dict) -> dict:
    """Async helper to send command over Unix socket."""
    reader, writer = await asyncio.open_unix_connection(str(CONTROL_SOCK))
    try:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()

        data = await asyncio.wait_for(reader.readline(), timeout=60.0)
        return json.loads(data.decode())
    finally:
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    main()
