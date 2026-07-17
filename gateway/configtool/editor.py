"""$EDITOR resolution for the config TUI's raw-edit escape hatch.

No existing $EDITOR-resolution helper exists elsewhere in this repo — this
is new code, kept deliberately tiny.
"""

from __future__ import annotations

import os
import shlex


def resolve_editor_command(config_path: str) -> list[str]:
    """Return the argv to run to edit `config_path`.

    Resolution order: $EDITOR, then $VISUAL, then a hardcoded 'nano'
    fallback (widely available, beginner-friendly — this is the one path a
    user with neither env var set will hit).

    The resolved value is split with shlex so an editor command that
    includes its own flags (e.g. EDITOR="code --wait") works correctly.
    """
    raw = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    return [*shlex.split(raw), config_path]
