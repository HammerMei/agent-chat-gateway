"""ToolPresetsScreen — view (and, in a later phase, edit) one named
tool_presets entry.

Presets are global/shared across every agent, structurally flat (a preset's
own rule list may only contain inline rules — gateway/config.py's
_parse_tool_presets rejects a preset referencing another preset). Phase 1
just lists the rules; a later phase adds add/remove and a "used by N
agents" warning before allowing deletion.
"""

from __future__ import annotations

from typing import Literal

from ..model import EditableConfig
from .base import DetailScreen


class ToolPresetsScreen(DetailScreen):
    BODY_ID = "preset-detail-body"

    def __init__(self, cfg: EditableConfig, preset_name: str, mode: Literal["view", "edit"] = "view"):
        super().__init__()
        self.cfg = cfg
        self.preset_name = preset_name
        self.mode = mode

    def _body_text(self) -> str:
        rules = self.cfg.tool_presets_raw.get(self.preset_name, [])
        # Checked against the MERGED view (agent_defaults + entry), not the
        # raw entry alone — a preset referenced only via agent_defaults
        # (common: shared across every agent that doesn't override its own
        # tool list) would otherwise never show up as "used by" anyone.
        used_by = []
        for name, entry in self.cfg.agents_raw.items():
            try:
                merged = self.cfg.merged_entry("agent_defaults", entry)
            except (ValueError, FileNotFoundError):
                merged = entry
            owner_tools = merged.get("owner_allowed_tools") or []
            guest_tools = merged.get("guest_allowed_tools") or []
            if self.preset_name in owner_tools or self.preset_name in guest_tools:
                used_by.append(name)

        lines = [f"[bold]{self.preset_name}[/bold]  ({len(rules)} rule(s))"]
        lines.append(
            f"used by: {', '.join(used_by)}" if used_by else "used by: (no agent references it)"
        )
        lines.append("")
        for rule in rules:
            if isinstance(rule, dict):
                tool = rule.get("tool", "?")
                params = rule.get("params")
                lines.append(f"  {tool} / {params or '(any)'}")
        return "\n".join(lines)
