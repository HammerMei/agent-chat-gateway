"""Modal screens for the config TUI (docs/design/config-tool.md's "modals"
row in the screen inventory). `ConfirmModal` is the first one built, landing
alongside `EditableConfig.save()`/dirty tracking — everything that needs to
ask "discard unsaved changes?" before navigating away or quitting shares it.
`MessageModal` (dismiss-only) followed once user feedback showed `self.notify()`
toasts auto-vanish before an error message this important can be read.
Later phases add `EntityPickerModal`/`PresetOrInlineModal`/
`InlineToolRuleModal` here too.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static


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


class MessageModal(ModalScreen[None]):
    """A dismiss-only informational/error dialog. User-reported:
    `self.notify(..., severity="error")` toasts auto-vanish on their own
    timer, and a save/delete failure's explanation (often several lines —
    e.g. a validator error) needs more than a glance to actually read. This
    stays up until the user presses Enter/Escape or clicks OK — callers
    `await self.app.push_screen_wait(MessageModal(...))`. Use for anything
    the user needs time to read (validation/save/delete failures); routine
    short confirmations ("Saved.") stay as `self.notify()` toasts — this
    isn't a blanket replacement, just for messages worth blocking on.
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "OK", show=False),
        Binding("enter", "dismiss_modal", "OK", show=False),
    ]

    DEFAULT_CSS = """
    MessageModal {
        align: center middle;
    }
    #message-dialog {
        width: auto;
        max-width: 80;
        height: auto;
        border: thick $error;
        padding: 1 2;
        background: $surface;
    }
    #message-title {
        text-style: bold;
    }
    #message-body {
        margin-top: 1;
    }
    #message-buttons {
        height: auto;
        margin-top: 1;
        align: right middle;
    }
    """

    def __init__(self, message: str, title: str = "Error"):
        super().__init__()
        self.message = message
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(id="message-dialog"):
            yield Static(self.title_text, id="message-title")
            yield Static(self.message, id="message-body")
            with Horizontal(id="message-buttons"):
                yield Button("OK", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class TypePickerModal(ModalScreen[str | None]):
    """Pick one of a fixed set of string options — e.g. an agent's `type`
    (claude/opencode) or a connector's `type` (rocketchat/mattermost/voice/
    script). `dismiss(None)` on cancel/Escape, `dismiss(chosen)` on
    Enter/click. Generic across both use cases rather than one modal per
    entity kind — the two callers just differ in `title` and `options`.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    TypePickerModal {
        align: center middle;
    }
    #type-picker-dialog {
        width: auto;
        min-width: 30;
        max-width: 60;
        height: auto;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    #type-picker-list {
        height: auto;
        margin-top: 1;
    }
    """

    def __init__(self, title: str, options: list[str]):
        super().__init__()
        self.title_text = title
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="type-picker-dialog"):
            yield Static(self.title_text, id="type-picker-title")
            yield ListView(
                *[ListItem(Label(option), name=option) for option in self.options],
                id="type-picker-list",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)

    def action_cancel(self) -> None:
        self.dismiss(None)
