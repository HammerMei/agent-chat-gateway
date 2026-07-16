"""OverviewScreen — the config TUI's root screen.

Five tabs: Connectors, Agents, Watchers, Defaults, Tool Presets — the latter
two are first-class per docs/design/config-tool.md (shared resources, not
footnotes). Phase 1 is read-only: selecting a row pushes a *DetailScreen in
view mode; there is no add/edit/delete yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from ..formatting import status_badge
from ..model import StatusIndex
from .agent_detail import AgentDetailScreen
from .connector_detail import ConnectorDetailScreen
from .defaults import DefaultsScreen
from .tool_presets import ToolPresetsScreen
from .watcher_detail import WatcherDetailScreen

if TYPE_CHECKING:
    from ..app import ConfigToolApp


class OverviewScreen(Screen):
    """Root screen — never popped (quitting the app pops the whole stack)."""

    BINDINGS = [
        Binding("e", "edit_config", "Edit in $EDITOR"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static(id="banner")
            with TabbedContent():
                with TabPane("Connectors", id="tab-connectors"):
                    yield DataTable(id="connectors-table", cursor_type="row")
                with TabPane("Agents", id="tab-agents"):
                    yield DataTable(id="agents-table", cursor_type="row")
                with TabPane("Watchers", id="tab-watchers"):
                    yield DataTable(id="watchers-table", cursor_type="row")
                with TabPane("Defaults", id="tab-defaults"):
                    yield DataTable(id="defaults-table", cursor_type="row")
                with TabPane("Tool Presets", id="tab-presets"):
                    yield DataTable(id="presets-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        for table_id in (
            "#connectors-table", "#agents-table", "#watchers-table",
            "#defaults-table", "#presets-table",
        ):
            self.query_one(table_id, DataTable).cursor_type = "row"
        self.refresh_overview()

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_edit_config(self) -> None:
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.open_editor_and_reload()

    def action_refresh(self) -> None:
        # Must go through app.reload_config() (re-reads EditableConfig.document
        # from disk), NOT call self.refresh_overview() directly — that only
        # repaints from whatever EditableConfig already has in memory, which
        # run_validate() (reading the file fresh internally) can silently
        # disagree with once the file has changed on disk since app startup.
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.reload_config()

    # ── Core refresh logic (the one testable seam per docs/design) ──────────

    def refresh_overview(self) -> None:
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        banner = self.query_one("#banner", Static)

        connectors_table = self.query_one("#connectors-table", DataTable)
        agents_table = self.query_one("#agents-table", DataTable)
        watchers_table = self.query_one("#watchers-table", DataTable)
        defaults_table = self.query_one("#defaults-table", DataTable)
        presets_table = self.query_one("#presets-table", DataTable)
        for table in (connectors_table, agents_table, watchers_table, defaults_table, presets_table):
            table.clear(columns=True)

        if app.load_error is not None:
            banner.update(
                f"[red]✗ config.yaml does not currently load:[/red] {app.load_error}"
            )
            return

        cfg = app.editable_config
        result = app.run_validate()

        if result.ok:
            summary = f"[green]✓ valid[/green] — {result.watcher_count} watcher(s)"
            if result.entry_count and result.entry_count != result.watcher_count:
                summary += f" (expanded from {result.entry_count} entries)"
        else:
            summary = f"[red]✗ {len(result.errors)} error(s)[/red]"
        if result.warnings:
            summary += f", {len(result.warnings)} warning(s)"
        if app.lint and result.lint_findings:
            summary += f", {len(result.lint_findings)} lint finding(s)"
        banner.update(summary)

        status = StatusIndex(result.findings)

        # Each table is populated defensively: run_validate() already caught
        # any GatewayConfig.from_file failure into `result` (shown in the
        # banner above), but several accessors here call the real loader
        # AGAIN independently (merged_entry/defaults_block/expanded_watchers
        # all replay _extract_defaults_block, and expanded_watchers() calls
        # validated_view() -> GatewayConfig.from_file() directly) — the exact
        # same failure would otherwise raise a second, unhandled time here.
        # A table that can't be computed shows one row saying so rather than
        # crashing the whole overview; the banner above already has the
        # actual error text.

        connectors_table.add_columns("Name", "Type", "Status")
        for c in cfg.connectors_raw:
            name = c.get("name", "?")
            try:
                merged = cfg.merged_entry("connector_defaults", c)
            except (ValueError, FileNotFoundError):
                merged = c
            connectors_table.add_row(
                name, merged.get("type", "?"), status_badge(status.status_for("connector", name)),
                key=name,
            )

        agents_table.add_columns("Name", "Type", "Command", "Status")
        for name, entry in cfg.agents_raw.items():
            try:
                merged = cfg.merged_entry("agent_defaults", entry)
            except (ValueError, FileNotFoundError):
                merged = entry
            agents_table.add_row(
                name,
                merged.get("type", "claude"),
                merged.get("command", "claude"),
                status_badge(status.status_for("agent", name)),
                key=name,
            )

        watchers_table.add_columns("Name", "Connector", "Room", "Agent", "Status")
        try:
            expanded = cfg.expanded_watchers()
        except (ValueError, FileNotFoundError):
            expanded = None
        if expanded is None:
            watchers_table.add_row("(unavailable — config does not currently load)", "", "", "", "")
        else:
            for ew in expanded:
                w = ew.watcher
                watchers_table.add_row(
                    w.name, w.connector, w.room, w.agent,
                    status_badge(status.status_for("watcher", w.name)),
                    key=w.name,
                )

        defaults_table.add_columns("Block", "Keys set")
        for kind in ("connector_defaults", "agent_defaults", "watcher_defaults"):
            try:
                block = cfg.defaults_block(kind)
            except (ValueError, FileNotFoundError):
                block = {}
            defaults_table.add_row(kind, str(len(block)), key=kind)

        presets_table.add_columns("Name", "Rules")
        for name, rules in cfg.tool_presets_raw.items():
            presets_table.add_row(name, str(len(rules)), key=name)

    # ── Row selection → push detail screens ──────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        cfg = app.editable_config
        if cfg is None:
            return
        table_id = event.data_table.id
        key = str(event.row_key.value)

        if table_id == "connectors-table":
            entry = next((c for c in cfg.connectors_raw if c.get("name") == key), None)
            if entry is not None:
                self.app.push_screen(ConnectorDetailScreen(cfg, entry, mode="view"))
        elif table_id == "agents-table":
            entry = cfg.agents_raw.get(key)
            if entry is not None:
                self.app.push_screen(AgentDetailScreen(cfg, key, entry, mode="view"))
        elif table_id == "watchers-table":
            ew = next((e for e in cfg.expanded_watchers() if e.watcher.name == key), None)
            if ew is not None:
                self.app.push_screen(WatcherDetailScreen(cfg, ew, mode="view"))
        elif table_id == "defaults-table":
            self.app.push_screen(DefaultsScreen(cfg, key, mode="view"))
        elif table_id == "presets-table":
            self.app.push_screen(ToolPresetsScreen(cfg, key, mode="view"))
