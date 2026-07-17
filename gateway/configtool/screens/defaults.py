"""DefaultsScreen — view (and, in a later phase, edit) one `*_defaults:`
block.

Phase 1 shows the block's own contents plus how many connector/agent/watcher
entries currently inherit vs. override each of its keys ("blast radius") —
per docs/design/config-tool.md, editing a shared default must show this
before commit; Phase 1 displays it, a later phase gates the edit on it.
"""

from __future__ import annotations

from typing import Literal

from ..formatting import format_value
from ..model import EditableConfig
from .base import DetailScreen

_ENTRY_ACCESSOR = {
    "connector_defaults": lambda cfg: cfg.connectors_raw,
    "agent_defaults": lambda cfg: list(cfg.agents_raw.values()),
    "watcher_defaults": lambda cfg: cfg.watchers_raw,
}


class DefaultsScreen(DetailScreen):
    BODY_ID = "defaults-detail-body"

    def __init__(self, cfg: EditableConfig, kind: str, mode: Literal["view", "edit"] = "view"):
        super().__init__()
        self.cfg = cfg
        self.kind = kind
        self.mode = mode

    def _body_text(self) -> str:
        entries = _ENTRY_ACCESSOR[self.kind](self.cfg)
        lines = [f"[bold]{self.kind}[/bold]  ({len(entries)} entries)"]

        raw_block = self.cfg.document.get(self.kind)
        description = raw_block.get("description") if isinstance(raw_block, dict) else None
        if description:
            lines.append(f"[dim]{description}[/dim]")
        lines.append("")

        try:
            block = self.cfg.defaults_block(self.kind)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(f"[red]Could not read this block: {exc}[/red]")
            return "\n".join(lines)

        if not block:
            lines.append("(empty — no shared defaults set)")
            return "\n".join(lines)

        for key, value in block.items():
            inherit_count = sum(1 for e in entries if key not in e)
            override_count = len(entries) - inherit_count
            lines.append(
                f"{key}: {format_value(value)}  "
                f"[dim]({inherit_count} entries inherit, {override_count} override)[/dim]"
            )

        return "\n".join(lines)
