"""DetailScreen — shared base for the config TUI's detail/entity screens.

Code review (post Phase 1) flagged that ConnectorDetailScreen, AgentDetailScreen,
WatcherDetailScreen, DefaultsScreen, and ToolPresetsScreen each hand-duplicated
the same `BINDINGS = [Binding("escape", "back", "Back")]`, the same
Header/VerticalScroll(Static)/Footer compose() shape, and the same
`action_back()`. Extracted here so Phase 2's edit/create additions to these
screens have one place to change navigation/layout, not five. Purely a
refactor — no behavior change; each subclass keeps its own widget `id` (tests
query these directly) via `BODY_ID`.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class DetailScreen(Screen):
    """Base for every pushed detail screen. Subclasses implement `_body_text()`
    and set `BODY_ID` to the widget id their tests/callers query for.
    """

    BINDINGS = [Binding("escape", "back", "Back")]

    BODY_ID: str = "detail-body"

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(self._body_text(), id=self.BODY_ID))
        yield Footer()

    def _body_text(self) -> str:
        raise NotImplementedError

    def action_back(self) -> None:
        self.app.pop_screen()
