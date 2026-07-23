"""OverviewScreen — the config TUI's root screen.

Five tabs: Connectors, Agents, Watchers, Defaults, Tool Presets — the latter
two are first-class per docs/design/config-tool.md (shared resources, not
footnotes). Selecting a row (Enter) pushes a *DetailScreen in view mode.
'e'/'d' on the Connectors/Agents tabs act directly on the row under the
cursor — edit opens straight into edit mode (no view detour), delete runs
the same confirm/referencing-watcher-check/save flow FormScreen.
action_delete() already has, without requiring a screen push first (user-
reported: 'e' used to be shadowed by this screen's OWN 'e' binding for the
$EDITOR escape hatch — see action_edit_config() below, now on ctrl+e). 'n'
(new_entity) creates an entry on the active tab — so far only Agents/
Connectors support it; other tabs still notify rather than doing nothing
or crashing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from ..formatting import status_badge
from ..modals import TypePickerModal
from ..model import StatusIndex
from .agent_detail import AgentDetailScreen
from .connector_detail import CONNECTOR_TYPES, ConnectorDetailScreen
from .defaults import DefaultsScreen
from .tool_presets import ToolPresetsScreen
from .watcher_detail import WatcherDetailScreen

_AGENT_TYPES = ("claude", "opencode")

# Tab IDs in display order — used by action_previous_tab()/action_next_tab()
# to wrap around, and to look up each tab's own DataTable id for focusing.
_TAB_ORDER = ("tab-connectors", "tab-agents", "tab-watchers", "tab-defaults", "tab-presets")
_TABLE_ID_FOR_TAB = {
    "tab-connectors": "connectors-table",
    "tab-agents": "agents-table",
    "tab-watchers": "watchers-table",
    "tab-defaults": "defaults-table",
    "tab-presets": "presets-table",
}

if TYPE_CHECKING:
    from ..app import ConfigToolApp


class OverviewScreen(Screen):
    """Root screen — never popped (quitting the app pops the whole stack)."""

    BINDINGS = [
        # User-reported: this used to be 'e', shadowing the row-level direct-
        # edit shortcut below every time — pressing 'e' hoping to edit the
        # selected connector/agent instead opened $EDITOR on the whole
        # config.yaml. Moved to ctrl+e (clear of every other single-letter
        # list-page/detail-screen binding: e/d/r/n/q here, ctrl+s/ctrl+r/
        # ctrl+t on FormScreen).
        Binding("ctrl+e", "edit_config", "Edit in $EDITOR", show=True),
        Binding("r", "refresh", "Refresh"),
        # Screen already binds tab/shift+tab to app.focus_next/focus_previous
        # with show=False (textual/screen.py) — on mount, focus starts on the
        # tab bar itself, not the list, so surfacing this in the footer (same
        # action, just visible) is the fix for "how do I get into the list?"
        Binding("tab", "app.focus_next", "Focus next / enter list", show=True),
        # App already binds ctrl+q -> quit (show=False, Textual's own
        # default) — 'q' here is the documented, discoverable quit key (the
        # design's original intent, missed at first implementation). Scoped
        # to OverviewScreen (not detail screens) since phase 2/3 add text
        # Input widgets on those screens, where a bare 'q' typed into a field
        # must not quit the app.
        Binding("q", "app.quit", "Quit", show=True),
        Binding("n", "new_entity", "New", show=True),
        # Direct edit/delete on the row under the cursor (Connectors/Agents
        # tabs only — the only ones with a real detail-screen mode="edit"/
        # delete flow) — user-requested, to skip "select row -> view page ->
        # press e/d" for the common case of just wanting to edit or delete
        # one entry. check_action() below hides these on any other tab so
        # the footer doesn't advertise a no-op.
        Binding("e", "edit_row", "Edit", show=True),
        Binding("d", "delete_row", "Delete", show=True),
        # User-requested: focus starts on the list itself (see on_mount()),
        # not the tab bar, so left/right must be able to switch tabs WITHOUT
        # the user first moving focus off the list. priority=True is
        # required: DataTable (which has focus in the common case now) has
        # its OWN left/right bindings (cell/column cursor movement), and the
        # binding chain used to resolve a keypress starts at the FOCUSED
        # widget and walks up — without priority=True, DataTable's own
        # binding would always win and this one would never even be
        # considered. (cursor_type="row" everywhere in this screen, so
        # DataTable's left/right never actually do anything useful today
        # regardless — but priority=True is what makes this correct even if
        # that ever changes, not an accident of DataTable being otherwise
        # idle on these keys.)
        Binding("left", "previous_tab", "Previous tab", show=True, priority=True),
        Binding("right", "next_tab", "Next tab", show=True, priority=True),
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
        self.repaint_from_memory()
        # User-requested: default focus straight to the list, not the tab
        # bar — "tab: Focus next / enter list" (the BINDINGS comment above)
        # was the previous fix for reaching the list at all; this goes
        # further and puts focus there immediately, so the very first
        # keypress (arrow keys to move the cursor, 'e'/'d' to act on a row)
        # already lands on the table with no extra step.
        self._focus_active_tab_table()

    def _focus_active_tab_table(self) -> None:
        active_tab = self.query_one(TabbedContent).active
        table_id = _TABLE_ID_FOR_TAB.get(active_tab)
        if table_id is not None:
            self.query_one(f"#{table_id}", DataTable).focus()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Fires whenever the active tab changes, by ANY means — clicking a
        tab, action_previous_tab()/action_next_tab() below, or a future
        programmatic switch — so the list-focus behavior stays correct
        without needing to be re-applied at every call site that changes
        tabs. User-requested: switching tabs should always leave focus
        ready on that tab's list, not on the tab bar."""
        self._focus_active_tab_table()

    # ── Actions ──────────────────────────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide 'Edit'/'Delete' from the footer on tabs that don't support
        them (Watchers/Defaults/Tool Presets — Phase 3) so the footer never
        advertises a key that would just notify "not supported yet"."""
        if action in ("edit_row", "delete_row"):
            active_tab = self.query_one(TabbedContent).active
            return active_tab in ("tab-connectors", "tab-agents")
        return True

    def action_edit_config(self) -> None:
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.open_editor_and_reload()

    def action_previous_tab(self) -> None:
        """'left' — wraps from the first tab to the last. Setting `.active`
        (rather than any lower-level Tabs API) triggers TabbedContent's own
        TabActivated message the same way a mouse click would, so
        on_tabbed_content_tab_activated() re-focuses the list for us — one
        path for "the active tab changed," not a second one special-cased
        for the keyboard."""
        tabs = self.query_one(TabbedContent)
        index = _TAB_ORDER.index(tabs.active)
        tabs.active = _TAB_ORDER[(index - 1) % len(_TAB_ORDER)]

    def action_next_tab(self) -> None:
        """'right' — wraps from the last tab to the first. See
        action_previous_tab()'s docstring for why this only sets `.active`
        rather than also handling focus here directly."""
        tabs = self.query_one(TabbedContent)
        index = _TAB_ORDER.index(tabs.active)
        tabs.active = _TAB_ORDER[(index + 1) % len(_TAB_ORDER)]

    @work
    async def action_edit_row(self) -> None:
        """'e' on the Connectors/Agents tabs: open the row under the cursor
        DIRECTLY in edit mode — no view-mode detour. User-requested: the
        common case is "I know which entry I want to change," and having to
        select it, land on a read-only page, then press 'e' again was an
        extra, pointless step for that case. (Selecting a row via Enter into
        a read-only view first is still available and unchanged, e.g. for
        just double-checking a value.)"""
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        cfg = app.editable_config
        if cfg is None:
            self.notify("Config does not currently load.", severity="error")
            return

        active_tab = self.query_one(TabbedContent).active
        if active_tab == "tab-connectors":
            key = self._cursor_row_key("connectors-table")
            if key is None:
                return
            entry = self._connector_entry_for_key(cfg, key)
            if entry is None:
                return
            screen = ConnectorDetailScreen(cfg, entry, mode="edit")
        elif active_tab == "tab-agents":
            key = self._cursor_row_key("agents-table")
            if key is None:
                return
            entry = cfg.agents_raw.get(key)
            if entry is None:
                return
            screen = AgentDetailScreen(cfg, key, entry, mode="edit")
        else:
            return
        screen._started_in_edit_mode = True
        self.app.push_screen(screen)

    @work
    async def action_delete_row(self) -> None:
        """'d' on the Connectors/Agents tabs: delete the row under the
        cursor directly, reusing FormScreen.action_delete()'s existing
        confirm/referencing-watcher-check/save flow verbatim (no
        reimplementation) — just triggered without a screen push first.

        Pushes the target screen (in view mode — action_delete() requires
        it, see its own check) SILENTLY, immediately invokes its delete
        action, then pops back out to the list regardless of outcome
        (confirmed, cancelled, or blocked by a referencing watcher) —
        action_delete() itself already pops the screen on a SUCCESSFUL
        delete, so the extra pop_screen() below only fires for the
        cancelled/blocked paths, where action_delete() deliberately leaves
        the screen in place (it was designed to be reached via view mode,
        where staying put makes sense — reached from here, staying put
        would leave the user looking at a screen they never asked to see).
        """
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        cfg = app.editable_config
        if cfg is None:
            self.notify("Config does not currently load.", severity="error")
            return

        active_tab = self.query_one(TabbedContent).active
        if active_tab == "tab-connectors":
            key = self._cursor_row_key("connectors-table")
            if key is None:
                return
            entry = self._connector_entry_for_key(cfg, key)
            if entry is None:
                return
            screen = ConnectorDetailScreen(cfg, entry, mode="view")
        elif active_tab == "tab-agents":
            key = self._cursor_row_key("agents-table")
            if key is None:
                return
            entry = cfg.agents_raw.get(key)
            if entry is None:
                return
            screen = AgentDetailScreen(cfg, key, entry, mode="view")
        else:
            return

        self.app.push_screen(screen)
        # Call _do_delete() directly (a plain coroutine) rather than the
        # @work-decorated action_delete() — nesting a second @work worker
        # inside this one (via action_delete().wait()) turned out to be
        # fragile: if this outer worker gets cancelled while the inner one
        # is suspended at a push_screen_wait(), Worker.wait() re-raises
        # that as WorkerCancelled INSIDE this method, an unrelated-looking
        # crash. _do_delete() has the exact same logic, just callable
        # without going through the worker system a second time.
        await screen._do_delete()
        if self.app.screen is screen:
            # Cancelled, or blocked by a referencing watcher — action_delete()
            # left the screen in place (correct for its OWN view-mode entry
            # point). Reached from the list directly, staying here would
            # strand the user on a screen they never asked to see — send
            # them back to the list instead, same as Escape would.
            self.app.pop_screen()

    @work
    async def action_new_entity(self) -> None:
        """'n' — scoped to whichever tab is active. Agents and Connectors
        support creation; Watchers/Defaults/Tool Presets don't yet (Phase 3).
        Unsupported tabs just notify, rather than doing nothing silently or
        crashing."""
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        if app.editable_config is None:
            self.notify("Config does not currently load — nothing to add to.", severity="error")
            return

        active_tab = self.query_one(TabbedContent).active
        if active_tab == "tab-agents":
            agent_type = await self.app.push_screen_wait(
                TypePickerModal("New agent — pick a type", list(_AGENT_TYPES))
            )
            if agent_type is None:
                return
            self.app.push_screen(
                AgentDetailScreen(app.editable_config, "", {"type": agent_type}, mode="create")
            )
        elif active_tab == "tab-connectors":
            connector_type = await self.app.push_screen_wait(
                TypePickerModal("New connector — pick a type", list(CONNECTOR_TYPES))
            )
            if connector_type is None:
                return
            self.app.push_screen(
                ConnectorDetailScreen(app.editable_config, {"type": connector_type}, mode="create")
            )
        else:
            self.notify("Creating a new entry isn't supported on this tab yet.", severity="warning")

    def action_refresh(self) -> None:
        # Must go through app.reload_config() (re-reads EditableConfig.document
        # from disk), NOT call self.repaint_from_memory() directly — that only
        # repaints from whatever EditableConfig already has in memory, which
        # run_validate() (reading the file fresh internally) can silently
        # disagree with once the file has changed on disk since app startup.
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        app.reload_config()

    # ── Core refresh logic (the one testable seam per docs/design) ──────────

    def repaint_from_memory(self) -> None:
        """Redraw every tab from EditableConfig's CURRENT in-memory document —
        does not touch disk. Name is deliberate (code review item 9: the prior
        name `refresh_overview` invited exactly the bug action_refresh's
        comment above warns against — reaching for "the refresh method" and
        getting a stale repaint instead of a disk reload). Call
        `app.reload_config()` when the on-disk file may have changed."""
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

        # Keyed by list POSITION, not by name — unlike agents_raw (a dict,
        # inherently-unique keys) or watchers (names GatewayConfig.from_file
        # already guarantees unique), connectors_raw is the raw, pre-
        # validation list: two connectors can share a name, or both be
        # missing one (falling back to "?"), and Textual's DataTable.add_row
        # raises DuplicateKey on a repeated key — exactly the kind of config
        # mistake this tool exists to surface gracefully, not crash on.
        connectors_table.add_columns("Name", "Type", "Status")
        for i, c in enumerate(cfg.connectors_raw):
            name = c.get("name", "?")
            try:
                merged = cfg.merged_entry("connector_defaults", c)
            except (ValueError, FileNotFoundError):
                merged = c
            connectors_table.add_row(
                name, merged.get("type", "?"), status_badge(status.status_for("connector", name)),
                key=str(i),
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

    def _cursor_row_key(self, table_id: str) -> str | None:
        """The row key under the cursor for the given table, or None if the
        table is empty/cursor isn't on a valid row — shared by the direct
        edit/delete actions below and (indirectly, via the same key lookup
        logic) on_data_table_row_selected()."""
        table = self.query_one(f"#{table_id}", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        except Exception:
            return None
        return str(cell_key.row_key.value)

    def _connector_entry_for_key(self, cfg, key: str) -> dict | None:
        # key is the row's list position (see repaint_from_memory) — not
        # the connector's name, which isn't guaranteed unique/present.
        connectors = cfg.connectors_raw
        index = int(key)
        if 0 <= index < len(connectors):
            return connectors[index]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        app: "ConfigToolApp" = self.app  # type: ignore[assignment]
        cfg = app.editable_config
        if cfg is None:
            return
        table_id = event.data_table.id
        key = str(event.row_key.value)

        if table_id == "connectors-table":
            entry = self._connector_entry_for_key(cfg, key)
            if entry is not None:
                self.app.push_screen(ConnectorDetailScreen(cfg, entry, mode="view"))
        elif table_id == "agents-table":
            entry = cfg.agents_raw.get(key)
            if entry is not None:
                self.app.push_screen(AgentDetailScreen(cfg, key, entry, mode="view"))
        elif table_id == "watchers-table":
            # Unlike repaint_from_memory()'s population of this same table,
            # this used to call expanded_watchers() completely unguarded —
            # if the config became invalid on disk after the table was
            # painted (e.g. an external edit), selecting ANY row (including
            # the keyless "(unavailable...)" placeholder row shown in that
            # case) crashed the whole app. Guarded the same way
            # repaint_from_memory() already is.
            try:
                expanded = cfg.expanded_watchers()
            except (ValueError, FileNotFoundError):
                return
            ew = next((e for e in expanded if e.watcher.name == key), None)
            if ew is not None:
                self.app.push_screen(WatcherDetailScreen(cfg, ew, mode="view"))
        elif table_id == "defaults-table":
            self.app.push_screen(DefaultsScreen(cfg, key, mode="view"))
        elif table_id == "presets-table":
            self.app.push_screen(ToolPresetsScreen(cfg, key, mode="view"))
