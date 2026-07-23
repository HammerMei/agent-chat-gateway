"""Modal screens for the config TUI (docs/design/config-tool.md's "modals"
row in the screen inventory). `ConfirmModal` is the first one built, landing
alongside `EditableConfig.save()`/dirty tracking — everything that needs to
ask "discard unsaved changes?" before navigating away or quitting shares it.
`MessageModal` (dismiss-only) followed once user feedback showed `self.notify()`
toasts auto-vanish before an error message this important can be read.
`TextPromptModal`, `InlineToolRuleModal`, and `PresetOrInlineModal` are the
tool-list editor's modals (docs/design/config-tool.md's tool-list-editor
work) — `EntityPickerModal` (Phase 3, watcher creation's connector/agent
picker) is still not yet built.
"""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static


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


class TextPromptModal(ModalScreen[str | None]):
    """Prompt for a single line of free text — e.g. a new tool_presets
    entry's name. Unlike an agent/connector `type`, preset names aren't a
    fixed enum, so `TypePickerModal` doesn't fit; this is the free-text
    equivalent. `dismiss(None)` on cancel/Escape. An empty/whitespace-only
    submission is rejected in place (inline error, dialog stays open) rather
    than dismissing with a blank string every caller would have to
    re-validate anyway.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    TextPromptModal {
        align: center middle;
    }
    #prompt-dialog {
        width: auto;
        min-width: 40;
        max-width: 70;
        height: auto;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    #prompt-input {
        margin-top: 1;
    }
    #prompt-error {
        color: $error;
        margin-top: 1;
    }
    #prompt-buttons {
        height: auto;
        margin-top: 1;
        align: right middle;
    }
    #prompt-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, title: str, placeholder: str = ""):
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-dialog"):
            yield Static(self.title_text, id="prompt-title")
            yield Input(id="prompt-input", placeholder=self.placeholder)
            yield Static("", id="prompt-error")
            with Horizontal(id="prompt-buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("OK", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#prompt-input", Input).value.strip()
        if not value:
            self.query_one("#prompt-error", Static).update("A value is required.")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class InlineToolRuleModal(ModalScreen[dict | None]):
    """Write one inline tool rule — used by both the per-agent tool-list
    editor (`AgentDetailScreen`) and `ToolPresetsScreen`. Validates both
    regexes live, with the SAME compile flags gateway/core/config.py's
    `ToolRule.from_config()` uses (tool: IGNORECASE; params:
    IGNORECASE | DOTALL) — a rule accepted here is guaranteed to load
    cleanly, never a save()-time surprise. `dismiss(None)` on cancel/Escape,
    `dismiss({"tool": ..., "params": ...})` (params omitted if blank) once
    both patterns compile.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    InlineToolRuleModal {
        align: center middle;
    }
    #rule-dialog {
        width: auto;
        min-width: 50;
        max-width: 80;
        height: auto;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    #rule-dialog .field-row {
        height: auto;
        margin-top: 1;
    }
    #rule-dialog .field-label {
        width: 14;
        padding-top: 1;
    }
    #rule-error {
        color: $error;
        margin-top: 1;
    }
    #rule-buttons {
        height: auto;
        margin-top: 1;
        align: right middle;
    }
    #rule-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, initial: dict | None = None):
        super().__init__()
        self._initial = initial or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="rule-dialog"):
            yield Static("[bold]Tool rule[/bold]")
            with Horizontal(classes="field-row"):
                yield Static("Tool regex", classes="field-label")
                yield Input(id="rule-tool", value=str(self._initial.get("tool", "")))
            with Horizontal(classes="field-row"):
                yield Static("Params regex", classes="field-label")
                yield Input(
                    id="rule-params",
                    value=str(self._initial.get("params") or ""),
                    placeholder="(optional)",
                )
            yield Static("", id="rule-error")
            with Horizontal(id="rule-buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#rule-tool", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._validate()

    def _validate(self) -> tuple[str, str | None] | None:
        """Returns (tool, params) if both patterns currently compile, else
        None (updating the inline error Static as a side effect)."""
        tool_pattern = self.query_one("#rule-tool", Input).value
        params_text = self.query_one("#rule-params", Input).value
        params_pattern = params_text or None
        error_widget = self.query_one("#rule-error", Static)

        if not tool_pattern:
            error_widget.update("Tool regex is required.")
            return None
        try:
            re.compile(tool_pattern, re.IGNORECASE)
        except re.error as exc:
            error_widget.update(f"Invalid tool regex: {exc}")
            return None
        if params_pattern is not None:
            try:
                re.compile(params_pattern, re.IGNORECASE | re.DOTALL)
            except re.error as exc:
                error_widget.update(f"Invalid params regex: {exc}")
                return None
        error_widget.update("")
        return tool_pattern, params_pattern

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._submit()
        else:
            self.dismiss(None)

    def _submit(self) -> None:
        result = self._validate()
        if result is None:
            return
        tool_pattern, params_pattern = result
        rule: dict[str, str] = {"tool": tool_pattern}
        if params_pattern is not None:
            rule["params"] = params_pattern
        self.dismiss(rule)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PresetOrInlineModal(ModalScreen[tuple[str, str | None] | None]):
    """The per-agent tool-list editor's "add a rule" entry point. Lists every
    existing `tool_presets` name (reference it directly — an agent's
    owner/guest_allowed_tools entry can be a bare string naming a preset, see
    gateway/config.py's `_parse_tool_presets`/`_resolve_agent_tools`), plus
    two fixed actions: write a one-off inline rule, or detour through
    `ToolPresetsScreen` to create a brand-new preset (returned as a plain
    result — this modal itself never pushes another screen; the caller does).

    Dismisses with `(kind, preset_name)`: `kind` is `"preset"` (`preset_name`
    set to the chosen preset), `"inline"`, or `"new_preset"` (`preset_name`
    is `None` for the latter two). `dismiss(None)` on cancel/Escape.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    PresetOrInlineModal {
        align: center middle;
    }
    #preset-or-inline-dialog {
        width: auto;
        min-width: 40;
        max-width: 60;
        height: auto;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    #preset-or-inline-list {
        height: auto;
        max-height: 15;
        margin-top: 1;
    }
    """

    _INLINE_LABEL = "✎ Write an inline rule…"
    _NEW_PRESET_LABEL = "+ Create a new preset…"

    def __init__(self, preset_names: list[str]):
        super().__init__()
        self.preset_names = preset_names

    def compose(self) -> ComposeResult:
        with Vertical(id="preset-or-inline-dialog"):
            yield Static("Add a tool rule", id="preset-or-inline-title")
            items = [
                ListItem(Label(f"→ preset: {name}"), name=f"preset:{name}")
                for name in self.preset_names
            ]
            items.append(ListItem(Label(self._INLINE_LABEL), name="inline"))
            items.append(ListItem(Label(self._NEW_PRESET_LABEL), name="new_preset"))
            yield ListView(*items, id="preset-or-inline-list")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = event.item.name or ""
        if name.startswith("preset:"):
            self.dismiss(("preset", name.removeprefix("preset:")))
        elif name == "inline":
            self.dismiss(("inline", None))
        elif name == "new_preset":
            self.dismiss(("new_preset", None))

    def action_cancel(self) -> None:
        self.dismiss(None)
