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
from .form_common import FieldSpec

_KNOWN_FIELDS = [
    "session_id", "online_notification", "offline_notification",
    "context_inject_files", "history_handoff",
]

# A watcher template's own field list — relocated here from the deleted
# defaults.py (was WATCHER_DEFAULTS_FIELDS), reused by TemplateDetailScreen.
# gateway/config.py forbids {name, room, rooms, session_id} on a watcher
# template, since each of those pins one SPECIFIC watcher's identity and has
# no business in a block every watcher merges against.
WATCHER_TEMPLATE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("online_notification", "str", "Online notification"),
    FieldSpec("offline_notification", "str", "Offline notification"),
    FieldSpec("context_inject_files", "list", "Context inject files (comma-separated)"),
    FieldSpec("history_handoff.enabled", "bool", "History handoff enabled"),
    FieldSpec("history_handoff.fetch_count", "int", "History handoff fetch count"),
    FieldSpec("history_handoff.verbatim_tail", "int", "History handoff verbatim tail"),
)
# Mirrors gateway/config.py's OWN `.get(key, X)` calls at the watcher-parsing
# site (NOT HistoryHandoffConfig's dataclass field defaults, which have
# already drifted from them once — the dataclass itself defaults
# history_handoff.enabled to True, but the loader's own
# `hh_raw.get("enabled", False)` actually applies False whenever the key is
# absent). Matching the LOADER, not the dataclass, is what makes this form an
# honest "what would this evaluate to right now" preview.
WATCHER_TEMPLATE_DATACLASS_DEFAULTS: dict[str, object] = {
    "online_notification": None,
    "offline_notification": None,
    "context_inject_files": [],
    "history_handoff.enabled": False,
    "history_handoff.fetch_count": 50,
    "history_handoff.verbatim_tail": 15,
}


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

        template_name = self.cfg.entry_template_name(entry)
        lines.append(f"inherits: {template_name if template_name else '(none)'}")
        lines.append("")
        try:
            merged = self.cfg.merged_entry("watcher", entry)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(f"[red]Could not compute effective values: {exc}[/red]")
            return "\n".join(lines)

        # Always shown regardless of presence — these fields have sensible
        # defaults (None/[]) even when absent from both the entry and its
        # inherits: template, so a line is still useful.
        for key in _KNOWN_FIELDS:
            provenance = self.cfg.field_provenance("watcher", entry, key)
            lines.append(
                f"{key}: {format_value(merged.get(key))}  "
                f"[dim]({provenance_label(provenance, template_name)})[/dim]"
            )

        return "\n".join(lines)
