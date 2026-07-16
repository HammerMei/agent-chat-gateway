"""ConnectorDetailScreen — view (and, in a later phase, edit/create) a
single connector.

Phase 1 is view-only: a generic recursive dump of the raw connector entry
(secrets masked). Connector `raw` is deliberately type-flexible (see
gateway/schema/config.schema.json's connector definition), so unlike Agent/
WatcherDetailScreen this does not attempt a fixed per-field layout — that is
Phase 3's per-type template + generic tree editor (docs/design/config-tool.md).
"""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from ..formatting import mask_if_secret, provenance_label
from ..model import EditableConfig


class ConnectorDetailScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]

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

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(self._body_text(), id="connector-detail-body"))
        yield Footer()

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

    def action_back(self) -> None:
        self.app.pop_screen()
