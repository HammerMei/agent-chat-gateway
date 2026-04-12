# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.8] - 2026-04-12

### Fixed
- **`LimitOverrunError` on large tool results**: the two streaming
  `create_subprocess_exec` calls in `ClaudeBackend` now set
  `limit=16 * 1024 * 1024` (16 MB) on the underlying `asyncio.StreamReader`,
  raising it from the 64 KB default. Large single-line outputs — base64-encoded
  images or audio attachments — previously caused
  `asyncio.exceptions.LimitOverrunError: Separator is not found, and chunk
  exceed the limit`, crashing the turn. The non-streaming path that uses
  `communicate()` is unaffected and unchanged.

---

## [0.2.7] - 2026-04-12

### Fixed
- **`acg reset` AttributeError**: removing `--connector` from the reset subparser
  left a stale `args.connector` reference in the reset handler, causing
  `AttributeError: 'Namespace' object has no attribute 'connector'` on every
  `acg reset` invocation. The dead code has been removed.

---

## [0.2.6] - 2026-04-11

### Fixed
- **Permission cross-room bypass**: `approve`/`deny` commands are now scoped to
  the originating room — an owner in room B can no longer resolve a permission
  request that was raised in room A. The registry entry is left pending (not
  removed) on a mismatch so the correct room can still resolve it, and the
  response is identical to "no pending request" to avoid leaking request
  existence across rooms.
- **Permission cross-thread confusion**: when both the pending request and the
  incoming command carry a `thread_id`, they must match. Approvals sent from a
  different thread within the same room are rejected. Room-level approvals
  (`from_thread_id=None`) are intentionally still allowed — no major chat
  platform (RC, Slack, Discord, Teams) enforces thread-level permissions
  separate from the room, so blocking room-level approval would hurt UX with
  no security benefit.
- **RC connector `_handle_send_busy` TypeError**: the `thread_id` kwarg was
  incorrectly named `thread_id=` instead of `tmid=` when calling
  `post_message()`, causing a `TypeError` on every busy-notification attempt.
  The error was silently swallowed by the caller, so busy users never received
  the retry message. Fixed to use the correct `tmid=` kwarg.
- **Scheduler `run_count` consumed on injection failure**: finite jobs
  (`times > 0`) no longer lose a run when the target watcher is unavailable.
  On failure, `next_run` is advanced (to avoid a retry flood) but `run_count`
  and `last_run` are left unchanged so the remaining budget is preserved.
  Infinite jobs (`times = 0`) still advance `run_count` on failure, consistent
  with prior behaviour (the count is non-binding for completion).
- **Scheduler catch-up replay of previously-failed fires**: added
  `last_attempted_at` field to `ScheduledJob` (set on every `_fire_once` call,
  including failures). On daemon restart, the catch-up anchor now uses
  `last_attempted_at` instead of `last_run`, preventing replay of fire slots
  where injection already failed. Backward-compatible: old `jobs.json` files
  without the field fall back to `last_run` as before.

### Changed
- **`acg reset` no longer requires `--connector`**: watcher names are globally
  unique across all connectors, so the control server now resolves the owning
  connector automatically by searching all entries. The `--connector` flag has
  been removed from the CLI to eliminate a redundant and confusing argument.
- **Scheduler injection failure notifications**: when a scheduled job cannot be
  delivered (watcher not running), a best-effort notification is sent directly
  to the watcher's room via the connector — bypassing the watcher queue so it
  arrives even when the watcher is paused or its queue is full. Paused watchers
  log at INFO level (expected state) rather than WARNING and receive no
  notification; only unexpected failures (non-paused watcher unavailable) get
  notified.

---

## [0.2.5] - 2026-04-11

### Added
- **`Skill` tool allow-list support**: added `"skill"` field mapping to
  `_CLAUDE_PARAM_FIELD` so the `Skill` tool extracts the skill name directly
  (e.g. `"daily-briefing"`) instead of falling back to the full JSON blob.
  Config rule `params: "daily-briefing"` now correctly auto-approves Skill
  tool calls without triggering a permission prompt.
- **Daily-briefing runner script** (`gateway/agents/opencode/skills/daily-briefing/runner.py`):
  stdlib-only Python script that fetches all briefing data sources in parallel
  (stocks via Yahoo Finance/stooq, TechCrunch RSS, Hacker News, GitHub
  Trending, world news RSS, entertainment RSS) and prints a single JSON object
  to stdout. Reduces daily briefing run time from ~4–5 min to ~1–1.5 min by
  collapsing multiple LLM/HTTP round-trips into one parallel fetch + one LLM
  pass for summary and HTML generation.

---

## [0.2.4] - 2026-04-11

### Fixed
- **Heredoc command matching in allow-list**: bash commands using heredoc
  redirects (`python3 << 'EOF' ... EOF`) were incorrectly reduced to just
  the interpreter name (`python3`) by the tree-sitter AST walker. The full
  `redirected_statement` text (including the heredoc body) is now extracted,
  so allow-list patterns can inspect the heredoc content — e.g.
  `python3.*github\.com/trending.*` now correctly matches a Python heredoc
  that fetches GitHub trending. Compound commands with heredoc (e.g.
  `python3 << 'EOF'...EOF && rm -rf /`) are still split correctly — the
  dangerous sub-command is extracted separately and must also satisfy the
  allow list.

---

## [0.2.3] - 2026-04-11

### Fixed
- **Watcher validation at schedule create time**: unknown watcher names are
  now rejected immediately with an actionable error listing all available
  watcher names — the agent can self-correct without waiting for fire time.

### Changed
- **`--connector` removed from `acg schedule create`**: the watcher name
  uniquely identifies the connector; specifying `--connector` was redundant
  and was the root cause of the watcher validation bypass bug.
- **`scheduling-context.md`**: added explicit warning to always use the exact
  watcher name from `acg list` — do not guess or invent names.

---

## [0.2.2] - 2026-04-11

### Changed
- **`JobStore.save()` cleanup**: removed the EBUSY fallback added in 0.2.1 —
  superseded by the `data/` directory mount which allows atomic `rename(2)`
  natively. Drops the unused `errno` import.

---

## [0.2.1] - 2026-04-11

### Fixed
- **Docker EBUSY error**: `JobStore.save()` now falls back to in-place write
  when `rename()` returns `EBUSY` (caused by Docker single-file bind-mounts
  pinning the file inode).
- **`jobs.json` moved to `data/` subdirectory** (`~/.agent-chat-gateway/data/jobs.json`):
  use a directory bind-mount (`./data:/root/.agent-chat-gateway/data`) instead
  of a single-file mount to avoid the EBUSY issue entirely. The `data/`
  directory is pre-created in `Dockerfile.acg` and in `docker-compose.example/`.
  Future persistent runtime files can be added to `data/` without changing
  the Docker volume configuration.

### Migration (Docker users upgrading from 0.2.0)
If you had `./jobs.json` mounted as a single-file volume:
1. `mkdir data && mv jobs.json data/`
2. Update `docker-compose.yml`: replace `- ./jobs.json:/root/.agent-chat-gateway/jobs.json`
   with `- ./data:/root/.agent-chat-gateway/data`
3. `docker compose up -d`

---

## [0.2.0] - 2026-04-10

### Added
- **In-process job scheduler** (`acg schedule`): schedule recurring or one-shot
  agent tasks without leaving the chat. Jobs persist across restarts in
  `~/.agent-chat-gateway/jobs.json` with atomic writes.
- **`acg schedule create`**: create recurring jobs (`--every 1h`, `--every 1d`,
  `--every 1w`) or one-shot reminders (`--in 30m`, `--in 2h`), with optional
  `--times N` run limit and `--tz` timezone support.
- **`acg schedule list`**: display active/paused jobs in a formatted table;
  `--all` includes recently completed jobs.
- **`acg schedule delete / pause / resume`**: full lifecycle management.
- **Direct message injection**: scheduled jobs bypass the Rocket.Chat self-message
  filter entirely — messages are injected directly into the watcher's message
  processor queue as `OWNER`-role messages.
- **Catch-up on restart**: all missed fires are replayed immediately on daemon
  startup, with correct run-count tracking.
- **`scheduling-context.md`**: built-in context file auto-injected into every
  agent session, teaching the agent the `acg schedule` CLI commands.
- **Thread-safe `JobStore`**: `threading.Lock` + copy-on-write pattern ensures
  concurrent reads/writes from `asyncio.to_thread()` workers are safe.
- **TTL-based completed job purge**: completed jobs are automatically removed
  after `scheduler.completed_job_ttl_days` (default 7 days).
- **`gateway/core/tz_utils.py`**: cross-platform IANA timezone detection utility.
- **New dependency**: `croniter>=2.0.0` for cron expression parsing.

### Changed
- Built-in context files (`rc-gateway-context.md`, `scheduling-context.md`)
  moved from `contexts/` to `gateway/contexts/` (shipped inside the Python
  package) so they are always available regardless of install method.
- `config.py`: built-in context files are now auto-injected at Layer 0 for all
  connectors; no manual `context_inject_files` entry needed for the defaults.

---

## [0.1.9] - 2026-04-06

### Changed
- Re-release of 0.1.8 to fix PyPI publish after history rewrite removed
  `docker_env/` (which contained sensitive data) from all prior commits.
  No functional code changes from 0.1.8.

---

## [0.1.8] - 2026-04-06

### Added
- **Docker support**: `Dockerfile.acg`, `docker/entrypoint.acg.sh`, and
  `docker/docker-compose.example/` for deploying ACG via Docker. The image
  is published to `ghcr.io/hammermei/agent-chat-gateway` on every release.
- **GitHub Container Registry**: `.github/workflows/docker.yml` builds and
  pushes `linux/amd64` + `linux/arm64` images on every `v*` tag.

---

## [0.1.7] - 2026-04-06

### Fixed
- **OpenCode bash permission bypass**: OpenCode's default permission ruleset
  uses `"*": "allow"`, which caused all bash commands to run without emitting
  a `permission.asked` SSE event, completely bypassing ACG's permission broker.
  ACG now injects `bash["*"] = "ask"` via `OPENCODE_CONFIG_CONTENT` at sidecar
  startup so that bash tool calls are properly intercepted and enforced by
  `owner_allowed_tools` / `guest_allowed_tools`. A set of read-only git commands
  and `agent-chat-gateway send` are pre-approved as safe defaults. Users who
  explicitly set a `"*"` catch-all in their own `OPENCODE_CONFIG_CONTENT` are
  unaffected.

---

## [0.1.6] - 2026-04-04

### Added
- **OpenCode SSE streaming** (`stream()` method on `OpenCodeBackend`): intermediate
  agent events — tool calls, text deltas, step completions — are now surfaced in
  real time via the `GET /event` SSE endpoint instead of waiting for the full turn
  to complete. Events are consumed via an `asyncio.Queue` background task and yielded
  as `AgentEvent` objects with deduplication and deadline enforcement.
- `_post_message_async()`: new fire-and-forget POST to `/session/{id}/prompt_async`
  (HTTP 202) using a dedicated `_PROMPT_ASYNC_POST_TIMEOUT` internal constant.
- RC typing-indicator is now refreshed periodically during long SSE streaming turns.

### Changed
- `_SSE_QUEUE_POLL_INTERVAL` renamed to `_SSE_QUEUE_MAX_WAIT` to more accurately
  describe its role as an upper bound on `queue.get()` blocking time.
- `has_usage` token sentinel now includes cache token buckets so that cache-only
  turns correctly produce a `TokenUsage` object instead of returning `None`.
- `duration_ms` in `AgentResponse` is coerced to `int` (or `None`) from the HTTP
  response; the SSE path explicitly does not populate this field.
- All error messages across the OpenCode adapter are now sanitized: no raw exception
  strings, response bodies, or internal host:port values appear in user-facing errors.

### Fixed
- `base_url` is captured before spawning the SSE background task to eliminate a
  race with concurrent `stop()` calls that could null out `self._base_url`.
- `assert` statement replaced with `if/raise` for the SSE handshake invariant check
  (bare asserts are stripped under Python `-O` optimization flag).
- Token accumulator fields (`input_tokens`, `output_tokens`, etc.) now apply
  `int()` coercion in both SSE and HTTP parse paths, preventing silent float
  violations of `TokenUsage`'s `int` type contract.
- `create_session` no longer includes the raw API response dict in `RuntimeError`
  messages; raw body is logged at `DEBUG` level instead.

---

## [0.1.5] - 2026-04-01

### Added
- Context files (`contexts/`) are now copied to `~/.agent-chat-gateway/contexts/`
  on install so that `config.yaml` path references resolve correctly without
  pointing into the git repo.
- `upgrade` now syncs context files after `git pull` with smart merge:
  unchanged user copies are overwritten; user-modified copies are saved as
  `<name>.default` with a warning to merge manually.

### Changed
- `config.example.yaml`: moved `contexts/rc-gateway-context.md` from
  agent-level to connector-level `context_inject_files` so it is shared
  across all agents using that connector.
- `onboard.py`: generated config now sets connector-level
  `context_inject_files` (was incorrectly empty before).

### Fixed
- `install.sh`: `RUNTIME_DIR` is now defined before the context copy block
  (was referenced before assignment in the previous release).

---

## [0.1.4] - 2026-04-01

### Fixed
- `install.sh`: always write `install_meta.json` so that `upgrade` works even
  when `--no-onboard` skips the interactive wizard.
- `upgrade`: resolve `uv` via common fallback paths (`~/.local/bin/uv`,
  `~/.cargo/bin/uv`) when it is absent from PATH — common in SSH sessions.

---

## [0.1.3] - 2026-04-01

### Added
- Agent can now send files or attachments to Rocket.Chat by running
  `agent-chat-gateway send <room> --attach /path/to/file` directly.
  Added Bash allow-rule (`agent-chat-gateway\s+send\s+.*`) to
  `config.example.yaml` for both owner and guest tool lists (Claude and
  OpenCode sections), and documented the pattern in
  `contexts/rc-gateway-context.md`.
- Added `--no-onboard` flag to `install.sh` for agent-driven installs that
  skip the interactive onboarding wizard.
- Installer now informs the user of the executable location and the shell
  source command needed to activate it post-install.

### Fixed
- OpenCode adapter: gracefully handle empty body and non-JSON API responses
  instead of crashing with an unhandled exception.
- Installer: install `uv` first, then use it to install Python 3.12 when the
  system Python is too old.

### Changed
- `install.sh` now clones the repo into `~/.agent-chat-gateway/repo` instead
  of directly into `~/`.

### Docs
- Clarify `working_directory` as the project folder; default to current
  directory when omitted.
- Add PATH setup step for pip-installed packages.

---

## [0.1.2] - 2026-03-30

### Fixed
- `upgrade` command: detect pip-installed packages and run
  `pip install --upgrade agent-chat-gateway` automatically.

### Docs
- Promote AI-guided install path, add git prerequisite note, make
  `install-agent.md` more agent-friendly.

---

## [0.1.1] - 2026-03-30

### Fixed
- Move dependencies into `[project]` section in `pyproject.toml`.
- Resolve all ruff lint violations and pre-existing `PID_FILE` import error.

---

## [0.1.0] - Initial release

First public release of agent-chat-gateway.
