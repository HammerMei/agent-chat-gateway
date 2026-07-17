"""ConfigToolApp — the config TUI's root Textual application.

Reached via `agent-chat-gateway config` (gateway/cli.py's `_run_config`).
See docs/design/config-tool.md for the full M1–M3 design; this is Phase 1:
read-only overview + detail screens, plus the $EDITOR escape hatch.
"""

from __future__ import annotations

import subprocess

from textual.app import App

from ..config_validate import ValidationResult, validate_config
from .editor import resolve_editor_command
from .model import EditableConfig
from .screens.overview import OverviewScreen


class ConfigToolApp(App):
    """Root app. Owns the single EditableConfig instance every screen reads
    (`self.editable_config`), or `self.load_error` if the file doesn't
    currently parse."""

    TITLE = "agent-chat-gateway config"

    def __init__(self, config_path: str, lint: bool = False):
        super().__init__()
        self.config_path = str(config_path)
        self.lint = lint
        self.editable_config: EditableConfig | None = None
        self.load_error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            self.editable_config = EditableConfig.load(self.config_path)
            self.load_error = None
        except (ValueError, FileNotFoundError) as exc:
            self.editable_config = None
            self.load_error = str(exc)

    def on_mount(self) -> None:
        self.push_screen(OverviewScreen())

    # ── Shared, non-action helpers (called from OverviewScreen's actions) ───

    def run_validate(self) -> ValidationResult:
        """Run the exact same check `acg config validate` uses — single
        source of truth for what "valid" means, per docs/design/config-tool.md."""
        return validate_config(self.config_path, lint=self.lint)

    def reload_config(self) -> None:
        """Re-read config.yaml from disk (e.g. after the $EDITOR round-trip,
        or a manual 'refresh' action) and repaint the active screen."""
        self._load()
        if isinstance(self.screen, OverviewScreen):
            self.screen.refresh_overview()

    def open_editor_and_reload(self) -> None:
        """Suspend the TUI, open $EDITOR on config.yaml, resume + reload.

        Only reachable from OverviewScreen (see docs/design/config-tool.md,
        Q6) — restricting it there means it can never race with an in-progress
        edit on some other screen holding unsaved in-memory state.
        """
        try:
            # resolve_editor_command must stay INSIDE this try — it calls
            # shlex.split() on $EDITOR/$VISUAL, which raises ValueError on
            # unbalanced quoting (e.g. EDITOR="vim '"). That's exactly the
            # kind of editor-launch failure this method exists to catch and
            # notify on, not crash on.
            editor_argv = resolve_editor_command(self.config_path)
            with self.suspend():
                subprocess.call(editor_argv)  # pragma: no cover — needs a real terminal
        except Exception as exc:
            self.notify(f"Could not open editor: {exc}", severity="error")
            return
        self.reload_config()
