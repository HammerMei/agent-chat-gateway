# Config editing tool — full design (M1–M3) + implementation status

Status: **Phase 1 shipped** (read-only overview + detail screens + $EDITOR
escape hatch). **Phase 2 in progress:** items 7–10 from the Phase 1 code
review cleared; `EditableConfig.save()`/dirty-tracking/`ConfirmModal`
foundation shipped; agent create/edit shipped (`AgentDetailScreen`,
`TypePickerModal`, `n` on the Agents tab). Connector create/edit, the `.env`
writer, and the tool-list/preset editor are not yet built. Phase 3 is
designed below but not yet started. The
v0.2 format simplification (`connector_defaults`/`agent_defaults`/
`watcher_defaults`, `tool_presets`, watcher `rooms:`) plus `acg config
validate` and the JSON Schema (see `docs/migration-0.2.md`) are prerequisites
and have landed. A `description:` field (free-text, informational-only,
ignored at runtime) was added to connectors/agents/watchers/`*_defaults`
blocks alongside Phase 1, specifically so annotations survive a future
TUI-driven save without needing YAML-comment preservation (see "YAML I/O"
below).

Reached via `agent-chat-gateway config` (no subcommand). `agent-chat-gateway
config validate` stays a separate, scriptable command backed by
`gateway/config_validate.py` — never touched by anything below.

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
converge into "one tool."

**Chosen: `textual`** (`textual>=8.2.0`; pulls in an effective `rich>=14.2.0`
floor, up from this project's prior `rich>=13.0` — confirmed at `uv lock`
time to cause no other conflicts). Runs over plain SSH with zero network
surface.

## Part 1 — Data/persistence model

### Keystone: the editor reads/writes the PRE-MERGE raw document, never `GatewayConfig`

`GatewayConfig.from_file` deep-merges `*_defaults`, expands `rooms:` groups,
resolves tool presets, and expands `$VAR` env references — all one-way,
provenance-destroying transforms, and the last one means loading (or ever
saving) through that path would write **resolved secrets** into config.yaml.
`gateway/configtool/model.py`'s `EditableConfig` loads via plain
`yaml.safe_load` instead:

```
EditableConfig
├── document: dict            (yaml.safe_load directly — never GatewayConfig.from_file)
├── path: Path
├── provenance(kind, entry, field) -> EXPLICIT | INHERITED | EXPLICIT_SUPPRESSING
│     replays the REAL _deep_merge / _extract_defaults_block imported from
│     gateway/config.py (never reimplemented)
├── merged_entry(kind, entry) -> dict   (real _deep_merge; the effective value)
├── expanded_watchers() -> list[ExpandedWatcher]
│     pairs each validated_view() WatcherConfig with the raw `rooms:` entry
│     (and sibling-room count) it came from, by replaying the loader's own
│     room-counting order — no name-generation/merge logic reimplemented
├── validated_view() -> GatewayConfig   (read-only; display/cross-ref only)
├── dirty: bool / mark_dirty()   # Phase 2 foundation, shipped
│     the ONE sanctioned seam after any in-place edit to `document` — clears
│     the defaults_block() cache and flips `dirty`. There is deliberately no
│     per-field mutation API (no `set_entry_field()`) on EditableConfig
│     itself: each Phase 2/3 edit screen mutates `document` (or a raw dict
│     reachable from it) in whatever shape that screen's form needs, then
│     calls mark_dirty(). This resolved code review item 7 — the original
│     open question was what shape a generic accessor API should take;
│     the answer is that there isn't one, only the invalidation seam.
└── save()   # Phase 2 foundation, shipped — see decision 5 below
```

Shipped in Phase 1: `document`, `provenance`/`field_provenance`,
`merged_entry`, `expanded_watchers`, `validated_view`, plus a `StatusIndex`
that groups `ValidationResult.findings` by `(entity_kind, entity_name)` for
per-row status lookups.

### Decisions for later phases

1. **YAML I/O: plain PyYAML + `description:` fields, not `ruamel.yaml`**
   (owner decision, made instead of the originally-planned "adopt ruamel for
   comment-preserving round-trip writes"). Every connector/agent/watcher
   entry and every `*_defaults` block may carry an optional free-text
   `description:` — informational only, ignored at runtime, editable as a
   normal form field. Because it's data, PyYAML round-trips it perfectly; no
   new dependency, no `CommentedMap` in-place-mutation invariant to maintain.
   **Trade-off, documented in `docs/migration-0.2.md`:** a TUI-driven save
   (phase 2+) loses pre-existing hand-written YAML comments on that file (a
   timestamped backup is taken first, matching `onboard.py`'s
   `config.yaml.bak.<unix-ts>` convention) — users migrate prose they care
   about into `description:` fields ahead of time.
2. **Editing an inherited field always writes an explicit per-entry
   override** (smallest blast radius a single-entry form can take). Every
   form field shows its provenance. "Change the shared default instead" is
   only offered on a dedicated Defaults screen, which shows blast radius ("N
   entries inherit, M override") before commit. Surface `--lint`'s existing
   "duplicates the default" findings as the nudge to promote repeated
   overrides into defaults. List fields are provenance-binary at the
   whole-list level (merge replaces lists wholesale — no per-item marking).
3. **`rooms:` group editing — two-tier rule:** deleting a room removes it
   from the list (normalizing `rooms: [x]` → `room: x`); editing a
   **per-room** field (`name`/`session_id` — already hard-restricted to
   single-room by `from_file` — plus `online/offline_notification`,
   `history_handoff`, `context_inject_files`) auto-splits that room into its
   own single-room entry, inserted adjacent to the source group; editing a
   **group-shared** field (`connector`, `agent`) edits in place, whole group
   moves together.
4. **Entity creation: schema-driven where the schema is complete, per-type
   templates where it's deliberately open.** Agent + watcher forms generate
   from the JSON Schema (`$defs` are `additionalProperties: false` — zero
   drift). Connector `raw` is deliberately open in the schema — per-type
   starter templates (rocketchat/mattermost/voice/script) pre-fill a create
   flow, a generic nested tree editor handles the body. Template drift
   degrades guidance only, never correctness — `_check_connectors`' real
   dataclass instantiation is the backstop.
5. **Save flow: validate-before-write via a same-directory temp file.
   Shipped** as `EditableConfig.save()`. Serializes to `config.yaml.tmp`
   **beside** the real file (not `/tmp` — `working_directory`/
   `context_inject_files` resolve relative to `config_dir`), runs the
   unchanged `validate_config(tmp)`, blocks save on errors (raises
   `ValueError`, temp file deleted, real file untouched); on success:
   timestamped backup (`config.yaml.bak.<unix-ts>`, `shutil.copy2`, matching
   `onboard.py`'s convention) then `os.replace()` (atomic on POSIX) — the
   daemon never observes a partially-written config.yaml. `dirty`/
   `mark_dirty()` also shipped alongside it; `ConfigToolApp.action_quit()`
   gates on `dirty` via a new `ConfirmModal` (Textual `@work`-decorated,
   since `push_screen_wait()` requires a worker context) — no edit screen
   sets `dirty` for real yet, but the mechanism is in place for Phase 2's
   CRUD screens to use without further plumbing.
6. **`.env` secret handling:** password/token/secret fields get a per-field
   "store in .env" toggle (default ON) → `${VAR}` placeholder in config,
   value into `.env`. Needs a new merge-by-key `.env` writer (`onboard.py`'s
   `_write_env` overwrites the whole file with 3 hardcoded keys — insufficient
   once multiple connectors each need their own secret upserted).

## Part 2 — Screens / navigation / UX

### Screen inventory

| Screen | Kind | Status |
|---|---|---|
| `OverviewScreen` | root | **Shipped.** 5 tabs: Connectors, Agents, Watchers, Defaults, Tool Presets |
| `AgentDetailScreen` | pushed | **Shipped, all 3 modes.** view/edit/create. Form fields are a manually-maintained mirror of `$defs/agent` (not a runtime schema interpreter — safe since the schema is closed). Nothing is written to `document` until Save; Save diffs every field against its value-at-open and writes only what changed (docs/design/config-tool.md decision 2). Tool-list fields (`owner_allowed_tools`/`guest_allowed_tools`) stay view-only here — separate tool-list-editor work. Escape with unsaved changes routes through `ConfirmModal` |
| `ConnectorDetailScreen` / `WatcherDetailScreen` | pushed | **Shipped**, `mode="view"` only still — connector edit/create is the next Phase 2 slice; watcher CRUD is Phase 3. Each already takes a `mode: Literal["view","edit","create"]` param — no screen-class rework needed when their turn comes |
| `DefaultsScreen` | pushed | **Shipped** (view-only): shows blast radius per key |
| `ToolPresetsScreen` | pushed | **Shipped** (view-only): rule list + "used by" (checked against the MERGED per-agent tool list, not the raw entry — see gotchas below) |
| `ConfirmModal` | modal | **Shipped** (`gateway/configtool/modals.py`) — yes/no dialog, Cancel focused by default. Gates `ConfigToolApp.action_quit()` on `EditableConfig.dirty`, and `AgentDetailScreen`'s own per-screen form-dirty flag on Escape |
| `TypePickerModal` | modal | **Shipped** (`gateway/configtool/modals.py`) — generic list-of-strings picker (`ListView`-based), used today for the agent-type picker (claude/opencode); the connector-type picker (rocketchat/mattermost/voice/script) will reuse the same class |
| `EntityPickerModal`, `PresetOrInlineModal`, `InlineToolRuleModal` | modals | Not yet built (phase 2/3) |
| `RoomListEditorScreen` | pushed (2nd level) | Not yet built (phase 3) |
| `$EDITOR` escape hatch | action | **Shipped.** `App.suspend()` + subprocess; Overview-only |
| `HelpScreen` | modal or pushed | **Not yet built — owner-requested addition, tracked for phase 2.** On mount, focus starts on the tab bar rather than the list (a real gap a user hit) — Phase 1's fix was surfacing the existing `tab`→focus-next binding in the footer (`Binding("tab", "app.focus_next", "Focus next / enter list", show=True)` on `OverviewScreen`), but a dedicated help screen (`?` binding, listing every screen's keybindings) is the more scalable fix once phase 2/3 add enough screens/actions that the footer alone gets crowded |

Max stack depth 3 (Overview → detail → modal/RoomListEditor).

### Key UX flows (design, ahead of phase 2/3 implementation)

- **New connector:** `n` → type picker (rocketchat/mattermost/voice/script) →
  detail screen in create mode, per-type template pre-filled. Mattermost gets
  a token-XOR-user/pass toggle (mirrors `MattermostConfig.__post_init__`);
  secrets masked + `.env` toggle.
- **New agent — shipped.** Type picker (claude/opencode, via the generic
  `TypePickerModal`) → form (`gateway/configtool/screens/agent_detail.py`,
  a manually-maintained mirror of `$defs/agent`, matching Phase 1's
  `_KNOWN_FIELDS` pattern rather than a runtime schema interpreter — safe
  because the schema is closed). **Correction from the original design:**
  `working_directory` existence is NOT a soft warning at save time —
  `GatewayConfig.from_file` hard-requires the directory to exist (raises
  `ValueError`), and `save()` reuses that unchanged validator by design (see
  decision 5's rationale for never special-casing it), so a missing
  directory still hard-blocks Save. What's actually built: an early, live,
  non-blocking inline hint next to the field (updates as you type, resolved
  the same way the loader resolves it — `expanduser()` then relative to
  `config_dir`) so the user finds out before hitting Save, not just from a
  generic validator error after.
- **New watcher:** connector Select + agent Select + room(s) free text
  (single or comma-list — mirrors the `room`/`rooms` duality already in the
  format). Agent Select pre-suggests the connector's existing pairing
  (1-agent-per-connector convention per this repo's `CLAUDE.md`), never
  enforces it.
- **Watchers table shows EXPANDED rows** (what `list`/`pause`/`resume`/
  `reset` operate on) — shipped. Detail screen shows a persistent
  group-membership banner when the watcher's raw entry has `rooms:` with more
  than one room; split-triggering edits/deletes (phase 3) will route through
  a `ConfirmModal` naming the group and the specific room being split.
- **Per-agent tool list:** renders the raw representation (`→ preset: name`
  vs `tool / params`), never the resolved flat list — shipped, view-only.
  Add-item flow (phase 3): reference existing preset / write inline rule
  (live regex validation via the same compile checks as
  `ToolRule.from_config`) / create a new preset inline (detour through
  `ToolPresetsScreen`).

### Validation attribution (structured, additive — shipped)

`gateway/config_validate.py`'s `ValidationResult` gained `findings:
list[Finding]` (`severity`, `entity_kind`, `entity_name`, `field`, `message`)
alongside the existing flat string lists, which remain untouched (`acg
config validate`'s CLI output is byte-identical — regression-tested). Honest
boundary, as designed: `_check_connectors`/`_lint_config` findings are
per-entity (often per-field); a `GatewayConfig.from_file` load failure is
inherently global (`entity_kind="global"`, `entity_name=None`) — the Overview
shows that as one banner, never smeared into false per-row status.

**Known, accepted gap:** `_lint_config`'s per-watcher findings are attributed
to the RAW entry's own `name` (or a `watchers[i]` placeholder when unnamed).
For a multi-room `rooms:` group with no explicit name, that placeholder
matches none of the group's expanded watcher names, so those specific lint
findings don't surface on any single row in the Watchers table (they're
never dropped from `result.lint_findings` overall — only from the per-row
index). Not worth fixing until a phase where lint findings need field-level
attribution on expanded watchers specifically.

## Part 3 — Phase 2/3 order (owner decision — swapped from the original M2/M3)

**Phase 2: Agent/connector CRUD + shared resources** (moved ahead of watcher
CRUD). Rationale: "add a new bot" = connector + agent first, watcher second —
matches the actual dependency order; creation flows are also where the TUI
beats `$EDITOR` most (typed forms, credential handling via `.env`, live regex
validation), so front-loading them delivers value sooner. Before any new UI,
Phase 2 first cleared the Phase 1 code review's deferred items 7–10
(refactor/perf only, no behavior change — see "Implementation notes" below):

- **Shipped:** items 9/10 (renamed `refresh_overview()` →
  `repaint_from_memory()`; extracted a shared `DetailScreen` base class for
  the five detail/list screens) and item 8 (`EditableConfig.defaults_block()`
  cached per kind, invalidated by `load()`/`reload()`/`mark_dirty()`).
- **Shipped:** item 7 (`EditableConfig` accessor redesign) resolved as
  `dirty`/`mark_dirty()` — see the keystone diagram above — landing
  together with `EditableConfig.save()`, since the right shape depended on
  what the mutation layer needed, per the plan's own risk note.
- **Shipped:** `EditableConfig.save()` (decision 5) and `ConfirmModal`
  (`gateway/configtool/modals.py`), gating `ConfigToolApp.action_quit()`.
- **Shipped:** `AgentDetailScreen` edit/create + `TypePickerModal` (generic,
  built for reuse by the connector-type picker). `n` on the Agents tab of
  `OverviewScreen` (`action_new_entity`) opens it; other tabs notify rather
  than doing nothing or crashing, since they don't support creation yet.
  Tool-list fields stay view-only on this screen (separate work, below).
  See "Implementation notes" for the write-back diffing mechanism and two
  Textual gotchas hit while building it (`@work`/`push_screen_wait`, and
  Input/Select firing `Changed` once at initial mount).

**Not yet built:** `ConnectorDetailScreen` edit/create, `TypePickerModal`'s
connector-type variant + per-type templates + generic tree editor for
connector `raw`, `.env` merge-by-key writer + secret toggle, tool-list editor
+ `PresetOrInlineModal` + `InlineToolRuleModal`, `ToolPresetsScreen` made
editable (used-by warnings).

**Phase 3: Watcher CRUD + defaults editing.** `WatcherDetailScreen`
edit/create; new-watcher flow (pickers now enumerate entities creatable
in-app since phase 2); `RoomListEditorScreen`; the `rooms:` split rule +
group `ConfirmModal`; `DefaultsScreen` made editable (lowest churn, last).
Golden-file YAML round-trip tests (key order, description retention,
split-out insertion position).

## Implementation notes from Phase 1 (for whoever builds phase 2/3)

- **Never name a Screen/Widget method `_render`** — it collides with
  Textual's own internal `Widget._render()` compositing machinery and fails
  with a confusing `AttributeError: 'str' object has no attribute
  'render_strips'` deep in Textual's rendering internals, not at your call
  site. All detail screens here use `_body_text()` instead.
- **`App.suspend()` raises `SuspendNotSupported` under `App.run_test()`'s
  headless Pilot driver** (confirmed empirically while building this). The
  $EDITOR round-trip's actual suspend+subprocess line is `# pragma: no
  cover` and manual-QA-only; `open_editor_and_reload()`'s exception handling
  around it IS tested (catches the failure, notifies, doesn't crash), and
  the reload/refresh logic downstream of a successful suspend is tested
  directly via `reload_config()`/`refresh_overview()`.
- **A "refresh" action must reload `EditableConfig.document` from disk, not
  just re-run validation.** `validate_config()` reads the file fresh
  internally on every call, but `EditableConfig.document` is loaded once at
  construction — a screen that recomputes its display from `cfg.connectors_raw`
  /`cfg.expanded_watchers()` etc. without first calling `app.reload_config()`
  (which replaces `app.editable_config` via a fresh `EditableConfig.load()`)
  silently shows stale data even though the validation banner looks current.
  Real bug hit and fixed while building the `r` (refresh) binding — the fix
  is `action_refresh()` calling `self.app.reload_config()`, not
  `self.repaint_from_memory()` directly (renamed from `refresh_overview()`
  in Phase 2's cleanup pass — the old name invited exactly this bug).
- **Any field that's commonly set via a `*_defaults` block (or via
  `agent_defaults`/`connector_defaults` transitively) must be looked up
  through `merged_entry()`, not the raw entry.** Two real display bugs were
  caught this way: the Connectors table/detail screen showing `type: ?` for
  a connector whose `type` came only from `connector_defaults`, and
  `ToolPresetsScreen`'s "used by" list missing an agent that only referenced
  the preset via `agent_defaults.owner_allowed_tools` (not on its own
  entry). Both fixed by merging before reading.
- **Every screen accessor that calls into `EditableConfig` must be wrapped in
  `try/except (ValueError, FileNotFoundError)`**, even inside the Overview
  (which already shows a load-failure banner from `validate_config()`) —
  `merged_entry()`/`defaults_block()`/`expanded_watchers()` each
  independently call the real loader again and can raise the *same* error a
  second, unhandled time if not guarded per-table/per-screen.
- **`App.push_screen_wait()` requires a Textual worker context — it calls
  `get_current_worker()` internally and raises `NoActiveWorker` otherwise.**
  A plain action method (even an `async def`) triggered by a keybinding is
  NOT automatically a worker. `ConfigToolApp.action_quit()` needs
  `await self.push_screen_wait(ConfirmModal(...))` to block on the user's
  answer, so it's decorated `@work` (from `textual`). Any future action that
  awaits a modal's result for its own control flow (rather than using
  `push_screen(..., callback=...)`) needs the same decorator — confirmed
  empirically while building the quit-confirmation flow (the first version,
  undecorated, failed every Pilot test with `NoActiveWorker`).
- **A modal's own `BINDINGS` can silently lose to a focused `Button`'s
  built-in key handling.** `ConfirmModal` originally bound `enter` to a
  screen-level `action_confirm`, matching `escape`→`action_cancel` — but
  Textual auto-focuses the first focusable widget (the Cancel button) on
  mount, and a focused `Button` handles Enter itself before it ever reaches
  a screen binding. Fixed by dropping the screen-level Enter binding
  entirely and explicitly focusing Cancel in `on_mount()` (safe-by-default:
  a reflexive Enter cancels, not confirms); Tab+Enter reaches Confirm.
  `escape`→cancel still needs its own binding since `Button` doesn't bind it.
- **`Input` and `Select` fire their own `Changed` message once at initial
  mount, using whatever value the constructor was given — `Checkbox` does
  not.** Confirmed empirically while building `AgentDetailScreen`'s
  edit/create form: simply opening the edit form (before the user touches
  anything) immediately marked it dirty and would have wrongly prompted a
  discard-confirmation on Escape. Fixed with a `self._populating` guard,
  set before composing the form and cleared via `self.call_after_refresh(...)`
  (confirmed empirically to run AFTER that initial burst of Changed
  messages, not before). Important: `Screen.recompose()` does NOT re-run
  `on_mount()` — only the screen's own first push does — so a screen that
  recomposes itself between modes (e.g. view → edit) has to re-arm the
  guard and reschedule `call_after_refresh` at the recompose call site
  itself, not rely on `on_mount` firing again.
- **`push_screen_wait()` needs a `@work`-decorated caller**, same gotcha as
  `ConfigToolApp.action_quit()` — `AgentDetailScreen.action_back()` awaits a
  `ConfirmModal` result for its own discard-vs-keep-editing decision, so it's
  `@work`-decorated too.
- Verified against the real, currently-live production config (8 connectors,
  4 agents, 24 watchers expanded from 12 raw entries, 2 tool presets):
  renders correctly, and drilling into all 36 rows (24 watchers + 8
  connectors + 4 agents) works with zero crashes. Group-membership banners
  appeared on exactly the 18 watchers actually belonging to a multi-room
  group (matching the known migration structure).
- `TabbedContent` (not side-by-side `Horizontal` panes) was used for the
  Overview's 5 sections — reads fine at typical terminal widths; revisit
  only if phase 2/3 forms need more horizontal space than a single tab pane
  provides.
- Coverage: no blanket `omit` added for `gateway/configtool/` (unlike
  `gateway/tools/tui.py`) — only the one `subprocess.call(...)` line is
  `# pragma: no cover`. Package coverage is ~92%; the remaining gap is the
  real-terminal-only launch path (`run_app`'s `ConfigToolApp(...).run()`)
  and the suspend-succeeds branch of `open_editor_and_reload()`.
- **Manual, real-terminal QA still needed** (cannot be automated / was not
  performed by the implementing agent, which has no real TTY): visual
  layout at typical terminal sizes, the actual `$EDITOR` suspend/resume
  round-trip, terminal resize behavior, and behavior over a real SSH session
  (color depth, mouse forwarding). See `docs/migration-0.2.md`-adjacent
  release notes / PR description for the specific checklist.
