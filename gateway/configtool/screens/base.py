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

    # Field-row layout shared by every screen with an actual edit FORM —
    # all of them FormScreen subclasses now (AgentDetailScreen/
    # ConnectorDetailScreen/TemplateDetailScreen). Historically also shared
    # with the old DefaultsScreen (deleted in the v0.3 templates/inherits
    # redesign — see template_detail.py), which deliberately did NOT extend
    # FormScreen; kept here on the common ancestor rather than on FormScreen
    # itself since that was true at the time. Textual's CSS type selectors
    # below match by ancestry, not literal class name, so `DetailScreen
    # .field-row` applies equally inside any FormScreen subclass's composed
    # tree.
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
    /* The Inherits row's current-value Static (agent_detail.py/
    connector_detail.py's Inherits rows, built by _open_inherits_picker())
    — a bare Static with no width override defaults to 1fr (same as Input's
    own default, see the comment below), which claims the rest of the row
    and pushes the trailing "Change…" Button off past the terminal's right
    edge, exactly the failure mode `.field-row Input`'s own `width: 1fr`
    override already exists to prevent. A FIXED width (not `auto`) is used
    here specifically — user-reported: `auto` sizes the box exactly to the
    template name's own length, which puts the Button flush against (or,
    depending on terminal font metrics, visually overlapping) the text with
    no breathing room. Fixed width + `content-align: center` centers the
    name within a consistent-looking column regardless of how long it is;
    `margin-right` is the actual gap before the Button.
    User-reported (2nd round, with a screenshot): the name still rendered
    flush against the row's top edge, visibly above the button's own label.
    A prior fix here tried `height: 3; content-align: center top` based on
    comparing both widgets' own `render_line()` output directly — WRONG
    methodology: `render_line()` operates in each widget's own CONTENT-only
    coordinate space, which excludes border rows entirely (those are
    composited on separately), so "both widgets' text is on render_line
    row 0" does NOT mean they land on the same absolute screen row once
    Button's border compositing shifts its content down. Confirmed via
    `App.export_screenshot()` (real compositing, not per-widget
    introspection): with the old height:3 + top-align rule, the value text
    rendered exactly ONE ROW ABOVE "Inherits"/"Change…" (verified by
    comparing each `<text>` element's SVG y-coordinate — a `padding-top: 1`
    row is ~24.4 SVG units on this test's font metrics). `.field-label`
    (right above) already gets this right — `padding-top: 1`, no explicit
    height, height computed as auto (padding + 1 content line = 2) — so
    mirroring that exactly (not inventing a parallel height:3 scheme) is
    what actually lines up: `padding-top: 1` pushes the text down into the
    row Button/label both use; `content-align: center top` keeps the
    horizontal centering without `middle` re-absorbing the padding (the
    ORIGINAL bug from the 1st round of this fix). Re-verified via
    `export_screenshot()`: "Inherits", the value text, and "Change…" all
    now share the identical SVG y-coordinate. */
    DetailScreen .field-value {
        width: 30;
        padding-top: 1;
        content-align: center top;
        margin-right: 2;
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
