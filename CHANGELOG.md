# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
