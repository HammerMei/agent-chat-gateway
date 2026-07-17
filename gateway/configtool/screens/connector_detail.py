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

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Checkbox, Input, Static

from ..env_writer import upsert_env_vars
from ..formatting import mask_if_secret, provenance_label
from ..modals import MessageModal
from ..model import EditableConfig
from .form_common import (
    FieldSpec,
    FormScreen,
    apply_update,
    env_toggle_widget_id,
    env_var_name_for,
    find_referencing_watcher_labels,
    looks_like_env_var_reference,
)

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

    def _entity_label(self) -> str:
        return self.entry.get("name", "?")

    def _find_own_index(self) -> int:
        # Matched by object IDENTITY, not equality — connectors_raw is a
        # fresh list each call but wraps the SAME dict objects living in
        # `document`, and two connectors could (in a broken config) have
        # byte-identical raw content; identity is the only way to be sure
        # this is the exact entry this screen was opened on.
        connectors = self.cfg.document.get("connectors") or []
        return next(i for i, c in enumerate(connectors) if c is self.entry)

    def _remove_entry_from_document(self) -> None:
        self._deleted_index = self._find_own_index()
        del self.cfg.document["connectors"][self._deleted_index]

    def _reinsert_entry_into_document(self) -> None:
        connectors = self.cfg.document.setdefault("connectors", [])
        connectors.insert(self._deleted_index, self.entry)

    def _install_trial_entry(self, target_entry: dict) -> None:
        self._edit_index = self._find_own_index()
        self.cfg.document["connectors"][self._edit_index] = target_entry

    def _rollback_trial_entry(self) -> None:
        self.cfg.document["connectors"][self._edit_index] = self.entry

    def _referencing_watcher_labels(self) -> list[str]:
        return find_referencing_watcher_labels(self.cfg, connector_name=self._entity_label())

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

    @work
    async def action_save(self) -> None:
        if self.mode == "view":
            return

        updates = self._collect_field_updates()
        if updates is None:
            await self.app.push_screen_wait(
                MessageModal(self._last_field_error or "Invalid field.", title="Could not save")
            )
            return

        name = self.entry.get("name")
        if self.mode == "create":
            name = self.query_one("#field-name", Input).value.strip()
            if not name:
                await self.app.push_screen_wait(
                    MessageModal("Name is required.", title="Could not save")
                )
                return
            existing_names = {c.get("name") for c in self.cfg.connectors_raw}
            if name in existing_names:
                await self.app.push_screen_wait(
                    MessageModal(
                        f"A connector named '{name}' already exists.", title="Could not save"
                    )
                )
                return

        # Secret fields with "Store in .env" checked: if the field's value
        # actually changed to a genuine new plaintext secret (not already a
        # $VAR/${VAR} reference the user typed directly), swap it for a
        # ${VAR} placeholder in `updates`.
        env_writes: dict[str, str] = {}
        entity_name_for_env = name if self.mode == "create" else self.entry.get("name", "?")
        for spec in self._field_specs():
            if not spec.secret or spec.key not in updates:
                continue
            new_value = updates[spec.key]
            if not new_value or looks_like_env_var_reference(new_value):
                continue
            toggle = self.query_one(f"#{env_toggle_widget_id(spec.key)}", Checkbox)
            if not toggle.value:
                continue
            var_name = env_var_name_for(entity_name_for_env, spec.key)
            env_writes[var_name] = new_value
            updates[spec.key] = f"${{{var_name}}}"

        if env_writes:
            # MUST happen BEFORE cfg.save(), not after: save()'s own
            # validate_config() calls GatewayConfig.from_file, which
            # resolves every ${VAR} placeholder immediately via
            # load_dotenv(path.parent / ".env") + os.path.expandvars — if
            # the var isn't in .env yet, save() itself fails with
            # "unresolved environment variable" before config.yaml is ever
            # written. Accepted trade-off: if cfg.save() still fails for
            # some OTHER, unrelated reason after this, the value written
            # here is left in .env, unreferenced by anything — harmless,
            # equivalent to a user having pre-populated .env with a value
            # not wired up yet. Nothing else has been touched yet at this
            # point, so a failure here is a clean, early return.
            try:
                upsert_env_vars(self.cfg.path.parent / ".env", env_writes)
            except OSError as exc:
                await self.app.push_screen_wait(
                    MessageModal(f"Could not write to .env: {exc}", title="Could not save")
                )
                return

        # ALWAYS a trial copy, never self.entry directly — even for "edit",
        # where self.entry is the SAME object already living in
        # cfg.document. Mutating it here, before save() has even run, would
        # leave invalid data sitting in the document if save() then fails
        # (a real bug: reported as "Save failed, but Back still showed the
        # invalid value" — the fix is never mutating the original until
        # save() has actually succeeded).
        target_entry = dict(self.entry)
        for key, value in updates.items():
            apply_update(target_entry, key, value)

        inserted_index: int | None = None
        if self.mode == "create":
            target_entry["name"] = name
            connectors = self.cfg.document.setdefault("connectors", [])
            connectors.append(target_entry)
            inserted_index = len(connectors) - 1
        else:
            self._install_trial_entry(target_entry)
        self.cfg.mark_dirty()

        try:
            self.cfg.save()
        except (ValueError, FileNotFoundError) as exc:
            if self.mode == "create" and inserted_index is not None:
                # Nothing existed under this name before this screen ever
                # ran — remove it so a failed save doesn't leave a phantom
                # half-created connector sitting in memory.
                del self.cfg.document["connectors"][inserted_index]
            else:
                self._rollback_trial_entry()
            await self.app.push_screen_wait(MessageModal(str(exc), title="Could not save"))
            return

        self.entry = target_entry
        self.app.pop_screen()
        app = self.app
        app.notify(f"Saved connector '{name}'.", severity="information")
        app.reload_config()  # type: ignore[attr-defined]
