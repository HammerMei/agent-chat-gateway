# Config editing tool — direction (decision record)

Status: **decided, not yet implemented.** This document records the decision
so the direction survives between sessions; no TUI code exists yet. The v0.2
format simplification (`connector_defaults`/`agent_defaults`/`watcher_defaults`,
`tool_presets`, watcher `rooms:`) plus `acg config validate` and the JSON
Schema (see `docs/migration-0.2.md`) are prerequisites and have landed.

## Problem

`config.yaml` is hand-edited YAML. Even after the v0.2 format shrinks it
considerably, typos are still possible, and the onboarding wizard
(`gateway/onboard.py`) and any future interactive editor would otherwise be
two separate things to maintain with duplicated logic.

## Decision: a `textual`-based full-screen TUI, absorbing onboarding

**Rejected: a local web UI.** An HTTP endpoint that edits permission
allow-lists is a real attack surface (CSRF from any local browser tab, other
local users) — directly against the OpenClaw-style principle this project
already follows of not trusting user-controlled/network-reachable surfaces
with config that grants tool access. It also needs port-forwarding to be
usable when ACG runs on a remote server reached over SSH, which is the
common deployment shape for this project.

**Rejected as the primary interface: plain CLI subcommands**
(`acg config add-connector`, etc.). These don't give an overview of what's
already configured, and a growing flag surface per subcommand doesn't
converge into "one tool." `acg config validate` stays as a plain command
regardless — it's a scriptable check, not an editor.

**Chosen: `textual`** (same authors as `rich`, which is already a
dependency; textual builds on top of it). It runs over plain SSH — the
primary way this project is operated — with zero network surface, and its
widget/form model gives a natural mapping from the JSON Schema to editable
fields.

## Consolidation: one interactive surface, not two

- `acg config` launches the app.
  - Missing/empty `config.yaml` → setup flow (the current wizard, expressed
    as screens: backend detection, connector credentials, first watcher).
  - Existing `config.yaml` → editor: an overview screen listing
    connectors/agents/watchers with live validation status, drilling into
    per-entity forms.
- `acg onboard` becomes an alias into the setup flow, then is deprecated once
  the TUI ships.
- `gateway/onboard.py`'s non-UI logic (`detect_agent_backends`, `.env`
  writing with 0600 perms, `install_meta.json`, opencode plugin install) is
  reused as-is by the setup flow; only the `rich` `Prompt`/`Confirm` wizard
  UI is eventually deleted.

## Single source of truth for validation

- **Field-level** (types, enums, defaults, descriptions): the JSON Schema
  (`gateway/schema/config.schema.json`). The TUI generates form widgets and
  help text from it; `acg config validate` and the schema-sync tests
  (`tests/unit/test_config_schema.py`) check documents against it.
- **Cross-field / semantic** (watcher→connector/agent references, duplicate
  names, `timeout > permissions.timeout`, working_directory existence): this
  logic currently lives inline in `GatewayConfig.from_file`
  (`gateway/config.py`) and in `gateway/config_validate.py`
  (`validate_config()`), which already wraps `from_file` plus the extra
  per-connector-type and state-orphan checks. `validate_config()` is written
  as a plain, reusable function (not CLI-only) specifically so the TUI's
  save-time check can call the same code path as `acg config validate` — no
  third implementation of "is this config valid."
- **Note for whoever implements the TUI:** `GatewayConfig.from_file` has a
  side effect (`load_dotenv`) and a hard filesystem check
  (`working_directory` must exist) baked into the same pass as pure
  structural validation. Before wiring up live-as-you-type validation in the
  editor, consider whether that check needs to become a separate, explicitly
  invoked mode (e.g. "check syntax" vs "check ready to run") — that wasn't a
  concern for a single validate-then-report CLI command, but matters for a
  UI that re-validates on every keystroke.

## Naming note

`gateway/tools/tui.py` already exists — it's an unrelated interactive REPL
for chatting with agent backends directly (`gateway-tui` console script),
not a config editor. A naming collision only; the config tool should live in
its own module (e.g. `gateway/configtool/`), not `gateway/tools/`.

## Incremental delivery

1. **M1 (MVP):** read-only overview screen (connectors/agents/watchers +
   inline validation errors from `validate_config()`), plus an
   "open in `$EDITOR`, re-validate on return" action. Small, immediately
   useful, establishes the skeleton.
2. **M2:** watcher editing (highest-churn entity — add/remove/edit room
   bindings via `rooms:`), schema-driven forms, atomic save with a
   timestamped backup (reuse `onboard.py`'s existing backup convention).
   Decide at this point whether to adopt `ruamel.yaml` for comment-preserving
   round-trip writes, or accept comment loss (PyYAML) with a pre-save backup.
3. **M3:** agent editing (tool allow-lists using `tool_presets`) and
   connector editing (credentials → `.env`, reusing `_write_env`).
4. **M4:** setup flow (empty-config path) reusing the same forms;
   `acg onboard` aliases it; delete the wizard's `Prompt`/`Confirm` UI so
   exactly one interactive surface remains.
