"""CLI entry point for agent-chat-gateway."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
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

    # schedule (sub-subcommands)
    schedule_p = sub.add_parser("schedule", help="Manage scheduled agent tasks")
    schedule_sub = schedule_p.add_subparsers(dest="schedule_cmd", help="Schedule subcommands")

    # schedule create
    sched_create_p = schedule_sub.add_parser("create", help="Create a new scheduled task")
    sched_create_p.add_argument("watcher", help="Watcher name as defined in config.yaml")
    sched_create_p.add_argument("message", help="Message text to inject into the agent session")
    sched_create_p.add_argument(
        "--every",
        default=None,
        metavar="INTERVAL",
        help="Recurrence interval: 30m, 1h, 6h, 1d, 1w. Use --starting to set a time anchor.",
    )
    sched_create_p.add_argument(
        "--starting",
        default=None,
        metavar="TIME",
        help=(
            "Time anchor / start time. With --every: sets the first run time and (for 1d/1w) "
            "pins the cron time-of-day (e.g. '09:00', 'Mon 10:00', 'Apr 15 09:00'). "
            "Without --every: specific datetime for a one-shot task (e.g. '2026-04-10 15:30'). "
            "Accepts smart partial inputs: '09:00' (today/tomorrow), 'Apr 15 09:00', "
            "'04-15 09:00', 'Mon 09:00', or '2026-05-01 09:00' (explicit full datetime)."
        ),
    )
    sched_create_p.add_argument(
        "--times",
        type=int,
        default=0,
        metavar="N",
        help="Number of times to run (0 = forever, default: 0)",
    )
    sched_create_p.add_argument(
        "--tz",
        default=None,
        metavar="TIMEZONE",
        help="IANA timezone (e.g. 'Asia/Taipei', 'America/New_York', 'UTC'). "
             "Fallback: scheduler.default_timezone in config, then server local.",
    )

    # schedule list
    sched_list_p = schedule_sub.add_parser("list", help="List scheduled tasks")
    sched_list_p.add_argument(
        "--connector",
        default=None,
        metavar="NAME",
        help="Filter by connector name",
    )
    sched_list_p.add_argument(
        "--all",
        action="store_true",
        dest="include_completed",
        help="Also show recently completed tasks (within TTL window)",
    )

    # schedule delete
    sched_delete_p = schedule_sub.add_parser("delete", help="Delete a scheduled task")
    sched_delete_p.add_argument("job_id", help="Job ID (e.g. acg-a3f2b1c0)")

    # schedule pause
    sched_pause_p = schedule_sub.add_parser("pause", help="Pause a scheduled task")
    sched_pause_p.add_argument("job_id", help="Job ID")

    # schedule resume
    sched_resume_p = schedule_sub.add_parser("resume", help="Resume a paused scheduled task")
    sched_resume_p.add_argument("job_id", help="Job ID")

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

    elif args.command == "schedule":
        _run_schedule(args)


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


def _run_schedule(args) -> None:
    """Handle 'schedule' subcommands."""
    if not hasattr(args, "schedule_cmd") or not args.schedule_cmd:
        print("Usage: agent-chat-gateway schedule {create,list,delete,pause,resume}")
        sys.exit(1)

    if args.schedule_cmd == "create":
        _run_schedule_create(args)
    elif args.schedule_cmd == "list":
        _run_schedule_list(args)
    elif args.schedule_cmd == "delete":
        _run_schedule_delete(args)
    elif args.schedule_cmd == "pause":
        _run_schedule_pause(args)
    elif args.schedule_cmd == "resume":
        _run_schedule_resume(args)
    else:
        print(f"Unknown schedule subcommand: {args.schedule_cmd}", file=sys.stderr)
        sys.exit(1)


def _run_schedule_create(args) -> None:
    """Handle 'schedule create': parse interval, build cron, send to daemon."""
    # For one-shot tasks (--starting datetime, no --every), enforce times=1 to prevent
    # the job from re-firing every year (5-field cron has no year field).
    times = args.times
    if args.every is None and times == 0:
        # No recurring interval + default times=0 (forever) → treat as one-shot.
        # This branch only fires when --starting is provided without --every
        # (pure datetime one-shot).  Branch 2 below (relative one-shot via --every
        # Nm/Nh --times 1) does NOT reach here because args.every is not None.
        # The two branches are mutually exclusive: Branch 1 fires when
        # args.starting is not None; Branch 2 fires when args.every is not None
        # and times (after this assignment) is 1.
        times = 1  # default one-shot to exactly 1 run

    cron: str | None = None
    next_run_override: str | None = None
    # Tracks whether we generated a UTC-coordinate one-shot cron.  When True,
    # the daemon must interpret the cron in UTC — not in the server's local
    # timezone — otherwise the offset is double-applied (e.g. UTC-7 shifts the
    # fire time 7 hours into the future instead of N minutes).
    _one_shot_utc_cron = False
    parsed: "_ParsedStarting | None" = None

    # M4: capture now_utc once and reuse across all branches.  Two separate
    # datetime.now(UTC) calls in Branch 1 and Branch 2 could straddle a minute
    # boundary if the process is preempted between them, causing the generated
    # one-shot cron to reflect a minute that is already in the past.
    now_utc = datetime.now(UTC)

    tz_name = args.tz or None

    # ── Branch 1: --starting provided ─────────────────────────────────────────
    if args.starting is not None:
        try:
            parsed = _parse_starting(args.starting, tz_name, now_utc)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if parsed.was_past:
            print(
                f"Warning: --starting {args.starting!r} was in the past. "
                f"Advancing to next occurrence: {parsed.first_run.isoformat()}",
                file=sys.stderr,
            )

        if args.every is None:
            # One-shot: --starting "2026-04-10 15:30" → specific datetime cron
            fr = parsed.first_run
            cron = f"{fr.minute} {fr.hour} {fr.day} {fr.month} *"
            next_run_override = fr.isoformat()
            _one_shot_utc_cron = True
        else:
            every_lower = args.every.strip().lower()
            # For sub-hourly/sub-daily intervals (Nm, Nh): --starting sets first_run
            # but does NOT change the cron pattern.
            # For 1d / 1w: --starting also anchors the cron time.
            _at_for_cron: str | None = None
            if every_lower in ("1d", "1w"):
                # Build the --at-style string for cron anchoring
                if parsed.dow is not None:
                    _dow_rev = {v: k for k, v in _DOW_MAP.items()}
                    day_name = _dow_rev.get(parsed.dow, parsed.dow)
                    _at_for_cron = f"{day_name.capitalize()} {parsed.hour:02d}:{parsed.minute:02d}"
                else:
                    _at_for_cron = f"{parsed.hour:02d}:{parsed.minute:02d}"

            try:
                cron = _build_cron_expression(args.every, _at_for_cron)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

            next_run_override = parsed.first_run.isoformat()

    # ── Branch 2: no --starting, one-shot relative reminders ──────────────────
    # For --every Nm/Nh --times 1 (no --starting), accept ANY positive integer interval
    # (e.g. 7m, 23m, 90m) and compute now + N to generate a one-shot datetime cron.
    elif times == 1 and args.every is not None:
        from datetime import timedelta

        interval_minutes = _parse_one_shot_interval(args.every)
        if interval_minutes is not None:
            target = now_utc + timedelta(minutes=interval_minutes)
            cron = f"{target.minute} {target.hour} {target.day} {target.month} *"
            _one_shot_utc_cron = True

    # ── Branch 3: no --starting, recurring with no time anchor ─────────────────
    if cron is None:
        try:
            cron = _build_cron_expression(args.every, None)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    cmd_data: dict = {
        "cmd": "schedule-create",
        "watcher": args.watcher,
        "message": args.message,
        "cron": cron,
        "times": times,
    }
    if next_run_override is not None:
        cmd_data["next_run"] = next_run_override
    if _one_shot_utc_cron and next_run_override is None:
        # Relative one-shot: cron coordinates are UTC, force UTC timezone.
        # Any --tz flag is intentionally ignored: timezone is irrelevant for
        # relative one-shot reminders ("in 7 minutes" means the same everywhere).
        cmd_data["timezone"] = "UTC"
    elif _one_shot_utc_cron and next_run_override is not None:
        # --starting + no --every: UTC datetime cron
        cmd_data["timezone"] = "UTC"
    elif args.tz:
        cmd_data["timezone"] = args.tz
    elif parsed is not None:
        # --starting was used without explicit --tz: store the resolved timezone
        # (server local) so that 1d/1w cron expressions are interpreted correctly
        # on subsequent runs.
        cmd_data["timezone"] = parsed.tz_str

    result = _send_command(cmd_data)
    if result["ok"]:
        job_id = result.get("job_id", "?")
        next_run = result.get("next_run", "?")
        print(f"Scheduled job created: {job_id}")
        print(f"Next run:              {next_run}")
        if times == 0:
            print(f"Recurrence:            {args.every or 'see cron: ' + cron} (forever)")
        else:
            print(f"Runs:                  {times} time(s)")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _run_schedule_list(args) -> None:
    """Handle 'schedule list': display jobs in a tabular format."""
    import textwrap

    cmd_data: dict = {"cmd": "schedule-list", "include_completed": args.include_completed}
    if args.connector:
        cmd_data["connector"] = args.connector

    result = _send_command(cmd_data)
    if not result["ok"]:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    jobs = result.get("jobs", [])
    if not jobs:
        print("No scheduled tasks.")
        return

    def _fmt_ts(ts: str | None) -> str:
        """Format an ISO 8601 UTC timestamp for display, stripping the +00:00 suffix."""
        if not ts:
            return "-"
        # Strip trailing +00:00 / Z for readability; header already says (UTC)
        return ts.replace("+00:00", "").replace("Z", "").replace("T", " ")

    # Header
    print(
        f"{'ID':<14}  {'WATCHER':<20}  {'STATUS':<10}  "
        f"{'CRON':<20}  {'RUNS':<12}  {'NEXT RUN (UTC)':<22}  MESSAGE"
    )
    print("-" * 124)

    for j in jobs:
        job_id = j.get("id", "?")
        watcher = j.get("watcher", "?")
        status = j.get("status", "?")
        cron = j.get("cron", "?")
        run_count = j.get("run_count", 0)
        times = j.get("times", 0)
        runs_str = f"{run_count}/∞" if times == 0 else f"{run_count}/{times}"
        # For completed jobs, show completed_at under a different label
        if status == "completed":
            raw_ts = j.get("completed_at")
            next_run_str = f"done {_fmt_ts(raw_ts)}" if raw_ts else "done"
        else:
            next_run_str = _fmt_ts(j.get("next_run"))
        message = textwrap.shorten(j.get("message", ""), width=40, placeholder="…")
        print(
            f"{job_id:<14}  {watcher:<20}  {status:<10}  "
            f"{cron:<20}  {runs_str:<12}  {next_run_str:<22}  {message}"
        )


def _run_schedule_delete(args) -> None:
    result = _send_command({"cmd": "schedule-delete", "job_id": args.job_id})
    if result["ok"]:
        print(f"Scheduled job {args.job_id!r} deleted.")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _run_schedule_pause(args) -> None:
    result = _send_command({"cmd": "schedule-pause", "job_id": args.job_id})
    if result["ok"]:
        print(f"Scheduled job {args.job_id!r} paused.")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _run_schedule_resume(args) -> None:
    result = _send_command({"cmd": "schedule-resume", "job_id": args.job_id})
    if result["ok"]:
        next_run = result.get("next_run", "?")
        print(f"Scheduled job {args.job_id!r} resumed.")
        print(f"Next run: {next_run}")
    else:
        print(f"Error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


def _get_local_tz_name() -> str:
    """Return the server's local IANA timezone name.

    Delegates to the canonical shared implementation in ``gateway.core.tz_utils``
    to avoid duplicating the /etc/localtime parsing logic here.
    """
    from gateway.core.tz_utils import local_iana_timezone
    return local_iana_timezone()


def _advance_by_one_year(candidate: "datetime") -> "datetime":
    """Return candidate advanced by one year, handling Feb 29 in non-leap years.

    ``datetime.replace(year=y+1)`` raises ``ValueError`` when the candidate is
    Feb 29 and the next year is not a leap year.  In that case we search forward
    for the next leap year (guaranteed within 8 years).
    """
    target_year = candidate.year + 1
    for offset in range(8):
        try:
            return candidate.replace(year=target_year + offset)
        except ValueError:
            continue
    # This is mathematically unreachable: leap years occur at least once every 4 years,
    # so within an 8-year search window there is always a valid Feb 29.  The raise
    # exists only as a defensive guard against a bug in the loop itself.
    raise ValueError(
        f"Internal error: _advance_by_one_year could not find a valid date for "
        f"{candidate.strftime('%b %d')} within 8 years — this is a bug, please report it."
    )


@dataclass
class _ParsedStarting:
    """Result of parsing a --starting value."""
    first_run: datetime       # UTC, always future after auto-advance
    hour: int                 # local hour (in the user's tz)
    minute: int               # local minute
    dow: str | None           # cron DOW digit e.g. "1" for Mon, or None
    was_past: bool            # True if the original parsed time was in the past
    tz_str: str               # IANA timezone name actually used (e.g. "America/Los_Angeles")


def _parse_starting(starting_str: str, tz_name: str | None, now_utc: datetime) -> "_ParsedStarting":
    """Parse a --starting value into a _ParsedStarting result.

    Accepts the following formats:
      - "09:00"              → today at 09:00 local; advance to tomorrow if past
      - "Apr 15 09:00"       → this year Apr 15 at 09:00; advance one year if past
      - "04-15 09:00"        → this year Apr 15 at 09:00 (MM-DD)
      - "Mon 09:00"          → next Monday at 09:00
      - "2026-05-01 09:00"   → explicit full datetime

    All times are interpreted in tz_name (default: UTC).
    first_run is always returned in UTC and is always in the future.
    """
    import re as _re
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    stripped = starting_str.strip()

    try:
        if tz_name:
            tz = ZoneInfo(tz_name)
            tz_str = tz_name
        else:
            # Fall back to the server's local IANA timezone (not UTC) so that
            # times like "22:38" are interpreted as local wall-clock time.
            # IMPORTANT: use ZoneInfo(tz_str), NOT datetime.now().astimezone().tzinfo.
            # The latter returns a fixed UTC-offset object (e.g. UTC-7) that does not
            # observe DST transitions, so tz and tz_str would silently diverge when
            # the clock changes — schedules could be off by one hour post-transition.
            tz_str = _get_local_tz_name()
            tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        tz_str = "UTC"
        tz = ZoneInfo("UTC")

    # Convert now_utc to local time for comparisons
    now_local = now_utc.astimezone(tz)

    # ── Format 1: "HH:MM" ─────────────────────────────────────────────────────
    if _re.fullmatch(r"\d{1,2}:\d{2}", stripped):
        h, m = _parse_hhmm(stripped)
        candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        was_past = candidate <= now_local
        if was_past:
            from datetime import timedelta
            candidate += timedelta(days=1)
        first_run = candidate.astimezone(UTC)
        return _ParsedStarting(first_run=first_run, hour=h, minute=m, dow=None, was_past=was_past, tz_str=tz_str)

    # ── Format 2: "Mon 09:00" (day-of-week + time) ─────────────────────────────
    dow_match = _re.fullmatch(r"([A-Za-z]{3})\s+(\d{1,2}:\d{2})", stripped)
    if dow_match:
        day_str, time_str = dow_match.group(1), dow_match.group(2)
        dow_digit = _DOW_MAP.get(day_str.lower())
        if dow_digit is None:
            raise ValueError(
                f"Unknown day of week {day_str!r}. Use: Mon, Tue, Wed, Thu, Fri, Sat, Sun."
            )
        h, m = _parse_hhmm(time_str)
        # Find next occurrence of this weekday
        # cron dow: 0=Sun, 1=Mon, ..., 6=Sat
        # Python weekday: 0=Mon, ..., 6=Sun  →  convert
        cron_to_python_dow = {"0": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5}
        target_python_dow = cron_to_python_dow[dow_digit]
        from datetime import timedelta
        # Start from today
        candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        days_ahead = (target_python_dow - now_local.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        was_past = candidate <= now_local
        if was_past:
            candidate += timedelta(days=7)
        first_run = candidate.astimezone(UTC)
        return _ParsedStarting(first_run=first_run, hour=h, minute=m, dow=dow_digit, was_past=was_past, tz_str=tz_str)

    # ── Format 3: "Apr 15 09:00" (month-name day time) ─────────────────────────
    month_name_match = _re.fullmatch(
        r"([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}:\d{2})", stripped
    )
    if month_name_match:
        month_str, day_str, time_str = (
            month_name_match.group(1),
            month_name_match.group(2),
            month_name_match.group(3),
        )
        _MONTH_MAP = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        month_num = _MONTH_MAP.get(month_str.lower())
        if month_num is None:
            raise ValueError(f"Unknown month {month_str!r}.")
        day_num = int(day_str)
        h, m = _parse_hhmm(time_str)
        try:
            candidate = now_local.replace(
                month=month_num, day=day_num, hour=h, minute=m, second=0, microsecond=0
            )
        except ValueError as e:
            raise ValueError(f"Invalid date in --starting: {e}") from e
        was_past = candidate <= now_local
        if was_past:
            candidate = _advance_by_one_year(candidate)
        first_run = candidate.astimezone(UTC)
        return _ParsedStarting(first_run=first_run, hour=h, minute=m, dow=None, was_past=was_past, tz_str=tz_str)

    # ── Format 4: "04-15 09:00" (MM-DD HH:MM) ─────────────────────────────────
    mmdd_match = _re.fullmatch(r"(\d{2})-(\d{2})\s+(\d{1,2}:\d{2})", stripped)
    if mmdd_match:
        month_num, day_num = int(mmdd_match.group(1)), int(mmdd_match.group(2))
        time_str = mmdd_match.group(3)
        h, m = _parse_hhmm(time_str)
        try:
            candidate = now_local.replace(
                month=month_num, day=day_num, hour=h, minute=m, second=0, microsecond=0
            )
        except ValueError as e:
            raise ValueError(f"Invalid date in --starting: {e}") from e
        was_past = candidate <= now_local
        if was_past:
            candidate = _advance_by_one_year(candidate)
        first_run = candidate.astimezone(UTC)
        return _ParsedStarting(first_run=first_run, hour=h, minute=m, dow=None, was_past=was_past, tz_str=tz_str)

    # ── Format 5: "YYYY-MM-DD HH:MM" (explicit full datetime) ─────────────────
    # Unlike partial formats (HH:MM, Mon HH:MM, etc.) which auto-advance to the
    # next occurrence, a full explicit datetime represents a single unambiguous
    # point in time.  If that point is in the past, it's almost certainly a typo
    # — raise an error rather than silently creating a job that fires immediately.
    full_dt_formats = ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M"]
    for fmt in full_dt_formats:
        try:
            naive_dt = datetime.strptime(stripped, fmt)
            candidate = naive_dt.replace(tzinfo=tz)
            first_run = candidate.astimezone(UTC)
            was_past = first_run <= now_utc
            if was_past:
                raise ValueError(
                    f"--starting {starting_str!r} is in the past "
                    f"({first_run.strftime('%Y-%m-%d %H:%M UTC')}). "
                    "Please provide a future datetime."
                )
            return _ParsedStarting(
                first_run=first_run,
                hour=naive_dt.hour,
                minute=naive_dt.minute,
                dow=None,
                was_past=False,
                tz_str=tz_str,
            )
        except ValueError as exc:
            # If we raised the "is in the past" error above, propagate it.
            # Otherwise (strptime format mismatch), try the next format.
            if "is in the past" in str(exc):
                raise
            continue

    raise ValueError(
        f"Cannot parse --starting value {starting_str!r}. "
        "Accepted formats: '09:00', 'Apr 15 09:00', '04-15 09:00', "
        "'Mon 09:00', '2026-05-01 09:00'."
    )


def _parse_one_shot_interval(every: str) -> int | None:
    """Parse an arbitrary interval string for one-shot relative reminders.

    Accepts any positive integer followed by ``m`` (minutes) or ``h`` (hours).
    Returns the total number of minutes, or ``None`` if the format is not
    a simple Nm/Nh expression (e.g. ``"1d"``, ``"1w"`` return ``None`` and
    fall through to ``_build_cron_expression``).

    Unlike ``_INTERVAL_MAP``, this accepts arbitrary values such as ``7m``,
    ``23m``, ``90m``, ``3h`` — because we compute ``now + N`` and generate a
    specific one-shot datetime cron, so cron-alignment is not required.

    Examples::

        _parse_one_shot_interval("7m")   → 7
        _parse_one_shot_interval("23m")  → 23
        _parse_one_shot_interval("90m")  → 90
        _parse_one_shot_interval("2h")   → 120
        _parse_one_shot_interval("1d")   → None  (falls through to _INTERVAL_MAP)
        _parse_one_shot_interval("bad")  → None
    """
    # Cap at 1 year to prevent absurdly far-future one-shot jobs that would be
    # hard to cancel and may not be the user's intent (e.g. "100000h").
    _MAX_ONE_SHOT_MINUTES = 365 * 24 * 60  # 525 600 minutes ≈ 1 year

    import re as _re
    m = _re.fullmatch(r"(\d+)(m|h)", every.strip().lower())
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    if value <= 0:
        return None
    total_minutes = value if unit == "m" else value * 60
    if total_minutes > _MAX_ONE_SHOT_MINUTES:
        return None  # reject; caller falls through to _build_cron_expression which will error
    return total_minutes

# Interval → (default_cron, description).  default_cron uses * for unset fields;
# --at overrides the time components.  Defined at module level (not inside the
# function) so the dict is not rebuilt on every call.
_INTERVAL_MAP: dict[str, tuple[str, str]] = {
    "1m":  ("* * * * *",    "every minute"),
    "5m":  ("*/5 * * * *",  "every 5 minutes"),
    "10m": ("*/10 * * * *", "every 10 minutes"),
    "15m": ("*/15 * * * *", "every 15 minutes"),
    "30m": ("*/30 * * * *", "every 30 minutes"),
    "1h":  ("0 * * * *",    "every hour"),
    "2h":  ("0 */2 * * *",  "every 2 hours"),
    "3h":  ("0 */3 * * *",  "every 3 hours"),
    "6h":  ("0 */6 * * *",  "every 6 hours"),
    "12h": ("0 */12 * * *", "every 12 hours"),
    "1d":  ("0 9 * * *",    "every day"),     # default 09:00
    "1w":  ("0 9 * * 1",    "every week"),    # default Monday 09:00
}

_DOW_MAP: dict[str, str] = {
    "sun": "0", "mon": "1", "tue": "2", "wed": "3",
    "thu": "4", "fri": "5", "sat": "6",
}


def _build_cron_expression(every: str | None, at: str | None) -> str:
    """Convert ``--every INTERVAL`` + optional ``--at TIME`` to a 5-field cron string.

    Supported intervals:
      - Any ``Nm`` (1 ≤ N ≤ 59): ``*/N * * * *``  e.g. ``2m`` → ``*/2 * * * *``
      - Any ``Nh`` (1 ≤ N ≤ 23): ``0 */N * * *``  e.g. ``3h`` → ``0 */3 * * *``
      - Named: ``1d``, ``1w`` (with optional ``--at`` for time/day anchoring)

    Supported --at formats (with --every):
      - "09:00"         → set hour/minute for a daily/weekly schedule
      - "Mon 09:00"     → set day-of-week + time for weekly schedules
    Supported --at formats (without --every, one-shot):
      - "2026-04-10 15:30" → specific datetime → "30 15 10 4 *"

    Raises ValueError on invalid input.
    """
    import re as _re

    if every is None and at is None:
        raise ValueError("Specify --every INTERVAL and/or --starting TIME. See --help for details.")

    if every is None:
        # One-shot: --at "2026-04-10 15:30"
        if not at:
            raise ValueError("--at requires a datetime value when --every is not specified.")
        return _parse_one_shot_at(at)

    # Recurring: --every INTERVAL [--at TIME]
    every_lower = every.strip().lower()

    # ── Named intervals (daily / weekly with --at support) ────────────────────
    if every_lower in _INTERVAL_MAP:
        default_cron, _ = _INTERVAL_MAP[every_lower]
        if at is None:
            return default_cron
        # fall through to --at override logic below

    # ── Arbitrary Nm / Nh (any positive integer) ──────────────────────────────
    elif _m := _re.fullmatch(r"(\d+)(m|h)", every_lower):
        n, unit = int(_m.group(1)), _m.group(2)
        if unit == "m":
            if not 1 <= n <= 59:
                raise ValueError(
                    f"Minute interval must be between 1 and 59 (got {n}m)."
                )
            default_cron = f"*/{n} * * * *" if n > 1 else "* * * * *"
        else:  # hours
            if not 1 <= n <= 23:
                raise ValueError(
                    f"Hour interval must be between 1 and 23 (got {n}h)."
                )
            default_cron = f"0 */{n} * * *" if n > 1 else "0 * * * *"

        if at is None:
            return default_cron
        # with --at override, fall through (sub-hourly + --at is unusual but allowed)

    else:
        raise ValueError(
            f"Unsupported interval {every!r}. Use Nm (e.g. 2m, 15m), Nh (e.g. 1h, 6h), "
            f"or 1d / 1w for daily/weekly."
        )

    # Apply --at override to the default cron
    at_stripped = at.strip()
    parts = default_cron.split()  # [minute, hour, dom, month, dow]

    # Check for "Mon 09:00" style (weekly with day override)
    at_parts = at_stripped.split()
    if len(at_parts) == 2:
        day_str, time_str = at_parts
        dow = _DOW_MAP.get(day_str.lower())
        if dow is None:
            raise ValueError(
                f"Unknown day of week {day_str!r}. Use: Mon, Tue, Wed, Thu, Fri, Sat, Sun."
            )
        h, m = _parse_hhmm(time_str)
        if every_lower not in ("1w",):
            raise ValueError("Day-of-week syntax (e.g. 'Mon 09:00') is only valid with --every 1w")
        return f"{m} {h} * * {dow}"

    # Plain "HH:MM" time override
    h, m = _parse_hhmm(at_stripped)
    # Classify the interval for --at semantics:
    #   sub-hourly (Nm): --at HH:MM makes no sense → reject
    #   hourly (Nh, 1h–23h): only minute part applies; hour is ignored
    #   daily/weekly: full HH:MM applies
    _at_m = _re.fullmatch(r"(\d+)(m|h)", every_lower)
    if _at_m and _at_m.group(2) == "m":
        raise ValueError(
            f"--at HH:MM is not applicable with --every {every} (sub-hourly interval)"
        )
    if _at_m and _at_m.group(2) == "h":
        # For sub-daily intervals, only the minute component of --at applies.
        # The hour is silently discarded since these jobs fire every N hours
        # regardless of starting hour.  Warn the user if they specified a non-zero hour.
        if h != 0:
            print(
                f"Warning: --at {at_stripped!r} with --every {every}: "
                f"the hour ({h:02d}) is ignored for sub-daily intervals. "
                f"Only the minute :{m:02d} is applied.",
                file=sys.stderr,
            )
        parts[0] = str(m)
        return " ".join(parts)
    # Daily / weekly: set hour and minute
    parts[0] = str(m)
    parts[1] = str(h)
    return " ".join(parts)


def _parse_one_shot_at(at: str) -> str:
    """Parse a 'YYYY-MM-DD HH:MM' string into a one-shot cron expression."""
    import sys
    from datetime import datetime
    formats = ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M"]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(at.strip(), fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(
            f"Cannot parse --at value {at!r}. "
            "Expected format: 'YYYY-MM-DD HH:MM' (e.g. '2026-04-10 15:30')."
        )
    # Warn if the specified datetime is in the past — the job will fire
    # on the next scheduler tick (within 60 s) rather than at the intended time.
    # NOTE: this function is an internal helper only reachable via _build_cron_expression
    # with an explicit `at` argument; the public CLI path uses _parse_starting instead.
    # C1: use UTC-aware comparison to avoid timezone-wrong result on non-UTC servers.
    from datetime import UTC as _UTC
    if dt.replace(tzinfo=_UTC) < datetime.now(_UTC):
        print(
            f"Warning: --at {at!r} is in the past. "
            "The job will fire immediately on the next scheduler tick.",
            file=sys.stderr,
        )
    return f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' → (hour, minute). Raises ValueError on bad input."""
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM format, got {time_str!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"Expected HH:MM format, got {time_str!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time {time_str!r}: hour must be 0-23, minute 0-59")
    return h, m


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
