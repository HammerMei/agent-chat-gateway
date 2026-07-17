"""ConnectorDetailScreen — view, edit, and create a single connector.

Connector `raw` is deliberately type-flexible in the schema
(gateway/schema/config.schema.json's connector definition has no
`additionalProperties: false`) — unlike Agent/WatcherDetailScreen, there's
no single closed field list. The design originally called for a generic
recursive tree editor to handle arbitrary/unknown keys; **deferred** here in
favor of per-type fixed field lists (`_FIELDS_BY_TYPE` below), one level of
nesting matching every real connector type's actual raw shape exactly
(`server.url`, `allowed_users.owners`, etc. — verified against all 4 types'
own `from_connector_config()` before choosing this). The generic tree editor
would only earn its complexity for truly arbitrary/unknown keys, and the
`$EDITOR` escape hatch already covers that case (docs/design/config-tool.md's
screen inventory: "covers what forms don't") — build it later if per-type
forms plus `$EDITOR` turn out not to be enough in practice, not preemptively.

`type` is immutable once a connector exists (only chosen via `TypePickerModal`
at creation, through `OverviewScreen.action_new_entity`) — rocketchat's and
mattermost's raw shapes differ enough that letting `type` change in place
would mean the form reshaping itself around one of its own fields' value.
Changing a connector's type after creation is a rare, advanced operation;
`$EDITOR` remains available for it. `name` is likewise immutable in edit
mode — watchers reference a connector by name (`connector: <name>`), so a
rename would silently orphan them.
"""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Static

from ..formatting import mask_if_secret, provenance_label
from ..model import EditableConfig
from .form_common import FieldSpec, FormScreen, apply_update

CONNECTOR_TYPES = ("rocketchat", "mattermost", "voice", "script")

_ROCKETCHAT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("server.url", "str", "Server URL"),
    FieldSpec("server.username", "str", "Bot username"),
    FieldSpec("server.password", "str", "Bot password", secret=True),
    FieldSpec("allowed_users.owners", "list", "Owners (comma-separated)"),
    FieldSpec("allowed_users.guests", "list", "Guests (comma-separated)"),
    FieldSpec("reply_in_thread", "bool", "Reply in thread"),
    FieldSpec("permission_reply_in_thread", "bool", "Permission replies in thread"),
    FieldSpec("require_mention", "bool", "Require @mention"),
    FieldSpec("filter_sender", "bool", "Filter by allow-list"),
    FieldSpec("timezone", "str", "Timezone (IANA, optional)"),
)
_MATTERMOST_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("server.url", "str", "Server URL"),
    FieldSpec("server.team", "str", "Team"),
    FieldSpec("server.token", "str", "API token", secret=True),
    FieldSpec("server.username", "str", "Bot username"),
    FieldSpec("server.password", "str", "Bot password", secret=True),
    FieldSpec("allowed_users.owners", "list", "Owners (comma-separated)"),
    FieldSpec("allowed_users.guests", "list", "Guests (comma-separated)"),
    FieldSpec("reply_in_thread", "bool", "Reply in thread"),
    FieldSpec("permission_reply_in_thread", "bool", "Permission replies in thread"),
    FieldSpec("require_mention", "bool", "Require @mention"),
    FieldSpec("filter_sender", "bool", "Filter by allow-list"),
    FieldSpec("timezone", "str", "Timezone (IANA, optional)"),
)
_VOICE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("port", "int", "Port"),
    FieldSpec("host", "str", "Bind host"),
    FieldSpec("secret", "str", "Bearer secret (optional)", secret=True),
    FieldSpec("timeout", "int", "Reply timeout (seconds)"),
)
_SCRIPT_FIELDS: tuple[FieldSpec, ...] = ()  # ScriptConnector never reads raw

_FIELDS_BY_TYPE: dict[str, tuple[FieldSpec, ...]] = {
    "rocketchat": _ROCKETCHAT_FIELDS,
    "mattermost": _MATTERMOST_FIELDS,
    "voice": _VOICE_FIELDS,
    "script": _SCRIPT_FIELDS,
}

# Each connector type's own dataclass defaults (gateway/connectors/*/config.py)
# — used ONLY to prefill the form with the true effective value when a field
# is set by neither the entry nor connector_defaults.
_DATACLASS_DEFAULTS_BY_TYPE: dict[str, dict[str, object]] = {
    "rocketchat": {
        "server.url": "", "server.username": "", "server.password": "",
        "allowed_users.owners": [], "allowed_users.guests": [],
        "reply_in_thread": False, "permission_reply_in_thread": True,
        "require_mention": True, "filter_sender": True, "timezone": "",
    },
    "mattermost": {
        "server.url": "", "server.team": "", "server.token": "",
        "server.username": "", "server.password": "",
        "allowed_users.owners": [], "allowed_users.guests": [],
        "reply_in_thread": False, "permission_reply_in_thread": True,
        "require_mention": True, "filter_sender": True, "timezone": "",
    },
    "voice": {"port": 8765, "host": "0.0.0.0", "secret": "", "timeout": 45},
    "script": {},
}


class ConnectorDetailScreen(FormScreen):
    BODY_ID = "connector-detail-body"

    def __init__(
        self,
        cfg: EditableConfig,
        entry: dict,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        self.entry = entry
        self.mode = mode
        if self.mode != "view":
            self._compute_initial_values(self.entry)
            self._populating = True

    def _entity_noun(self) -> str:
        return "connector"

    def _on_enter_edit_mode(self) -> None:
        self._compute_initial_values(self.entry)

    def _connector_type(self) -> str:
        return self.entry.get("type", "rocketchat")

    def _field_specs(self) -> tuple[FieldSpec, ...]:
        return _FIELDS_BY_TYPE.get(self._connector_type(), ())

    def _defaults_kind(self) -> str:
        return "connector_defaults"

    def _dataclass_defaults(self) -> dict[str, object]:
        return _DATACLASS_DEFAULTS_BY_TYPE.get(self._connector_type(), {})

    # ── view mode ────────────────────────────────────────────────────────────

    def _body_text(self) -> str:
        name = self.entry.get("name", "?")
        description = self.entry.get("description")
        try:
            merged = self.cfg.merged_entry("connector_defaults", self.entry)
            type_provenance = self.cfg.field_provenance(
                "connector_defaults", self.entry, "type"
            )
        except (ValueError, FileNotFoundError):
            merged = self.entry
            type_provenance = None
        conn_type = merged.get("type", "?")

        type_suffix = f"  [dim]({provenance_label(type_provenance)})[/dim]" if type_provenance else ""
        lines = [f"[bold]{name}[/bold]  (type: {conn_type}){type_suffix}"]
        if description:
            lines.append(f"[dim]{description}[/dim]")
        lines.append("")

        # 'type' itself is shown in the header above (with its own provenance
        # marker); everything else is a plain dump of this entry's OWN raw
        # fields — connector_defaults values that this entry simply inherits
        # (and never overrides) are intentionally not repeated here, since
        # raw is type-flexible and there's no fixed field list to merge
        # against field-by-field the way agent/watcher detail screens do.
        for key, value in self.entry.items():
            if key in ("name", "type", "description"):
                continue
            lines.append(self._render_field(key, value, indent=0))
        return "\n".join(lines)

    def _render_field(self, key: str, value: object, indent: int) -> str:
        prefix = "  " * indent
        if isinstance(value, dict):
            sub = "\n".join(
                self._render_field(k, v, indent + 1) for k, v in value.items()
            )
            return f"{prefix}{key}:\n{sub}"
        return f"{prefix}{key}: {mask_if_secret(key, value)}"

    # ── edit/create form ─────────────────────────────────────────────────────

    def _compose_form(self) -> ComposeResult:
        conn_type = self._connector_type()
        # can_focus=False: see AgentDetailScreen's identical comment — the
        # container was itself the first stop in the Tab cycle, needing an
        # extra Tab press to reach the first real field.
        with VerticalScroll(classes="entity-form", can_focus=False):
            if self.mode == "create":
                yield Static(f"[bold]New {conn_type} connector[/bold]")
                with Horizontal(classes="field-row"):
                    yield Static("Name", classes="field-label")
                    yield Input(id="field-name", placeholder="connector name")
            else:
                name = self.entry.get("name", "?")
                yield Static(f"[bold]{name}[/bold]  (type: {conn_type}, editing)")

            with Horizontal(classes="field-row"):
                yield Static("Description", classes="field-label")
                yield Input(
                    id="field-description",
                    value=self._initial_values.get("description") or "",
                )

            if conn_type == "mattermost":
                # UX guidance only — save()'s validate_config() (which runs
                # the real MattermostConfig.__post_init__) is the actual
                # enforcement, not reimplemented here.
                yield Static(
                    "[yellow]Configure EITHER 'API token' OR 'username' + "
                    "'password' below — not both, not neither. Saving with "
                    "the wrong combination shows a validation error.[/yellow]"
                )

            if not self._field_specs():
                yield Static(
                    f"[dim]'{conn_type}' connectors have no type-specific "
                    "fields to configure here.[/dim]"
                )

            for spec in self._field_specs():
                yield from self._compose_field_row(spec, self.entry)

    # ── save ─────────────────────────────────────────────────────────────────

    async def action_save(self) -> None:
        if self.mode == "view":
            return

        updates = self._collect_field_updates()
        if updates is None:
            return  # a field failed to parse; notify() already shown

        name = self.entry.get("name")
        if self.mode == "create":
            name = self.query_one("#field-name", Input).value.strip()
            if not name:
                self.notify("Name is required.", severity="error")
                return
            existing_names = {c.get("name") for c in self.cfg.connectors_raw}
            if name in existing_names:
                self.notify(f"A connector named '{name}' already exists.", severity="error")
                return

        target_entry = self.entry if self.mode == "edit" else dict(self.entry)
        for key, value in updates.items():
            apply_update(target_entry, key, value)

        inserted_index: int | None = None
        if self.mode == "create":
            target_entry["name"] = name
            connectors = self.cfg.document.setdefault("connectors", [])
            connectors.append(target_entry)
            inserted_index = len(connectors) - 1
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create" and inserted_index is not None:
                # Nothing existed under this name before this screen ever
                # ran — remove it so a failed save doesn't leave a phantom
                # half-created connector sitting in memory.
                del self.cfg.document["connectors"][inserted_index]
            self.notify(f"Could not save: {exc}", severity="error")
            return

        self.app.pop_screen()
        app = self.app
        app.notify(f"Saved connector '{name}'.", severity="information")
        app.reload_config()  # type: ignore[attr-defined]
