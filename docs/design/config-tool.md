# Config editing tool — full design (M1–M3) + implementation status

Status: **Phase 1 shipped** (read-only overview + detail screens + $EDITOR
escape hatch). **Phase 2 in progress:** items 7–10 from the Phase 1 code
review cleared; `EditableConfig.save()`/dirty-tracking/`ConfirmModal`
foundation shipped; agent create/edit shipped; connector create/edit shipped
(per-type field lists — the generic tree editor originally planned is
deferred, see Part 3); delete shipped for both agents and connectors. The
`.env` "store in .env" toggle shipped, then removed in favor of storing
secrets directly in `config.yaml` (chmod 0600) with an enforced one-time
auto-migration for any config still using `.env` — see decision 6's
"Reversed shortly after" entry below for the full reasoning. The
tool-list/preset editor is not yet built. Phase 3 is designed below but not
yet started. The
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
   **Gap fixed:** str/int/list fields could always revert an explicit
   override back to inherited by clearing the field to blank
   (`apply_update()` pops the key), but a `bool`/`enum` field had no
   equivalent — a `Checkbox`/`Select` has no "blank" state, so once touched
   it stayed explicit forever, even set back to a value matching the
   default (user-reported, exactly this way). Fixed with `ctrl+r`
   (`action_reset_field()`, `gateway/configtool/screens/form_common.py`):
   resets the FOCUSED field to its pure-`*_defaults` value and marks it in
   `self._reset_keys`; `_collect_field_updates()` writes a field in
   `_reset_keys` as "clear to inherited" on Save regardless of kind, as
   long as the widget still shows the reset value (a further real edit
   supersedes it and falls back to normal diffing). Chosen over a tri-state
   control or a per-row reset button — one consistent keybinding across
   every field kind, including str/int/list where it's a faster alternative
   to clearing the box.
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
   timestamped backup under `<config_dir>/.config-backups/`
   (`config.yaml.bak.<unix-ts>`, `shutil.copy2`, matching `onboard.py`'s own
   backup step, which writes to the same directory) then `os.replace()`
   (atomic on POSIX) — the daemon never observes a partially-written
   config.yaml. Both `config.yaml` and every backup are `chmod`'d `0600`
   (the backup directory `0700`) on every save — see decision 6's
   discussion below for why this matters as much for `config.yaml` as for
   `.env`. `dirty`/
   `mark_dirty()` also shipped alongside it; `ConfigToolApp.action_quit()`
   gates on `dirty` via a new `ConfirmModal` (Textual `@work`-decorated,
   since `push_screen_wait()` requires a worker context) — no edit screen
   sets `dirty` for real yet, but the mechanism is in place for Phase 2's
   CRUD screens to use without further plumbing.
6. **`.env` secret handling.** Password/token/secret fields get a per-field
   "store in .env" toggle (default ON) → `${VAR}` placeholder in config,
   value into `.env`. **Writer shipped:** `gateway/configtool/env_writer.py`'s
   `upsert_env_vars()` (`onboard.py`'s own `_write_env` overwrites the whole
   file with 3 hardcoded keys — insufficient once multiple connectors each
   need their own secret upserted independently). Merges by key: an existing
   `KEY=...` line is replaced in place; a new key is appended; every other
   line — comments, blank lines, unrelated keys — is preserved verbatim, in
   its original position. Restricts the file to 0600 after every write.
   `.env` resolves the same way `gateway/config.py`'s loader itself resolves
   it — `EditableConfig.path.parent / ".env"` (`load_dotenv(path.parent /
   ".env")`), NOT `onboard.py`'s hardcoded `~/.agent-chat-gateway/` — since a
   config file can live anywhere. The toggle UI + connector-form wiring
   (choosing the env var name, writing the `${VAR}` placeholder into the
   entry) lands with connector create/edit, the writer's first real caller.

## Part 2 — Screens / navigation / UX

### Screen inventory

| Screen | Kind | Status |
|---|---|---|
| `OverviewScreen` | root | **Shipped.** 5 tabs: Connectors, Agents, Watchers, Defaults, Tool Presets |
| `AgentDetailScreen` | pushed | **Shipped, all 3 modes.** view/edit/create. Form fields are a manually-maintained mirror of `$defs/agent` (not a runtime schema interpreter — safe since the schema is closed). Nothing is written to `document` until Save; Save diffs every field against its value-at-open and writes only what changed (docs/design/config-tool.md decision 2). Tool-list fields (`owner_allowed_tools`/`guest_allowed_tools`) stay view-only here — separate tool-list-editor work. Escape with unsaved changes routes through `ConfirmModal` |
| `ConnectorDetailScreen` | pushed | **Shipped, all 3 modes.** Per-type fixed field lists (tree editor deferred — see Part 3). `type`/`name` immutable in edit mode |
| `WatcherDetailScreen` | pushed | **Shipped**, `mode="view"` only still — Phase 3. Already takes a `mode: Literal["view","edit","create"]` param — no screen-class rework needed when its turn comes |
| `DefaultsScreen` | pushed | **Shipped** (view-only): shows blast radius per key |
| `ToolPresetsScreen` | pushed | **Shipped** (view-only): rule list + "used by" (checked against the MERGED per-agent tool list, not the raw entry — see gotchas below) |
| `ConfirmModal` | modal | **Shipped** (`gateway/configtool/modals.py`) — yes/no dialog, Cancel focused by default. Gates `ConfigToolApp.action_quit()` on `EditableConfig.dirty`, and `AgentDetailScreen`'s own per-screen form-dirty flag on Escape |
| `MessageModal` | modal | **Shipped** (`gateway/configtool/modals.py`) — dismiss-only error/info dialog; replaces `notify(severity="error")` for anything worth blocking on (validation/save/delete failures) — user-reported that toasts vanish before a multi-line error can be read |
| `TypePickerModal` | modal | **Shipped** (`gateway/configtool/modals.py`) — generic list-of-strings picker (`ListView`-based), reused as-is for both the agent-type picker (claude/opencode) and the connector-type picker (rocketchat/mattermost/voice/script) |
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

- **Shipped:** `gateway/configtool/env_writer.py`'s `upsert_env_vars()` — the
  merge-by-key `.env` writer (decision 6). No UI wiring yet (see below).
- **Shipped:** `FormScreen` (`gateway/configtool/screens/form_common.py`) —
  `AgentDetailScreen`'s edit/create machinery (populating guard,
  check_action, `@work action_back` + `ConfirmModal`, the generic
  field-diff/Save collection, `refresh_bindings()` after `recompose()`)
  extracted into a shared base once `ConnectorDetailScreen` became a second
  concrete user, rather than guessed at up front. `FieldSpec`/`widget_id`/
  `get_nested`/`apply_update`/`read_widget_value` moved alongside it as
  plain reusable functions. Each subclass still owns its own field list,
  `*_defaults` kind, dataclass-default map, `_compose_form()`, and
  `action_save()` (insertion semantics differ enough — a dict keyed by name
  vs. a list where each entry carries its own `name` — that forcing a
  shared implementation would be more awkward than it's worth).
- **Shipped:** `ConnectorDetailScreen` edit/create + the connector-type
  variant of `TypePickerModal` (rocketchat/mattermost/voice/script), wired
  to `n` on the Connectors tab. **Deliberate scope cut from the original
  design:** per-type FIXED field lists (`_FIELDS_BY_TYPE` in
  `connector_detail.py`) instead of the generic nested tree editor
  originally planned for `raw`. Verified first that every real connector
  type's raw shape is exactly one level deep (`server.url`,
  `allowed_users.owners`, `attachments.*`, etc.) — precisely what
  `FormScreen`'s existing dotted-key machinery already handles, so this
  isn't a compromise so much as reuse of code already proven correct for
  agent's `permissions.*`. The tree editor is deferred, not abandoned — it
  would only earn its complexity for truly arbitrary/unknown keys, and the
  `$EDITOR` escape hatch already covers that case; build it later if
  per-type forms + `$EDITOR` turn out not to be enough in practice.
  `type`/`name` are immutable in edit mode (only chosen at creation, via the
  type picker) — rocketchat/mattermost's raw shapes differ enough that
  letting `type` change in place would mean the form reshaping itself
  around one of its own fields, and a `name` change would silently orphan
  any watcher referencing the old name. Mattermost's token-XOR-user/pass
  constraint gets a plain informational `Static` line, not an interactive
  RadioSet — `save()`'s `validate_config()` (which runs the real
  `MattermostConfig.__post_init__`) is the actual enforcement either way,
  so the simpler static hint was chosen over building a stateful widget for
  guidance alone.
- **Shipped: the `.env` "store in .env" toggle.** A "Store in .env"
  `Checkbox` (default ON) next to every `secret=True` field
  (`FieldSpec.secret` already existed for masking; now also drives this).
  On Save, for each secret field whose CURRENT value is a genuine plaintext
  value (a value already matching `$VAR`/`${VAR}` — checked via
  `looks_like_env_var_reference()` — is left alone, since the user is
  explicitly referencing an externally-managed var, not typing a new
  secret) with the toggle checked: `env_var_name_for()` generates a
  deterministic name (`"<ENTITY>_<FIELD>"`, e.g. `RC_HOME_PASSWORD`),
  `upsert_env_vars()` writes it to `.env`, and the entry's field becomes
  `"${VAR}"`. **Ordering subtlety that cost a wrong-first-attempt:** the
  `.env` write must happen BEFORE `cfg.save()`, not after — `save()`'s own
  `validate_config()` calls `GatewayConfig.from_file`, which resolves every
  `${VAR}` placeholder immediately (`load_dotenv(path.parent / ".env")` +
  `os.path.expandvars`); if the var isn't in `.env` yet, `save()` itself
  fails with "unresolved environment variable" before config.yaml is ever
  written. Accepted trade-off from writing `.env` first: if `cfg.save()`
  still fails for some OTHER, unrelated reason afterward, the value already
  written to `.env` is left there, unreferenced by anything — harmless,
  equivalent to a user having pre-populated `.env` with a value not wired
  up yet; not worth building a transactional rollback for.
- **Three user-reported follow-ups on the toggle, all fixed:**
  1. *Not gated on "did this session's edit change the field."* The
     original implementation only converted a secret to `.env` if
     `spec.key in updates` — meaning a plaintext secret saved earlier with
     the toggle unchecked could never be migrated later without literally
     retyping the same password (which the user correctly flagged as a
     dead end). Now every secret field's CURRENT value is checked
     regardless of whether it changed THIS session — leaving the toggle at
     its default (checked) and saving, even while editing a totally
     unrelated field, migrates an already-plaintext secret into `.env`.
     Already-a-`${VAR}` values are still skipped either way, so this
     doesn't cause repeated/redundant `.env` writes on every subsequent save.
  2. *Deleting a connector left its `.env` secret orphaned.* Fixed via a
     new `FormScreen._on_deleted_successfully()` hook, fired after the
     document save succeeds — `ConnectorDetailScreen`'s override removes
     any `.env` key matching the deleted connector's exact deterministic
     name, but ONLY where the raw entry's value was EXACTLY that
     placeholder (never a heuristic "looks like it belongs to this
     connector" guess).
  3. *Var name collisions were an accepted, silent risk — user pushed
     back, correctly.* What shipped instead: `read_env_vars()` checks
     whether the generated name already exists in `.env` before writing;
     if it does AND this connector doesn't already own it (its pre-save
     value for that field wasn't already this exact placeholder — a plain
     re-save isn't a collision), the save is BLOCKED with a clear message
     naming the conflicting key, rather than silently overwriting an
     unrelated secret. `env_writer.py` gained `read_env_vars()` (parse
     `.env` into a dict) and `remove_env_vars()` (drop specific keys,
     same merge-preserving-everything-else behavior as `upsert_env_vars()`)
     to support both fixes.
- **Shipped (nice-to-have, user-requested): `ctrl+t` reveals/re-masks the
  FOCUSED secret field** (`Input.password` is a reactive — display-only,
  never affects `.value`, so this has zero interaction with the diff/save
  logic). **Gotcha:** the natural first choice, `ctrl+p`, is Textual's own
  `App.COMMAND_PALETTE_BINDING` — a priority binding that silently
  intercepts the keypress before it ever reaches a screen-level binding
  for the same key. Caught by a failing test, not shipped wrong; moved to
  `ctrl+t`.
- **Verified (the actual keystone test for this screen, same weight as
  `EditableConfig.save()`'s $VAR round-trip):** opening an existing
  connector whose `server.password` is `"${SOME_VAR}"`, editing an
  unrelated field, and saving leaves the password field's raw value
  exactly `"${SOME_VAR}"` — never resolved, never masked at the data level.
  Holds naturally from the existing diff-based Save mechanism (the widget's
  initial snapshot and its unedited value-at-save are both the same literal
  placeholder string) — no special-casing needed, confirmed by a dedicated
  test rather than assumed.
- **User pushed back on the whole feature: is `.env` worth it, versus just
  relying on `config.yaml`'s own file permissions?** Investigated rather
  than assumed. Findings that settled it: `.env` was the ONLY thing in this
  codebase getting deliberate secret hardening (`onboard.py` `chmod 0600`)
  — `config.yaml` itself was never chmod'd anywhere, and worse,
  `EditableConfig.save()` writes `config.yaml.tmp` via plain `open(...,
  "w")` (process umask, not whatever the real file's permissions were) and
  atomically replaces the real file with it — so even a MANUAL `chmod 600
  config.yaml` would silently revert to the umask default (typically 644)
  on the very next TUI save. Separately, every save's `config.yaml.bak.
  <unix-ts>` backup (and `onboard.py`'s own backup step) sat right next to
  the real files, matched by neither `.gitignore` entry (`config.yaml`/
  `.env` are exact-filename matches, not globs) nor any chmod — meaning a
  password that was EVER in plaintext, even briefly before being migrated
  into `.env`, lived on forever in an unprotected, git-visible-if-not-for-
  luck snapshot. Conclusion: "just use config.yaml permissions instead"
  wasn't actually a real alternative as things stood — nothing enforced
  config.yaml's permissions at all, and doing so properly (chmod every
  save, chmod every backup, fix `.gitignore`) is comparable effort to
  fixing `.env`'s rough edges, with the downside of losing the "config.yaml
  is structurally safe to share/back up, `.env` is the only sensitive file"
  separation. **Decision: keep `.env`, harden both.** Shipped as part of
  the same round:
  - `EditableConfig.save()` now writes every backup under
    `<config_dir>/.config-backups/` (created `chmod 0700`; each backup file
    `chmod 0600`) instead of scattered flat `.bak.<ts>` files beside
    `config.yaml`, and `chmod`s `config.yaml` itself to `0600` after every
    successful save — closing the "manual chmod gets silently undone" gap.
    `onboard.py`'s own backup step (the "start fresh" wizard path) and its
    initial `config.yaml` write were aligned to the same directory/chmod
    convention, so there's one backup location and one permission
    convention, not two. `.gitignore` gained `.config-backups/`.
  - **Two more toggle bugs, both from the same root cause — the checkbox
    was hardcoded `value=True` unconditionally, never reflecting the
    field's actual current state:**
    1. *Reopening after an explicit uncheck-and-save-as-plaintext showed
       the checkbox checked again.* Fixed: the checkbox's initial value
       now reflects reality — checked iff the field's own raw value
       already looks like a `$VAR`/`${VAR}` reference, unchecked otherwise
       (create mode still defaults checked — there's no current state to
       reflect yet, and it nudges new secrets toward `.env`). Direct
       consequence: leaving the toggle at ITS default no longer silently
       migrates an untouched plaintext secret just because some unrelated
       field changed (the "migrate without retyping" fix from the previous
       round) — migrating now requires explicitly CHECKING a box that
       accurately started unchecked, which is a deliberate action instead
       of an implicit side effect of saving anything else. Still zero
       retyping required either way.
    2. *A field already pointing at `.env` only ever showed the literal
       `"${VAR}"` placeholder — no way to see or sanely change the actual
       password.* `FormScreen._resolve_secret_display()` resolves a
       `$VAR`/`${VAR}` field's value against `.env` FOR DISPLAY ONLY (reads
       `env_writer.read_env_vars()` directly — never `GatewayConfig.
       from_file`/`load_dotenv`/`os.environ` — so the keystone above still
       holds: `self.entry`/`document` never see a resolved value). Both
       `_compute_initial_values()` (the diff baseline) and the widget's
       starting value go through this, so an untouched field still diffs
       as "unchanged" — nothing gets written anywhere — and typing a new
       value over the resolved display diffs as a genuine change, exactly
       like any other field; this is the actual fix for "how do you expect
       the user to change the password." Falls back to the literal
       placeholder (with a `(not found in .env — type a new value to set
       one)` hint) when the var isn't resolvable — e.g. sourced from the
       shell environment rather than a `.env` file — never silently
       blanking a field that has a real value. `action_reset_field()`
       (ctrl+r) goes through the same resolver for consistency.
       **Bug caught while writing this fix's own test:** rotating an
       already-`.env`-backed secret (typing a new value over the resolved
       display, toggle left at its now-correctly-checked default) was
       recomputing a FRESH deterministic var name
       (`env_var_name_for()`) instead of reusing whatever var name the
       field was ALREADY pointing at — harmless for a field the tool
       itself had migrated (deterministic name matches by construction),
       but for a field hand-set to a non-conventional name via the
       `$EDITOR` escape hatch, rotation would have silently spawned a
       SECOND, differently-named `.env` entry and orphaned the original
       with the stale old password still in it. Fixed:
       `ConnectorDetailScreen.action_save()` now reuses the var name
       extracted from the field's own current raw value when it's already
       a reference, and only falls back to generating a fresh deterministic
       name for a genuine plaintext-to-`.env` migration — the collision
       guard is unaffected (it's only ever load-bearing on that
       fresh-generation path; reusing an owned name can never collide with
       itself by construction).
  - **Known, accepted, out-of-scope-for-now gap:** unchecking the toggle on
    an already-`.env`-backed field WITHOUT changing the value does nothing
    — the diff sees "unchanged" (the resolved display looks the same
    either way) and nothing gets written, so the field stays `.env`-backed.
    "Un-migrate this secret back to plaintext without rotating it" is a
    real but different feature from anything the user actually asked for
    this round (which was: see/change the value, and stop the checkbox
    lying about the field's actual state) — not built, to avoid scope
    creep past what was requested.

- **Reversed shortly after, in a follow-up PR: "keep `.env`, harden both"
  above became "remove `.env` entirely, enforce a one-time migration."**
  User's own framing: two supported formats going forward is tech debt by
  itself, and "if the migration is simple — even manual — enforcing it
  beats indefinitely supporting both." Two design questions this settled:
  1. *Why not just auto-collapse an explicit field back to "inherited" when
     its value happens to match the default* (a related question raised in
     the same conversation, about `require_mention`/`connector_defaults`)*?*
     Answer: the tool can't tell "I want this pinned regardless of future
     default changes" apart from "this happens to match right now" from the
     value alone — collapsing on value-match would silently reinterpret
     past intent the next time someone edits the shared default. `--lint`
     surfacing the redundancy as a suggestion (a real, separate gap:
     `_CONNECTOR_LINT_DEFAULTS` didn't cover `require_mention`/
     `filter_sender` at all) plus `ctrl+r` as the explicit "yes, track the
     default" action is the correct split — decide, don't guess.
  2. *Why offer a "Store in .env" checkbox at all if the goal is "all
     secrets in `.env`"?* Because it couldn't have been a real gate anyway
     — the `$EDITOR` escape hatch lets anyone write a plaintext secret with
     zero friction regardless of what the form does, and nothing in
     `config_validate.py` flagged a plaintext secret either. A checkbox in
     one editing surface was a nudge, not enforcement — once `config.yaml`
     itself got the same `chmod 0600` hardening `.env` had, plaintext-in-
     config.yaml stopped being a strictly worse choice, just a different
     one (one file to manage vs. an easier "share config.yaml without also
     handing over secrets"). The sharing benefit is real but conditional
     (matters if you actually share your config) — best served by a
     dedicated export/redaction path rather than by two files as the
     permanent norm for everyone.
  - **Shipped:** `gateway/config_migrate.py` — one-time migration resolving
    every `$VAR`/`${VAR}` in the raw document to its literal value (reusing
    `EditableConfig.load()`/`.save()` and `gateway/config.py`'s own
    `load_dotenv`/`_expand_env_vars` — never reimplemented, so the resolved
    values are guaranteed to match what the daemon already used at
    runtime), then moves `.env` into `.config-backups/`. Backup -> migrate
    -> validate -> fail-closed: a migration that would fail
    `validate_config()` never touches the real `config.yaml`.
  - Same function, two triggers, per the design principle "separate the
    mutation LOGIC from the TRIGGER": automatic at every
    `gateway/daemon.py` `start_daemon()` (becomes a permanent no-op once
    `.env` is gone — this is what makes it actual enforcement rather than
    a nag someone can ignore forever) and standalone via
    `agent-chat-gateway config migrate-env` for a manual/dry run. A
    successful migration is reported on BOTH the log and the console — the
    startup handshake pipe gained an `info:` line type alongside the
    existing `error:`/`ok`, specifically so this isn't a silent operation.
  - `docker/entrypoint.acg.sh`: Mode 1 (volume mount) now keys off
    `config.yaml` alone, not `config.yaml` + `.env` — otherwise a container
    restart after the first migration would misdetect as Mode 2 and demand
    `-e RC_URL=...` again or hard-fail. Mode 2 (env-var quick start) writes
    credentials straight into the generated `config.yaml` instead of
    generating `.env`.
  - The TUI's "Store in .env" checkbox, the `env_writer.py` write path
    (`upsert_env_vars()`/`remove_env_vars()`), the delete-time `.env`
    cleanup hook, and `env_var_name_for()` were all removed — dead code
    once nothing writes a NEW `.env` reference. `_resolve_secret_display()`
    (resolve an EXISTING `${VAR}` for display/editing) and
    `read_env_vars()` stay: an existing config not yet auto-migrated still
    needs to display/edit sanely. `onboard.py`'s wizard writes credentials
    directly into `config.yaml` instead of generating a companion `.env`.
  - **8-angle code review round (user-requested) on the full removal +
    auto-migration diff, before merge.** 8 independent finder agents (line-
    by-line, removed-behavior audit, cross-file tracer, reuse, simplification,
    efficiency, altitude, CLAUDE.md conventions) surfaced 10 confirmed
    findings — all fixed:
    1. **`config.yaml` was never unconditionally `chmod 0o600`'d** —
       `migrate_env_to_config()`'s `cfg.save()` only did it as a side
       effect of an actual migration, so a hand-written `config.yaml` with
       no `.env` (exactly the path the docs now recommend) was never
       protected by `agent-chat-gateway start` at all, contradicting the
       documented guarantee. Fixed with a new `gateway/daemon.py`
       `_harden_config_permissions()`, called unconditionally in
       `start_daemon()` regardless of whether migration ran.
    2. **The `agent-chat-gateway config migrate-env` CLI command didn't
       resolve the config path** before use, unlike `start_daemon()`'s
       automatic trigger — so in Docker's Mode 1 (symlinked bind-mount),
       running it manually (exactly what `docker/entrypoint.acg.sh`'s own
       comments recommend) silently "migrated" the container-local symlinks
       only, left the real host files untouched, and reported false
       success. This turned what the module docstring called a "known,
       accepted limitation" into an actual bug once traced to its real
       cause — the daemon path only ever looked safe by accident (it
       happened to resolve the path for an unrelated reason). Fixed by
       resolving `config_path` unconditionally as the first line of
       `migrate_env_to_config()` itself, so every caller gets the guarantee
       uniformly rather than depending on each call site remembering to.
    3. `_run_config_migrate_env()` only caught `(ValueError,
       FileNotFoundError)`, not plain `OSError` (e.g. a `PermissionError`
       from `env_path.rename()`) — could crash with a raw traceback. Now
       catches `(ValueError, OSError)` (`FileNotFoundError` is already an
       `OSError` subclass).
    4. `gateway/daemon.py`'s new `info:`/`error:` pipe writes bypassed
       `gateway/service.py`'s `_write_startup_signal()`, which already
       strips embedded newlines to protect the line-oriented handshake
       protocol — and `EditableConfig.save()`'s `ValueError` genuinely can
       contain them (`"\n".join(result.errors)`). Fixed with a small
       `_sanitize_pipe_message()` helper applied at both new write sites.
    5. `migrate_env_to_config()` fully YAML-parsed `config.yaml` via
       `EditableConfig.load()` BEFORE checking whether `.env` even existed
       — meaning every daemon start double-parsed `config.yaml` (once here,
       once via `GatewayConfig.from_file()`) for a check that resolves to
       "nothing to do" the overwhelming majority of the time. Fixed by
       checking `.env`'s existence (a plain `Path.exists()` stat) first;
       this also naturally fixed finding 10 below as a side effect.
    6. The `.config-backups/` directory bootstrap (`mkdir` + `chmod 0o700`)
       was duplicated between `migrate_env_to_config()` and
       `EditableConfig.save()` (which the former's own `cfg.save()` call,
       moments earlier in the same function, already performs). Removed
       the redundant copy — reuse, don't re-create.
    7. `_count_env_refs()`'s regex diverged from `_expand_env_vars()`'s
       (`gateway/config.py`) — two different definitions of "is this an
       env-var reference." Fixed to match exactly.
    8. `_on_deleted_successfully()` was a permanently dead hook — its
       docstring described `ConnectorDetailScreen`'s `.env`-cleanup
       override, deleted in this same branch's earlier commit — with no
       remaining caller. Removed entirely (call site + definition) rather
       than left as an always-no-op hook with a misleading comment.
    9. `EditableConfig.save()`'s docstring still described the removed
       "Store in .env" checkbox and got the migration direction backwards.
       Corrected.
    10. A missing `config.yaml` (fresh install, before onboarding) surfaced
        as "Config migration failed: Config file not found" instead of the
        clearer pre-existing "Failed to load config: Config file not
        found" — **claimed** fixed as a side effect of finding 5's
        reordering. Round 2 (below) found this claim didn't actually hold
        and the reordering introduced a worse, new regression instead.
    New tests added for each: a real symlinked-config-path repro (confirms
    the real host files get migrated, not just the runtime symlinks), a
    chmod-permissions test, a pipe-message-sanitization test, an `OSError`-
    catch test, and reorder/no-op edge cases.

  - **Round 2 (user-requested a second pass on round 1's own fixes) — 8
    more findings, all fixed. The headline: round 1's fixes for #1 and #5/
    #10 above INTERACTED to create a new, real regression neither one had
    in isolation, caught by three independent finder angles (removed-
    behavior audit, line-by-line, cross-file tracer) converging on the
    same root cause.**
    1. **The interaction bug.** Round 1's reordering (check `.env` before
       loading config.yaml) let a MISSING config.yaml with no `.env`
       alongside it slip through `migrate_env_to_config()` as a silent,
       false `MigrationResult(migrated=False)` no-op — no exception at
       all. That, in turn, meant round 1's OTHER new line —
       `_harden_config_permissions()`'s unconditional, unguarded
       `chmod()` — was reached with a nonexistent file and crashed with an
       uncaught `FileNotFoundError`, bypassing `_cleanup()` and the clean
       "error:" pipe message every other fatal branch in `start_daemon()`
       produces. Separately, the CLI's `config migrate-env` reported a
       FALSE "Nothing to migrate" success (exit 0) for the same missing-
       path case — masking a broken `--config` path or not-yet-mounted
       Docker volume as a healthy no-op. Fixed by adding an explicit,
       unconditional `config_path.exists()` check as the actual first
       thing `migrate_env_to_config()` does (still before `.env`'s own
       check, so the double-parse fix is unaffected) — raising
       `FileNotFoundError` immediately, regardless of whether `.env`
       exists, so this can never be silently skipped by any caller again.
       This walks back round 1's claim about finding #10 above: the clean
       "Failed to load config" wording doesn't get inherited this way
       after all — `start_daemon()`'s existing "Config migration failed"
       wrapper catches it instead, same as before round 1 touched anything
       — but the ACTUAL regression (uncaught crash / false success) is
       what mattered and is what's fixed.
    2. **A second, independent way `_harden_config_permissions()` could
       crash `start_daemon()`:** a config.yaml that exists but is
       read-only or owned by a different uid than the daemon process
       (both realistic for a Docker `:ro` bind-mount or host-uid-mismatched
       volume) makes `chmod()` raise `PermissionError` even though the
       file loads fine — silently converting a previously-working
       deployment (pre-this-PR, `start_daemon()` never chmod'd config.yaml
       unconditionally at all) into a hard, every-single-start crash.
       Fixed: wrapped in `try/except OSError`, logging a warning rather
       than failing startup — permission-hardening is now best-effort,
       never a startup blocker.
    3. `gateway/cli.py`'s `_run_config_migrate_env()` still didn't catch
       `yaml.YAMLError` (raised by `EditableConfig.load()` for malformed
       YAML — neither a `ValueError` nor an `OSError`). Widened to a bare
       `except Exception`, matching `gateway/daemon.py`'s own handling of
       this exact function.
    4. **The sanitize-message fix from round 1 was itself incomplete AND a
       new duplication** — flagged independently by the reuse, altitude,
       and simplification angles. It added a LOCAL `_sanitize_pipe_message()`
       in `gateway/daemon.py`, applied at only the 2 new write sites round 1
       introduced — leaving 3 PRE-EXISTING pipe writes (lock-acquire
       failure, config-load failure, service-crash failure) unsanitized,
       and duplicating `gateway/service.py`'s own inline copy of the exact
       same logic in `_write_startup_signal()`. Concrete trigger: a
       malformed config.yaml makes `GatewayConfig.from_file()` raise
       `yaml.YAMLError`, whose message is routinely multi-line — written
       unsanitized, it would split into extra unparseable pipe lines.
       Fixed properly this time: `sanitize_pipe_message()` is now a single
       public function in `gateway/service.py` (used by
       `_write_startup_signal()` there), imported into `gateway/daemon.py`
       and applied at all 5 of its pipe-write sites, old and new alike.
    5. **The regex-divergence fix from round 1 also didn't eliminate the
       duplication** (reuse + simplification angles, independently) — it
       made the two regex *strings* match, but `_count_env_refs()`'s copy
       and `_expand_env_vars()`'s copy were still two independent literals
       tied together only by a comment, exactly the setup that let them
       drift apart the first time. Fixed properly: `gateway/config.py` now
       exports `ENV_VAR_REF_RE` as a named module-level constant (the ONE
       definition), and `gateway/config_migrate.py` imports it instead of
       declaring its own.
    - **Assessed, not changed:** `.config-backups/` bootstrap logic is now
      independently implemented a THIRD time in `gateway/onboard.py`'s
      `_handle_existing_config()` (the reuse angle's finding) — a real,
      documented duplication, but lower priority (a rare, one-shot code
      path) than the fixes above; deferred rather than risking a broader
      refactor of `onboard.py` in the same round. `_harden_config_
      permissions()` as a separate function (simplification angle's
      concern it's over-engineered for one line) — kept, for the same
      testability-without-forking reason `_sanitize_pipe_message()`
      originally was, and unlike that one it isn't reused elsewhere so
      there's no reuse argument either way.

- **Final revision, in a further follow-up: `$VAR`/`${VAR}` expansion
  removed from `GatewayConfig.from_file()` entirely — not just deprecated,
  gone.** User's own framing, continuing the same thread that produced the
  "remove `.env`, enforce migration" decision above: with `.env` migration
  now enforced at both `agent-chat-gateway start` and the config TUI's
  launch (see below), the TUI's `_resolve_secret_display()` machinery
  (resolve a `${VAR}` for display, added earlier this same document to
  solve "how do you change a password behind a placeholder") existed ONLY
  to serve a case — an unmigrated `.env`-backed config being opened in the
  TUI — that shouldn't be reachable anymore. Keeping it "just in case" was
  tech debt for a case the system itself now prevents.
  - **Audited before cutting, not assumed:** is there any REAL usage of
    `$VAR` resolved from an AMBIENT (non-`.env`) source — a systemd unit's
    `Environment=`, a Kubernetes manifest, a bare `RC_PASSWORD=xxx
    agent-chat-gateway start` invocation? Exhaustive repo search: no
    systemd unit or K8s manifest exists anywhere in this project;
    `docker/entrypoint.acg.sh`'s own env-var quick-start mode (Mode 2)
    deliberately resolves credentials itself and writes LITERAL values
    into the generated config.yaml, specifically avoiding `${VAR}`-in-
    config.yaml; every doc mentioning `$VAR` already framed it as
    "backward compatibility only, not recommended"; no committed example
    anywhere used it as a live pattern. The only real exercise of ambient
    (non-`.env`) resolution was two unit tests validating the mechanism
    itself and the migration's own safety net for it. Conclusion: this was
    gold-plating — a theoretical capability of `os.path.expandvars`
    nothing in the project actually depended on.
  - **The false-positive risk this closes:** with `${VAR}` still
    "meaning something," a real secret whose plaintext value happens to
    resemble a placeholder (`${SOME_WORD}`) risked being silently
    misinterpreted — either raising a confusing "unresolved environment
    variable" error for a password that was never meant to be a reference,
    or (worse) actually resolving against an unrelated, coincidentally-
    matching env var. Once `$VAR` is never expanded, a value is always
    exactly what it says, full stop — this is the same reasoning applied
    at the very start of this thread (the user: "what happens if someone's
    password itself just looks like a VAR?").
  - **Shipped:**
    - `gateway/config.py`'s `GatewayConfig.from_file()` no longer calls
      `load_dotenv()`/`_expand_env_vars()` at all. `_expand_env_vars()`
      and `ENV_VAR_REF_RE` are KEPT (not dead code) — `gateway/
      config_migrate.py`'s one-time migration is their only remaining
      caller, still needing to resolve a legacy `.env`-backed value into
      its literal form at migration time.
    - The config TUI's launch (`gateway/configtool/__init__.py`'s
      `run_app()`) now runs the SAME migration `agent-chat-gateway start`
      runs, before ever constructing `ConfigToolApp` — closing the one
      remaining gap where opening the TUI directly (without ever running
      `start`) could still show a pre-migration `${VAR}`-referencing
      config. A missing config.yaml is deliberately NOT fatal here (unlike
      `start_daemon()`, which needs an actually-loadable config to run the
      service) — the TUI already has its own graceful "does not currently
      load" banner for that case, so `FileNotFoundError` is let through to
      it rather than blocking the TUI from opening.
    - `_resolve_secret_display()`, `looks_like_env_var_reference()`
      (`gateway/configtool/screens/form_common.py`), and the entire
      `gateway/configtool/env_writer.py` module (its last remaining
      function, `read_env_vars()`, had no other caller) are all deleted —
      genuinely dead code once the TUI can assume every secret field it
      ever displays is already a real, literal value.
    - Tests updated across the board to assert the new invariant: a
      config value that looks like `${VAR}` — resolvable or not — is used
      exactly as written, never raised on, never resolved, by BOTH
      `GatewayConfig.from_file()` and `EditableConfig.load()` now (the
      distinction between the two loaders that used to matter for this
      reason no longer does; they still differ for the two reasons that
      remain — provenance and raw `rooms:` groupings, per `EditableConfig`'s
      own module docstring).

- **User-requested UX improvement: `e`/`d` moved from the detail screen to
  the list page itself, acting directly on the row under the cursor —
  skipping the "select row -> land on read-only view -> press e/d again"
  detour for the common case of just wanting to edit or delete one entry.**
  Root cause of the reported friction: `OverviewScreen`'s OWN `e` binding
  (the `$EDITOR`-on-the-whole-file escape hatch) was shadowing the user's
  intent every time they pressed `e` on the list hoping to edit the
  selected connector/agent — two completely different actions sharing one
  key, on two different screens, with no visual cue which one would fire.
  - **Shipped:** `OverviewScreen` gained its own `e`/`d` bindings
    (`action_edit_row()`/`action_delete_row()`), scoped via `check_action()`
    to the Connectors/Agents tabs only — the only two with a real
    edit/delete flow (Watchers/Defaults/Tool Presets stay Phase 3). Edit
    pushes the detail screen directly in `mode="edit"` (every detail
    screen's constructor already accepted this — no new machinery needed
    there) rather than `mode="view"` then simulating an `e` press. Delete
    reuses `FormScreen.action_delete()`'s existing confirm/referencing-
    watcher-check/save logic completely unchanged, just triggered from the
    list without a screen push first.
  - The old `e` -> `$EDITOR` binding moved to `ctrl+e` — clear of every
    other single-letter binding on both screens (`e`/`d`/`r`/`n`/`q` here,
    `ctrl+s`/`ctrl+r`/`ctrl+t` on `FormScreen`).
  - **A screen that skips view mode entirely has no view state to fall
    back to.** `FormScreen` gained a `_started_in_edit_mode` flag, set only
    by the new list-page shortcut; `action_back()` checks it alongside the
    existing `mode == "create"` case — both now pop straight back to
    whatever pushed the screen, rather than falling back to a `mode="view"`
    rendering of a screen the user, in this path, never asked to see. Without
    this, Escape (or a cancelled/blocked delete) would have stranded the
    user on a read-only page reachable only from this new shortcut, with
    no way back to it via the normal row-selection flow.
  - **Delete-from-the-list needed a screen pushed anyway** (`action_delete()`
    requires `self.mode == "view"` and reads `self.cfg`/`self.entry`/
    `self._referencing_watcher_labels()` off the instance) — pushed
    silently, the delete action fires immediately (the confirm/blocked
    modal covers it before it's ever really seen), and if the result is
    anything other than a successful delete (cancelled, or blocked by a
    referencing watcher — `action_delete()` deliberately leaves the screen
    in place for both, correct for its own view-mode-entry design), an
    explicit `pop_screen()` sends the user back to the list — the same
    place a successful delete already returns them to.
  - **A real implementation pitfall, caught by the test suite, not
    assumed:** the first attempt called the existing `@work`-decorated
    `action_delete()` and awaited the `Worker` it returns via `.wait()` —
    nesting one `@work` worker inside another. This is fragile: if the
    OUTER worker (`action_delete_row()`) is torn down while the INNER one
    is still suspended at a `push_screen_wait()`, `Worker.wait()` re-raises
    that as `WorkerCancelled` INSIDE the outer worker's own body — a
    crash with no bug in the delete logic itself, and exactly the kind of
    failure a quick manual test wouldn't reliably surface. Fixed by
    extracting `action_delete()`'s body into a plain (non-`@work`)
    `_do_delete()` coroutine, with `action_delete()` reduced to a thin
    `@work` wrapper around it — `action_delete_row()` calls `_do_delete()`
    directly, no nested worker at all.

- **Shipped (added after initial Phase 2 review — user caught that "CRUD"
  was being used loosely and Delete had never actually been designed for
  agents/connectors; checked, and they were right, nothing in this
  document specified it before now):** `d` from view mode on
  `AgentDetailScreen`/`ConnectorDetailScreen` — `FormScreen.action_delete()`
  (`gateway/configtool/screens/form_common.py`), shared the same way
  `action_back()`/`action_edit()` are. `ConfirmModal` first, then remove
  from `document`, `mark_dirty()`, `save()`. Same "let save() be the
  backstop" philosophy as everything else in this screen: no reference-
  counting reimplemented here — deleting an agent/connector still
  referenced by a watcher fails `save()`'s `validate_config()` with
  `GatewayConfig.from_file`'s own existing "references unknown agent/
  connector" error, and the entry is reinserted into `document` so a
  rejected delete never leaves memory silently out of sync with disk.
  Connector deletion matches by object IDENTITY (not equality) to find the
  right list index, since two connectors could in principle share
  byte-identical raw content. Hidden from the footer while
  editing/creating, via the same `check_action()` mechanism as 'Edit'/'Save'.
  **Refined immediately after user testing:** the generic validator error
  ("Watcher entry at index 11 references unknown connector 'X'") confused
  the user, who reasonably expected a delete-specific reason. Added
  `find_referencing_watcher_labels()` as a pre-flight check *before* the
  destructive confirm — a blocked delete now shows "Cannot delete agent
  'X' — still used by watcher(s): rc-general, rc-dev." and never even
  offers the confirm dialog. `save()`'s own rejection stays in place as a
  belt-and-suspenders backstop for anything the pre-check doesn't
  anticipate, not replaced by it. **Second round, same feature:** the
  labels initially fell back to the bare `room:`/joined `rooms:` string for
  an unnamed watcher — inconsistent with the REAL name that watcher gets
  everywhere else in the TUI (`_auto_watcher_name()`'s `"<connector>-<room>"`,
  gateway/config.py), and wrong for a `rooms:` group (one joined label
  instead of N separate real watchers). Rewritten to use
  `cfg.expanded_watchers()` directly — the same real, already-correct data
  the Overview's Watchers tab renders from — instead of re-deriving names
  from the raw entry.
- **`MessageModal`, a dismiss-only error/info dialog (`gateway/configtool/modals.py`).**
  User-reported: `self.notify(..., severity="error")` toasts auto-vanish on
  their own timer, and a save/delete failure's explanation (often several
  lines) needs more than a glance. Every error-severity `notify()` call
  across `AgentDetailScreen`/`ConnectorDetailScreen`'s create/edit/delete
  flows (duplicate/blank name, invalid integer, save() validation failures,
  delete failures) was converted to `await self.app.push_screen_wait(MessageModal(...))`
  — stays up until Enter/Escape/click. Success messages ("Saved.",
  "Deleted.") deliberately stayed as `notify()` toasts — short and don't
  need blocking review. `action_save()` on both screens is now
  `@work`-decorated (needed for `push_screen_wait`, same gotcha as
  `action_back()`/`action_quit()`).
- **Real bug, user-reported: a rejected Save in edit mode still mutated the
  live entry.** `action_save()`'s trial-copy logic was
  `target_entry = self.entry if self.mode == "edit" else dict(self.entry)`
  — for edit mode, `target_entry` WAS `self.entry`, the SAME dict object
  already living in `cfg.document` (entries are held by reference
  throughout this tool, never copied on load). Applying Save's updates to
  it, then having `save()` reject the result, left the invalid data sitting
  in `document` — no rollback path existed for edit mode (only create mode
  deleted its phantom entry on failure). Reported exactly as it would
  manifest: set an invalid `timeout`, Save fails with a clear error, press
  Back anyway — the invalid value was still showing, even though nothing
  had actually been written to disk. Fixed with the same
  install/rollback-hook pattern delete already uses:
  `_install_trial_entry(target_entry)` swaps a COPY (with updates applied)
  into `document` *before* `save()` runs; `_rollback_trial_entry()` restores
  the untouched original if `save()` rejects it. `self.entry` itself is
  never mutated until `save()` has actually succeeded.
- **Not yet built:** the `.env` toggle wiring (above), the generic tree
  editor (deferred, above), tool-list editor + `PresetOrInlineModal` +
  `InlineToolRuleModal`, `ToolPresetsScreen` made editable (used-by
  warnings).

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
- **A `VerticalScroll` is itself focusable by default, and ends up FIRST in
  the Tab cycle — before any of the widgets inside it.** User-reported:
  entering edit mode needed Tab pressed TWICE to reach the first real field
  (once to focus the form's own scroll container, once to move past it).
  Same root cause hit `AUTO_FOCUS` too (Textual's own default,
  `App.AUTO_FOCUS = "*"`, which normally auto-focuses the first focusable
  widget on a screen's first mount): in CREATE mode — a genuine fresh
  push, so `AUTO_FOCUS` does fire — focus still landed on the container,
  not the Name field, for the same reason. Fixed both at once with
  `VerticalScroll(classes="entity-form", can_focus=False)` on the form's own
  container — it isn't meant to be independently focused; scrolling still
  works via the mouse wheel/PageUp/PageDown. Worth checking for on any
  future container that wraps a form's fields.
- **`Input`'s own `DEFAULT_CSS` is `width: 100%` — inside a `Horizontal`
  field-row, that claims the ENTIRE row, pushing every sibling that comes
  after it off past the terminal's right edge.** User-reported: the new
  "Store in .env" `Checkbox` (task #27) was completely invisible. Confirmed
  via `widget.region` (NOT just `query_one()` succeeding — Pilot's query
  finds a widget regardless of where it's actually rendered, which is
  exactly why this shipped unnoticed): the checkbox's region started at
  `x=146` in a 120-column terminal, fully off-screen. The SAME root cause
  had already been silently hiding every field's provenance marker (the
  "(explicit)"/"(inherited from defaults)" label) on both `AgentDetailScreen`
  and `ConnectorDetailScreen` since Phase 2's agent form first shipped — a
  dim decorative label going unnoticed off-screen is a lot less obvious
  than a missing interactive control, which is probably why it took the
  checkbox to surface it. `Select`'s own `DEFAULT_CSS` already uses `width:
  1fr` and never had this problem — fixed by adding
  `FormScreen .field-row Input { width: 1fr; }`, so the Input shares the
  row's remaining space with its fixed/auto-width siblings instead of
  claiming all of it. **Any future field-row widget added after an Input
  needs a `.region`-based test, not just a `query_one()` existence check —
  that's the only way this class of bug gets caught before a user reports
  it.**
- **`push_screen_wait()` needs a `@work`-decorated caller**, same gotcha as
  `ConfigToolApp.action_quit()` — `AgentDetailScreen.action_back()` awaits a
  `ConfirmModal` result for its own discard-vs-keep-editing decision, so it's
  `@work`-decorated too.
- **`Footer` goes permanently blank after `recompose()` unless you call
  `Screen.refresh_bindings()` afterward.** User-reported: after entering
  `AgentDetailScreen`'s edit mode once, the footer became a blank bar and
  stayed blank for the rest of that screen's life — even going back to view
  mode and re-entering edit. Root cause: `Footer.on_mount()` subscribes to
  `Screen.bindings_updated_signal`; `Footer.compose()` renders nothing until
  its `_bindings_ready` reactive is set, which only happens inside its own
  `bindings_changed` signal callback. `recompose()` mounts a BRAND-NEW
  `Footer` instance every time (view↔edit), which re-subscribes but never
  receives that signal on its own — nothing re-fires it just because a new
  subscriber showed up, since the screen's bindings didn't structurally
  change from Textual's point of view. Fixed by calling
  `self.refresh_bindings()` (Screen's own public method — re-publishes the
  signal to every current subscriber) right after every `recompose()` in
  `action_edit()`/`action_back()`. Any future screen that recomposes itself
  in place (rather than pushing a new screen) needs this same call.
- **Up/Down field navigation was tried and reverted.** `VerticalScroll`
  (the form's container) binds Up/Down to `action_scroll_up`/
  `action_scroll_down` by default; a small `_AgentForm(VerticalScroll)`
  subclass overriding just those two actions to call
  `self.screen.focus_previous()`/`focus_next()` worked under Pilot's
  headless driver (regression tests passed) but was unreliable in the
  user's real terminal session — pressing Down sometimes did nothing,
  sometimes stopped advancing partway through the form. Root cause not
  isolated (Pilot's key-event simulation apparently doesn't reproduce
  whatever the real terminal/session does differently); reverted rather
  than ship a navigation feature that's flaky in exactly the environment it
  needs to work in. **What shipped instead:** a `tab`→`app.focus_next`
  binding re-bound with `show=True` (same pattern `OverviewScreen` already
  uses) so the footer tells the user how to move between fields at all —
  Tab/Shift+Tab was always the reliable mechanism underneath either way.
- **The footer showed 'Edit' even while already editing** (where pressing
  `e` is a no-op — `action_edit()` only does something from view mode),
  which read as broken rather than merely redundant. Fixed with
  `check_action()`: BINDINGS don't need to change per mode, just their
  visibility — `check_action("edit", ...)` returns `mode == "view"`,
  `check_action("save", ...)` returns `mode != "view"`. Works together with
  the `refresh_bindings()` fix above: the same call that fixes the
  went-permanently-blank bug also re-evaluates `check_action` for every
  binding, so mode-based visibility updates immediately on the same
  recompose, no separate plumbing needed.
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
