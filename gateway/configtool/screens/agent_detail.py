"""AgentDetailScreen — view (and, in a later phase, edit/create) a single
agent.

Unlike connectors, the agent schema is complete (additionalProperties:
false in gateway/schema/config.schema.json's $defs/agent), so Phase 1 shows
a fixed field list with a provenance marker per field (explicit / inherited
from agent_defaults / explicit-null-suppressing) instead of a generic dump.

Tool lists render the PRE-resolve representation (preset-name strings vs
inline {tool, params} dicts) — never the flattened list[ToolRule] that
_resolve_tool_entries produces, since that flattening is one-way and loses
which items came from a preset (docs/design/config-tool.md, Q3/Q5).
"""

from __future__ import annotations

from typing import Literal

from ..formatting import format_value, provenance_label
from ..model import EditableConfig
from .base import DetailScreen

# Top-level agent fields worth a dedicated provenance-annotated line, in the
# same order as AgentConfig's own fields (gateway/core/config.py).
_KNOWN_FIELDS = [
    "type", "command", "working_directory", "session_prefix",
    "lazy_instruction_loading", "new_session_args", "context_inject_files",
    "timeout", "permissions",
]


class AgentDetailScreen(DetailScreen):
    BODY_ID = "agent-detail-body"

    def __init__(
        self,
        cfg: EditableConfig,
        name: str,
        entry: dict,
        mode: Literal["view", "edit", "create"] = "view",
    ):
        super().__init__()
        self.cfg = cfg
        self.agent_name = name
        self.entry = entry
        self.mode = mode

    def _body_text(self) -> str:
        description = self.entry.get("description")
        lines = [f"[bold]{self.agent_name}[/bold]"]
        if description:
            lines.append(f"[dim]{description}[/dim]")
        lines.append("")

        try:
            merged = self.cfg.merged_entry("agent_defaults", self.entry)
        except (ValueError, FileNotFoundError) as exc:
            lines.append(f"[red]Could not compute effective values: {exc}[/red]")
            return "\n".join(lines)

        for key in _KNOWN_FIELDS:
            if key not in merged:
                continue
            provenance = self.cfg.field_provenance("agent_defaults", self.entry, key)
            lines.append(
                f"{key}: {format_value(merged[key])}  "
                f"[dim]({provenance_label(provenance)})[/dim]"
            )

        for label, field_key in (
            ("owner_allowed_tools", "owner_allowed_tools"),
            ("guest_allowed_tools", "guest_allowed_tools"),
        ):
            if field_key not in merged:
                continue
            provenance = self.cfg.field_provenance("agent_defaults", self.entry, field_key)
            lines.append("")
            lines.append(f"{label}:  [dim]({provenance_label(provenance)})[/dim]")
            for item in merged.get(field_key) or []:
                if isinstance(item, str):
                    lines.append(f"  → preset: {item}")
                elif isinstance(item, dict):
                    tool = item.get("tool", "?")
                    params = item.get("params")
                    lines.append(f"  {tool} / {params or '(any)'}")

        return "\n".join(lines)
