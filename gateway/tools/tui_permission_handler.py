"""Interactive TUI permission handler for AgentSession.

Displays a Rich panel showing the tool name and input whenever Claude
wants to call a tool, then prompts the user to approve or deny.

Usage with AgentSession::

    from gateway.tools.tui_permission_handler import tui_permission_handler

    async with AgentSession(
        ClaudeBackend("claude", [], 120),
        "/my/project",
        permission_handler=tui_permission_handler,
    ) as session:
        reply = await session.send("Do something that needs tools")

Usage with the gateway TUI::

    uv run python -m gateway.tools.tui --permissions
"""

from __future__ import annotations

import asyncio
import json

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError as exc:
    raise ImportError(
        "'rich' is required for tui_permission_handler.\n"
        "Install it with: uv add rich"
    ) from exc

_console = Console()


async def tui_permission_handler(tool_name: str, tool_input: dict) -> bool:
    """Interactive permission handler — prompts the user in the terminal.

    Displays a rich panel with the tool name and input parameters, then
    waits for the user to type 'y' (allow) or 'n' (deny).  Keeps asking
    until a valid answer is given.

    This is a ready-to-use ``permission_handler`` for ``AgentSession``::

        async with AgentSession(backend, cwd, permission_handler=tui_permission_handler) as s:
            ...
    """
    # ── Build display panel ───────────────────────────────────────────────────
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold yellow", no_wrap=True)
    grid.add_column()

    grid.add_row("tool", Text(tool_name, style="bold cyan"))

    if tool_input:
        try:
            formatted = json.dumps(tool_input, indent=2, ensure_ascii=False)
        except Exception:
            formatted = str(tool_input)
        # Truncate very long inputs so the panel stays readable
        if len(formatted) > 800:
            formatted = formatted[:800] + "\n  … (truncated)"
        grid.add_row("input", Text(formatted, style="dim"))
    else:
        grid.add_row("input", Text("(no parameters)", style="dim italic"))

    _console.print()
    _console.print(Panel(
        grid,
        title="[bold red]🔐 Permission Required[/bold red]",
        border_style="yellow",
        padding=(0, 1),
    ))

    # ── Ask user ──────────────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    while True:
        try:
            raw = await loop.run_in_executor(
                None, input, "  Allow this tool call? [y/n]: "
            )
        except EOFError:
            # Non-interactive context (piped stdin) — deny as safe default
            _console.print("[dim]  (EOF — denying as safe default)[/dim]")
            return False

        answer = raw.strip().lower()
        if answer in ("y", "yes"):
            _console.print("  [green]✅ Allowed[/green]\n")
            return True
        if answer in ("n", "no"):
            _console.print("  [red]❌ Denied[/red]\n")
            return False

        _console.print("  [dim]Please enter 'y' to allow or 'n' to deny.[/dim]")
