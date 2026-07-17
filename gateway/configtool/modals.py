"""Modal screens for the config TUI (docs/design/config-tool.md's "modals"
row in the screen inventory). `ConfirmModal` is the first one built, landing
alongside `EditableConfig.save()`/dirty tracking — everything that needs to
ask "discard unsaved changes?" before navigating away or quitting shares it.
Later phases add `TypePickerModal`/`EntityPickerModal`/`PresetOrInlineModal`/
`InlineToolRuleModal` here too.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool]):
    """A yes/no confirmation dialog. `dismiss(True)` on confirm, `dismiss(False)`
    on cancel — callers `await self.app.push_screen_wait(ConfirmModal(...))`
    to get the result.

    The Cancel button holds focus by default (safe-by-default for a dialog
    that's always asking about discarding something): pressing Enter presses
    whichever button is focused — Textual's `Button` handles Enter/Space
    itself once focused, so there is deliberately no screen-level `Binding`
    for "confirm" here (it would never fire while a Button has focus, which
    is always, by construction). Escape still needs its own binding since
    Button doesn't bind it.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-dialog {
        width: auto;
        max-width: 60;
        height: auto;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    #confirm-buttons {
        height: auto;
        margin-top: 1;
        align: right middle;
    }
    #confirm-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, message: str, confirm_label: str = "Yes", cancel_label: str = "No"):
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label
        self.cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self.message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button(self.cancel_label, id="cancel", variant="default")
                yield Button(self.confirm_label, id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
