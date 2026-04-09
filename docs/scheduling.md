# Built-in Task Scheduler

## Overview

The scheduler lets ACG proactively trigger tasks on a fixed cadence or at a specific time — without waiting for a human to send a message. You can use it to:

- Have the AI remind you of something in 5 minutes
- Run a daily standup prompt every weekday morning
- Send a weekly summary every Friday afternoon
- Fire a one-shot task at a precise datetime, then forget it

Jobs are owned by a watcher. When a job fires, ACG injects a message directly into that watcher's agent session — bypassing the normal self-message filter — and the agent responds as if a user sent the message.

The scheduler polls every 60 seconds, so jobs fire within one minute of their scheduled time.

---

## Teaching the Agent to Schedule

No configuration is required. ACG automatically injects scheduling context into every agent session at startup via `contexts/scheduling-context.md`. The agent already knows how to run `agent-chat-gateway schedule create` commands.

**Example interaction:**

> **You:** Remind me to review the deployment logs in 15 minutes.
>
> **Agent:** Sure! I'll set a reminder for 15 minutes from now.
> *(runs: `agent-chat-gateway schedule create general-watcher "Reminder: review the deployment logs" --every 15m --times 1`)*
>
> **Agent:** Done — you'll get a reminder in 15 minutes. Job ID: `acg-3a7f1c90`.

The agent creates the job itself using the CLI. From your side, all you do is ask in natural language.

---

## Creating Scheduled Tasks

### Basic syntax

```bash
agent-chat-gateway schedule create WATCHER MESSAGE [OPTIONS]
```

`WATCHER` is the name of the watcher (chat room binding) that will receive the injected message.
`MESSAGE` is the prompt that gets sent to the agent when the job fires.

### Options

| Option | Description |
|---|---|
| `--every INTERVAL` | Recurring interval. Accepted values: `1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `2h`, `3h`, `6h`, `12h`, `1d`, `1w` |
| `--at TIME` | One-shot or recurring at a fixed time. Accepts `"09:00"`, `"Mon 09:00"`, or `"2026-04-10 15:30"` |
| `--times N` | Max number of runs. `0` means run forever (default). `1` means run once then mark completed. |
| `--tz TIMEZONE` | IANA timezone, e.g. `"America/New_York"`, `"Europe/Berlin"`, `"UTC"`. Defaults to the `scheduler.default_timezone` config value, or the ACG server's local timezone if unset. Only relevant for daily/weekly schedules — omit for sub-hourly intervals. |
| `--connector NAME` | Which connector to use. Auto-detected when only one connector is configured. |

### Examples

```bash
# One-shot reminder in 5 minutes
agent-chat-gateway schedule create general-watcher "Reminder: check the oven" --every 5m --times 1

# Daily standup at 09:00 every day
agent-chat-gateway schedule create general-watcher "Run the daily standup" --every 1d --at "09:00" --tz "Asia/Taipei"

# Weekly report every Friday
agent-chat-gateway schedule create ops-watcher "Generate weekly ops summary" --at "Fri 17:00" --tz "America/New_York"

# One-shot at a specific datetime
agent-chat-gateway schedule create general-watcher "Review Q2 roadmap" --at "2026-04-10 15:30" --tz "Asia/Taipei"

# Health check every 30 minutes, forever
agent-chat-gateway schedule create ops-watcher "Check server health and report status" --every 30m

# Run exactly 3 times, every hour
agent-chat-gateway schedule create general-watcher "Hourly check-in" --every 1h --times 3
```

---

## Common Use Cases

### Relative reminder ("in N minutes")

Use `--every` with `--times 1`. This fires once after the interval and then marks the job completed.

```bash
agent-chat-gateway schedule create general-watcher "Reminder: stand up and stretch" --every 15m --times 1
```

Or ask the agent directly:
> "Remind me to call back the client in 30 minutes."

### Daily recurring task at a fixed time

```bash
agent-chat-gateway schedule create general-watcher "Good morning! Summarize yesterday's GitHub activity." \
  --every 1d --at "09:00" --tz "Asia/Taipei"
```

### Weekly report

```bash
agent-chat-gateway schedule create ops-watcher "Generate weekly infrastructure cost report and post summary." \
  --at "Fri 16:00" --tz "America/New_York"
```

### One-shot at a specific future datetime

```bash
agent-chat-gateway schedule create general-watcher "It's launch day — post the release announcement." \
  --at "2026-04-15 10:00" --tz "Europe/Berlin" --times 1
```

Once the job fires, it is automatically marked `completed` and will not run again.

---

## Listing and Managing Jobs

### List all jobs

```bash
agent-chat-gateway schedule list
```

Output:

```
ID              WATCHER               STATUS      CRON              RUNS          NEXT RUN (UTC)          MESSAGE
acg-bb47e7f4    general-watcher       active      0 9 * * 1-5       3/∞           2026-04-10 09:00:00     Run daily standup
acg-2f6cb289    ops-watcher           paused      */30 * * * *      12/∞          -                       Check server health
acg-b8c2a409    general-watcher       completed   * * * * *         1/1           done 2026-04-09 07:23   提醒：去刷牙
```

- **RUNS** shows `run_count / max_runs` (∞ means no limit).
- **NEXT RUN** shows `-` for paused jobs and `done <timestamp>` for completed ones.

### Filter by connector

```bash
agent-chat-gateway schedule list --connector rc-home
```

### Show all jobs including completed

```bash
agent-chat-gateway schedule list --all
```

### Pause a job

```bash
agent-chat-gateway schedule pause acg-bb47e7f4
```

Paused jobs do not fire until resumed. The `NEXT RUN` column shows `-`.

### Resume a paused job

```bash
agent-chat-gateway schedule resume acg-bb47e7f4
```

### Delete a job

```bash
agent-chat-gateway schedule delete acg-bb47e7f4
```

Deletion is permanent. Completed jobs can also be deleted to clean up the list.

---

## Tool Allow-List Rules

### Owners

Owners have `agent-chat-gateway send`, `agent-chat-gateway schedule`, and `date` auto-approved. No configuration is needed. When the agent runs a schedule command on your behalf, it is never blocked waiting for your approval.

### Guests

Guest rules are intentionally all-manual — there are no built-in guest approvals. You decide explicitly what guests can trigger.

If you want guests to be able to request schedules (i.e., ask the agent to create a job), add rules to the agent's `guest_allowed_tools` in your `config.yaml`:

```yaml
agents:
  my-agent:
    guest_allowed_tools:
      # Let guests ask the agent to run schedule commands
      - tool: "Bash"
        params: "agent-chat-gateway\\s+schedule\\s+.*"
      # Let the agent use date to compute relative times (used in --at values)
      - tool: "Bash"
        params: "date(\\s+.*)?"
```

Without these entries, the agent will pause and ask an owner to approve each `schedule` command a guest triggers.

> **Tip:** If guests should only be able to *see* scheduled output (the agent responds in the room when a job fires) but not *create* new jobs themselves, no guest rule changes are needed — scheduled jobs fire in the owner context and post to the room normally.

---

## Catch-Up Behavior on Restart

When ACG restarts (e.g., after a system reboot or a config change), any job that was due while the daemon was down is fired immediately on startup. This means:

- A daily job that was supposed to run at 09:00 while ACG was offline will fire as soon as ACG comes back up.
- If multiple jobs were missed, all of them fire in sequence at startup.

This is intentional — no missed reminders, no silent skips. If you want to avoid catch-up fires for a specific job, pause it before stopping ACG.

---

## Storage

All job state is persisted in:

```
~/.agent-chat-gateway/jobs.json
```

Each job record contains:

| Field | Description |
|---|---|
| `id` | Unique job identifier, format `acg-xxxxxxxx` |
| `watcher` | The watcher name the job targets |
| `cron` | The cron expression derived from `--every` or `--at` |
| `timezone` | IANA timezone string |
| `times` | Max runs (`0` = forever) |
| `run_count` | How many times the job has fired so far |
| `status` | `active`, `paused`, or `completed` |

You can inspect or back up `jobs.json` directly. Do not edit it while ACG is running — restart ACG after any manual edits.

---

## Timezone Handling

All times you specify with `--at` are interpreted in the timezone given by `--tz`. If `--tz` is omitted, UTC is used.

```bash
# Fires at 09:00 Taipei time every day
agent-chat-gateway schedule create general-watcher "Morning briefing" --at "09:00" --tz "Asia/Taipei"

# Fires at 09:00 UTC every day (same as no --tz)
agent-chat-gateway schedule create general-watcher "Morning briefing" --at "09:00"
```

`NEXT RUN` in `schedule list` is always displayed in UTC regardless of the job's configured timezone.

Use any valid [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones), such as:

- `UTC`
- `Asia/Taipei`
- `America/New_York`
- `Europe/Berlin`
- `Asia/Tokyo`
- `America/Los_Angeles`
