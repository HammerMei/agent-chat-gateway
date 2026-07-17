"""WatcherDetailScreen — view (and, in a later phase, edit/create) a single
EXPANDED watcher.

Shows a persistent group-membership banner whenever this watcher is part of
a shared `rooms:` list (docs/design/config-tool.md, Q4) — visible read-only
information, not gated behind an edit attempt. The raw group entry itself
is never a second editing surface in this design; all mutation (a later
phase) happens per expanded watcher, with the data layer silently handling
any resulting split.
"""

from __future__ import annotations

from typing import Literal

from ..formatting import format_value, provenance_label
from ..model import EditableConfig, ExpandedWatcher
from .base import DetailScreen

_KNOWN_FIELDS = [
    "session_id", "online_notification", "offline_notification",
    "context_inject_files", "history_handoff",
]


class WatcherDetailScreen(DetailScreen):
    BODY_ID = "watcher-detail-body"

    def __init__(
        self,
        cfg: EditableConfig,
        expanded_watcher: ExpandedWatcher,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        self.expanded_watcher = expanded_watcher
        self.mode = mode

    def _body_text(self) -> str:
        ew = self.expanded_watcher
        w = ew.watcher
        entry = ew.raw_entry
        description = entry.get("description")

        lines = [f"[bold]{w.name}[/bold]"]
        if description:
            lines.append(f"[dim]{description}[/dim]")
        lines.append(f"connector: {w.connector}")
        lines.append(f"agent: {w.agent}")
        lines.append(f"room: {w.room}")

        if ew.group_size > 1:
            siblings = ", ".join(ew.sibling_rooms)
            lines.append("")
            lines.append(
                f"[yellow]Part of a shared rooms: group with: {siblings} "
                f"({ew.group_size - 1} other room(s))[/yellow]"
            )

        lines.append("")
        try:
            merged = self.cfg.merged_entry("watcher_defaults", entry)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(f"[red]Could not compute effective values: {exc}[/red]")
            return "\n".join(lines)

        # Always shown regardless of presence — these fields have sensible
        # defaults (None/[]) even when absent from both the entry and
        # watcher_defaults, so a line is still useful.
        for key in _KNOWN_FIELDS:
            provenance = self.cfg.field_provenance("watcher_defaults", entry, key)
            lines.append(
                f"{key}: {format_value(merged.get(key))}  "
                f"[dim]({provenance_label(provenance)})[/dim]"
            )

        return "\n".join(lines)
