# Config TUI

`config.yaml` is plain YAML and can always be hand-edited, but the config
TUI gives you a full-screen, keyboard-driven editor with validation,
provenance tracking (see at a glance whether a value is explicit, inherited
from a template, or a built-in default), and safe writes — every save is
validated against a temp file before it ever touches your real
`config.yaml`, with an automatic timestamped backup.

## Launching

```bash
agent-chat-gateway config
```

```bash
# Edit a config file other than the default (~/.agent-chat-gateway/config.yaml
# or $ACG_CONFIG)
agent-chat-gateway config --config /path/to/config.yaml

# Also flag values that just restate a built-in default, or duplicate a
# value already inherited from a template — useful for cleaning up a config
# that's grown noisy over time
agent-chat-gateway config --lint
```

Two related, non-interactive commands live under the same `config`
subcommand:

```bash
# Validate config.yaml without starting the daemon or opening the TUI
agent-chat-gateway config validate [--lint]

# One-time: fold .env secrets into config.yaml as literal values, then
# remove .env (also runs automatically the next time you start the daemon
# or open the TUI, if it detects a .env-backed config — this lets you do it
# as a manual step or a dry run instead)
agent-chat-gateway config migrate-env
```

## Layout

Five tabs across the top: **Connectors**, **Agents**, **Watchers**,
**Templates**, **Tool Presets**. A banner above them shows the config's
current validation status (`✓ valid` or `✗ N error(s)`, plus warning/lint
counts) — press `v` when it says "press 'v' to view details" to see the
actual messages, not just a count.

Every list is sorted by name, so a newly created or merged entry is always
easy to find regardless of where it landed in the underlying file.

## Keybindings

On the list (Overview) screen:

| Key | Action |
|---|---|
| `↑`/`↓` or click | Move the cursor in the current tab's list |
| `←`/`→` | Switch tabs |
| `Enter` | View the selected entry (read-only) |
| `e` | Edit the selected entry directly (skips the view-only step) |
| `d` | Delete the selected entry |
| `n` | Create a new entry on the current tab |
| `c` | *(Watchers tab only)* Clone the selected watcher's settings to more rooms — see [Watchers](#watchers) below |
| `r` | Refresh from disk (picks up changes made outside the TUI) |
| `v` | View the full text of any validation errors/warnings/lint findings |
| `ctrl+e` | Open `$EDITOR` on the whole `config.yaml` file, then reload when you exit |
| `q` or `Escape` | Quit (prompts to discard if there are unsaved changes) |

Inside an edit/create form:

| Key | Action |
|---|---|
| `Tab` | Move to the next field |
| `ctrl+s` | Save |
| `ctrl+r` | Reset the focused field back to its inherited/default value |
| `ctrl+t` | Show/hide the focused field's value, if it's a masked secret |
| `Escape` | Go back (prompts to discard if there are unsaved changes) |

## Editing an entry

A field labeled with a trailing `*` (e.g. `Working directory *`) is
required — save will refuse to go through without it. Required fields are
always listed first in the form.

Every field shows its current *provenance* next to it: `(explicit)` if this
entry sets it directly, `(from '<template>')` if it comes from an
`inherits:` template, or `(default)` if nothing sets it and it's just the
built-in fallback. Clearing an explicit value back to blank (or `ctrl+r`)
reverts it to inherited/default — it does **not** write an explicit `null`.

Nothing is written to `config.yaml` until you press `ctrl+s`. Save
validates the whole file first; if your change would introduce a **new**
problem, it's rejected with the exact error message and nothing is written.
A problem that already existed elsewhere in the file before your edit never
blocks an unrelated save.

## Connectors

Four types: `rocketchat`, `mattermost`, `voice`, `script`. **`voice` and
`script` are experimental** — see
[docs/supported-features.md](supported-features.md#voice-gateway-experimental-)
for `voice`'s known limitations, and
[docs/scheduling.md](scheduling.md#headless-scheduling-no-chat-platform)
for `script`'s actual use case (a headless identity for scheduled jobs, not
a chat platform). A connector's type is fixed once created — to change it,
use `ctrl+e` to hand-edit the file.

Mattermost's authentication is dual-mode (a Personal Access Token, or a
username+password login) — pick which one via the "Auth method" dropdown;
the inactive group's fields are hidden so the two modes can't collide.

## Agents

Two types: `claude`, `opencode` — also fixed once created. `Owner allowed
tools`/`Guest allowed tools` each have their own list editor below the main
fields: **+ Add** to append a rule (either a reference to a named tool
preset, or a one-off inline rule), **- Remove** to delete the selected one,
and **Edit** to change a selected *inline* rule in place (grayed out until
you select an inline rule — a preset reference isn't editable here; remove
it and add a different preset reference instead).

## Watchers

A `rooms: [a, b, c]` entry in `config.yaml` expands into several
independent watchers that all share one underlying entry — the Watchers tab
always shows them as separate rows, one per room.

- **Editing a shared setting** (connector, agent, room, session ID,
  notifications, `inherits`, ...) on one room splits it out into its own
  entry; the other rooms in the group are untouched. The only field that
  edits the whole group in place is **Description** (a free-text note with
  no effect on behavior).
- **Creating** a room whose connector, agent, and other settings exactly
  match an existing entry merges it into that entry's `rooms:` list instead
  of creating a near-duplicate. This also happens automatically when a
  split-out room's new settings happen to match some other existing entry.
- **Clone for rooms** (`c`, from the list or from inside a watcher) bulk-adds
  several rooms sharing the current watcher's settings in one step — type a
  comma-separated list of room names. A room that's already in the group
  you're cloning from is silently skipped, not an error.
- There's no field for a watcher's `name:` (an explicit override of the
  auto-generated `<connector>-<room>` identity) in this form — it's a rare
  edge case (only useful when two *different* agents need to watch the same
  connector+room). A watcher that already has one keeps it; use `ctrl+e` if
  you need to set one.

## Templates

`agent_templates`, `connector_templates`, and `watcher_templates` are named,
reusable field sets — any entry can opt in via its own `inherits: <name>`
field. The Templates tab lists all of them, across all three kinds, in one
flat list.

Editing a field on a template shows a confirm dialog naming every entry
that would be affected (scoped to entries that actually inherit *this*
template and don't already override the field) before saving — so you know
the blast radius up front. Deleting a template still in use by some entry
is blocked with the same kind of message.

## Tool Presets

Named, reusable rule lists (`tool_presets:`) that an agent's
`owner_allowed_tools`/`guest_allowed_tools` can reference by name instead of
repeating the same rules on every agent. Deleting a preset still referenced
by an agent is blocked.

## Escape Hatch

`ctrl+e` (from the list screen) opens `$EDITOR` on the raw `config.yaml`
file for anything the forms don't cover — an unusual connector `raw` key, a
rare field this TUI doesn't expose, or a bulk find-and-replace. The TUI
reloads and re-validates the file as soon as you exit the editor.
