# ACG Scheduling Commands

You can schedule recurring or one-time tasks using the `agent-chat-gateway schedule` CLI. When a user asks you to set up a recurring task, reminder, or automated job, use these commands.

## Create a scheduled task

```bash
agent-chat-gateway schedule create <watcher> "<message>" [OPTIONS]
```

**Options:**
- `--every INTERVAL` — Recurrence interval. For **one-shot jobs** (`--times 1`): any `Nm` or `Nh` value works (e.g. `7m`, `23m`, `90m`, `3h`). For **recurring jobs** (`--times 0` or omitted): use a cron-aligned value: `1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `2h`, `3h`, `6h`, `12h`, `1d`, `1w`.
- `--at TIME` — Time override. With `--every`: set hour/minute (e.g. `09:00` or `Mon 09:00`). Without `--every`: specific datetime for a one-shot task (e.g. `2026-04-10 15:30`).
- `--times N` — Number of runs. `0` = forever (default). `N` = stop after N runs.
- `--tz TIMEZONE` — IANA timezone (e.g. `America/New_York`, `Europe/Berlin`, `UTC`). Only relevant for daily/weekly jobs anchored to a specific local time — **omit for sub-hourly intervals** (`1m`–`12h`), which fire on a fixed cadence regardless of timezone.
- `--connector NAME` — Connector name (auto-detected if omitted)

**Examples:**

```bash
# Remind the user in 5 minutes (one-shot relative reminder)
agent-chat-gateway schedule create general-watcher "提醒：去喝水！" --every 5m --times 1

# Remind the user in 1 hour (one-shot)
agent-chat-gateway schedule create general-watcher "Time to take a break!" --every 1h --times 1

# Run a daily standup check every weekday at 09:00
agent-chat-gateway schedule create general-watcher "Run the daily standup summary" --every 1d --at 09:00 --times 0

# Check CI status every hour, 24 times (one day)
agent-chat-gateway schedule create ops-watcher "Check CI pipeline status" --every 1h --times 24

# Weekly report every Monday at 10:00 AM
agent-chat-gateway schedule create general-watcher "Generate the weekly report" --every 1w --at "Mon 10:00" --times 0

# One-time reminder at a specific date/time
agent-chat-gateway schedule create general-watcher "Reminder: feature freeze today" --at "2026-04-10 15:30"

# Monitor every 30 minutes, forever (no --tz needed for sub-hourly jobs)
agent-chat-gateway schedule create ops-watcher "Check server health" --every 30m --times 0

# Daily standup at 09:00 in a specific timezone — use --tz here
agent-chat-gateway schedule create general-watcher "Run daily standup" --every 1d --at "09:00" --tz "America/New_York"
```

## List scheduled tasks

```bash
agent-chat-gateway schedule list              # Show active and paused tasks
agent-chat-gateway schedule list --all        # Also show recently completed tasks
agent-chat-gateway schedule list --connector rc-home  # Filter by connector
```

## Delete a scheduled task

```bash
agent-chat-gateway schedule delete <job-id>   # e.g.: agent-chat-gateway schedule delete acg-a3f2b1c0
```

## Pause and resume

```bash
agent-chat-gateway schedule pause <job-id>    # Temporarily stop a recurring task
agent-chat-gateway schedule resume <job-id>   # Re-enable a paused task
```

## Notes

- Scheduled messages are injected directly into your agent session — they do not appear as chat messages in the room.
- The minimum scheduling interval is 1 minute.
- Job IDs look like `acg-a3f2b1c0`. Use `agent-chat-gateway schedule list` to find a job's ID.
- If the gateway is restarted, any jobs missed during downtime will be fired immediately on startup.
- Forever jobs (`--times 0`) only stop when you explicitly delete them.

## ⚠️ Important: Relative reminders — use `--every` + `--times 1`, NOT `$(date ...)`

When the user asks for a reminder "in N minutes" or "in N hours", **always** use `--every` with `--times 1`.
**Never** use shell command substitution like `$(date ...)` in `--at` — it is not needed and causes permission errors.

```bash
# ✅ CORRECT — any number of minutes works for one-shot reminders
agent-chat-gateway schedule create <watcher> "<message>" --every 3m --times 1
agent-chat-gateway schedule create <watcher> "<message>" --every 7m --times 1
agent-chat-gateway schedule create <watcher> "<message>" --every 23m --times 1

# ✅ CORRECT — hours also work
agent-chat-gateway schedule create <watcher> "<message>" --every 2h --times 1

# ❌ WRONG — never do this
agent-chat-gateway schedule create <watcher> "<message>" --at "$(date -v+3M '+%Y-%m-%d %H:%M')"
```
