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

    # Field-row layout shared by every screen with an actual edit FORM
    # (FormScreen — AgentDetailScreen/ConnectorDetailScreen — and
    # DefaultsScreen, which intentionally does NOT extend FormScreen — see
    # its own module docstring for why). Lives here, on the common ancestor
    # both reach, rather than duplicated in each: Textual's CSS type
    # selectors below match by ancestry, not literal class name, so
    # `DetailScreen .field-row` applies equally inside a FormScreen
    # subclass's composed tree and a DefaultsScreen's.
    DEFAULT_CSS = """
    DetailScreen .entity-form {
        padding: 1 2;
    }
    DetailScreen .field-row {
        height: auto;
        margin-bottom: 1;
    }
    DetailScreen .field-label {
        width: 30;
        padding-top: 1;
    }
    DetailScreen .field-provenance {
        padding-top: 1;
        margin-left: 2;
        width: auto;
    }
    DetailScreen Checkbox {
        width: auto;
    }
    /* Input's own DEFAULT_CSS is `width: 100%` — inside a Horizontal
    field-row, that claims the ENTIRE row's width, pushing every sibling
    that comes after it (a "Store in .env" Checkbox, a provenance/blast-
    radius marker) off past the terminal's right edge. `1fr` matches
    Select's own DEFAULT_CSS (which never had this problem) — share the
    row's remaining space with fixed/auto-width siblings instead of
    claiming all of it. */
    DetailScreen .field-row Input {
        width: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(self._body_text(), id=self.BODY_ID))
        yield Footer()

    def _body_text(self) -> str:
        raise NotImplementedError

    def action_back(self) -> None:
        self.app.pop_screen()
